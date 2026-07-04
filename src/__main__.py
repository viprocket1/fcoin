"""
Run the fcoin MCP agent directly:
    python -m src

For installed package:
    fcoin-agent
"""
import argparse
import sys

from . import init_exchange, Session, TOOLS
from .server import MCPServer


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="fcoin MCP trading agent")
    parser.add_argument(
        "--transport", choices=["stdio", "sse"], default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--port", type=int, default=8080,
        help="HTTP port for SSE transport",
    )
    parser.add_argument(
        "--initial-price", type=float, default=100.0,
        help="Initial fcoin price in USDC",
    )
    parser.add_argument(
        "--initial-usdc", type=float, default=10_000.0,
        help="Starting USDC balance",
    )
    parser.add_argument(
        "--volatility", type=float, default=0.002,
        help="Price volatility per tick (GBM sigma)",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    ex = init_exchange(
        initial_usdc=args.initial_usdc,
        initial_fcoin=0.0,
        initial_price=args.initial_price,
        volatility=args.volatility,
    )

    session = Session(
        system_prompt=(
            "You are a fcoin trading agent. The synthetic fcoin/USDC market is live. "
            "Use get_ticker, get_orderbook, get_balance, and get_position to gather data. "
            "Use market_buy, market_sell, limit_buy, limit_sell to trade. "
            "Use cancel_order to cancel open orders. "
            "Think carefully about risk before placing orders."
        ),
        max_turns=20,
    )

    for tool in TOOLS:
        session.register_tool(tool)

    server = MCPServer(session=session, name="fcoin-trading-agent")

    print(f"fcoin MCP agent starting  price={args.initial_price}  usdc={args.initial_usdc}", file=sys.stderr)

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="sse")


if __name__ == "__main__":
    main()
