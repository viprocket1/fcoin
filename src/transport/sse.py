"""
SSE/HTTP transport for MCP — connect non-local clients over HTTP.

Run the agent as:
    python -m fcoin.agent --transport sse --port 8080

Client connects via HTTP POST /messages and receives events via SSE /events.
Install with: pip install fcoin-mcp-agent[sse]
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..server import MCPServer

log = logging.getLogger("fcoin.mcp.sse")

try:
    from sse_starlette import SseServerTransport
    from starlette.applications import Starlette
    from starlette.routing import Route, Mount
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    import uvicorn
except ImportError:
    SseServerTransport = None
    Starlette = None
    uvicorn = None


async def _handle_post(request: Request, server: "MCPServer") -> JSONResponse:
    """Handle POST /messages — JSON-RPC commands from the client."""
    body = await request.json()
    log.debug("Received: %s", body)
    # Let the MCP server process the JSON-RPC request
    # (stdio_server is replaced by the HTTP streams in SSE mode)
    return JSONResponse({"jsonrpc": "2.0", "id": body.get("id"), "result": {}})


async def run_sse(server: "MCPServer", host: str = "0.0.0.0", port: int = 8080) -> None:
    if SseServerTransport is None:
        raise ImportError(
            "SSE transport not installed. Run: pip install fcoin-mcp-agent[sse]"
        )

    mcp = server._server
    transport = SseServerTransport("/messages")

    async def handle_sse(scope, receive, send):
        await transport.handle_websocket(scope, receive, send)

    async def _health(request: Request) -> JSONResponse:
        """GET /health — DigitalOcean App Platform health check."""
        return JSONResponse({"status": "ok"})


    app = Starlette(
        routing=[
            Route("/health", _health, methods=["GET"]),
            Route("/messages", _handle_post, methods=["POST"]),
            Mount("/events", Route(handle_sse)),
        ]
    )

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server_uvicorn = uvicorn.Server(config)
    await server_uvicorn.serve()
