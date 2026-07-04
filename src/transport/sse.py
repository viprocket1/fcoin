"""
SSE/HTTP transport for MCP — connect non-local clients over HTTP.

Run the agent as:
    python -m src --transport sse --port 8080

Client connects via HTTP POST /messages and receives events via SSE /events.
Requires: mcp (included in core dependencies)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..server import MCPServer

from ..stream import market_stream
from starlette.responses import StreamingResponse

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
        "address": portfolio["address"],
        "usdc": portfolio["usdc"],
        "fcoin": portfolio["fcoin"],
        "position": portfolio["position"],
    })


async def _wallet(request: Request) -> JSONResponse:
    """GET /wallet — Ethereum wallet address for this agent."""
    agent_id = request.headers.get("X-Agent-ID", "default")
    wallet = get_exchange().get_or_create_agent(agent_id)
    return JSONResponse({
        "agent_id": agent_id,
        "address": wallet.address,
        "private_key": wallet.private_key_hex,
    })


async def _agents(request: Request) -> JSONResponse:
    """GET /agents — list all agents and their USDC/fcoin balances."""
    ex = get_exchange()
    agents = []
    for aid in ex.list_agents():
        portfolio = ex.get_portfolio(aid)
        agents.append({
            "agent_id": aid,
            "address":  portfolio["address"],
            "usdc":     portfolio["usdc"]["total"],
            "fcoin":    portfolio["fcoin"]["total"],
        })
    return JSONResponse({"agents": agents})


async def _prompt(request: Request) -> JSONResponse:
    """GET /prompt — returns instructions for this agent. Paste the 'url' into any AI and it reads the 'instructions'."""
    agent_id = request.headers.get("X-Agent-ID", "default")
    base = str(request.base_url).rstrip("/")
    instructions = f"""You are a fcoin trading agent.
Base URL: {base}
Agent ID: {agent_id}

INSTRUCTIONS:
1. Get your Ethereum wallet:
   GET {base}/wallet
   X-Agent-ID: {agent_id}

2. Check your portfolio:
   GET {base}/portfolio
   X-Agent-ID: {agent_id}

3. Get current market price:
   GET {base}/ticker

4. Buy fcoin (market order):
   POST {base}/trade
   X-Agent-ID: {agent_id}
   Content-Type: application/json
   {{"action": "buy", "amount": 100}}

5. Sell fcoin (market order):
   POST {base}/trade
   X-Agent-ID: {agent_id}
   {{"action": "sell", "amount": 50}}

6. Limit order (buy at specific price):
   POST {base}/trade
   X-Agent-ID: {agent_id}
   {{"action": "buy", "amount": 100, "price": 105.5}}

Make your own decisions. Start with checking your wallet and portfolio.
"""
    return JSONResponse({
        "agent_id": agent_id,
        "url": f"{base}/prompt?agent_id={agent_id}",
        "instructions": instructions,
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
    try:
        agent_id = request.headers.get("X-Agent-ID", "default")
        try:
            body = await request.json()
        except Exception:
            body = {}
        action = body.get("action", "").lower()
        amount_str = body.get("amount", "0")
        price = body.get("price")

        try:
            amount = float(amount_str)
        except (TypeError, ValueError):
            return JSONResponse({"error": "amount must be a number"}, status_code=400)

        if amount <= 0:
            return JSONResponse({"error": "amount must be > 0"}, status_code=400)
        if action not in ("buy", "sell"):
            return JSONResponse({"error": "action must be 'buy' or 'sell'"}, status_code=400)

        ex = get_exchange()
        result = ex.trade(agent_id, action, amount, price)
        return JSONResponse({"agent_id": agent_id, "status": "ok", **result})
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


async def _create_coin(request: Request) -> JSONResponse:
    """
    POST /create_coin — Create a new agent-issued coin.
    Header: X-Agent-ID: <owner-agent-id>
    Body: {"symbol": "ALICE", "name": "Alice Coin", "total_supply": 10000, "price": 2.5}
    """
    try:
        agent_id = request.headers.get("X-Agent-ID", "default")
        body = await request.json()
        symbol       = body.get("symbol", "")
        name         = body.get("name", symbol)
        total_supply = float(body.get("total_supply", 0))
        decimals     = int(body.get("decimals", 18))
        price        = float(body.get("price", 1.0))

        if not symbol:
            return JSONResponse({"error": "symbol is required"}, status_code=400)
        ex = get_exchange()
        result = ex.create_coin(
            owner=agent_id,
            symbol=symbol,
            name=name,
            total_supply=total_supply,
            decimals=decimals,
            price=price,
        )
        return JSONResponse({"agent_id": agent_id, **result})
    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


async def _trade_coin(request: Request) -> JSONResponse:
    """
    POST /trade_coin — Trade an agent-issued coin.
    Header: X-Agent-ID: <agent-id>
    Body: {"action": "buy"|"sell", "symbol": "ALICE", "quantity": 100}
    """
    try:
        agent_id = request.headers.get("X-Agent-ID", "default")
        body = await request.json()
        action   = body.get("action", "").lower()
        symbol   = body.get("symbol", "")
        quantity = float(body.get("quantity", 0))

        if action not in ("buy", "sell"):
            return JSONResponse({"error": "action must be 'buy' or 'sell'"}, status_code=400)
        if not symbol:
            return JSONResponse({"error": "symbol is required"}, status_code=400)
        if quantity <= 0:
            return JSONResponse({"error": "quantity must be > 0"}, status_code=400)

        ex = get_exchange()
        result = ex.trade_coin(agent_id=agent_id, action=action, symbol=symbol, quantity=quantity)
        return JSONResponse({"agent_id": agent_id, **result})
    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


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

    async def handle_market_stream(request: Request) -> Response:
        """GET /stream — SSE stream of live ticker, orderbook, and trade events."""
        filter_str = request.query_params.get("events", "ticker,orderbook,trade")
        # Subscribe first so we don't miss any events
        sub = await market_stream.subscribe(put_fn=None, event_filter=filter_str)

        async def event_generator():
            yield b"event: connected\ndata: {\"type\":\"connected\",\"events\":[" + \
                ",".join(f'"{e}"' for e in filter_str.split(",")) + b"]}\n\n"
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(sub.queue.get(), timeout=30)
                        name = event.get("type", "message")
                        payload = json.dumps(event).encode()
                        yield f"event: {name}\ndata: ".encode() + payload + b"\n\n"
                    except asyncio.TimeoutError:
                        yield b": ping\n\n"
            finally:
                await market_stream.unsubscribe(sub)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def _trade_handler(request: Request) -> JSONResponse:
        return await _trade(request, server)

    app = Starlette()
    app.add_route("/health", _health, methods=["GET"])
    app.add_route("/ticker", _ticker, methods=["GET"])
    app.add_route("/portfolio", _portfolio, methods=["GET"])
    app.add_route("/wallet", _wallet, methods=["GET"])
    app.add_route("/agents", _agents, methods=["GET"])
    app.add_route("/prompt", _prompt, methods=["GET"])
    app.add_route("/trade", _trade_handler, methods=["POST"])
    app.add_route("/create_coin", _create_coin, methods=["POST"])
    app.add_route("/trade_coin", _trade_coin, methods=["POST"])
    app.add_route("/events", handle_sse, methods=["GET"])
    app.add_route("/stream", handle_market_stream, methods=["GET"])
    app.add_route("/orderbook", lambda r: JSONResponse(get_exchange()._book.to_dict()), methods=["GET"])
    app.mount("/messages/", app=sse_transport.handle_post_message)

    # Capture the async event loop so broadcast() works from background threads
    market_stream.setup()

    config = uvicorn.Config(app, host=host, port=port, log_level="info")
    server_uvicorn = uvicorn.Server(config)
    try:
        await server_uvicorn.serve()
    except Exception:
        import traceback
        traceback.print_exc()
        raise
