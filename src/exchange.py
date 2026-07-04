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


@dataclass
class Order:
    id:         str
    agent_id:   str
    side:       Side
    order_type: OrderType
    price:      float | None
    quantity:   float
    filled:     float = 0.0
    status:     str   = "open"
    created_at: str   = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass
class Trade:
    id:         str
    agent_id:   str
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
        self._price    = initial_price
        self._vol      = volatility
        self._drift    = drift
        self._lock     = threading.Lock()
        self._rng      = random.Random(seed)

    def get_price(self) -> float:
        """Return the current mid-price (sync, thread-safe)."""
        with self._lock:
            return self._price

    def step(self) -> float:
        """Advance price one tick using geometric Brownian motion."""
        with self._lock:
            shock = self._rng.gauss(0, self._vol)
            self._price = max(0.001, self._price * (1 + self._drift + shock))
            return self._price

    def set_price(self, price: float) -> None:
        """Override price directly (e.g. with live feed)."""
        with self._lock:
            self._price = price


# ---------------------------------------------------------------------------
# OrderBook (shared market L2 data)
# ---------------------------------------------------------------------------

@dataclass
class Level:
    price:    float
    quantity: float


@dataclass
class OrderBook:
    bids: list[Level] = field(default_factory=list)
    asks: list[Level] = field(default_factory=list)

    @property
    def mid_price(self) -> float | None:
        if not self.bids or not self.asks:
            return None
        return (self.bids[0].price + self.asks[0].price) / 2

    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    def simulate_from_spread(self, mid: float, spread_bps: float = 20) -> None:
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
# AgentWallet — isolated wallet for one agent
# ---------------------------------------------------------------------------

class AgentWallet:
    """
    Isolated balances, orders, and trades for a single agent.
    Includes an Ethereum-style secp256k1 wallet (address + private key).
    """

    DEFAULT_INITIAL_USDC  = 10_000.0
    DEFAULT_INITIAL_FCOIN = 0.0

    def __init__(
        self,
        agent_id:   str,
        initial_usdc:  float = DEFAULT_INITIAL_USDC,
        initial_fcoin: float = DEFAULT_INITIAL_FCOIN,
        priv_key: bytes | None = None,
    ):
        self.agent_id   = agent_id
        self._balances: dict[str, Balance] = {
            "usdc":  Balance(available=initial_usdc),
            "fcoin": Balance(available=initial_fcoin),
        }
        self._orders:   dict[str, Order] = {}
        self._trades:   list[Trade]      = []
        self._order_cnt = 0
        # Ethereum-style wallet
        self._key = self._generate_key(priv_key)

    @property
    def available_usdc(self) -> float:
        return self._balances["usdc"].available

    @available_usdc.setter
    def available_usdc(self, value: float) -> None:
        self._balances["usdc"].available = value

    @property
    def available_fcoin(self) -> float:
        return self._balances["fcoin"].available

    @available_fcoin.setter
    def available_fcoin(self, value: float) -> None:
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
        # Generate random 32 bytes using HMAC-DRBG seeded from os.urandom
        seed = hashlib.sha256(__import__("os").urandom(32)).digest()
        ctx = hmac.new(seed, b"secp256k1", hashlib.sha256).digest()
        return ctx

    @property
    def private_key_hex(self) -> str:
        """Raw private key as 0x-prefixed hex string. Keep secret!"""
        return "0x" + self._key.hex()

    @property
    def address(self) -> str:
        """Ethereum-style address derived from public key (Keccak-256)."""
        import hashlib
        p = self._derive_public_key()
        try:
            h = hashlib.sha3_256(p).digest()
        except AttributeError:
            import sha3
            h = sha3.sha3_256(p).digest()
        return "0x" + h[-20:].hex()

    def _derive_public_key(self) -> bytes:
        """Derive uncompressed secp256k1 public key bytes (0x04 || x || y)."""
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.backends import default_backend
        from cryptography.hazmat.primitives import serialization
        pk = ec.derive_private_key(int.from_bytes(self._key, "big"), ec.SECP256K1(), default_backend())
        pub = pk.public_key()
        # Uncompressed: 0x04 || x || y
        return b"\x04" + pub.public_numbers().x.to_bytes(32, "big") + pub.public_numbers().y.to_bytes(32, "big")

    # ---------------------------------------------------------------------------
    # Trading API
    # ---------------------------------------------------------------------------

    def get_balance(self, asset: str) -> dict[str, float]:
        b = self._balances.get(asset, Balance())
        return {"available": b.available, "locked": b.locked, "total": b.total}

    def get_position(self, price: float) -> dict[str, Any]:
        fc = self._balances["fcoin"]
        return {
            "asset":           "fcoin",
            "quantity":        fc.total,
            "avg_entry":        0.0,
            "current_price":   price,
            "unrealized_pnl":  0.0,
        }

    def get_orders(self, status: str | None = None) -> list[dict[str, Any]]:
        orders = [o for o in self._orders.values() if o.agent_id == self.agent_id]
        if status:
            orders = [o for o in orders if o.status == status]
        return [
            {
                "order_id":   o.id,
                "side":       o.side.value,
                "type":       o.order_type.value,
                "price":      o.price,
                "quantity":   o.quantity,
                "filled":     o.filled,
                "status":     o.status,
                "created_at": o.created_at,
            }
            for o in orders
        ]

    def get_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        agent_trades = [t for t in self._trades if t.agent_id == self.agent_id]
        return [
            {
                "id":         t.id,
                "order_id":   t.order_id,
                "side":       t.side.value,
                "price":      t.price,
                "quantity":   t.quantity,
                "created_at": t.created_at,
            }
            for t in agent_trades[-limit:]
        ]

    def place_order(
        self,
        side:       str,
        quantity:   float,
        price:      float | None = None,
        order_type: str = "market",
        fee_rate:   float = 0.001,
        book:       OrderBook | None = None,
        mid_price:  float = 100.0,
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
            self._execute_order(order, book, mid_price, fee_rate)
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

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        order = self._orders.get(order_id)
        if not order or order.agent_id != self.agent_id:
            raise ValueError(f"Order not found: {order_id}")
        if order.status != "open":
            raise ValueError(f"Order is not open: {order.status}")

        asset = "fcoin" if order.side == Side.SELL else "usdc"
        lock_qty = (order.quantity - order.filled) * (order.price or 100.0)
        b = self._balances[asset]
        b.locked = max(0, b.locked - lock_qty)
        order.status = "cancelled"
        return {"order_id": order_id, "status": "cancelled"}

    # ---------------------------------------------------------------------------
    # Internal
    # ---------------------------------------------------------------------------

    def _lock_limit_order(self, order: Order, fee_rate: float) -> None:
        if order.side == Side.SELL:
            if self._balances["fcoin"].available < order.quantity:
                raise ValueError("Insufficient fcoin balance")
            self._balances["fcoin"].available -= order.quantity
            self._balances["fcoin"].locked    += order.quantity
        else:
            cost = order.price * order.quantity
            fee  = cost * fee_rate
            if self._balances["usdc"].available < cost + fee:
                raise ValueError("Insufficient USDC balance")
            self._balances["usdc"].available -= cost + fee
            self._balances["usdc"].locked    += cost + fee
        self._orders[order.id] = order

    def _execute_order(
        self,
        order:    Order,
        book:     OrderBook | None,
        mid_price: float,
        fee_rate: float,
    ) -> None:
        if order.side == Side.BUY:
            exec_price = (book.best_ask() if book else None) or mid_price
        else:
            exec_price = (book.best_bid() if book else None) or mid_price

        cost = exec_price * order.quantity
        fee  = cost * fee_rate

        if order.side == Side.BUY:
            total = cost + fee
            if self.available_usdc < total:
                order.status = "rejected"
                return
            self.available_usdc -= total
            self.available_fcoin += order.quantity
        else:
            if self.available_fcoin < order.quantity:
                order.status = "rejected"
                return
            self.available_fcoin -= order.quantity
            self.available_usdc  += cost - fee

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

    Usage:
        mgr = ExchangeManager()
        mgr.create_agent("agent-1", initial_usdc=5000)
        mgr.trade("agent-1", "buy", quantity=10)
        mgr.get_portfolio("agent-1")
    """

    def __init__(
        self,
        initial_price: float = 100.0,
        volatility:     float = 0.002,
        maker_fee:      float = 0.001,
        taker_fee:      float = 0.001,
        seed:           int | None = None,
    ):
        self._price_feed = PriceFeed(
            initial_price=initial_price,
            volatility=volatility,
            seed=seed,
        )
        self._wallets: dict[str, AgentWallet] = {}
        self._book     = OrderBook()
        self._book_lock = threading.Lock()
        self._maker_fee = maker_fee
        self._taker_fee = taker_fee
        self._refresh_book()
        log.info("ExchangeManager initialised  price=%.4f", initial_price)

    # ---------------------------------------------------------------------------
    # Agent management
    # ---------------------------------------------------------------------------

    def create_agent(
        self,
        agent_id: str | None = None,
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
        if agent_id not in self._wallets:
            self.create_agent(agent_id)
        return self._wallets[agent_id]

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
        action: str,           # "buy" or "sell"
        quantity: float,
        price: float | None = None,  # None = market order
    ) -> dict[str, Any]:
        """Place a trade for a specific agent (auto-creates wallet if needed)."""
        wallet = self.get_or_create_agent(agent_id)
        order_type = "limit" if price is not None else "market"
        return wallet.place_order(
            side=action,
            quantity=quantity,
            price=price,
            order_type=order_type,
            fee_rate=self._taker_fee,
            book=self._book if order_type == "market" else None,
            mid_price=self._price_feed.get_price(),
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
    # Internal
    # ---------------------------------------------------------------------------

    def _refresh_book(self) -> None:
        price = self._price_feed.get_price()
        with self._book_lock:
            self._book.simulate_from_spread(price)


# ---------------------------------------------------------------------------
# Backwards compatibility — single-exchange singleton
# ---------------------------------------------------------------------------

_exchange_manager: ExchangeManager | None = None


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
    if agent_id:
        _exchange_manager.create_agent(
            agent_id=agent_id,
            initial_usdc=initial_usdc,
            initial_fcoin=initial_fcoin,
        )
    return _exchange_manager


def get_exchange() -> ExchangeManager:
    if _exchange_manager is None:
        return init_exchange()
    return _exchange_manager
