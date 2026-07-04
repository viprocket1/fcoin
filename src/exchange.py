"""
Mock exchange engine — in-memory, deterministic simulation of fcoin trading.
Thread-safe, runs in-process.  Replace the price feed with real market data
whenever you're ready to go live.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

log = logging.getLogger("fcoin.exchange")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class Side(Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    MARKET = "market"
    LIMIT  = "limit"


@dataclass
class Order:
    id:        str
    side:      Side
    order_type: OrderType
    price:     float | None
    quantity:  float
    filled:    float = 0.0
    status:    str   = "open"
    created_at: str  = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class Trade:
    id:         str
    order_id:   str
    side:       Side
    price:      float
    quantity:   float
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class Balance:
    available: float = 0.0
    locked:    float = 0.0

    @property
    def total(self) -> float:
        return self.available + self.locked


# ---------------------------------------------------------------------------
# Price feed — swap this out for a real market data source
# ---------------------------------------------------------------------------

class PriceFeed:
    """
    Simulated price with optional mean-reversion & volatility.
    Override `get_price()` to inject live data (webSocket, REST, etc.).
    """

    def __init__(
        self,
        initial_price: float = 100.0,
        volatility:     float = 0.002,
        drift:          float = 0.0,
        seed:           int | None = None,
    ):
        self._price   = initial_price
        self._vol     = volatility
        self._drift   = drift
        self._lock    = asyncio.Lock()
        self._sync_lock = threading.Lock()
        self._rng     = random.Random(seed)

    def get_price(self) -> float:
        """Return the current mid-price (sync, no simulation step)."""
        with self._sync_lock:
            return self._price

    async def step(self) -> float:
        """Advance price one tick using geometric Brownian motion."""
        async with self._lock:
            shock = self._rng.gauss(0, self._vol)
            self._price = max(0.001, self._price * (1 + self._drift + shock))
            return self._price

    def set_price(self, price: float) -> None:
        """Override price directly (e.g. with live feed)."""
        with self._sync_lock:
            self._price = price


# ---------------------------------------------------------------------------
# OrderBook
# ---------------------------------------------------------------------------

@dataclass
class Level:
    price:    float
    quantity: float


@dataclass
class OrderBook:
    bids: list[Level] = field(default_factory=list)  # sorted desc
    asks: list[Level] = field(default_factory=list)  # sorted asc

    @property
    def mid_price(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def simulate_from_spread(self, spread_bps: float = 20) -> None:
        """
        Generate synthetic L2 book around the current mid-price.
        Useful when no real market data is available.
        """
        mid = self.mid_price
        if mid is None:
            return
        half_spread = mid * spread_bps / 10_000
        bid_price = mid - half_spread
        ask_price = mid + half_spread

        def make_levels(base: float, side: Side, depth: int = 10) -> list[Level]:
            levels = []
            for i in range(depth):
                qty = round(random.uniform(0.1, 5.0), 4)
                if side == Side.BUY:
                    p = round(base - i * half_spread * 0.5, 4)
                else:
                    p = round(base + i * half_spread * 0.5, 4)
                levels.append(Level(price=max(0.001, p), quantity=qty))
            return levels

        self.bids = make_levels(bid_price, Side.BUY)
        self.asks = make_levels(ask_price, Side.SELL)


# ---------------------------------------------------------------------------
# Exchange
# ---------------------------------------------------------------------------

class Exchange:
    """
    In-memory simulated exchange for fcoin.

    Balances are stored as  {asset}: Balance  where asset="fcoin" or "usdc".
    All prices/quantities are floats.  Use `set_live_price()` to inject real data.
    """

    DEFAULT_FEES = 0.001  # 0.1% maker/taker

    def __init__(
        self,
        initial_usdc: float = 10_000.0,
        initial_fcoin: float = 0.0,
        price_feed: PriceFeed | None = None,
        maker_fee: float = DEFAULT_FEES,
        taker_fee: float = DEFAULT_FEES,
    ):
        self._balances: dict[str, Balance] = {
            "usdc":  Balance(available=initial_usdc),
            "fcoin": Balance(available=initial_fcoin),
        }
        self._orders:    dict[str, Order]   = {}
        self._trades:    list[Trade]         = []
        self._order_cnt  = 0
        self._book       = OrderBook()
        self._price_feed = price_feed or PriceFeed()
        self._book_lock  = threading.Lock()
        self._trade_lock = threading.Lock()
        self._fee        = (maker_fee, taker_fee)
        # Bootstrap the orderbook
        self._refresh_book()

    # ------------------------------------------------------------------
    # Public API (sync — safe for MCP tools)
    # ------------------------------------------------------------------

    def get_balance(self, asset: str) -> dict[str, float]:
        b = self._balances.get(asset, Balance())
        return {"available": b.available, "locked": b.locked, "total": b.total}

    def get_position(self) -> dict[str, Any]:
        fc = self._balances["fcoin"]
        px = self._price_feed.get_price()
        return {
            "asset":  "fcoin",
            "quantity":     fc.total,
            "avg_entry":    0.0,        # TODO: track cost basis per fill
            "current_price": px,
            "unrealized_pnl": 0.0,
        }

    def get_orderbook(self, depth: int = 20) -> dict[str, Any]:
        with self._book_lock:
            return {
                "bids": [{"price": l.price, "qty": l.quantity} for l in self._book.bids[:depth]],
                "asks": [{"price": l.price, "qty": l.quantity} for l in self._book.asks[:depth]],
                "mid_price": self._book.mid_price,
            }

    def get_market_price(self) -> float:
        return self._price_feed.get_price()

    def get_ticker(self) -> dict[str, Any]:
        px = self._price_feed.get_price()
        return {"symbol": "fcoin/usdc", "price": px, "mid": px}

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def place_order(
        self,
        side:       str,
        quantity:   float,
        price:      float | None = None,
        order_type: str = "market",
    ) -> dict[str, Any]:
        """
        Place a market or limit order.
        Returns order details with status.
        """
        if side not in ("buy", "sell"):
            raise ValueError(f"Invalid side: {side!r}")
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        if order_type not in ("market", "limit"):
            raise ValueError(f"Invalid order type: {order_type!r}")
        if order_type == "limit" and price is None:
            raise ValueError("Limit orders require a price")

        self._order_cnt += 1
        order_id = f"ord_{self._order_cnt:06d}"
        order = Order(
            id=order_id,
            side=Side(side),
            order_type=OrderType(order_type),
            price=price,
            quantity=quantity,
        )

        with self._trade_lock:
            self._orders[order_id] = order
            if order_type == "market":
                self._execute_order(order)
            else:
                self._place_limit_order(order)

            # Refresh synthetic book
            self._refresh_book()

            o = self._orders[order_id]
            return {
                "order_id":  o.id,
                "side":      o.side.value,
                "type":      o.order_type.value,
                "price":     o.price,
                "quantity":  o.quantity,
                "filled":    o.filled,
                "status":    o.status,
                "created_at": o.created_at,
            }

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        with self._trade_lock:
            order = self._orders.get(order_id)
            if not order:
                raise ValueError(f"Order not found: {order_id}")
            if order.status != "open":
                raise ValueError(f"Order is not open: {order.status}")

            # Release locked balance
            asset = "fcoin" if order.side == Side.SELL else "usdc"
            lock_qty = (order.quantity - order.filled) * (order.price if order.price is not None else self._price_feed.get_price())
            self._balances[asset].locked = max(0, self._balances[asset].locked - lock_qty)
            order.status = "cancelled"
            return {"order_id": order_id, "status": "cancelled"}

    def get_orders(self, status: str | None = None) -> list[dict[str, Any]]:
        orders = self._orders.values()
        if status:
            orders = [o for o in orders if o.status == status]
        return [
            {
                "order_id":   o.id,
                "side":       o.side.value,
                "type":       o.type_.value,
                "price":      o.price,
                "quantity":   o.quantity,
                "filled":     o.filled,
                "status":     o.status,
                "created_at": o.created_at,
            }
            for o in orders
        ]

    def get_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._trade_lock:
            return [
                {
                    "id":        t.id,
                    "order_id":  t.order_id,
                    "side":      t.side.value,
                    "price":     t.price,
                    "quantity":  t.quantity,
                    "created_at": t.created_at,
                }
                for t in self._trades[-limit:]
            ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _execute_order(self, order: Order) -> None:
        """Immediate execution for market orders against the book."""
        if order.side == Side.BUY:
            exec_price = self._book.best_ask() or self._price_feed.get_price()
        else:
            exec_price = self._book.best_bid() or self._price_feed.get_price()

        cost = exec_price * order.quantity
        fee  = cost * self._fee[1]

        # Lock USDC for buys
        self._balances["usdc"].locked += cost + fee
        # Deduct available
        if self._balances["usdc"].available < cost + fee:
            order.status = "cancelled"
            return

        self._balances["usdc"].available -= cost + fee
        self._balances["usdc"].locked    -= cost + fee
        self._balances["fcoin"].available += order.quantity

        order.filled = order.quantity
        order.status  = "filled"

        self._add_trade(order, exec_price)

    def _place_limit_order(self, order: Order) -> None:
        """Lock balance and queue the limit order."""
        if order.side == Side.SELL:
            qty = order.quantity
            if self._balances["fcoin"].available < qty:
                raise ValueError("Insufficient fcoin balance")
            self._balances["fcoin"].available -= qty
            self._balances["fcoin"].locked    += qty
        else:
            cost = order.price * order.quantity
            fee  = cost * self._fee[0]
            if self._balances["usdc"].available < cost + fee:
                raise ValueError("Insufficient USDC balance")
            self._balances["usdc"].available -= cost + fee
            self._balances["usdc"].locked    += cost + fee

    def _add_trade(self, order: Order, price: float) -> None:
        trade_id = f"tr_{len(self._trades) + 1:06d}"
        trade = Trade(id=trade_id, order_id=order.id, side=order.side,
                      price=price, quantity=order.filled)
        self._trades.append(trade)

    def _refresh_book(self) -> None:
        """Regenerate synthetic L2 book around current mid-price."""
        mid = self._price_feed.get_price()
        if mid and mid > 0:
            self._book.simulate_from_spread()
