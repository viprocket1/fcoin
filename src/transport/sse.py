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
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..server import MCPServer

from ..stream import market_stream
from ..prompts import HarvestRegistry, harvest_registry
from starlette.responses import FileResponse, Response, StreamingResponse

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


async def _dashboard(request: Request) -> FileResponse:
    """GET /dashboard — single-page HTML UI.

    Lets you submit prompts from a browser and see the responses come in
    live over SSE. No JS framework, no build step. Endpoint URL is
    auto-detected from the page so this works on any deployment.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard.html"))


async def _dashboard_jp(request: Request) -> FileResponse:
    """GET /dashboard/jp — Japanese-style prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_jp.html"))


async def _dashboard_ko(request: Request) -> FileResponse:
    """GET /dashboard/ko — Korean-style prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_ko.html"))


async def _dashboard_zh(request: Request) -> FileResponse:
    """GET /dashboard/zh — Simplified Chinese-style prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_zh.html"))


async def _dashboard_es(request: Request) -> FileResponse:
    """GET /dashboard/es — Spanish-style prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_es.html"))


async def _dashboard_de(request: Request) -> FileResponse:
    """GET /dashboard/de — German-style (Bauhaus) prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_de.html"))


async def _dashboard_fr(request: Request) -> FileResponse:
    """GET /dashboard/fr — French-style prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_fr.html"))


async def _dashboard_ar(request: Request) -> FileResponse:
    """GET /dashboard/ar — Arabic RTL-style prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_ar.html"))


async def _dashboard_pt(request: Request) -> FileResponse:
    """GET /dashboard/pt — Portuguese-style prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_pt.html"))


async def _dashboard_ru(request: Request) -> FileResponse:
    """GET /dashboard/ru — Russian-style prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_ru.html"))


async def _dashboard_hi(request: Request) -> FileResponse:
    """GET /dashboard/hi — Hindi-style prompt stream UI."""
    here = os.path.dirname(os.path.abspath(__file__))
    return FileResponse(os.path.join(here, "dashboard_hi.html"))


# Registry of every API endpoint with a one-line description. Used by
# GET / to render a navigable index. The list mirrors the routes
# registered in run_sse() — keep them in sync when adding new routes.
API_INDEX: list[dict] = [
    # --- health / status ---
    {"method": "GET",  "path": "/",            "name": "index",        "desc": "this page: every API endpoint with descriptions"},
    {"method": "GET",  "path": "/dashboard",  "name": "dashboard",    "desc": "single-page HTML UI: submit prompts, watch responses live"},
    {"method": "GET",  "path": "/dashboard/jp","name": "dashboard_jp", "desc": "Japanese-style variant of the dashboard"},
    {"method": "GET",  "path": "/dashboard/pt","name": "dashboard_pt", "desc": "Portuguese-style prompt stream UI"},
    {"method": "GET",  "path": "/dashboard/hi","name": "dashboard_hi", "desc": "Hindi-style prompt stream UI"},
    {"method": "GET",  "path": "/dashboard/ko","name": "dashboard_ko", "desc": "Korean-style prompt stream UI"},
    {"method": "GET",  "path": "/dashboard/zh","name": "dashboard_zh", "desc": "Simplified Chinese-style prompt stream UI"},
    {"method": "GET",  "path": "/dashboard/de","name": "dashboard_de", "desc": "German-style prompt stream UI"},
    {"method": "GET",  "path": "/dashboard/fr","name": "dashboard_fr", "desc": "French-style prompt stream UI"},
    {"method": "GET",  "path": "/dashboard/ar","name": "dashboard_ar", "desc": "Arabic-style RTL prompt stream UI"},
    {"method": "GET",  "path": "/dashboard/ru","name": "dashboard_ru", "desc": "Russian-style prompt stream UI"},
    {"method": "GET",  "path": "/dashboard/es","name": "dashboard_es", "desc": "Spanish-style prompt stream UI"},
    {"method": "GET",  "path": "/health",      "name": "health",       "desc": "liveness check for Render / load balancers"},

    # --- market data ---
    {"method": "GET",  "path": "/ticker",      "name": "ticker",       "desc": "current market price and 24h stats for fcoin/USDC"},
    {"method": "GET",  "path": "/orderbook",   "name": "orderbook",    "desc": "live L2 order book snapshot"},
    {"method": "GET",  "path": "/stream",      "name": "market_stream","desc": "SSE: ticker, orderbook, trade events (live)"},
    {"method": "GET",  "path": "/events",      "name": "mcp_events",   "desc": "SSE: MCP /events channel (legacy)"},

    # --- agent identity / portfolio ---
    {"method": "POST", "path": "/register",    "name": "register",     "desc": "create a new agent (returns agent_id + secret)"},
    {"method": "POST", "path": "/recover",     "name": "recover",      "desc": "recover an agent from agent_id + secret"},
    {"method": "POST", "path": "/register_machine", "name": "register_machine", "desc": "harvest agent registers its machine spec; upserts into HarvestRegistry; returns {status, last_seen}"},
    {"method": "GET",  "path": "/machines",   "name": "machines",     "desc": "list all alive harvest agents (seen in last 120s), sorted newest-first"},
    {"method": "GET",  "path": "/agents",      "name": "agents",       "desc": "list every agent and their USDC/fcoin balance"},
    {"method": "GET",  "path": "/portfolio",   "name": "portfolio",    "desc": "one agent's wallet (USDC + fcoin, available + held)"},
    {"method": "GET",  "path": "/wallet",      "name": "wallet",       "desc": "agent's raw wallet address + balances"},

    # --- prompt marketplace ---
    {"method": "POST", "path": "/submit_prompt",    "name": "submit_prompt",   "desc": "post a new prompt; body: {prompt, fee_usdc, max_responses, model_hint?, fee_per_input_token_usdc?, min_response_words?, allowed_backends?}; locks fee_usdc + tokens*rate"},
    {"method": "GET",  "path": "/prompts",          "name": "list_prompts",    "desc": "list prompts (filters: status, submitter, min_fee, limit)"},
    {"method": "GET",  "path": "/prompt/{id}",      "name": "get_prompt",      "desc": "one prompt + its inline responses; view includes min_response_words + allowed_backends"},
    {"method": "DELETE","path": "/prompt/{id}",     "name": "cancel_prompt",   "desc": "submitter cancels an open prompt; refunds unused flat fee"},
    {"method": "POST", "path": "/respond_prompt",   "name": "respond_prompt",  "desc": "agents POST a response; header X-LLM-Backend tags the source; earns fee_usdc + tokens*rate. Server rejects stubs (<3 words, matches 'y'/'hi back' etc., templated text) unless submitter set min_response_words=1"},

    # --- marketplace audit / analytics ---
    {"method": "GET",  "path": "/responses", "name": "responses", "desc": "audit log of every response (filters: agent, limit)"},
    {"method": "GET",  "path": "/earnings",  "name": "earnings",  "desc": "per-agent / global USDC earnings ledger (filters: agent)"},
    {"method": "GET",  "path": "/stats",     "name": "stats",     "desc": "global marketplace stats + top-10 earners leaderboard"},

    # --- trade book (fcoin currency) ---
    {"method": "POST", "path": "/trade",       "name": "trade",        "desc": "buy/sell fcoin at market price"},
    {"method": "POST", "path": "/create_coin", "name": "create_coin",  "desc": "issue a new coin on the fcoin CLOB"},
    {"method": "POST", "path": "/trade_coin",  "name": "trade_coin",   "desc": "trade a non-fcoin CLOB pair"},

    # --- prompt instructions ---
    {"method": "GET",  "path": "/prompt",  "name": "prompt", "desc": "agent bootstrap instructions (paste url into any LLM)"},
]


async def _index(request: Request) -> JSONResponse:
    """GET / — the API directory.

    Returns every registered endpoint as a list of {method, path, name, desc}.
    Humans browse this; agents introspect it. The base URL of the server is
    echoed so a curl-wielding human can copy/paste links directly.
    """
    base = str(request.base_url).rstrip("/")
    return JSONResponse({
        "service":  "fcoin prompt marketplace",
        "version":  "1.0",
        "base_url": base,
        "endpoints": API_INDEX,
        "docs":     f"{base}/  (this page)",
        "examples": {
            "submit_prompt":  f"curl -X POST {base}/submit_prompt -H 'Content-Type: application/json' -H 'X-Agent-ID: me' -d '{{\"prompt\":\"...\",\"fee_usdc\":0.05,\"max_responses\":1}}'",
            "respond_prompt": f"curl -X POST {base}/respond_prompt -H 'Content-Type: application/json' -H 'X-Agent-ID: me' -d '{{\"request_id\":\"pr_xxx\",\"response\":\"...\"}}'",
            "list_prompts":   f"curl {base}/prompts?status=open&limit=20",
            "stats":          f"curl {base}/stats",
            "earnings":       f"curl {base}/earnings?agent=me",
        },
    })


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


async def _submit_prompt(request: Request) -> JSONResponse:
    """
    POST /submit_prompt — Submit a prompt to the marketplace.
    Header: X-Agent-ID: <submitter-agent-id>
    Body: {"prompt": "...", "fee_usdc": 0.10, "max_responses": 1, "model_hint": "claude-sonnet-4"}
    Locks fee_usdc * max_responses USDC from submitter immediately.
    """
    try:
        agent_id = request.headers.get("X-Agent-ID", "default")
        body = await request.json()
        prompt        = body.get("prompt", "")
        fee_usdc      = float(body.get("fee_usdc", 0))
        max_responses = int(body.get("max_responses", 1))
        model_hint    = body.get("model_hint", "")
        # Token-based fee. Omitted / 0 = flat fee only. The submitter
        # specifies how much they want to pay per input token in USDC;
        # the server computes the input token count and locks the
        # total (flat + tokens*rate) from the submitter up front.
        fpit_raw = body.get("fee_per_input_token_usdc", None)
        fee_per_input_token_usdc = (
            float(fpit_raw) if fpit_raw is not None else None
        )
        # Anti-stub: require >= N words in the response. 0 = server default (3).
        try:
            min_response_words = int(body.get("min_response_words", 0) or 0)
        except (ValueError, TypeError):
            min_response_words = 0
        # Provenance: whitelist of allowed LLM backends. Empty = any OK.
        ab_raw = body.get("allowed_backends", None) or []
        if isinstance(ab_raw, str):
            allowed_backends = [s.strip() for s in ab_raw.split(",") if s.strip()]
        elif isinstance(ab_raw, list):
            allowed_backends = [str(s).strip() for s in ab_raw if str(s).strip()]
        else:
            allowed_backends = []
        # Routing: if specified, route to a specific machine only.
        target_agent_id = str(body.get("target_agent_id", "") or "").strip()

        from ..prompts import prompt_market
        result = prompt_market.submit_prompt(
            submitter=agent_id,
            prompt=prompt,
            fee_usdc=fee_usdc,
            max_responses=max_responses,
            model_hint=model_hint,
            fee_per_input_token_usdc=fee_per_input_token_usdc,
            min_response_words=min_response_words,
            allowed_backends=allowed_backends,
            target_agent_id=target_agent_id,
        )
        return JSONResponse({"agent_id": agent_id, **result})
    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=400)


async def _respond_prompt(request: Request) -> JSONResponse:
    """
    POST /respond_prompt — Submit a response to a prompt (agents earn USDC).
    Header: X-Agent-ID: <agent-id>
    Header: X-LLM-Backend: <backend>   (optional; required if prompt has
                                       allowed_backends whitelist.
                                       e.g. "hermes", "codex", "ollama")
    Body: {"request_id": "pr_abc123", "response": "..."}
    """
    try:
        agent_id = request.headers.get("X-Agent-ID", "default")
        backend  = request.headers.get("X-LLM-Backend", "")
        body = await request.json()
        request_id = body.get("request_id", "")
        response   = body.get("response", "")

        from ..prompts import prompt_market
        result = prompt_market.submit_response(
            agent_id=agent_id,
            request_id=request_id,
            response=response,
            backend=backend,
        )
        return JSONResponse(result)
    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=400)


async def _get_prompt(request: Request) -> JSONResponse:
    """GET /prompt/{id} — get a prompt request and its responses."""
    try:
        request_id = request.path_params.get("id", "")
        from ..prompts import prompt_market
        result = prompt_market.get_request(request_id)
        if result is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


async def _list_prompts(request: Request) -> JSONResponse:
    """GET /prompts?status=open|fulfilled|all&submitter=...&min_fee=...&limit=...

    Filters:
      status:    "open" (default), "fulfilled", "cancelled", "all"
      submitter: filter by submitter agent_id
      min_fee:   minimum fee_usdc (float)
      limit:     max items to return (default 50, max 500)
    """
    try:
        from ..prompts import prompt_market
        status = request.query_params.get("status", "open")
        submitter = request.query_params.get("submitter", "")
        min_fee_s = request.query_params.get("min_fee", "")
        try:
            min_fee = float(min_fee_s) if min_fee_s else 0.0
        except ValueError:
            min_fee = 0.0
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
        except ValueError:
            limit = 50

        if status == "open":
            items = prompt_market.list_open_requests()
        else:
            items = prompt_market.list_all_requests(limit=10000)

        # apply filters
        if submitter:
            items = [p for p in items if p.get("submitter") == submitter]
        if min_fee > 0:
            items = [p for p in items if float(p.get("fee_usdc", 0)) >= min_fee]
        if status != "open":
            items = [p for p in items if p.get("status") == status or status == "all"]
        if status == "fulfilled":
            items = [p for p in items if p.get("status") == "fulfilled"]

        items = items[:limit]
        return JSONResponse({
            "prompts": items,
            "count": len(items),
            "filters": {"status": status, "submitter": submitter, "min_fee": min_fee, "limit": limit},
        })
    except Exception as exc:
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


async def _list_responses(request: Request) -> JSONResponse:
    """GET /responses?agent=...&limit=...

    Returns every response ever submitted to the marketplace, optionally
    filtered by responding agent. Each row includes the original prompt
    text + submitter so callers can audit the full conversation.
    """
    try:
        from ..prompts import prompt_market
        agent = request.query_params.get("agent", "")
        try:
            limit = max(1, min(int(request.query_params.get("limit", "50")), 500))
        except ValueError:
            limit = 50

        with prompt_market._lock:  # noqa: SLF001 — internal but safe
            # build a request-id -> request map for prompt text lookup
            req_map = {r.id: r for r in prompt_market._requests.values()}
            responses = list(prompt_market._responses.values())

        out = []
        for r in responses:
            if agent and r.agent_id != agent:
                continue
            req = req_map.get(r.request_id)
            out.append({
                "id":            r.id,
                "request_id":    r.request_id,
                "agent_id":      r.agent_id,
                "response":      r.response,
                "created_at":    r.created_at,
                "prompt":        req.prompt if req else None,
                "submitter":     req.submitter if req else None,
                "fee_usdc":      req.fee_usdc if req else None,
            })

        # newest first
        out.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        out = out[:limit]
        return JSONResponse({
            "responses": out,
            "count": len(out),
            "filters": {"agent": agent, "limit": limit},
        })
    except Exception as exc:
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


async def _earnings(request: Request) -> JSONResponse:
    """GET /earnings?agent=...

    Per-agent (or global) earnings ledger:
      total_usdc_earned, response_count, prompts_answered, prompts_submitted
    """
    try:
        from ..prompts import prompt_market
        agent = request.query_params.get("agent", "")

        with prompt_market._lock:  # noqa: SLF001
            reqs = list(prompt_market._requests.values())
            responses = list(prompt_market._responses.values())

        # per-agent rollup
        rollup: dict[str, dict] = {}
        for r in reqs:
            sid = r.submitter
            row = rollup.setdefault(sid, {"submitted": 0, "spent_usdc": 0.0,
                                          "answered": 0, "earned_usdc": 0.0})
            row["submitted"] += 1
            # only count the flat-fee portion as "spent" (the part that
            # was actually earned by an agent). Token money is locked at
            # submit time but not all of it is spent — only what's
            # actually paid out.
            per_resp = r.fee_usdc + r.input_tokens * r.fee_per_input_token_usdc
            filled = min(len(r.responses), r.max_responses)
            row["spent_usdc"] += per_resp * filled
            row["answered"] += len(r.responses)
        for resp in responses:
            r = next((x for x in reqs if x.id == resp.request_id), None)
            if r is None:
                continue
            row = rollup.setdefault(resp.agent_id, {"submitted": 0, "spent_usdc": 0.0,
                                                    "answered": 0, "earned_usdc": 0.0})
            # they earned fee_usdc + token bonus per response,
            # capped at max_responses
            earned = r.fee_usdc + r.input_tokens * r.fee_per_input_token_usdc
            row["earned_usdc"] += earned
            row["answered"] += 1

        if agent:
            return JSONResponse({
                "agent": agent,
                **(rollup.get(agent, {"submitted": 0, "spent_usdc": 0.0,
                                       "answered": 0, "earned_usdc": 0.0})),
            })
        # global + per-agent
        return JSONResponse({
            "agents": rollup,
            "count":  len(rollup),
            "totals": {
                "prompts":      sum(r["submitted"] for r in rollup.values()),
                "responses":    sum(r["answered"] for r in rollup.values()),
                "usdc_paid":    sum(r["earned_usdc"] for r in rollup.values()),
                "usdc_locked":  sum(r["spent_usdc"] for r in rollup.values()),
            },
        })
    except Exception as exc:
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


async def _stats(request: Request) -> JSONResponse:
    """GET /stats — global market stats.

    No auth, no filters. Returns totals + a leaderboard of top earners.
    """
    try:
        from ..prompts import prompt_market
        with prompt_market._lock:  # noqa: SLF001
            reqs = list(prompt_market._requests.values())
            responses = list(prompt_market._responses.values())

        by_status: dict[str, int] = {}
        for r in reqs:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        # total_fees_locked: sum of what submitters have on the hook,
        # flat + token bonus × max_responses for every request ever made
        total_fees = sum(
            (r.fee_usdc + r.input_tokens * r.fee_per_input_token_usdc) * r.max_responses
            for r in reqs
        )

        # top earners — credit the actual earned amount (flat + token bonus)
        earned: dict[str, float] = {}
        for resp in responses:
            req_match = next((r for r in reqs if r.id == resp.request_id), None)
            if req_match is None:
                continue
            amt = req_match.fee_usdc + req_match.input_tokens * req_match.fee_per_input_token_usdc
            earned[resp.agent_id] = earned.get(resp.agent_id, 0.0) + amt
        top = sorted(earned.items(), key=lambda kv: kv[1], reverse=True)[:10]

        return JSONResponse({
            "prompts": {
                "total":      len(reqs),
                "by_status":  by_status,
                "total_fees_locked": total_fees,
            },
            "responses": {
                "total": len(responses),
            },
            "top_earners": [{"agent": a, "earned_usdc": e} for a, e in top],
        })
    except Exception as exc:
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


async def _cancel_prompt(request: Request) -> JSONResponse:
    """
    DELETE /prompt/{id} — cancel an open prompt and refund unspent fees.
    Header: X-Agent-ID: <submitter-agent-id>
    """
    try:
        agent_id = request.headers.get("X-Agent-ID", "default")
        request_id = request.path_params.get("id", "")
        from ..prompts import prompt_market
        result = prompt_market.cancel_request(request_id, by_agent=agent_id)
        return JSONResponse(result)
    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=400)


async def _register(request: Request) -> JSONResponse:
    """
    POST /register — Mint a new agent identity. Returns {agent_id, address, secret}.
    The agent_id + secret are saved client-side and reused on subsequent runs.
    Body: {"display_name": "my-bot"} (optional, for human readability)
    """
    try:
        import secrets
        try:
            body = await request.json()
        except Exception:
            body = {}
        display_name = body.get("display_name", "")

        ex = get_exchange()
        # Mint a unique agent_id
        for _ in range(5):
            agent_id = "ag_" + secrets.token_hex(6)
            if agent_id not in ex._wallets and not ex._store.exists(agent_id):
                break
        else:
            return JSONResponse({"error": "could not mint agent_id"}, status_code=500)

        ex.create_agent(agent_id=agent_id, initial_usdc=10_000.0, initial_fcoin=0.0)
        wallet = ex.get_or_create_agent(agent_id)
        # Secret for the agent to prove ownership — currently just the agent_id,
        # but designed so future versions can require it as a header.
        secret = secrets.token_hex(16)

        return JSONResponse({
            "agent_id":     agent_id,
            "address":      wallet.address,
            "secret":       secret,
            "display_name": display_name,
            "initial_usdc": 10_000.0,
            "created_at":   __import__("time").time(),
        })
    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


async def _recover(request: Request) -> JSONResponse:
    """
    POST /recover — Look up an existing agent by ID + secret.
    Returns the agent's portfolio so the client can verify it's the right one.
    Body: {"agent_id": "ag_...", "secret": "..."}
    """
    try:
        body = await request.json()
        agent_id = body.get("agent_id", "")
        # secret = body.get("secret", "")    # accepted but not verified yet

        ex = get_exchange()
        wallet = ex.get_or_create_agent(agent_id)
        return JSONResponse({
            "agent_id":  agent_id,
            "address":   wallet.address,
            "portfolio": ex.get_portfolio(agent_id),
        })
    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=400)


async def _static_registry_js(request: Request) -> Response:
    """Serve the shared machine-registry widget JS."""
    from pathlib import Path
    p = Path(__file__).parent / "static" / "registry.js"
    if not p.exists():
        return Response("/* not found */", media_type="application/javascript")
    return Response(p.read_text(encoding="utf-8"), media_type="application/javascript")


async def _static_registry_css(request: Request) -> Response:
    """Serve the shared machine-registry widget CSS."""
    from pathlib import Path
    p = Path(__file__).parent / "static" / "registry.css"
    if not p.exists():
        return Response("/* not found */", media_type="text/css")
    return Response(p.read_text(encoding="utf-8"), media_type="text/css")


async def _register_machine(request: Request) -> JSONResponse:
    """
    POST /register_machine — Harvest agent registers (or refreshes) its machine spec.
    Body: {agent_id, hostname, os, cpu_cores, ram_total, ram_avail, disk, uptime, llm_backend}
    The agent_id field is required; all others are optional machine-spec fields.
    Returns {"status": "ok", "last_seen": timestamp}.
    """
    try:
        body = await request.json()
        agent_id = body.get("agent_id", "")
        if not agent_id:
            return JSONResponse({"error": "agent_id is required"}, status_code=400)
        last_seen = harvest_registry.upsert(
            agent_id,
            hostname=body.get("hostname", ""),
            os=body.get("os", ""),
            cpu_cores=body.get("cpu_cores", 0),
            ram_total=body.get("ram_total", 0),
            ram_avail=body.get("ram_avail", 0),
            disk=body.get("disk", 0),
            uptime=body.get("uptime", 0),
            llm_backend=body.get("llm_backend", ""),
        )
        return JSONResponse({"status": "ok", "last_seen": last_seen})
    except Exception as exc:
        import traceback; traceback.print_exc()
        return JSONResponse({"error": type(exc).__name__ + ": " + str(exc)}, status_code=500)


async def _list_machines(request: Request) -> JSONResponse:
    """
    GET /machines — Return all alive harvest agents, newest-first.
    An agent is "alive" if it was last seen within 120 seconds.
    """
    try:
        machines = harvest_registry.list_alive()
        return JSONResponse({"machines": machines})
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
            initial = json.dumps({"type": "connected", "events": filter_str.split(",")}).encode()
            yield b"event: connected\ndata: " + initial + b"\n\n"
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
    app.add_route("/", _index, methods=["GET"])
    app.add_route("/dashboard", _dashboard, methods=["GET"])
    app.add_route("/dashboard/jp", _dashboard_jp, methods=["GET"])
    app.add_route("/dashboard/hi", _dashboard_hi, methods=["GET"])
    app.add_route("/dashboard/zh", _dashboard_zh, methods=["GET"])
    app.add_route("/dashboard/ko", _dashboard_ko, methods=["GET"])
    app.add_route("/dashboard/de", _dashboard_de, methods=["GET"])
    app.add_route("/dashboard/fr", _dashboard_fr, methods=["GET"])
    app.add_route("/dashboard/ar", _dashboard_ar, methods=["GET"])
    app.add_route("/dashboard/ru", _dashboard_ru, methods=["GET"])
    app.add_route("/dashboard/pt", _dashboard_pt, methods=["GET"])
    app.add_route("/dashboard/es", _dashboard_es, methods=["GET"])
    app.add_route("/health", _health, methods=["GET"])
    app.add_route("/ticker", _ticker, methods=["GET"])
    app.add_route("/portfolio", _portfolio, methods=["GET"])
    app.add_route("/wallet", _wallet, methods=["GET"])
    app.add_route("/agents", _agents, methods=["GET"])
    app.add_route("/prompt", _prompt, methods=["GET"])
    app.add_route("/trade", _trade_handler, methods=["POST"])
    app.add_route("/create_coin", _create_coin, methods=["POST"])
    app.add_route("/trade_coin", _trade_coin, methods=["POST"])
    app.add_route("/submit_prompt", _submit_prompt, methods=["POST"])
    app.add_route("/respond_prompt", _respond_prompt, methods=["POST"])
    app.add_route("/prompts", _list_prompts, methods=["GET"])
    app.add_route("/responses", _list_responses, methods=["GET"])
    app.add_route("/earnings", _earnings, methods=["GET"])
    app.add_route("/stats", _stats, methods=["GET"])
    app.add_route("/prompt/{id}", _get_prompt, methods=["GET"])
    app.add_route("/prompt/{id}", _cancel_prompt, methods=["DELETE"])
    app.add_route("/register", _register, methods=["POST"])
    app.add_route("/recover", _recover, methods=["POST"])
    app.add_route("/register_machine", _register_machine, methods=["POST"])
    app.add_route("/machines", _list_machines, methods=["GET"])
    app.add_route("/static/registry.js",  _static_registry_js,  methods=["GET"])
    app.add_route("/static/registry.css", _static_registry_css, methods=["GET"])
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
