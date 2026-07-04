"""
Auto-trader daemon — runs N agent loops that continuously trade fcoin.
Each agent has its own wallet, makes random or signal-based trades,
and syncs state to Redis for persistence.

Usage:
    from src.auto_trader import AutoTrader
    trader = AutoTrader(exchange, n_agents=5, interval_secs=10.0)
    trader.start()
    # ... app runs ...
    trader.stop()
"""
from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

log = logging.getLogger("fcoin.auto_trader")

Signal = int  # -1 = sell, 0 = hold, 1 = buy


# ---------------------------------------------------------------------------
# Simple trading signals (stub — replace with real alpha)
# ---------------------------------------------------------------------------

def moving_average_signal(prices: list[float], window: int = 5) -> Signal:
    """Buy when price < MA(window), sell when price > MA(window)."""
    if len(prices) < window:
        return 0
    ma = sum(prices[-window:]) / window
    mid = prices[-1]
    if mid < ma * 0.995:
        return 1
    if mid > ma * 1.005:
        return -1
    return 0


def random_signal(prices: list[float]) -> Signal:
    return random.choice([-1, 1])  # 50/50 buy or sell


# ---------------------------------------------------------------------------
# Per-agent trading loop
# ---------------------------------------------------------------------------

@dataclass
class AgentLoop:
    """One background thread that repeatedly trades for a single agent."""
    agent_id:     str
    exchange:     any  # ExchangeManager
    interval:     float = 10.0
    seed_capital: float = 10_000.0
    position_size: float = 0.5       # fraction of available USDC per buy
    max_position: float = 10.0       # stop accumulating above this
    signal_fn:    Callable[[list[float]], Signal] = moving_average_signal
    _running:     bool = field(default=False, init=False)
    _thread:      threading.Thread | None = field(default=None, init=False)
    _price_history: list[float] = field(default_factory=list)
    _lock:        threading.Lock = field(default_factory=threading.Lock, init=False)

    # Internal state
    _stop: threading.Event = field(default_factory=threading.Event, init=False)

    def _price_too_high(self, price: float) -> bool:
        """Reject buys above a sanity cap (2x initial)."""
        return price > 200.0

    def _run(self) -> None:
        """Main loop — runs until self._stop is set."""
        log.info(f"[{self.agent_id}] loop started  capital={self.seed_capital}")

        while not self._stop.wait(self.interval):
            try:
                self._tick()
            except Exception as exc:
                log.warning(f"[{self.agent_id}] tick error: {exc}")

        log.info(f"[{self.agent_id}] loop stopped")

    def _tick(self) -> None:
        """One trading decision."""
        ex = self.exchange
        ticker = ex.get_ticker()
        price = ticker["mid"]
        portfolio = ex.get_portfolio(self.agent_id)
        usdc_bal = portfolio["usdc"]["available"]
        fcoin_bal = portfolio["fcoin"]["available"]
        position = portfolio["position"]

        # Track price history
        with self._lock:
            self._price_history.append(price)
            if len(self._price_history) > 50:
                self._price_history = self._price_history[-50:]
            signal = self.signal_fn(self._price_history.copy())

        # --- decision logic ---
        action: str | None = None
        amount: float | None = None

        if signal == 1 and usdc_bal > 10:
            # Buy signal: spend position_size fraction of USDC
            if self._price_too_high(price):
                log.debug(f"[{self.agent_id}] price {price:.2f} too high, skipping buy")
                return
            cost_budget = usdc_bal * self.position_size
            amount = cost_budget / price
            action = "buy"

        elif signal == -1 and fcoin_bal > 0.01:
            # Sell signal: offload half the position
            amount = fcoin_bal * 0.5
            action = "sell"

        elif usdc_bal < 5 and fcoin_bal > 0:
            # Out of dry powder — liquidate to get some
            amount = fcoin_bal * 0.25
            action = "sell"

        if action and amount:
            amount = round(amount, 4)
            if amount < 0.001:
                return
            try:
                result = ex.trade(self.agent_id, action, amount)
                status = result.get("status", "?")
                filled = result.get("filled", 0)
                exec_price = result.get("price", 0)
                log.info(
                    f"[{self.agent_id}] {action.upper()} {filled:.4f}fcoin "
                    f"@ {exec_price:.4f}  status={status}  "
                    f"usdc={usdc_bal:.2f}→{ex.get_portfolio(self.agent_id)['usdc']['available']:.2f}"
                )
            except Exception as exc:
                log.warning(f"[{self.agent_id}] trade failed: {exc}")

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)


# ---------------------------------------------------------------------------
# AutoTrader — manages N agent loops
# ---------------------------------------------------------------------------

@dataclass
class AutoTrader:
    """
    Spawns and manages N autonomous trading agents.

    Usage:
        trader = AutoTrader(exchange, n_agents=5, interval_secs=15.0)
        trader.start()
        # ...
        trader.stop()
    """
    exchange:    any  # ExchangeManager
    n_agents:    int = 5
    interval:    float = 15.0
    seed_capital: float = 10_000.0
    signal_fn:   Callable[[list[float]], Signal] = moving_average_signal
    _loops:      list[AgentLoop] = field(default_factory=list)
    _running:    bool = field(default=False, init=False)

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        for i in range(self.n_agents):
            agent_id = f"agent-{i+1:03d}"
            # Pre-create agent with seed capital
            self.exchange.create_agent(
                agent_id=agent_id,
                initial_usdc=self.seed_capital,
                initial_fcoin=0.0,
            )
            loop = AgentLoop(
                agent_id=agent_id,
                exchange=self.exchange,
                interval=self.interval,
                seed_capital=self.seed_capital,
                signal_fn=self.signal_fn,
            )
            loop.start()
            self._loops.append(loop)
            log.info(f"AutoTrader spawned {agent_id}")

        log.info(f"AutoTrader started {self.n_agents} agents")

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        for loop in self._loops:
            loop.stop()
        log.info("AutoTrader stopped")

    def status(self) -> dict:
        return {
            "running": self._running,
            "n_agents": len(self._loops),
            "agents": [
                {
                    "agent_id": lp.agent_id,
                    "alive": lp._running and lp._thread is not None and lp._thread.is_alive(),
                    "interval": lp.interval,
                }
                for lp in self._loops
            ],
        }
