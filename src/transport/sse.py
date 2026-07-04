"""
SSE/HTTP transport for MCP — connect non-local clients over HTTP.

Run the agent as:
    python -m src --transport sse --port 8080

Client connects via HTTP POST /messages and receives events via SSE /events.
Requires: mcp (included in core dependencies)
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..server import MCPServer

log = logging.getLogger("fcoin.mcp.sse")

try:
    from mcp.server.sse import SseServerTransport
    from ..tools import get_exchange
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from starlette.requests import Request
    from starlette.responses import JSONResponse, Response
    import uvicorn
except ImportError:
    SseServerTransport = None
    Starlette = None
    uvicorn = None


async def _health(request: Request) -> JSONResponse:
    """GET /health — DigitalOcean App Platform / Render health check."""
    return JSONResponse({"status": "ok"})


async def _portfolio(request: Request) -> JSONResponse:
    """GET /portfolio — account balances and positions for this agent."""
    agent_id = request.headers.get("X-Agent-ID", "default")
    ex = get_exchange()
    portfolio = ex.get_portfolio(agent_id)
    return JSONResponse({
        "agent_id": agent_id,
        "usdc": portfolio["usdc"],
        "fcoin": portfolio["fcoin"],
        "position": portfolio["position"],
    })


async def _ticker(request: Request) -> JSONResponse:
    """GET /ticker — current market price."""
    ex = get_exchange()
    return JSONResponse(ex.get_ticker())


async def _trade(request: Request, server: "MCPServer") -> JSONResponse:
    """
    POST /trade — Execute a trade for a specific agent.
    Header: X-Agent-ID: <agent-id>  (auto-created if missing)
    Body: {"action": "buy"|"sell", "amount": float, "price"?: float}
    """
    agent_id = request.headers.get("X-Agent-ID", "default")
    body = await request.json()
    action = body.get("action", "").lower()
    amount = float(body.get("amount", 0))
    price = body.get("price")  # None = market order

    if amount <= 0:
        return JSONResponse({"error": "amount must be > 0"}, status_code=400)
    if action not in ("buy", "sell"):
        return JSONResponse({"error": "action must be 'buy' or 'sell'"}, status_code=400)

    ex = get_exchange()
    try:
        result = ex.trade(agent_id, action, amount, price)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    return JSONResponse({"agent_id": agent_id, "status": "ok", **result})


async def run_sse(server: "MCPServer", host: str = "0.0.0.0", port: int = 8080) -> None:
    if SseServerTransport is None:
        raise ImportError(
            "MCP SSE transport not available. "
            "Ensure 'mcp' is installed: pip install fcoin-mcp-agent"
        )

    mcp_server = server._server
    sse_transport = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        """GET /events — SSE connection from the MCP client."""
        async with sse_transport.connect_sse(
            request.scope, request.receive, request._send
        ) as streams:
            await mcp_server.run(
                streams[0], streams[1], mcp_server.create_initialization_options()
            )
        return Response()

    app = Starlette()
    app.add_route("/health", _health, methods=["GET"])
    app.add_route("/ticker", _ticker, methods=["GET"])
    app.add_route("/portfolio", _portfolio, methods=["GET"])
    app.add_route("/trade", lambda r: _trade(r, server), methods=["POST"])
    app.add_route("/events", handle_sse, methods=["GET"])
    app.mount("/messages/", app=sse_transport.handle_post_message)

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server_uvicorn = uvicorn.Server(config)
    await server_uvicorn.serve()
