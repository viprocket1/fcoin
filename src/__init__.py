"""fcoin MCP agent scaffold — universal, model-agnostic."""

# Core agent (no circular deps here)
from .agent import Session, ToolDef, ResourceDef, LLMProvider, Turn

# Exchange
from .exchange import Exchange, PriceFeed

# Trading tools
from .tools.trading import TOOLS, init_exchange, get_exchange

__all__ = [
    # Agent core
    "Session", "ToolDef", "ResourceDef", "LLMProvider", "Turn",
    # Exchange
    "Exchange", "PriceFeed",
    # Trading tools
    "TOOLS", "init_exchange", "get_exchange",
]
