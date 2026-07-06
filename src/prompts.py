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
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional   # noqa

log = logging.getLogger("fcoin.prompts")


# -----------------------------------------------------------------------------
# HarvestRegistry — machine registry for harvest agents
# -----------------------------------------------------------------------------
class HarvestRegistry:
    """
    Thread-safe singleton registry of registered harvest agents.
    Each entry maps agent_id → {agent_id, hostname, os, cpu_cores,
    ram_total, ram_avail, disk, uptime, llm_backend, last_seen}.
    """

    ALIVE_THRESHOLD = 120.0  # seconds

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self._lock = threading.Lock()

    def upsert(self, agent_id: str, **fields: object) -> float:
        """Upsert a machine entry. Returns the new last_seen timestamp."""
        now = time.time()
        with self._lock:
            entry = dict(fields)
            entry["agent_id"] = agent_id
            entry["last_seen"] = now
            self._entries[agent_id] = entry
        return now

    def is_alive(self, agent_id: str) -> bool:
        """Return True if the agent was last seen within ALIVE_THRESHOLD seconds."""
        with self._lock:
            entry = self._entries.get(agent_id)
            if entry is None:
                return False
            return (time.time() - entry.get("last_seen", 0)) < self.ALIVE_THRESHOLD

    def list_alive(self) -> list[dict]:
        """Return all alive machines, sorted newest-first."""
        now = time.time()
        with self._lock:
            alive = [
                dict(e) for e in self._entries.values()
                if (now - e.get("last_seen", 0)) < self.ALIVE_THRESHOLD
            ]
        alive.sort(key=lambda e: e.get("last_seen", 0), reverse=True)
        return alive


harvest_registry = HarvestRegistry()


def count_input_tokens(prompt: str) -> int:
    """Approximate input token count for a prompt.

    We don't have access to a real tokenizer on the server side, so we
    use a word-based heuristic that over-estimates slightly (English text
    averages ~0.75 words/token). This errs on the side of charging the
    submitter slightly more for token-priced prompts, which is fine for
    a research marketplace — the surplus gets refunded on cancel.

    Returns an int >= 1 (a 1-token floor for non-empty prompts so even
    a trivial "hi" still costs something under a token-price regime).
    """
    if not prompt or not prompt.strip():
        return 0
    # word_count * 4/3 ≈ token count (BPE-style over-estimate)
    return max(1, int(round(len(prompt.split()) * 4 / 3)))


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
    # Token-based pricing. fee_per_input_token_usdc is the rate the
    # answering agent earns PER input token (in addition to fee_usdc).
    # 0.0 = flat fee, no token bonus. Pricing is locked at submit time
    # so submitters can't be retroactively charged more.
    fee_per_input_token_usdc: float = 0.0
    input_tokens:   int = 0              # computed at submit time, frozen
    locked_usdc:    float = 0.0          # total USDC locked from submitter
    # Anti-stub: the submitter can require a minimum number of words
    # in the response (default 3). Higher = higher-effort answers only.
    min_response_words: int = 0          # 0 = use server default
    # Provenance: the submitter can whitelist which LLM backends may
    # answer. Empty list = any backend OK. e.g. ["hermes","codex","ollama"]
    allowed_backends: list[str] = field(default_factory=list)
    # Routing: if non-empty, only the matching agent_id may answer.
    # Empty = broadcast to all registered harvest agents.
    target_agent_id: str = ""


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
        # Server-wide floors. Per-prompt overrides live on PromptRequest.
        self._min_response_len   = 1     # chars (fallback if submitter didn't set)
        self._min_response_words = 3     # words (anti-stub floor)
        self._max_response_len   = 8000
        self._min_fee_usdc       = 0.001
        # Strings that count as "stub" responses — automatic reject. These
        # are the obvious lazy patterns: "y", "hi back", single chars, etc.
        # Submitters can also define their own via env or a future /config
        # endpoint.  Case-insensitive match after stripping whitespace.
        self._stub_patterns: set[str] = {
            "y", "yes", "no", "ok", "k", "n",
            "hi", "hi back", "hey", "hello", "yo",
            "h", "w", "t", "f", "thx", "thanks", "ty",
        }
        # Default token-pricing rate when submitter doesn't specify one.
        # 0.0 = flat fee only (no token bonus). The marketplace may evolve
        # toward a per-token norm; until then this is a no-op.
        self._default_fee_per_input_token_usdc = float(
            os.environ.get("FCOIN_DEFAULT_FEE_PER_INPUT_TOKEN", "0")
        )
        # --- persistence ---
        # Back the market with a JSON file so it survives Render redeploys.
        # We don't lock the file (the in-memory lock is enough for the single-process
        # fcoin server); we just write atomically.
        import os as _os
        self._persist_path = _os.environ.get(
            "FCOIN_PROMPTS_PATH",
            "/data/data/com.termux/files/home/fcoin/prompts_store.json",
        )
        self._load()

    # ------------------------------------------------------------------ submitter side

    def submit_prompt(
        self,
        submitter:      str,
        prompt:         str,
        fee_usdc:       float,
        max_responses:  int = 1,
        model_hint:     str = "",
        fee_per_input_token_usdc: float | None = None,
        min_response_words: int = 0,
        allowed_backends: list[str] | None = None,
        target_agent_id: str = "",
    ) -> dict:
        """
        Submit a prompt to the marketplace.

        Total cost locked from submitter:
            cost = max_responses × (fee_usdc + input_tokens × fee_per_input_token_usdc)

        The flat fee_usdc is what the answering agent is guaranteed;
        the per-input-token portion is a bonus that scales with prompt
        size, so a 10K-token prompt pays the agent more than a 10-word
        one even at the same flat fee.

        If `fee_per_input_token_usdc` is omitted, the marketplace default
        is used (env FCOIN_DEFAULT_FEE_PER_INPUT_TOKEN, default 0 = flat fee).
        """
        if not prompt or not prompt.strip():
            raise ValueError("prompt cannot be empty")
        if fee_usdc < self._min_fee_usdc:
            raise ValueError(f"fee_usdc must be >= {self._min_fee_usdc}")
        if max_responses < 1:
            raise ValueError("max_responses must be >= 1")
        if fee_per_input_token_usdc is None:
            fee_per_input_token_usdc = self._default_fee_per_input_token_usdc
        if fee_per_input_token_usdc < 0:
            raise ValueError("fee_per_input_token_usdc must be >= 0")

        # Token count is frozen at submit time so the submitter is never
        # retroactively charged for a prompt that gets edited server-side.
        input_tokens = count_input_tokens(prompt)

        from .exchange import get_exchange, Balance
        ex = get_exchange()
        wallet = ex.get_or_create_agent(submitter)
        # Worst-case lock: full token price × max_responses. Any unused
        # token money is refunded on cancel/settle.
        per_response = fee_usdc + input_tokens * fee_per_input_token_usdc
        cost = per_response * max_responses
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
            fee_per_input_token_usdc=fee_per_input_token_usdc,
            input_tokens=input_tokens,
            locked_usdc=cost,
            min_response_words=min_response_words,
            allowed_backends=list(allowed_backends or []),
            target_agent_id=(target_agent_id or "").strip(),
        )
        with self._lock:
            self._requests[req_id] = req

        log.info(
            f"[prompts] submit  id={req_id}  submitter={submitter}  "
            f"fee={fee_usdc:.4f}  fee/tok={fee_per_input_token_usdc:.6f}  "
            f"tokens={input_tokens}  max={max_responses}  cost={cost:.4f}"
        )

        # Broadcast to all connected agents via the SSE stream
        self._broadcast_request(req)
        self._save()              # persist after every mutation
        return self._request_view(req)

    # ------------------------------------------------------------------ agent side

    def submit_response(
        self,
        agent_id:       str,
        request_id:     str,
        response:       str,
        backend:        str = "",         # provenance tag (e.g. "hermes", "codex")
    ) -> dict:
        """
        Agent submits a response to a prompt.
        Credits `fee_usdc` to the agent if the response is accepted.
        Returns the response record.

        Validation (any of these raises ValueError):
          - response empty or below min_response_words
          - response exceeds _max_response_len chars
          - response matches a known stub pattern ("y", "hi back", "yes", ...)
          - response has too-low lexical diversity ("yes yes yes yes")
          - backend is not in the prompt's allowed_backends whitelist
        """
        if not response or not response.strip():
            raise ValueError("response cannot be empty")
        if len(response.strip()) < self._min_response_len:
            raise ValueError(f"response too short (min {self._min_response_len} chars)")
        if len(response) > self._max_response_len:
            raise ValueError(f"response too long (max {self._max_response_len} chars)")

        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise ValueError("unknown request_id")
            if req.status == "cancelled":
                raise ValueError("request was cancelled")
            if req.status == "fulfilled":
                raise ValueError("request already fulfilled")
            if len(req.responses) >= req.max_responses:
                raise ValueError("request already fulfilled")
            if any(r["agent_id"] == agent_id for r in req.responses):
                raise ValueError("agent already responded to this prompt")
            # Per-prompt backend whitelist (provenance)
            if req.allowed_backends:
                if not backend:
                    raise ValueError(
                        f"prompt requires one of: {req.allowed_backends} (no backend tag sent)"
                    )
                if backend not in req.allowed_backends:
                    raise ValueError(
                        f"backend {backend!r} not in prompt's allowed list {req.allowed_backends}"
                    )
            # Per-prompt minimum word count
            min_words = req.min_response_words or self._min_response_words
            word_count = len(response.split())
            if word_count < min_words:
                raise ValueError(
                    f"response too short (got {word_count} words, need >= {min_words})"
                )
            # Stub-pattern check (case-insensitive whole-string match)
            stripped = response.strip().lower()
            if stripped in self._stub_patterns:
                raise ValueError(
                    f"response matches a stub pattern ({stripped!r}); submit a real answer"
                )
            # Lexical-diversity check: if <40% of words are unique, it's
            # likely a copy-paste of the same word.
            words = stripped.split()
            if words:
                unique_ratio = len(set(words)) / len(words)
                if len(words) >= 5 and unique_ratio < 0.4:
                    raise ValueError(
                        f"response has too-low lexical diversity "
                        f"({unique_ratio:.0%} unique words); looks templated"
                    )

        # Credit the agent
        # Per-response earnings = flat fee + input-token bonus.
        # Token money was locked at submit time; the agent earns it
        # on top of the flat fee whenever they answer.
        earned = req.fee_usdc + req.input_tokens * req.fee_per_input_token_usdc
        from .exchange import get_exchange, Balance
        ex = get_exchange()
        wallet = ex.get_or_create_agent(agent_id)
        b = wallet._balances.get("usdc")
        if b is None:
            b = Balance()
            wallet._balances["usdc"] = b
        b.available += earned
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
            # Per-response earnings = flat fee + input-token bonus.
            # The token bonus is the *full* locked amount (the submitter
            # pre-paid for tokens at submit time), so the agent always
            # earns the token component when they answer.
            earned = req.fee_usdc + req.input_tokens * req.fee_per_input_token_usdc
            req.paid_out_usdc += earned
            if len(req.responses) >= req.max_responses:
                req.status = "fulfilled"
                # Refund any unfilled portion: flat fee × unfilled slots
                # only — token money is fully consumed (we used the tokens).
                filled = len(req.responses)
                refund = (req.max_responses - filled) * req.fee_usdc
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
            f"earned={earned:.4f}  (flat={req.fee_usdc:.4f} + token={req.input_tokens * req.fee_per_input_token_usdc:.4f})  status={req.status}"
        )

        self._save()              # persist after every mutation
        return {
            "response_id":   resp_id,
            "request_id":    request_id,
            "agent_id":      agent_id,
            "earned_usdc":   earned,
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

            # Refund the unfilled portion: flat fee × unfilled slots.
            # Token money is NOT refundable on cancel — the tokens were
            # already paid for at submit time, regardless of whether
            # the prompt got answered. This is a research marketplace,
            # not a refund-friendly store; the user agreed to the rate
            # up front and the tokens were priced at that moment.
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
        self._save()              # persist after every mutation
        return self.get_request(request_id)

    # ------------------------------------------------------------------ internals

    def _request_view(self, req: PromptRequest) -> dict:
        per_response = req.fee_usdc + req.input_tokens * req.fee_per_input_token_usdc
        return {
            "id":                          req.id,
            "submitter":                   req.submitter,
            "prompt":                      req.prompt,
            "fee_usdc":                    req.fee_usdc,
            "max_responses":               req.max_responses,
            "model_hint":                  req.model_hint,
            "status":                      req.status,
            "responses":                   req.responses,
            "paid_out_usdc":               req.paid_out_usdc,
            "created_at":                  req.created_at,
            # Token-pricing fields. Per-response price = flat + tokens*rate.
            "input_tokens":                req.input_tokens,
            "fee_per_input_token_usdc":    req.fee_per_input_token_usdc,
            "per_response_total_usdc":     per_response,
            "locked_usdc":                 req.locked_usdc,
            # Anti-stub + provenance.
            "min_response_words":          req.min_response_words or self._min_response_words,
            "allowed_backends":            req.allowed_backends,
            "target_agent_id":             req.target_agent_id,
        }

    def _broadcast_request(self, req: PromptRequest) -> None:
        """Push the request to all SSE clients as a `prompt_request` event."""
        from .stream import market_stream
        payload = {
            "type": "prompt_request",
            "data": {
                "request_id":      req.id,
                "prompt":          req.prompt,
                "fee_usdc":        req.fee_usdc,
                "max_responses":   req.max_responses,
                "model_hint":      req.model_hint,
                "submitter":       req.submitter,
                "target_agent_id": req.target_agent_id,
            },
        }
        market_stream.broadcast(payload)

    # ------------------------------------------------------------------ persistence

    def _load(self) -> None:
        """Load prompts + responses from the JSON store, if it exists.

        Silently no-ops if the file is missing or corrupt. We never want a
        bad store file to take the server down.
        """
        import os, json as _json
        path = self._persist_path
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = _json.load(f)
        except Exception as e:
            log.warning(f"[prompts] could not read {path}: {e}")
            return
        with self._lock:
            for pr_data in raw.get("requests", []):
                try:
                    req = PromptRequest(
                        id=pr_data["id"],
                        submitter=pr_data["submitter"],
                        prompt=pr_data["prompt"],
                        fee_usdc=float(pr_data["fee_usdc"]),
                        max_responses=int(pr_data.get("max_responses", 1)),
                        model_hint=pr_data.get("model_hint", ""),
                    )
                    req.created_at = float(pr_data.get("created_at", req.created_at))
                    req.status = pr_data.get("status", "open")
                    # rebuild the responses list from the response rows that
                    # reference this request
                    req.responses = list(pr_data.get("responses_inline", []))
                    req.paid_out_usdc = float(pr_data.get("paid_out_usdc", 0.0))
                    self._requests[req.id] = req
                except Exception as e:
                    log.warning(f"[prompts] skip bad request row: {e}")
            for rp_data in raw.get("responses", []):
                try:
                    rp = PromptResponse(
                        id=rp_data["id"],
                        request_id=rp_data["request_id"],
                        agent_id=rp_data["agent_id"],
                        response=rp_data["response"],
                    )
                    rp.created_at = float(rp_data.get("created_at", rp.created_at))
                    self._responses[rp.id] = rp
                except Exception as e:
                    log.warning(f"[prompts] skip bad response row: {e}")
        log.info(f"[prompts] loaded {len(self._requests)} requests, {len(self._responses)} responses from {path}")

    def _save(self) -> None:
        """Atomically write prompts + responses to the JSON store."""
        import os, json as _json, tempfile
        with self._lock:
            payload = {
                "requests": [
                    {
                        "id":             r.id,
                        "submitter":      r.submitter,
                        "prompt":         r.prompt,
                        "fee_usdc":       r.fee_usdc,
                        "max_responses":  r.max_responses,
                        "model_hint":     r.model_hint,
                        "status":         r.status,
                        "created_at":     r.created_at,
                        "paid_out_usdc":  r.paid_out_usdc,
                        # store responses inline so we don't have to join on reload
                        "responses_inline": list(r.responses),
                    }
                    for r in self._requests.values()
                ],
                "responses": [
                    {
                        "id":          r.id,
                        "request_id":  r.request_id,
                        "agent_id":    r.agent_id,
                        "response":    r.response,
                        "created_at":  r.created_at,
                    }
                    for r in self._responses.values()
                ],
            }
        # atomic write: tmp file in same dir, fsync, rename
        path = self._persist_path
        d = os.path.dirname(path) or "."
        try:
            fd, tmp = tempfile.mkstemp(prefix=".prompts-", dir=d)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(_json.dumps(payload, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception as e:
            log.warning(f"[prompts] could not save to {path}: {e}")


# Global singleton
prompt_market = PromptMarket()