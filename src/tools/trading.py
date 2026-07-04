"""
MCP trading tools for the fcoin synthetic exchange.
Exposes the Exchange as JSON-serialisable MCP tool handlers.
"""
from __future__ import annotations

import logging
from typing import Any

from ..exchange import Exchange, PriceFeed
from ..agent import ToolDef

log = logging.getLogger("fcoin.tools")

# ---------------------------------------------------------------------------
# Singleton exchange instance (shared across all tool calls)
# ---------------------------------------------------------------------------

_exchange: Exchange | None = None
_price_feed: PriceFeed | None = None


def init_exchange(
    initial_usdc: float   = 10_000.0,
    initial_fcoin: float  = 0.0,
    initial_price: float  = 100.0,
    volatility: float     = 0.002,
) -> Exchange:
    global _exchange, _price_feed
    if _exchange is None:
        _price_feed = PriceFeed(initial_price=initial_price, volatility=volatility)
        _exchange   = Exchange(
            initial_usdc=initial_usdc,
            initial_fcoin=initial_fcoin,
            price_feed=_price_feed,
        )
        log.info("Exchange initialised  usdc=%.2f  fcoin=%.4f  price=%.4f",
                 initial_usdc, initial_fcoin, initial_price)
    return _exchange


def get_exchange() -> Exchange:
    if _exchange is None:
        return init_exchange()
    return _exchange


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
    fn=lambda: {"price": get_exchange().get_market_price()},
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
    fn=lambda depth=20: get_exchange().get_orderbook(depth=depth),
)

_make_tool(
    name="get_trades",
    desc="Get recent trade history.",
    schema={
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "default": 50},
        },
        "additionalProperties": False,
    },
    fn=lambda limit=50: {"trades": get_exchange().get_trades(limit=limit)},
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
    fn=lambda asset="usdc": get_exchange().get_balance(asset),
)

_make_tool(
    name="get_position",
    desc="Get current fcoin position with current price and unrealised PnL.",
    schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    fn=lambda: get_exchange().get_position(),
)

_make_tool(
    name="get_open_orders",
    desc="List all open (unfilled) orders.",
    schema={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    fn=lambda: {"orders": get_exchange().get_orders(status="open")},
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
    fn=lambda quantity: get_exchange().place_order(
        side="buy", quantity=quantity, order_type="market"
    ),
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
    fn=lambda quantity: get_exchange().place_order(
        side="sell", quantity=quantity, order_type="market"
    ),
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
    fn=lambda quantity, price: get_exchange().place_order(
        side="buy", quantity=quantity, price=price, order_type="limit"
    ),
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
    fn=lambda quantity, price: get_exchange().place_order(
        side="sell", quantity=quantity, price=price, order_type="limit"
    ),
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
    fn=lambda order_id: get_exchange().cancel_order(order_id),
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
    fn=lambda price: (_price_feed.set_price(price) if _price_feed else None) or {"price": price},
)
