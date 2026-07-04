"""
MCP trading tools for the fcoin synthetic exchange.
Exposes the ExchangeManager as JSON-serialisable MCP tool handlers.

Agent identification: each MCP session uses a default agent_id. The REST API
uses X-Agent-ID header to identify agents.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from ..exchange import ExchangeManager, init_exchange, get_exchange
from ..agent import ToolDef

log = logging.getLogger("fcoin.tools")

DEFAULT_AGENT = "default"

# Ensure exchange is initialised (reads REDIS_URL from env)
init_exchange(redis_url=os.environ.get("REDIS_URL"))


def _wallet() -> ExchangeManager:
    return get_exchange()


def _agent_trade(action: str, quantity: float, price: float | None = None) -> dict[str, Any]:
    return _wallet().trade(DEFAULT_AGENT, action, quantity, price)


# ---------------------------------------------------------------------------
# Tool definitions (MCP schema)
# ---------------------------------------------------------------------------

TOOLS: list[ToolDef] = []


def _make_tool(name: str, desc: str, schema: dict[str, Any], fn: Any) -> ToolDef:
    td = ToolDef(name=name, description=desc, input_schema=schema, handler=fn)
    TOOLS.append(td)
    return td


# ---- Market data -----------------------------------------------------------

_make_tool(
    name="get_ticker",
    desc="Get the current mid-price of fcoin.",
    schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    fn=lambda: _wallet().get_ticker(),
)

_make_tool(
    name="get_orderbook",
    desc="Get the current limit-order book (bids and asks) for fcoin/usdc.",
    schema={
        "type": "object",
        "properties": {
            "depth": {"type": "integer", "default": 20, "description": "Number of price levels per side"},
        },
        "additionalProperties": False,
    },
    fn=lambda depth=20: _wallet().get_orderbook(depth=depth),
)

_make_tool(
    name="get_trades",
    desc="Get recent trade history for this agent.",
    schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 50},
        },
        "additionalProperties": False,
    },
    fn=lambda limit=50: {"trades": _wallet().get_portfolio(DEFAULT_AGENT)["trades"][-limit:]},
)

# ---- Account --------------------------------------------------------------

_make_tool(
    name="get_balance",
    desc="Get available, locked, and total balance for an asset.",
    schema={
        "type": "object",
        "properties": {
            "asset": {"type": "string", "enum": ["usdc", "fcoin"], "default": "usdc"},
        },
        "additionalProperties": False,
    },
    fn=lambda asset="usdc": _wallet().get_portfolio(DEFAULT_AGENT)[asset],
)

_make_tool(
    name="get_position",
    desc="Get current fcoin position with current price and unrealised PnL.",
    schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    fn=lambda: _wallet().get_portfolio(DEFAULT_AGENT)["position"],
)

_make_tool(
    name="get_open_orders",
    desc="List all open (unfilled) orders for this agent.",
    schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    fn=lambda: {"orders": _wallet().get_open_orders(DEFAULT_AGENT)},
)

# ---- Trading --------------------------------------------------------------

_make_tool(
    name="market_buy",
    desc="Place a market buy order for fcoin at the best available ask.",
    schema={
        "type": "object",
        "required": ["quantity"],
        "properties": {
            "quantity": {"type": "number", "description": "Amount of fcoin to buy"},
        },
        "additionalProperties": False,
    },
    fn=lambda quantity: _agent_trade("buy", quantity),
)

_make_tool(
    name="market_sell",
    desc="Place a market sell order for fcoin at the best available bid.",
    schema={
        "type": "object",
        "required": ["quantity"],
        "properties": {
            "quantity": {"type": "number", "description": "Amount of fcoin to sell"},
        },
        "additionalProperties": False,
    },
    fn=lambda quantity: _agent_trade("sell", quantity),
)

_make_tool(
    name="limit_buy",
    desc="Place a limit buy order for fcoin at a specific price.",
    schema={
        "type": "object",
        "required": ["quantity", "price"],
        "properties": {
            "quantity": {"type": "number", "description": "Amount of fcoin to buy"},
            "price":    {"type": "number", "description": "Limit price in USDC"},
        },
        "additionalProperties": False,
    },
    fn=lambda quantity, price: _agent_trade("buy", quantity, price),
)

_make_tool(
    name="limit_sell",
    desc="Place a limit sell order for fcoin at a specific price.",
    schema={
        "type": "object",
        "required": ["quantity", "price"],
        "properties": {
            "quantity": {"type": "number", "description": "Amount of fcoin to sell"},
            "price":    {"type": "number", "description": "Limit price in USDC"},
        },
        "additionalProperties": False,
    },
    fn=lambda quantity, price: _agent_trade("sell", quantity, price),
)

_make_tool(
    name="cancel_order",
    desc="Cancel an open order by its ID.",
    schema={
        "type": "object",
        "required": ["order_id"],
        "properties": {
            "order_id": {"type": "string", "description": "Order ID to cancel"},
        },
        "additionalProperties": False,
    },
    fn=lambda order_id: _wallet().cancel_order(DEFAULT_AGENT, order_id),
)

# ---- Admin ----------------------------------------------------------------

_make_tool(
    name="set_price",
    desc="Admin: override the current fcoin price (for simulation control).",
    schema={
        "type": "object",
        "required": ["price"],
        "properties": {
            "price": {"type": "number", "description": "New mid-price in USDC"},
        },
        "additionalProperties": False,
    },
    fn=lambda price: _wallet().set_price(price),
)
