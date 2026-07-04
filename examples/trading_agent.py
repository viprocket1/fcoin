"""fcoin MCP trading agent — wires the exchange into the MCP server."""
import sys
sys.path.insert(0, "..")

from src import Session, init_exchange, TOOLS
from src.server import MCPServer


def main():
    # Initialise the simulated exchange
    ex = init_exchange(
        initial_usdc=10_000.0,
        initial_fcoin=0.0,
        initial_price=100.0,
        volatility=0.002,
    )

    # Build an agent session
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

    # Register all trading tools
    for tool in TOOLS:
        session.register_tool(tool)

    # Attach to MCP server
    server = MCPServer(session=session, name="fcoin-trading-agent")
    print("fcoin MCP trading agent ready.", file=sys.stderr)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
