"""
Market data streamer — async SSE broadcast to all connected clients.
Non-blocking: trading threads call broadcast() and move on immediately.

Usage:
    from src.stream import market_stream

    # At startup (in async context):
    market_stream.setup()

    # SSE client connects:
    await market_stream.subscribe(event_filter="ticker,trade")

    # Trading thread fires an event (never blocks):
    market_stream.broadcast({"type": "trade", "data": {...}})
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Awaitable

log = logging.getLogger("fcoin.stream")


# -----------------------------------------------------------------------------
# Event types that can be streamed
# -----------------------------------------------------------------------------
MARKET_EVENTS = ("ticker", "orderbook", "trade", "trade_batch")


# -----------------------------------------------------------------------------
# Stream subscriber
# -----------------------------------------------------------------------------
@dataclass
class Subscriber:
    """One SSE client waiting on market_stream.events."""
    queue:       asyncio.Queue
    event_filter: set[str] | None = None   # None = all events


# -----------------------------------------------------------------------------
# MarketStream — global singleton
# -----------------------------------------------------------------------------
class MarketStream:
    """
    Async event bus for live market data.
    Trading threads call broadcast() which NEVER blocks.
    SSE handlers subscribe via subscribe() and consume from their queue.

    Event format:
        {"type": "ticker",     "data": {...}}
        {"type": "orderbook",  "data": {...}}
        {"type": "trade",      "data": {...}}
        {"type": "trade_batch","data": {"trades": [...]}}
    """

    def __init__(self):
        self._subs: list[Subscriber] = []
        self._lock = asyncio.Lock()
        self._max_queue = 200   # events per subscriber queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_lock = threading.Lock()

    def setup(self) -> None:
        """Call once from the async server startup to capture the event loop."""
        with self._loop_lock:
            self._loop = asyncio.get_running_loop()
        log.info("[stream] event loop captured")

    # ------------------------------------------------------------------------- public (sync — called from trading threads)

    def broadcast(self, event: dict) -> None:
        """
        Broadcast an event to all matching subscribers.
        Called from trading threads — MUST NOT block.
        """
        with self._loop_lock:
            loop = self._loop

        if loop is None:
            return  # not set up yet

        async def _deliver() -> None:
            async with self._lock:
                for sub in self._subs:
                    ev_type = event.get("type")
                    if sub.event_filter is None or ev_type in sub.event_filter:
                        try:
                            sub.queue.put_nowait(event)
                        except asyncio.QueueFull:
                            pass  # slow client — drop event

        try:
            asyncio.run_coroutine_threadsafe(_deliver(), loop)
        except RuntimeError:
            pass  # loop closed or not running

    # ------------------------------------------------------------------------- public (async — called from SSE handlers)

    async def subscribe(
        self,
        put_fn: Callable[[bytes], Awaitable[None]] | None = None,
        event_filter: str | None = None,
        subscriber: Subscriber | None = None,
    ) -> Subscriber:
        """
        Register a new SSE client and stream events to it.
        Returns the Subscriber whose .queue holds incoming events.

        put_fn is ignored — events flow through sub.queue instead.
        event_filter: comma-separated event types, or None for all.
        """
        if subscriber is None:
            queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self._max_queue)
            # Normalise filter to a set of event types
            filters = set(event_filter.split(",")) if event_filter else None
            subscriber = Subscriber(queue=queue, event_filter=filters)

        async with self._lock:
            self._subs.append(subscriber)

        log.info(f"[stream] client connected  filter={event_filter}  total={len(self._subs)}")
        return subscriber

    async def unsubscribe(self, subscriber: Subscriber) -> None:
        """Called when an SSE client disconnects."""
        async with self._lock:
            if subscriber in self._subs:
                self._subs.remove(subscriber)
                log.info(f"[stream] client disconnected  remaining={len(self._subs)}")

    async def push_to(self, subscriber: Subscriber, put_fn: Callable[[bytes], Awaitable[None]]) -> None:
        """
        DEPRECATED — kept for compatibility. Consumers should read from sub.queue instead.
        """
        while True:
            event = await subscriber.queue.get()
            await put_fn(json.dumps(event).encode())


# Global singleton
market_stream = MarketStream()
