"""
Market data streamer — async SSE broadcast to all connected clients.
Non-blocking: trading threads call broadcast() and move on immediately.

Usage:
    from src.stream import market_stream

    # SSE client connects:
    await market_stream.subscribe(sse_put, event_filter="trade")

    # Trading thread fires an event (never blocks):
    market_stream.broadcast({"type": "trade", "data": {...}})
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Callable, Awaitable

log = logging.getLogger("fcoin.stream")


# ---------------------------------------------------------------------------
# Event types that can be streamed
# ---------------------------------------------------------------------------
MARKET_EVENTS = ("ticker", "orderbook", "trade", "trade_batch")


# ---------------------------------------------------------------------------
# Stream subscriber
# ---------------------------------------------------------------------------

@dataclass
class Subscriber:
    """One SSE client waiting on market_stream.events."""
    queue:     asyncio.Queue
    event_filter: str | None = None   # None = all events


# ---------------------------------------------------------------------------
# MarketStream — global singleton
# ---------------------------------------------------------------------------

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

    # ------------------------------------------------------------------ public (sync — called from trading threads)

    def broadcast(self, event: dict) -> None:
        """
        Broadcast an event to all matching subscribers.
        Called from trading threads — MUST NOT block.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no event loop — not an async context

        async def _deliver():
            async with self._lock:
                for sub in self._subs:
                    if sub.event_filter is None or sub.event_filter == event.get("type"):
                        try:
                            sub.queue.put_nowait(event)
                        except asyncio.QueueFull:
                            pass  # slow client — drop event

        asyncio.create_task(_deliver())

    # ------------------------------------------------------------------ public (async — called from SSE handlers)

    async def subscribe(
        self,
        put_fn: Callable[[bytes], Awaitable[None]],
        event_filter: str | None = None,
        subscriber: Subscriber | None = None,
    ) -> Subscriber:
        """
        Register a new SSE client and stream events to it via put_fn.
        Returns the Subscriber so the caller can await events from sub.queue.
        """
        if subscriber is None:
            queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self._max_queue)
            subscriber = Subscriber(queue=queue, event_filter=event_filter)

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
        Drain subscriber.queue and send SSE-formatted events via put_fn.
        Await this coroutine in the SSE handler.
        """
        while True:
            try:
                event = await asyncio.wait_for(subscriber.queue.get(), timeout=30.0)
                line = f"event: {event.get('type','message')}\ndata: {json.dumps(event)}\n\n"
                await put_fn(line.encode())
            except asyncio.TimeoutError:
                # Send a keepalive ping
                try:
                    await put_fn(b": ping\n\n")
                except Exception:
                    break

    # ------------------------------------------------------------------ admin

    async def stats(self) -> dict:
        async with self._lock:
            return {
                "subscribers": len(self._subs),
                "events": list(MARKET_EVENTS),
            }


# Global singleton
market_stream = MarketStream()
