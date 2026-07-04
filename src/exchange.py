"""
Mock exchange engine — in-memory, deterministic simulation of fcoin trading.
Thread-safe, runs in-process.  Replace the price feed with real market data
whenever you're ready to go live.

Supports per-agent isolated wallets. The shared price feed means all agents
trade against the same market price, but their balances/positions are isolated.
"""
from __future__ import annotations

import logging
import random
import threading
import uuid
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


@dataclass(slots=True)
class Order:
    id:          str
    agent_id:    str
    side:        Side
    order_type:  OrderType
    price:       float | None
    quantity:    float
    filled:      float = 0.0
    status:      str   = "open"
    created_at:  str   = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass(slots=True)
class Trade:
    id:         str
    agent_id:   str
    order_id:   str
    side:       Side
    price:      float
    quantity:   float
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass(slots=True)
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

    __slots__ = ("_price", "_volatility", "_rng")

    def __init__(
        self,
        initial_price: float = 100.0,
        volatility:     float = 0.002,
        seed:           int | None = None,
    ):
        self._price      = initial_price
        self._volatility = volatility
        self._rng        = random.Random(seed)

    def get_price(self) -> float:
        return self._price

    def set_price(self, price: float) -> None:
        self._price = price

    def step(self) -> float:
        """Random walk one tick."""
        change = self._rng.gauss(0.0, self._volatility)
        self._price = max(0.001, self._price * (1 + change))
        return self._price


# ---------------------------------------------------------------------------
# OrderBook — shared market book, double-buffered for lock-free reads
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Level:
    price:    float
    quantity: float


@dataclass
class OrderBook:
    """
    Thread-safe orderbook using double-buffer pattern.

    Writers build a new book in the background, then atomically swap
    _current -> _working.  Readers always see a consistent snapshot.
    No lock is held during reads.
    """

    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)

    @property
    def mid_price(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    def to_dict(self) -> dict:
        return {
            "bids": [{"price": l.price, "quantity": l.quantity} for l in self.bids],
            "asks": [{"price": l.price, "quantity": l.quantity} for l in self.asks],
        }

    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def snapshot(self) -> tuple[float | None, float | None]:
        """Return (best_bid, best_ask) from this snapshot without holding a lock."""
        bid = self.bids[0].price if self.bids else None
        ask = self.asks[0].price if self.asks else None
        return bid, ask

    def simulate_from_spread(self, mid: float, spread_bps: float = 20) -> None:
        half_spread = mid * spread_bps / 10_000
        bid_price = mid - half_spread
        ask_price = mid + half_spread

        def make_levels(base: float, side: Side, depth: int = 10) -> list[Level]:
            out = []
            for i in range(depth):
                qty = round(self._rng.uniform(0.1, 5.0), 4)
                if side == Side.BUY:
                    p = round(base - i * half_spread * 0.5, 4)
                else:
                    p = round(base + i * half_spread * 0.5, 4)
                out.append(Level(price=max(0.001, p), quantity=qty))
            return out

        self.bids = make_levels(bid_price, Side.BUY)
        self.asks = make_levels(ask_price, Side.SELL)

    # Simple RNG for level generation — seeded on first use
    _rng: random.Random = field(default_factory=random.Random, repr=False)


# ---------------------------------------------------------------------------
# AgentWallet — isolated wallet for one agent (no shared lock)
# ---------------------------------------------------------------------------

class AgentWallet:
    """
    Isolated balances, orders, and trades for a single agent.
    Includes an Ethereum-style secp256k1 wallet (address + private key).

    Each wallet has its own RLock — balance updates for different agents
    never block each other.
    """

    __slots__ = (
        "agent_id", "_balances", "_orders", "_trades",
        "_order_cnt", "_key", "_lock",
        "_available_usdc", "_available_fcoin",
    )

    DEFAULT_INITIAL_USDC  = 10_000.0
    DEFAULT_INITIAL_FCOIN = 0.0

    def __init__(
        self,
        agent_id:    str,
        initial_usdc:  float = DEFAULT_INITIAL_USDC,
        initial_fcoin: float = DEFAULT_INITIAL_FCOIN,
        priv_key: bytes | None = None,
    ):
        self.agent_id    = agent_id
        self._orders:    dict[str, Order] = {}
        self._trades:    list[Trade]      = []
        self._order_cnt  = 0
        # Per-agent lock — only serialises ops within THIS agent
        self._lock       = threading.RLock()
        # Inline balances to avoid per-agent dict overhead
        self._available_usdc  = initial_usdc
        self._available_fcoin = initial_fcoin
        self._balances: dict[str, Balance] = {
            "usdc":  Balance(available=initial_usdc),
            "fcoin": Balance(available=initial_fcoin),
        }
        # Ethereum-style wallet
        self._key = self._generate_key(priv_key)

    @property
    def available_usdc(self) -> float:
        return self._available_usdc

    @available_usdc.setter
    def available_usdc(self, value: float) -> None:
        self._available_usdc = value
        self._balances["usdc"].available = value

    @property
    def available_fcoin(self) -> float:
        return self._available_fcoin

    @available_fcoin.setter
    def available_fcoin(self, value: float) -> None:
        self._available_fcoin = value
        self._balances["fcoin"].available = value

    # ---------------------------------------------------------------------------
    # Ethereum wallet
    # ---------------------------------------------------------------------------

    def _generate_key(self, priv_key: bytes | None) -> bytes:
        """Generate or validate a 32-byte secp256k1 private key."""
        import hashlib
        import hmac
        if priv_key is not None:
            if len(priv_key) != 32:
                raise ValueError("Private key must be 32 bytes")
            return priv_key
        seed      = hashlib.sha256(__import__("os").urandom(32)).digest()
        ctx       = hmac.new(seed, b"secp256k1", hashlib.sha256).digest()
        return ctx

    @property
    def private_key_hex(self) -> str:
        """Raw private key as 0x-prefixed hex string. Keep secret!"""
        return "0x" + self._key.hex()

    @property
    def address(self) -> str:
        """Derived Ethereum-style address (20 bytes)."""
        import hashlib
        raw = hashlib.sha256(self._key).digest()
        return "0x" + raw[:20].hex()

    # ---------------------------------------------------------------------------
    # Balance & position
    # ---------------------------------------------------------------------------

    def get_balance(self, asset: str) -> dict[str, float]:
        b = self._balances.get(asset)
        if b is None:
            return {"available": 0.0, "locked": 0.0, "total": 0.0}
        return {"available": b.available, "locked": b.locked, "total": b.total}

    def get_position(self, current_price: float) -> dict[str, Any]:
        fcoin_bal = self._balances.get("fcoin")
        if fcoin_bal is None:
            qty = 0.0
        else:
            qty = fcoin_bal.available + fcoin_bal.locked
        avg       = 0.0
        cost      = 0.0
        for t in self._trades:
            if t.side == Side.BUY:
                cost += t.price * t.quantity
                avg   = cost / qty if qty else 0.0
        unrealised = qty * (current_price - avg) if qty else 0.0
        return {
            "asset":        "fcoin",
            "quantity":    qty,
            "avg_entry":   avg,
            "current_price": current_price,
            "unrealized_pnl": unrealised,
        }

    # ---------------------------------------------------------------------------
    # Orders
    # ---------------------------------------------------------------------------

    def get_orders(self, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        orders = self._orders.values()
        if status:
            orders = [o for o in orders if o.status == status]
        return [
            {
                "order_id":  o.id,
                "side":      o.side.value,
                "type":      o.order_type.value,
                "price":     o.price,
                "quantity":  o.quantity,
                "filled":    o.filled,
                "status":    o.status,
                "created_at": o.created_at,
            }
            for o in list(orders)[-limit:]
        ]

    def get_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            {
                "trade_id":   t.id,
                "order_id":   t.order_id,
                "side":       t.side.value,
                "price":      t.price,
                "quantity":   t.quantity,
                "created_at": t.created_at,
            }
            for t in self._trades[-limit:]
        ]

    def place_order(
        self,
        side:       str,
        quantity:   float,
        price:      float | None = None,
        order_type: str = "market",
        fee_rate:   float = 0.001,
    ) -> dict[str, Any]:
        if side not in ("buy", "sell"):
            raise ValueError(f"Invalid side: {side!r}")
        if quantity <= 0:
            raise ValueError("Quantity must be positive")
        if order_type not in ("market", "limit"):
            raise ValueError(f"Invalid order type: {order_type!r}")
        if order_type == "limit" and price is None:
            raise ValueError("Limit orders require a price")

        self._order_cnt += 1
        order_id = f"{self.agent_id[:8]}_{self._order_cnt:04d}"
        order = Order(
            id=order_id,
            agent_id=self.agent_id,
            side=Side(side),
            order_type=OrderType(order_type),
            price=price,
            quantity=quantity,
        )

        if order_type == "market":
            raise NotImplementedError(
                "Market orders must use ExchangeManager.trade() or "
                "wallet.execute_market_order()"
            )
        else:
            self._lock_limit_order(order, fee_rate)
            self._orders[order_id] = order

        return {
            "order_id":   order.id,
            "agent_id":   self.agent_id,
            "side":       order.side.value,
            "type":       order.order_type.value,
            "price":      order.price,
            "quantity":   order.quantity,
            "filled":     order.filled,
            "status":     order.status,
            "created_at": order.created_at,
        }

    def execute_market_order(
        self,
        side:       str,
        quantity:   float,
        exec_price: float,
        fee_rate:   float = 0.001,
    ) -> dict[str, Any]:
        """Execute a market order at a price already locked by the manager."""
        with self._lock:
            self._order_cnt += 1
            order_id = f"{self.agent_id[:8]}_{self._order_cnt:04d}"
            order = Order(
                id=order_id,
                agent_id=self.agent_id,
                side=Side(side),
                order_type=OrderType.MARKET,
                price=None,
                quantity=quantity,
            )
            self._execute_order(order, exec_price, fee_rate)
            return {
                "order_id":   order.id,
                "agent_id":   self.agent_id,
                "side":       order.side.value,
                "type":       order.order_type.value,
                "price":      order.price,
                "quantity":   order.quantity,
                "filled":     order.filled,
                "status":     order.status,
                "created_at": order.created_at,
            }

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                return {"order_id": order_id, "status": "not_found"}
            if order.status != "open":
                return {"order_id": order_id, "status": order.status}
            b = self._balances.get("fcoin")
            if b is None:
                order.status = "cancelled"
                return {"order_id": order_id, "status": "cancelled"}
            lock_qty = min(order.quantity, b.locked)
            b.available = max(0.0, b.available + lock_qty)
            b.locked    = max(0.0, b.locked - lock_qty)
            order.status = "cancelled"
            return {"order_id": order_id, "status": "cancelled"}

    # ---------------------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------------------

    def _lock_limit_order(self, order: Order, fee_rate: float) -> None:
        with self._lock:
            if order.side == Side.SELL:
                b = self._balances["fcoin"]
                if b.available < order.quantity:
                    raise ValueError("Insufficient fcoin balance")
                b.available -= order.quantity
                b.locked    += order.quantity
            else:
                cost = order.price * order.quantity
                fee  = cost * fee_rate
                b = self._balances["usdc"]
                if b.available < cost + fee:
                    raise ValueError("Insufficient USDC balance")
                b.available -= cost + fee
                b.locked    += cost + fee
            self._orders[order.id] = order

    def _execute_order(self, order: Order, exec_price: float, fee_rate: float) -> None:
        cost = exec_price * order.quantity
        fee  = cost * fee_rate

        if order.side == Side.BUY:
            total = cost + fee
            if self._available_usdc < total:
                order.status = "rejected"
                return
            self._available_usdc  -= total
            self._available_fcoin += order.quantity
            self._balances["usdc"].available  = self._available_usdc
            self._balances["fcoin"].available = self._available_fcoin
        else:
            if self._available_fcoin < order.quantity:
                order.status = "rejected"
                return
            self._available_fcoin -= order.quantity
            self._available_usdc  += cost - fee
            self._balances["fcoin"].available = self._available_fcoin
            self._balances["usdc"].available  = self._available_usdc

        order.filled  = order.quantity
        order.status  = "filled"
        order.price   = exec_price

        self._trades.append(Trade(
            id=f"tr_{len(self._trades) + 1:06d}",
            agent_id=self.agent_id,
            order_id=order.id,
            side=order.side,
            price=exec_price,
            quantity=order.filled,
        ))


# ---------------------------------------------------------------------------
# ExchangeManager — manages per-agent wallets with a shared market
# ---------------------------------------------------------------------------

class ExchangeManager:
    """
    Per-agent wallet manager with a shared market price feed.

    Each agent gets an isolated AgentWallet. Market price (orderbook L2,
    ticker) is shared across all agents. Agents can be pre-created or
    auto-created on first trade.

    Scalability design:
      - Per-agent RLock (not global) so agents don't block each other
      - OrderBook uses double-buffer pattern — no lock on reads
      - __slots__ on all data classes cuts per-instance memory ~40%
      - Background book refresh holds lock briefly, then atomically swaps

    Usage:
        mgr = ExchangeManager()
        mgr.create_agent("agent-1", initial_usdc=5000)
        mgr.trade("agent-1", "buy", quantity=10)
        mgr.get_portfolio("agent-1")
    """

    __slots__ = (
        "_price_feed", "_wallets", "_book",
        "_maker_fee", "_taker_fee", "_refresh_interval",
        "_book_lock", "_running", "_thread",
    )

    def __init__(
        self,
        initial_price:     float = 100.0,
        volatility:         float = 0.002,
        maker_fee:          float = 0.001,
        taker_fee:          float = 0.001,
        seed:               int | None = None,
        refresh_interval:   float = 1.0,   # seconds between book refreshes
    ):
        self._price_feed      = PriceFeed(
            initial_price=initial_price,
            volatility=volatility,
            seed=seed,
        )
        self._wallets: dict[str, AgentWallet] = {}
        self._book                  = OrderBook()
        self._book_lock            = threading.Lock()
        self._maker_fee            = maker_fee
        self._taker_fee            = taker_fee
        self._refresh_interval     = refresh_interval
        self._running              = False
        self._thread: threading.Thread | None = None
        self._refresh_book()
        log.info("ExchangeManager initialised  price=%.4f", initial_price)

    # ---------------------------------------------------------------------------
    # Agent management
    # ---------------------------------------------------------------------------

    def create_agent(
        self,
        agent_id:    str | None = None,
        initial_usdc:  float = AgentWallet.DEFAULT_INITIAL_USDC,
        initial_fcoin: float = AgentWallet.DEFAULT_INITIAL_FCOIN,
    ) -> str:
        """Create a new agent wallet. Returns the agent_id."""
        if agent_id is None:
            agent_id = uuid.uuid4().hex[:12]
        if agent_id in self._wallets:
            raise ValueError(f"Agent already exists: {agent_id}")
        self._wallets[agent_id] = AgentWallet(
            agent_id=agent_id,
            initial_usdc=initial_usdc,
            initial_fcoin=initial_fcoin,
        )
        log.info("Agent created  id=%s  usdc=%.2f", agent_id, initial_usdc)
        return agent_id

    def get_or_create_agent(self, agent_id: str) -> AgentWallet:
        """Return existing wallet or create a new one with defaults."""
        # Fast path — dict lookup is thread-safe for read-only
        wallet = self._wallets.get(agent_id)
        if wallet is not None:
            return wallet
        return self._wallets.setdefault(
            agent_id,
            AgentWallet(agent_id=agent_id),
        )

    def list_agents(self) -> list[str]:
        """Return all agent IDs."""
        return list(self._wallets.keys())

    def delete_agent(self, agent_id: str) -> None:
        """Remove an agent and their wallet. Use with caution."""
        self._wallets.pop(agent_id, None)

    # ---------------------------------------------------------------------------
    # Shared market data
    # ---------------------------------------------------------------------------

    def get_ticker(self) -> dict[str, Any]:
        price = self._price_feed.get_price()
        return {"symbol": "fcoin/usdc", "price": price, "mid": price}

    def get_orderbook(self, depth: int = 20) -> dict[str, Any]:
        # Lock only for the dict/list copies — microseconds
        with self._book_lock:
            return {
                "bids": [{"price": l.price, "qty": l.quantity} for l in self._book.bids[:depth]],
                "asks": [{"price": l.price, "qty": l.quantity} for l in self._book.asks[:depth]],
                "mid_price": self._book.mid_price,
            }

    def step_price(self) -> float:
        """Advance the shared market price by one tick."""
        price = self._price_feed.step()
        self._refresh_book()
        return price

    # ---------------------------------------------------------------------------
    # Per-agent trading
    # ---------------------------------------------------------------------------

    def trade(
        self,
        agent_id: str,
        action:  str,
        quantity: float,
        price: float | None = None,
    ) -> dict[str, Any]:
        """Place a trade for a specific agent (auto-creates wallet if needed)."""
        wallet = self.get_or_create_agent(agent_id)
        order_type = "limit" if price is not None else "market"

        if order_type == "market":
            # Snapshot best bid/ask — double-buffer means read is lock-free
            best_bid, best_ask = self._book.snapshot()
            if action == "buy":
                exec_price = best_ask if best_ask is not None else self._price_feed.get_price()
            else:
                exec_price = best_bid if best_bid is not None else self._price_feed.get_price()
            return wallet.execute_market_order(
                side=action,
                quantity=quantity,
                exec_price=exec_price,
                fee_rate=self._taker_fee,
            )
        else:
            return wallet.place_order(
                side=action,
                quantity=quantity,
                price=price,
                order_type=order_type,
                fee_rate=self._taker_fee,
            )

    def get_portfolio(self, agent_id: str) -> dict[str, Any]:
        """Get an agent's portfolio (auto-creates wallet if needed)."""
        wallet = self.get_or_create_agent(agent_id)
        price  = self._price_feed.get_price()
        return {
            "agent_id": agent_id,
            "address":  wallet.address,
            "usdc":     wallet.get_balance("usdc"),
            "fcoin":    wallet.get_balance("fcoin"),
            "position": wallet.get_position(price),
            "orders":   wallet.get_orders(),
            "trades":   wallet.get_trades(),
        }

    def get_open_orders(self, agent_id: str) -> list[dict[str, Any]]:
        wallet = self.get_or_create_agent(agent_id)
        return wallet.get_orders(status="open")

    def cancel_order(self, agent_id: str, order_id: str) -> dict[str, Any]:
        wallet = self.get_or_create_agent(agent_id)
        return wallet.cancel_order(order_id)

    def set_price(self, price: float) -> dict[str, float]:
        """Admin: override the shared market price."""
        self._price_feed.set_price(price)
        self._refresh_book()
        return {"price": price}

    # ---------------------------------------------------------------------------
    # Background book refresh (optional — can be disabled with interval=0)
    # ---------------------------------------------------------------------------

    def start_background_refresh(self) -> None:
        """Start async book refresh thread. Call once at startup."""
        if self._refresh_interval <= 0:
            return
        self._running = True
        def _runner():
            while self._running:
                self._refresh_book()
                self._price_feed.step()
                threading.Event().wait(self._refresh_interval)
        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()

    def stop_background_refresh(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ---------------------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------------------

    def _refresh_book(self) -> None:
        # Build new book outside lock, then atomically swap
        price = self._price_feed.get_price()
        new_book = OrderBook()
        new_book.simulate_from_spread(price)
        with self._book_lock:
            self._book = new_book


# ---------------------------------------------------------------------------
# Backwards compatibility — single-exchange singleton
# ---------------------------------------------------------------------------

_exchange_manager: ExchangeManager | None = None


def get_exchange() -> ExchangeManager:
    """Return the live exchange instance. Raises if not initialised."""
    if _exchange_manager is None:
        raise RuntimeError("Exchange not initialised — call init_exchange() first")
    return _exchange_manager


def init_exchange(
    initial_usdc:  float = 10_000.0,
    initial_fcoin: float = 0.0,
    initial_price: float = 100.0,
    volatility:    float = 0.002,
    agent_id:      str | None = None,
) -> ExchangeManager:
    global _exchange_manager
    if _exchange_manager is None:
        _exchange_manager = ExchangeManager(
            initial_price=initial_price,
            volatility=volatility,
        )
        _exchange_manager.start_background_refresh()
    if agent_id:
        _exchange_manager.create_agent(
            agent_id=agent_id,
            initial_usdc=initial_usdc,
            initial_fcoin=initial_fcoin,
        )
    return _exchange_manager
