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
    asset:      str = "fcoin"   # "fcoin" or a coin symbol like "COINALICE"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())


@dataclass(slots=True)
class Coin:
    """An ERC-20-like token created by an agent."""
    id:            str   # unique id e.g. "COIN_alice_01"
    symbol:        str   # e.g. "COINALICE"
    name:          str   # e.g. "Alice Coin"
    total_supply:  float
    decimals:      int   # token decimal places
    owner:         str   # agent_id who created it
    price:         float # initial price in USDC per token


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
        self._balances: dict[str, Balance] = {
            "usdc":  Balance(available=initial_usdc),
            "fcoin": Balance(available=initial_fcoin),
        }
        # Ethereum-style wallet
        self._key = self._generate_key(priv_key)

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
        b_usdc  = self._balances.get("usdc")
        b_fcoin = self._balances.get("fcoin")

        if order.side == Side.BUY:
            total = cost + fee
            if b_usdc is None or b_usdc.available < total:
                order.status = "rejected"
                return
            b_usdc.available  -= total
            if b_fcoin is None:
                b_fcoin = Balance(available=0.0)
                self._balances["fcoin"] = b_fcoin
            b_fcoin.available += order.quantity
        else:
            if b_fcoin is None or b_fcoin.available < order.quantity:
                order.status = "rejected"
                return
            b_fcoin.available -= order.quantity
            b_usdc.available  += cost - fee

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
    # Sync to persistent store (called by ExchangeManager after mutations)
    # ---------------------------------------------------------------------------

    def sync_to_store(self, store: RedisWalletStore) -> None:
        """Persist current balances, orders, and trades to the store."""
        usdc_bal = self._balances.get("usdc")
        fcoin_bal = self._balances.get("fcoin")
        store.set_available_usdc(self.agent_id, usdc_bal.available if usdc_bal else 0.0)
        store.set_available_fcoin(self.agent_id, fcoin_bal.available if fcoin_bal else 0.0)
        orders = [
            {
                "id":           o.id,
                "agent_id":     o.agent_id,
                "side":         o.side.value,
                "order_type":   o.order_type.value,
                "price":        o.price,
                "quantity":     o.quantity,
                "filled":       o.filled,
                "status":       o.status,
                "created_at":   o.created_at,
            }
            for o in self._orders.values()
        ]
        store.save_orders(self.agent_id, orders)
        trades = [
            {
                "id":         t.id,
                "agent_id":   t.agent_id,
                "order_id":   t.order_id,
                "side":       t.side.value,
                "price":      t.price,
                "quantity":   t.quantity,
                "created_at": t.created_at,
            }
            for t in self._trades
        ]
        store.save_trades(self.agent_id, trades)


# ---------------------------------------------------------------------------
# Redis wallet store — wallets persisted across process restarts
# ---------------------------------------------------------------------------

import json
import redis


class RedisWalletStore:
    """
    AgentWallet storage backed by Redis hashes.

    Each agent's state is a Redis hash:
      WALLET:{agent_id} → {field: value, ...}
      ORDERS:{agent_id}  → JSON list of order dicts
      TRADES:{agent_id}  → JSON list of trade dicts

    Falls back to in-process dict if REDIS_URL is not set, so the same code
    works locally (no Redis) and in production (with Redis).
    """

    WALLET_KEY  = "WALLET:{agent_id}"
    ORDERS_KEY  = "ORDERS:{agent_id}"
    TRADES_KEY  = "TRADES:{agent_id}"

    def __init__(self, redis_url: str | None = None):
        self._local_wallets: dict[str, dict[str, str]] = {}  # agent_id → field→value
        self._local_orders:  dict[str, str] = {}              # agent_id → JSON list
        self._local_trades:  dict[str, str] = {}              # agent_id → JSON list
        self._redis: redis.Redis | None = None
        if redis_url:
            try:
                self._redis = redis.from_url(redis_url, decode_responses=True)
                self._redis.ping()
                log.info("RedisWalletStore connected  url=%s", redis_url.split(":")[2] if ":" in redis_url else "...")
            except Exception as exc:
                log.warning("Redis unavailable, using in-process store: %s", exc)
                self._redis = None

    # ---- wallet-level ops --------------------------------------------------

    def _wallet_key(self, agent_id: str) -> str:
        return self.WALLET_KEY.format(agent_id=agent_id)

    def _orders_key(self, agent_id: str) -> str:
        return self.ORDERS_KEY.format(agent_id=agent_id)

    def _trades_key(self, agent_id: str) -> str:
        return self.TRADES_KEY.format(agent_id=agent_id)

    def _hset_num(self, key: str, field: str, value: float) -> None:
        if self._redis:
            self._redis.hset(key, field, str(value))
        else:
            self._local_wallets.setdefault(key, {})[field] = str(value)

    def _hget_num(self, key: str, field: str) -> float:
        if self._redis:
            val = self._redis.hget(key, field)
            return float(val) if val is not None else 0.0
        return float(self._local_wallets.get(key, {}).get(field, "0.0"))

    def _hget_str(self, key: str, field: str) -> str | None:
        if self._redis:
            return self._redis.hget(key, field)
        return self._local_wallets.get(key, {}).get(field)

    # ---- public store API -------------------------------------------------

    def exists(self, agent_id: str) -> bool:
        k = self._wallet_key(agent_id)
        if self._redis:
            return bool(self._redis.exists(k))
        return k in self._local_wallets

    def create_wallet(
        self,
        agent_id: str,
        available_usdc: float,
        available_fcoin: float,
        order_cnt: int,
    ) -> None:
        k = self._wallet_key(agent_id)
        data = {
            "agent_id":         agent_id,
            "available_usdc":   str(available_usdc),
            "available_fcoin":  str(available_fcoin),
            "order_cnt":        str(order_cnt),
        }
        if self._redis:
            self._redis.hset(k, mapping=data)
        else:
            self._local_wallets[k] = data

    def get_available_usdc(self, agent_id: str) -> float:
        return self._hget_num(self._wallet_key(agent_id), "available_usdc")

    def get_available_fcoin(self, agent_id: str) -> float:
        return self._hget_num(self._wallet_key(agent_id), "available_fcoin")

    def set_available_usdc(self, agent_id: str, value: float) -> None:
        self._hset_num(self._wallet_key(agent_id), "available_usdc", value)

    def set_available_fcoin(self, agent_id: str, value: float) -> None:
        self._hset_num(self._wallet_key(agent_id), "available_fcoin", value)

    def get_order_cnt(self, agent_id: str) -> int:
        val = self._hget_str(self._wallet_key(agent_id), "order_cnt")
        return int(val) if val else 0

    def incr_order_cnt(self, agent_id: str) -> int:
        k = self._wallet_key(agent_id)
        if self._redis:
            n = self._redis.hincrby(k, "order_cnt", 1)
        else:
            d = self._local_wallets.setdefault(k, {})
            n = int(d.get("order_cnt", "0")) + 1
            d["order_cnt"] = str(n)
        return n

    # ---- orders ------------------------------------------------------------

    def get_orders(self, agent_id: str) -> list[dict]:
        k = self._orders_key(agent_id)
        if self._redis:
            raw = self._redis.get(k)
            return json.loads(raw) if raw else []
        return json.loads(self._local_orders.get(k, "[]"))

    def save_orders(self, agent_id: str, orders: list[dict]) -> None:
        k = self._orders_key(agent_id)
        if self._redis:
            self._redis.set(k, json.dumps(orders))
        else:
            self._local_orders[k] = json.dumps(orders)

    # ---- trades ------------------------------------------------------------

    def get_trades(self, agent_id: str) -> list[dict]:
        k = self._trades_key(agent_id)
        if self._redis:
            raw = self._redis.get(k)
            return json.loads(raw) if raw else []
        return json.loads(self._local_trades.get(k, "[]"))

    def save_trades(self, agent_id: str, trades: list[dict]) -> None:
        k = self._trades_key(agent_id)
        if self._redis:
            self._redis.set(k, json.dumps(trades))
        else:
            self._local_trades[k] = json.dumps(trades)


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
        "_book_lock", "_running", "_thread", "_store",
        "_coins", "_coin_registry",
    )

    def __init__(
        self,
        initial_price:     float = 100.0,
        volatility:         float = 0.002,
        maker_fee:          float = 0.001,
        taker_fee:          float = 0.001,
        seed:               int | None = None,
        refresh_interval:   float = 1.0,
        redis_url:          str | None = None,   # NEW: persist wallets to Redis
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
        self._store                = RedisWalletStore(redis_url)
        self._coins: dict[str, Coin]               = {}
        self._coin_registry: dict[str, str]        = {}
        self._refresh_book()
        log.info("ExchangeManager initialised  price=%.4f  redis=%s",
                 initial_price, "yes" if redis_url else "no (in-process)")

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
        """Return existing wallet or create/load one with defaults."""
        # Fast path — in-process cache
        wallet = self._wallets.get(agent_id)
        if wallet is not None:
            return wallet
        # Check persisted store (cold start after restart)
        if self._store.exists(agent_id):
            wallet = AgentWallet(agent_id=agent_id)
            stored_usdc  = self._store.get_available_usdc(agent_id)
            stored_fcoin = self._store.get_available_fcoin(agent_id)
            wallet._order_cnt = self._store.get_order_cnt(agent_id)
            # Re-hydrate balances dict
            wallet._balances["usdc"].available  = stored_usdc
            wallet._balances["fcoin"].available = stored_fcoin
            self._wallets[agent_id] = wallet
            log.info("Agent loaded from store  id=%s", agent_id)
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
            result = wallet.execute_market_order(
                side=action,
                quantity=quantity,
                exec_price=exec_price,
                fee_rate=self._taker_fee,
            )
            wallet.sync_to_store(self._store)
            self._broadcast_trade(result)
            return result
        else:
            result = wallet.place_order(
                side=action,
                quantity=quantity,
                price=price,
                order_type=order_type,
                fee_rate=self._taker_fee,
            )
            wallet.sync_to_store(self._store)
            return result

    def get_portfolio(self, agent_id: str) -> dict[str, Any]:
        """Get an agent's portfolio (auto-creates wallet if needed)."""
        wallet = self.get_or_create_agent(agent_id)
        price  = self._price_feed.get_price()
        # Include all asset balances (usdc, fcoin, and any agent-issued coins)
        assets = {}
        for asset, bal in wallet._balances.items():
            assets[asset] = {"available": bal.available, "locked": bal.locked, "total": bal.total}
        return {
            "agent_id": agent_id,
            "address":  wallet.address,
            "usdc":     assets.pop("usdc", {"available": 0.0, "locked": 0.0, "total": 0.0}),
            "fcoin":    assets.pop("fcoin", {"available": 0.0, "locked": 0.0, "total": 0.0}),
            "coins":    assets,
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
    # Agent-issued coins (ERC-20-like)
    # ---------------------------------------------------------------------------

    def create_coin(
        self,
        owner:        str,
        symbol:       str,
        name:         str,
        total_supply: float,
        decimals:     int = 18,
        price:        float = 1.0,
    ) -> dict[str, Any]:
        """
        Create a new agent-issued coin.
        The entire supply is minted to the owner's balance.
        Symbol is uppercased and stored uppercase.
        """
        if total_supply <= 0:
            raise ValueError("total_supply must be positive")
        if decimals < 0 or decimals > 18:
            raise ValueError("decimals must be between 0 and 18")
        symbol_upper = symbol.upper()
        if symbol_upper in ("USDC", "FCOIN"):
            raise ValueError(f"Cannot use reserved symbol {symbol_upper}")
        if symbol_upper in self._coin_registry:
            raise ValueError(f"Symbol {symbol_upper} already exists")

        coin_id = f"COIN_{owner}_{len([c for c in self._coins.values() if c.owner == owner]) + 1:02d}"
        coin = Coin(
            id=symbol_upper,
            symbol=symbol_upper,
            name=name,
            total_supply=total_supply,
            decimals=decimals,
            owner=owner,
            price=price,
        )
        self._coins[coin_id] = coin
        self._coin_registry[symbol_upper] = coin_id

        # Mint full supply to owner's balance
        owner_wallet = self.get_or_create_agent(owner)
        bal = owner_wallet._balances.get(symbol_upper)
        if bal is None:
            bal = Balance()
            owner_wallet._balances[symbol_upper] = bal
        bal.available += total_supply

        return {
            "coin_id":       coin_id,
            "symbol":        symbol_upper,
            "name":          name,
            "total_supply":  total_supply,
            "decimals":      decimals,
            "owner":         owner,
            "price":         price,
            "circulating":   total_supply,
        }

    def list_coins(self) -> list[dict[str, Any]]:
        """List all agent-issued coins."""
        return [
            {
                "symbol":       c.symbol,
                "name":         c.name,
                "total_supply": c.total_supply,
                "decimals":     c.decimals,
                "owner":        c.owner,
                "price":        c.price,
            }
            for c in self._coins.values()
        ]

    def get_coin_price(self, symbol: str) -> float | None:
        """Get the USDC price of an agent coin."""
        coin_id = self._coin_registry.get(symbol.upper())
        if coin_id is None:
            return None
        return self._coins[coin_id].price

    def trade_coin(
        self,
        agent_id: str,
        action:   str,
        symbol:   str,
        quantity: float,
    ) -> dict[str, Any]:
        """
        Execute a market order for an agent-issued coin.
        The counterparty is the coin owner (book is P2P, price is fixed at coin.price).
        """
        symbol_upper = symbol.upper()
        coin_id = self._coin_registry.get(symbol_upper)
        if coin_id is None:
            raise ValueError(f"Unknown symbol: {symbol_upper}")
        coin = self._coins[coin_id]

        if action not in ("buy", "sell"):
            raise ValueError("action must be 'buy' or 'sell'")
        if quantity <= 0:
            raise ValueError("quantity must be positive")

        buyer  = self.get_or_create_agent(agent_id)
        seller = self.get_or_create_agent(coin.owner)

        cost = quantity * coin.price
        fee  = cost * self._taker_fee

        with buyer._lock:
            if action == "buy":
                # Deduct USDC from buyer
                b_usdc = buyer._balances.get("usdc")
                if b_usdc is None or b_usdc.available < cost + fee:
                    return {"status": "rejected", "reason": "insufficient_usdc"}
                b_usdc.available -= (cost + fee)
                # Add coin tokens to buyer
                b_token = buyer._balances.get(symbol_upper)
                if b_token is None:
                    b_token = Balance()
                    buyer._balances[symbol_upper] = b_token
                b_token.available += quantity
            else:
                # Deduct coin tokens from seller (seller is always the owner)
                s_token = seller._balances.get(symbol_upper)
                if s_token is None or s_token.available < quantity:
                    return {"status": "rejected", "reason": f"insufficient_{symbol_upper}"}
                s_token.available -= quantity
                # Add USDC to seller
                seller._balances["usdc"].available += (cost - fee)

        # Record the trade
        trade_id = f"tr_{len(buyer._trades) + 1:06d}"
        trade = Trade(
            id=trade_id,
            agent_id=agent_id,
            order_id=f"{agent_id[:8]}_coin_{len(buyer._trades):04d}",
            side=Side(action),
            price=coin.price,
            quantity=quantity,
            asset=symbol_upper,
        )
        buyer._trades.append(trade)

        # Sync both wallets
        buyer.sync_to_store(self._store)
        seller.sync_to_store(self._store)

        return {
            "status":    "filled",
            "trade_id":  trade_id,
            "agent_id":  agent_id,
            "symbol":    symbol_upper,
            "side":      action,
            "price":     coin.price,
            "quantity":  quantity,
            "cost":      cost,
            "fee":       fee,
            "owner":     coin.owner,
        }

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
        # Broadcast market data to SSE clients (non-blocking)
        try:
            from src.stream import market_stream
            mid = (self._book.bids[0].price + self._book.asks[0].price) / 2
            market_stream.broadcast({"type": "ticker",    "data": self.get_ticker()})
            market_stream.broadcast({"type": "orderbook", "data": self._book.to_dict()})
        except Exception:
            pass

    # ------------------------------------------------------------------
    # SSE broadcast helpers (non-blocking)
    # ------------------------------------------------------------------

    def _broadcast_trade(self, result: dict) -> None:
        """Broadcast a filled trade to SSE clients."""
        try:
            from src.stream import market_stream
            market_stream.broadcast({"type": "trade", "data": result})
        except Exception:
            pass

    # --------------------------------------------------------------------------
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
    redis_url:     str | None = None,   # NEW
) -> ExchangeManager:
    global _exchange_manager
    if _exchange_manager is None:
        _exchange_manager = ExchangeManager(
            initial_price=initial_price,
            volatility=volatility,
            redis_url=redis_url,
        )
        _exchange_manager.start_background_refresh()
    if agent_id:
        _exchange_manager.create_agent(
            agent_id=agent_id,
            initial_usdc=initial_usdc,
            initial_fcoin=initial_fcoin,
        )
    return _exchange_manager
