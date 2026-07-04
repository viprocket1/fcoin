#!/usr/bin/env python3
"""
agent_runner.py — Local LLM-powered agent that listens for prompt_request events
on the fcoin /stream SSE endpoint, runs the prompt with its own LLM key, and
submits the response back to earn USDC fees.

Usage:
    export ANTHROPIC_API_KEY=sk-ant-...
    # or
    export OPENAI_API_KEY=sk-...

    python agent_runner.py --agent-id my-bot --base-url https://fcoin.onrender.com

Optional flags:
    --provider anthropic|openai   LLM provider to use
    --model MODEL                model name (default: claude-sonnet-4-5 or gpt-4o-mini)
    --filter-difficulty easy|medium|hard   only respond to prompts of a given difficulty
    --min-fee 0.05               skip prompts below this USDC fee
    --dry-run                    print responses but don't POST them
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from typing import Optional


# -----------------------------------------------------------------------------
# Local identity persistence
# -----------------------------------------------------------------------------
import os
from pathlib import Path

IDENTITY_FILE = Path.home() / ".fcoin" / "agent.json"


def load_identity(base_url: str) -> dict | None:
    """Load saved identity for this base_url, or None."""
    if not IDENTITY_FILE.exists():
        return None
    try:
        data = json.loads(IDENTITY_FILE.read_text())
        if data.get("base_url") == base_url:
            return data
    except Exception:
        pass
    return None


def save_identity(identity: dict) -> None:
    """Persist identity to ~/.fcoin/agent.json."""
    IDENTITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    IDENTITY_FILE.write_text(json.dumps(identity, indent=2))
    try:
        os.chmod(IDENTITY_FILE, 0o600)  # owner-only — contains secret
    except Exception:
        pass


# -----------------------------------------------------------------------------
# HTTP helpers
# -----------------------------------------------------------------------------
def http_post(url: str, body: dict, headers: dict = None) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def http_get(url: str, headers: dict = None) -> str:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=None) as resp:
        return resp.read().decode()


# -----------------------------------------------------------------------------
# LLM providers
# -----------------------------------------------------------------------------
def call_anthropic(prompt: str, model: str) -> str:
    import urllib.request
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps({
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read().decode())
        return out["content"][0]["text"].strip()


def call_openai(prompt: str, model: str) -> str:
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps({
            "model": model,
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": prompt}],
        }).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read().decode())
        return out["choices"][0]["message"]["content"].strip()


# -----------------------------------------------------------------------------
# SSE stream consumer (stdlib only, no deps)
# -----------------------------------------------------------------------------
def stream_events(base_url: str, events: str, on_event):
    """
    Connect to /stream and call on_event(event_name, data_dict) for each event.
    Uses urllib for SSE — simple line-by-line parser.
    """
    url = f"{base_url.rstrip('/')}/stream?events={events}"
    req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
    with urllib.request.urlopen(req, timeout=None) as resp:
        current_event = None
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            if not line:
                current_event = None
                continue
            if line.startswith(":"):
                continue  # SSE comment / ping
            if line.startswith("event:"):
                current_event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                payload = line[len("data:"):].strip()
                try:
                    data = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                on_event(current_event or data.get("type", "message"), data)


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="fcoin prompt-market agent runner")
    parser.add_argument("--agent-id", required=True, help="Your fcoin agent ID")
    parser.add_argument("--base-url", default="https://fcoin.onrender.com",
                        help="Base URL of the fcoin server")
    parser.add_argument("--provider", choices=["anthropic", "openai"], default=None,
                        help="LLM provider (auto-detected from env vars)")
    parser.add_argument("--model", default=None, help="Model name (provider default)")
    parser.add_argument("--min-fee", type=float, default=0.0,
                        help="Skip prompts below this USDC fee")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print responses but don't POST them back")
    parser.add_argument("--reset", action="store_true",
                        help="Forget saved identity and mint a new agent")
    parser.add_argument("--show-identity", action="store_true",
                        help="Print the saved agent_id and exit")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/")

    # Reset clears local identity
    if args.reset and IDENTITY_FILE.exists():
        IDENTITY_FILE.unlink()
        print(f"[identity] reset  removed {IDENTITY_FILE}")

    # Show-and-exit
    if args.show_identity:
        ident = load_identity(base_url)
        if ident is None:
            print("[identity] none saved")
        else:
            print(f"[identity] agent_id={ident['agent_id']}  address={ident.get('address')}")
        return

    # Load or mint identity
    identity = load_identity(base_url)
    if identity is None:
        print(f"[identity] none saved — registering new agent at {base_url} ...")
        try:
            identity = http_post(f"{base_url}/register", {"display_name": args.agent_id})
            identity["base_url"] = base_url
            save_identity(identity)
            print(f"[identity] registered  agent_id={identity['agent_id']}")
            print(f"[identity] address={identity.get('address')}")
            print(f"[identity] saved to {IDENTITY_FILE}")
        except Exception as exc:
            print(f"[identity] registration failed: {exc}", file=sys.stderr)
            sys.exit(1)
        args.agent_id = identity["agent_id"]
    else:
        args.agent_id = identity["agent_id"]
        print(f"[identity] loaded  agent_id={args.agent_id}  address={identity.get('address')}")

    # Auto-detect provider
    provider = args.provider
    if provider is None:
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        else:
            print("ERROR: set ANTHROPIC_API_KEY or OPENAI_API_KEY", file=sys.stderr)
            sys.exit(1)

    # Default model
    model = args.model
    if model is None:
        model = "claude-sonnet-4-5" if provider == "anthropic" else "gpt-4o-mini"

    call_llm = call_anthropic if provider == "anthropic" else call_openai

    print(f"[agent] id={args.agent_id} provider={provider} model={model} "
          f"min_fee={args.min_fee} dry_run={args.dry_run}")
    print(f"[agent] connecting to {args.base_url}/stream?events=prompt_request ...")

    headers = {"X-Agent-ID": args.agent_id}

    def handle_event(event_name: str, data: dict) -> None:
        if event_name != "prompt_request":
            return
        req_id = data.get("request_id")
        prompt = data.get("prompt", "")
        fee = float(data.get("fee_usdc", 0))

        if fee < args.min_fee:
            print(f"[skip] {req_id} fee={fee} below min_fee={args.min_fee}")
            return

        print(f"[prompt] {req_id} fee={fee} prompt={prompt[:60]!r}")
        try:
            answer = call_llm(prompt, model)
        except Exception as exc:
            print(f"[error] LLM call failed: {exc}", file=sys.stderr)
            return

        print(f"[answer] {req_id} -> {answer[:120]!r}")

        if args.dry_run:
            return

        try:
            res = http_post(
                f"{args.base_url}/respond_prompt",
                {"request_id": req_id, "response": answer},
                headers=headers,
            )
            print(f"[paid] {req_id} earned={res.get('earned_usdc')} "
                  f"status={res.get('request_status')}")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode(errors="replace")
            print(f"[error] {exc.code} {err_body}", file=sys.stderr)
        except Exception as exc:
            print(f"[error] POST failed: {exc}", file=sys.stderr)

    while True:
        try:
            stream_events(args.base_url, "prompt_request", handle_event)
        except KeyboardInterrupt:
            print("\n[agent] stopped")
            break
        except Exception as exc:
            print(f"[stream] disconnected: {exc} — reconnecting in 3s")
            time.sleep(3)


if __name__ == "__main__":
    main()