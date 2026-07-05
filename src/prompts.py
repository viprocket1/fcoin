"""
Prompt marketplace — users submit prompts, agents run them with their own LLM
keys, and successful responses earn the agent USDC fees.

Flow:
    1. Submitter POSTs /submit_prompt with prompt + fee_per_response
    2. Server broadcasts prompt_request events on /stream
    3. Connected agents run the prompt with their own LLM key
    4. Agent POSTs /respond_prompt with the LLM's response
    5. Server validates, debits submitter, credits agent
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional   # noqa

log = logging.getLogger("fcoin.prompts")


# -----------------------------------------------------------------------------
# Prompt request / response
# -----------------------------------------------------------------------------
@dataclass
class PromptRequest:
    id:             str
    submitter:      str                  # agent_id who paid
    prompt:         str
    fee_usdc:       float                # paid per valid response
    max_responses:  int = 1              # how many agents to accept
    model_hint:     str = ""             # e.g. "claude-sonnet-4" — advisory
    created_at:     float = field(default_factory=time.time)
    responses:      list[dict] = field(default_factory=list)   # [{agent_id, response, ts}]
    status:         str = "open"         # open | fulfilled | expired | cancelled
    paid_out_usdc:  float = 0.0


@dataclass
class PromptResponse:
    id:             str
    request_id:     str
    agent_id:       str
    response:       str
    created_at:     float = field(default_factory=time.time)


# -----------------------------------------------------------------------------
# PromptMarket — global singleton
# -----------------------------------------------------------------------------
class PromptMarket:
    """
    Thread-safe prompt queue and settlement ledger.
    All methods are sync — safe to call from any thread.
    """

    def __init__(self):
        self._requests: dict[str, PromptRequest] = {}
        self._responses: dict[str, PromptResponse] = {}   # response_id -> response
        self._lock = threading.Lock()
        self._min_response_len = 1          # accept any non-empty response (let the market judge)
        self._max_response_len = 8000
        self._min_fee_usdc    = 0.001       # reject zero-fee spam

    # ------------------------------------------------------------------ submitter side

    def submit_prompt(
        self,
        submitter:      str,
        prompt:         str,
        fee_usdc:       float,
        max_responses:  int = 1,
        model_hint:     str = "",
    ) -> dict:
        """
        Submit a prompt to the marketplace.
        Locks `fee_usdc * max_responses` USDC from the submitter's wallet immediately.
        Returns the created request.
        """
        if not prompt or not prompt.strip():
            raise ValueError("prompt cannot be empty")
        if fee_usdc < self._min_fee_usdc:
            raise ValueError(f"fee_usdc must be >= {self._min_fee_usdc}")
        if max_responses < 1:
            raise ValueError("max_responses must be >= 1")

        from .exchange import get_exchange, Balance
        ex = get_exchange()
        wallet = ex.get_or_create_agent(submitter)
        cost = fee_usdc * max_responses
        b = wallet._balances.get("usdc")
        if b is None or b.available < cost:
            raise ValueError(
                f"Insufficient USDC: need {cost:.4f}, have "
                f"{b.available if b else 0:.4f}"
            )
        # Lock the funds immediately
        b.available -= cost
        wallet.sync_to_store(ex._store)

        req_id = f"pr_{uuid.uuid4().hex[:10]}"
        req = PromptRequest(
            id=req_id,
            submitter=submitter,
            prompt=prompt.strip(),
            fee_usdc=fee_usdc,
            max_responses=max_responses,
            model_hint=model_hint,
        )
        with self._lock:
            self._requests[req_id] = req

        log.info(
            f"[prompts] submit  id={req_id}  submitter={submitter}  "
            f"fee={fee_usdc:.4f}  max={max_responses}  cost={cost:.4f}"
        )

        # Broadcast to all connected agents via the SSE stream
        self._broadcast_request(req)
        return self._request_view(req)

    # ------------------------------------------------------------------ agent side

    def submit_response(
        self,
        agent_id:       str,
        request_id:     str,
        response:       str,
    ) -> dict:
        """
        Agent submits an LLM-generated response to a prompt.
        Credits `fee_usdc` to the agent if the response is accepted.
        Returns the response record.
        """
        if not response or len(response.strip()) < self._min_response_len:
            raise ValueError(f"response too short (min {self._min_response_len} chars)")
        if len(response) > self._max_response_len:
            raise ValueError(f"response too long (max {self._max_response_len} chars)")

        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise ValueError(f"unknown request_id: {request_id}")
            if req.status != "open":
                raise ValueError(f"request is {req.status}")
            if len(req.responses) >= req.max_responses:
                raise ValueError("request already fulfilled")
            # Prevent same agent answering twice
            if any(r["agent_id"] == agent_id for r in req.responses):
                raise ValueError("agent already responded to this prompt")

        # Credit the agent
        from .exchange import get_exchange, Balance
        ex = get_exchange()
        wallet = ex.get_or_create_agent(agent_id)
        b = wallet._balances.get("usdc")
        if b is None:
            b = Balance()
            wallet._balances["usdc"] = b
        b.available += req.fee_usdc
        wallet.sync_to_store(ex._store)

        resp_id = f"rsp_{uuid.uuid4().hex[:10]}"
        resp = PromptResponse(
            id=resp_id,
            request_id=request_id,
            agent_id=agent_id,
            response=response.strip(),
        )
        with self._lock:
            self._responses[resp_id] = resp
            req.responses.append({
                "response_id": resp_id,
                "agent_id":    agent_id,
                "response":    resp.response,
                "ts":          resp.created_at,
            })
            req.paid_out_usdc += req.fee_usdc
            if len(req.responses) >= req.max_responses:
                req.status = "fulfilled"
                # Refund any unfilled portion
                refund = (req.max_responses - len(req.responses)) * req.fee_usdc
                if refund > 0:
                    submitter_wallet = ex.get_or_create_agent(req.submitter)
                    sb = submitter_wallet._balances.get("usdc")
                    if sb is None:
                        sb = type(sb)()
                        submitter_wallet._balances["usdc"] = sb
                    sb.available += refund
                    submitter_wallet.sync_to_store(ex._store)
                    log.info(f"[prompts] refund  id={request_id}  amount={refund:.4f}")

        log.info(
            f"[prompts] response  request={request_id}  agent={agent_id}  "
            f"earned={req.fee_usdc:.4f}  status={req.status}"
        )

        return {
            "response_id":   resp_id,
            "request_id":    request_id,
            "agent_id":      agent_id,
            "earned_usdc":   req.fee_usdc,
            "request_status": req.status,
        }

    # ------------------------------------------------------------------ reads

    def get_request(self, request_id: str) -> dict | None:
        with self._lock:
            req = self._requests.get(request_id)
            return self._request_view(req) if req else None

    def list_open_requests(self) -> list[dict]:
        with self._lock:
            return [
                self._request_view(r)
                for r in self._requests.values()
                if r.status == "open"
            ]

    def list_all_requests(self, limit: int = 50) -> list[dict]:
        with self._lock:
            items = sorted(
                self._requests.values(),
                key=lambda r: r.created_at,
                reverse=True,
            )
            return [self._request_view(r) for r in items[:limit]]

    def get_response(self, response_id: str) -> Optional[dict]:
        with self._lock:
            r = self._responses.get(response_id)
            if r is None:
                return None
            return {
                "id":         r.id,
                "request_id": r.request_id,
                "agent_id":   r.agent_id,
                "response":   r.response,
                "created_at": r.created_at,
            }

    def cancel_request(self, request_id: str, by_agent: str) -> dict:
        """Cancel an open request — refund any unspent fees."""
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise ValueError("unknown request_id")
            if req.submitter != by_agent:
                raise ValueError("only the submitter can cancel")
            if req.status != "open":
                raise ValueError(f"request is {req.status}")

            remaining = (req.max_responses - len(req.responses)) * req.fee_usdc
            req.status = "cancelled"

        if remaining > 0:
            from .exchange import get_exchange, Balance
            ex = get_exchange()
            wallet = ex.get_or_create_agent(by_agent)
            b = wallet._balances.get("usdc")
            if b is None:
                b = Balance()
                wallet._balances["usdc"] = b
            b.available += remaining
            wallet.sync_to_store(ex._store)
        return self.get_request(request_id)

    # ------------------------------------------------------------------ internals

    def _request_view(self, req: PromptRequest) -> dict:
        return {
            "id":             req.id,
            "submitter":      req.submitter,
            "prompt":         req.prompt,
            "fee_usdc":       req.fee_usdc,
            "max_responses":  req.max_responses,
            "model_hint":     req.model_hint,
            "status":         req.status,
            "responses":      req.responses,
            "paid_out_usdc":  req.paid_out_usdc,
            "created_at":     req.created_at,
        }

    def _broadcast_request(self, req: PromptRequest) -> None:
        """Push the request to all SSE clients as a `prompt_request` event."""
        from .stream import market_stream
        payload = {
            "type": "prompt_request",
            "data": {
                "request_id":    req.id,
                "prompt":        req.prompt,
                "fee_usdc":      req.fee_usdc,
                "max_responses": req.max_responses,
                "model_hint":    req.model_hint,
            },
        }
        market_stream.broadcast(payload)


# Global singleton
prompt_market = PromptMarket()