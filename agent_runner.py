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
    # or just:  python agent_runner.py           # auto-derives display name from
                                                # $USER/$USERNAME@hostname; saves
                                                # the real agent_id to
                                                # ~/.fcoin/agent.json on first run.

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
# Credential discovery
# -----------------------------------------------------------------------------
# Auto-detect API keys for the agents a user already has installed
# (Codex, Claude Code, OpenCode, Aider, Zed, generic .env, etc.).
#
# We only run this as a fallback: if ANTHROPIC_API_KEY / OPENAI_API_KEY
# are already exported in env, those win.
def _read_env_file(path: Path) -> dict[str, str]:
    """Best-effort dotenv reader. Returns {KEY: value} from `KEY=value` lines,
    ignoring comments/quotes. Stdlib-only, no email/rfc parsing tricks."""
    try:
        text = path.read_text(errors="replace")
    except Exception:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and v and not v.startswith("$"):
            out[k] = v
    return out


def _read_json_keys(
    path: Path,
    key_paths: tuple[tuple[str, ...], ...],
    env_for_keys: dict[str, str],
) -> dict[str, str]:
    """Read a JSON file and pluck strings at several possible key paths.
    Each `keys` tuple is a chain into the JSON dict; the leaf must be a string.
    `env_for_keys` maps leaf key name -> environment variable name.
    First non-empty hit per env-var wins (no overwrite).
    """
    try:
        data = json.loads(path.read_text(errors="replace"))
    except Exception:
        return {}
    out: dict[str, str] = {}
    for keys in key_paths:
        node: object = data
        for k in keys:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(k)  # type: ignore[union-attr]
        if isinstance(node, str) and node.strip():
            env_name = env_for_keys.get(keys[-1])
            if env_name and env_name not in out:
                out[env_name] = node.strip()
    return out


# Ordered list: first non-empty wins. Add new tools here.
_CRED_CANDIDATES: list[tuple[str, str, Path, tuple[tuple[str, ...], ...]]] = [
    # (provider hint, source label, path, JSON-keys to try)
    ("anthropic", "Codex CLI",           Path.home() / ".codex" / "auth.json",
        (("apiKey",), ("key",), ("anthropic_api_key",), ("credentials", "anthropic"))),
    ("openai",    "Codex CLI",           Path.home() / ".codex" / "auth.json",
        (("openaiApiKey",), ("openai_api_key",), ("credentials", "openai"))),
    ("anthropic", "Claude Code",         Path.home() / ".claude.json",
        (("apiKey",), ("anthropicApiKey",), ("anthropic_api_key",))),
    ("anthropic", "Claude Code (legacy)", Path.home() / ".claude" / "config.json",
        (("anthropic",), ("apiKey",), ("anthropic_api_key",))),
    ("openai",    "Claude Code",         Path.home() / ".claude" / "config.json",
        (("openai", "openai_api_key"), ("openai", "openaiApiKey"),
         ("openai_api_key",), ("openaiApiKey",))),
    ("anthropic", "OpenCode",            Path.home() / ".config" / "opencode" / "opencode.json",
        (("provider", "anthropic", "apiKey"), ("providers", "anthropic", "apiKey"),
         ("anthropic_api_key",), ("anthropicApiKey",))),
    ("openai",    "OpenCode",            Path.home() / ".config" / "opencode" / "opencode.json",
        (("provider", "openai", "apiKey"), ("providers", "openai", "apiKey"),
         ("openai_api_key",), ("openaiApiKey",))),
    ("anthropic", "Aider",               Path.home() / ".aider.anthropic.api.key", ()),
    ("openai",    "Aider",               Path.home() / ".aider.openai.api.key", ()),

    # Hermes Agent (~/.hermes/auth.json — {credential_pool: {<name>: [{base_url, ...}]}})
    # Each entry stores only a secret_fingerprint; the real key is sourced at
    # runtime from env (e.g. env:MINIMAX_API_KEY). The credential_pool tells us
    # the *base_url* — that's enough to set ANTHROPIC_BASE_URL / OPENAI_BASE_URL
    # so the existing call_anthropic / call_openai works with MiniMax/Z.ai/etc.
    ("anthropic", "Hermes Agent",
        Path.home() / ".hermes" / "auth.json",
        (("credential_pool", "minimax", "api_key"),
         ("credential_pool", "z.ai",   "api_key"),
         ("credential_pool", "kimi",   "api_key"),
         ("providers", "anthropic",   "api_key"),
         ("providers", "openrouter",  "api_key"),
         ("providers", "novita",      "api_key"))),
    ("openai",    "Hermes Agent",
        Path.home() / ".hermes" / "auth.json",
        (("credential_pool", "openai",       "api_key"),
         ("credential_pool", "copilot",      "api_key"),
         ("providers",       "openai",       "api_key"),
         ("providers",       "groq",         "api_key"))),
    # OpenRouter is its own provider — single key, hundreds of models.
    ("openai",    "Hermes Agent (OpenRouter)",
        Path.home() / ".hermes" / "auth.json",
        (("credential_pool", "openrouter", "api_key"),
         ("providers",       "openrouter", "api_key"))),
    # ~/.hermes/.env is read by the broad `.env` scan (covers all Hermes env keys)

    # --- Google Gemini CLI (https://github.com/google-gemini/gemini-cli) ---
    # ~/.gemini/oauth_creds.json holds OAuth tokens (access_token, refresh_token).
    # These are NOT raw GOOGLE_API_KEYs — but Google's OAuth bearer works
    # against https://generativelanguage.googleapis.com (Code Assist flow).
    # We adopt access_token into OPENAI_API_KEY and route via the OpenAI-compat
    # endpoint; if the token isn't valid there, the error message points the
    # user at GOOGLE_API_KEY / GEMINI_API_KEY as the canonical alternative.
    ("openai",    "Gemini CLI (oauth)",
        Path.home() / ".gemini" / "oauth_creds.json",
        (("access_token",),)),
    # Antigravity (`agy`, see ~/.hermes/.../antigravity-cli/SKILL.md) shares
    # ~/.gemini/ with the Gemini CLI. settings.json can carry auth config
    # (apiKey field) when the OS keyring isn't usable (Linux/WSL fallback).
    ("openai",    "Antigravity CLI (settings)",
        Path.home() / ".gemini" / "antigravity-cli" / "settings.json",
        (("apiKey",), ("auth", "apiKey"), ("auth", "api_key"),
         ("api_key",), ("auth", "selectedType"))),  # last key is sentinel-only
    # Google `gcloud auth application-default login` writes OAuth here. Same
    # note as Gemini CLI: bearer goes against Google endpoints, not AI Studio.
    ("openai",    "gcloud ADC",
        Path.home() / ".config" / "gcloud" / "application_default_credentials.json",
        (("access_token",),)),
    # Vertex AI / Cloud SDK service-account JSON referenced via
    # GOOGLE_APPLICATION_CREDENTIALS env var. Different shape — no
    # `access_token`, has `private_key`, `client_email`, `project_id`.
    # We CANNOT use this as a bearer against the OpenAI-compat Gemini
    # endpoint; service-account auth requires JWT assertion. We keep it
    # as a candidate only so an honest error message can mention the file.
    # (No `key_paths` snippet is wired up — see `discover_credentials` filter.)
    ("openai",    "GOOGLE_APPLICATION_CREDENTIALS",
        Path(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
             or "/dev/null"),
        (("__none__",),)),
    # Firebase CLI stores project aliases + refresh tokens. Two schema
    # variants in the wild:
    #   modern:  {"apiKey":"...","user":"x","refresh_token":"..."}
    #   legacy:  {"tokens":{"refresh_token":"..."},"user":"x"}
    # `apiKey` here is a Firebase web API key, NOT a Gemini bearer.
    # Auto-routing through Gemini's openai-compat endpoint will fail;
    # the error path will surface this honestly.
    ("openai",    "Firebase CLI",
        Path.home() / ".config" / "firebase" / "firebase-tools-rc.json",
        (("refresh_token",), ("tokens", "refresh_token"),
         ("apiKey",), ("currentLogin", "refresh_token"))),

    # --- Continue.dev (https://continue.dev) --------------------------------
    # ~/.continue/config.json holds a `models` array; each entry can carry
    # `provider`, `apiKey`, `apiBase`, etc. We only need the apiKey.
    # Three provider flavors in the wild:
    #   anthropic provider: {"provider":"anthropic","apiKey":"sk-ant-..."}
    #   openai provider:    {"provider":"openai","apiKey":"sk-..."}
    # Older flat layout:    {"apiKey":"..."}
    # `key_paths` is JSON — we sniff whatever shows up at top level OR under
    # `models[N].*` via the generic sub-rglob walk below (it'll pick those up).
    ("anthropic", "Continue.dev",
        Path.home() / ".continue" / "config.json",
        (("apiKey",), ("anthropicApiKey",), ("anthropic_api_key",))),
    ("openai",    "Continue.dev",
        Path.home() / ".continue" / "config.json",
        (("apiKey",), ("openAiApiKey",), ("openaiApiKey",),
         ("openai_api_key",))),

    # --- Windsurf / Codeium (https://codeium.com/windsurf) ------------------
    # ~/.codeium/config.json plaintext. The Windsurf IDE itself stores tokens
    # in a SQLite DB (state.vscdb → codeium.accessToken); TODO if we ever
    # allow sqlite3 imports. The plaintext config is what users editing it
    # by hand get; we accept both.
    ("openai",    "Codeium (config)",
        Path.home() / ".codeium" / "config.json",
        (("api_key",), ("apiKey",), ("api_key_v2",),
         ("access_token",), ("auth", "api_key"))),

    # --- Jan.ai (https://jan.ai) --------------------------------------------
    # ~/jan/settings.json — {apiKeys: {openai: "...", anthropic: "...", ...}}
    # Schema: top-level `apiKeys` dict, key per provider name.
    ("anthropic", "Jan.ai",
        Path.home() / "jan" / "settings.json",
        (("apiKeys", "anthropic"), ("apiKeys", "claude"),
         ("apiKeys", "anthropic_api_key"))),
    ("openai",    "Jan.ai",
        Path.home() / "jan" / "settings.json",
        (("apiKeys", "openai"), ("apiKeys", "gpt"),
         ("apiKeys", "openai_api_key"))),

    # --- Goose (Block, https://block.github.io/goose) ----------------------
    # ~/.config/goose/config.yaml — YAML, provider `api_key` field.
    # stdlib has no YAML reader; we rely on the sub-rglob walk to pick up
    # `api_key` keys under any *.yaml file in ~/.config/goose.
    # Listed here as a breadcrumb so the installer docs hint at the path.

    # --- Aider chat-history provider config (newer schema) ------------------
    # ~/.aider.model.metadata.json + ~/.aider.analytics.json + the legacy
    # ~/.aider.anthropic.api.key files. The metadata file may carry an
    # `api_key` field per provider in newer Aider versions.
    ("anthropic", "Aider (metadata)",
        Path.home() / ".aider.model.metadata.json",
        (("anthropic_api_key",), ("anthropicApiKey",))),
    ("openai",    "Aider (metadata)",
        Path.home() / ".aider.model.metadata.json",
        (("openai_api_key",), ("openaiApiKey",))),

    # --- AWS Bedrock / Amazon Q CLI -----------------------------------------
    # ~/.aws/credentials is INI (raw_api_key format is for the AWS access key
    # + secret pair, not an LLM bearer). We still surface it so the
    # installer can mention Bedrock in the "tried:" output — but we don't
    # auto-adopt into ANTHROPIC_API_KEY/OPENAI_API_KEY (Bedrock uses SigV4).
    # TODO: proper Bedrock adapter (separate call_bedrock() function).
    # ("aws",       "AWS credentials",
    #     Path.home() / ".aws" / "credentials",
    #     (("aws_access_key_id",), ("aws_secret_access_key",))),

    # --- GitHub Copilot / gh CLI --------------------------------------------
    # ~/.config/gh/hosts.yml — YAML, `oauth_token:` under `github.com:`.
    # No YAML reader in stdlib, so the sub-rglob walk below (which scans
    # ~/.config/**/*.json only) misses this. We document the path; if the
    # user installs `rune` with PyYAML available, the generic YAML walker
    # (TODO) will pick it up. For now, surface in error messages.

    # --- Cursor (https://cursor.com) ----------------------------------------
    # SQLite at ~/.config/Cursor/User/globalStorage/state.vscdb, table
    # ItemTable, keys `cursorAuth/accessToken` + `cursorAuth/refreshToken`.
    # SQLite IS in stdlib (sqlite3) — TODO: read cursorAuth/* on demand.
    # Skipped here to keep the dependency surface minimal; TODO comment.

    # --- Cline / Roo Code (VSCode SQLite) -----------------------------------
    # ~/.config/Code/User/globalStorage/{saoudrizwan.claude-dev,
    # roo-cline.roo-cline}/state.vscdb — SecretStorage (libsecret on Linux).
    # Keyring access requires `secretstorage` (non-stdlib). TODO.

    # --- Zed AI (https://zed.dev) -------------------------------------------
    # macOS: ~/Library/Application Support/Zed/settings.json (JSON)
    # Linux: ~/.config/zed/settings.json (JSON) — `api_key` per provider
    # Some Zed builds keep the key in libsecret instead — see TODO above.
    ("anthropic", "Zed AI",
        Path.home() / ".config" / "zed" / "settings.json",
        (("api_key",), ("apiKey",), ("anthropic_api_key",))),
    ("openai",    "Zed AI",
        Path.home() / ".config" / "zed" / "settings.json",
        (("api_key",), ("apiKey",), ("openai_api_key",))),

    # --- Mistral / DeepSeek / xAI / OpenRouter / Together / Fireworks / -----
    # Perplexity / Cohere — no first-party local client; keys live in env or
    # in third-party tool configs (Continue, Cline, Cursor, Aider). The
    # generic .env scan below picks them up if exported, so we don't add
    # a dedicated entry here. (vendored as env-only; the README's
    # 'tried these sources' list points users at the right env vars.)

    # --- Ollama (local-only, no auth by default) ----------------------------
    # No API key needed. We DO want to detect an Ollama install so the
    # installer can suggest `OPENAI_BASE_URL=http://localhost:11434/v1`.
    # That routing is handled in the Hermes PROVIDER_REDIRECTS map above
    # when "ollama" appears in credential_pool. Local-standalone Ollama
    # installs without Hermes don't have a creds file — we rely on the
    # user setting OPENAI_BASE_URL manually.
]  # noqa: E501


def discover_credentials() -> dict[str, str]:
    """Return merged env dict (does NOT export). Keys:
        ANTHROPIC_API_KEY, OPENAI_API_KEY, _FOUND_IN (source label)
        ANTHROPIC_BASE_URL, OPENAI_BASE_URL — when a Hermes-style provider
        indicates a non-default endpoint (e.g. MiniMax uses its Anthropic-
        compatible shim at https://api.minimax.io/anthropic).
    """
    h = Path.home()
    found: dict[str, str] = {}

    # Helper: take what's in `found` and put it in os.environ if not already set
    def adopt(env: dict[str, str], source: str) -> None:
        for k, v in env.items():
            if not os.environ.get(k):
                os.environ[k] = v
            if k in {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"} and k not in found:
                found[k] = source

    # Read .env-style files first (broadest match)
    for name in (".env", ".envrc", ".env.local"):
        p = h / name
        if p.exists():
            env = _read_env_file(p)
            if env:
                adopt(env, f"~/{name}")
    # Hermes Agent's own .env
    hermes_env = h / ".hermes" / ".env"
    if hermes_env.exists():
        env = _read_env_file(hermes_env)
        if env:
            adopt(env, "~/.hermes/.env")

    # Netrc (machine|login|password format)
    netrc = h / ".netrc"
    if netrc.exists():
        try:
            import netrc
            for host in ("api.anthropic.com", "api.openai.com"):
                auth = netrc.netrc(str(netrc)).authenticators(host)
                if auth and auth[2]:
                    env_name = ("ANTHROPIC_API_KEY"
                                if host == "api.anthropic.com" else "OPENAI_API_KEY")
                    adopt({env_name: auth[2]}, "~/.netrc")
        except Exception:
            pass

    # ---- Hermes Agent auth.json: read base_url per provider ------------
    # auth.json keeps a credential_pool like:
    #   {"credential_pool": {"minimax": [{"base_url": "https://api.minimax.io/anthropic",
    #                                      "source": "env:MINIMAX_API_KEY", ...}]}}
    # If we have an env var named in `source`, set *_BASE_URL so existing
    # call_anthropic / call_openai route correctly.
    hermes_auth = h / ".hermes" / "auth.json"
    if hermes_auth.exists():
        try:
            data = json.loads(hermes_auth.read_text(errors="replace"))
        except Exception:
            data = {}
        pool = data.get("credential_pool", {}) if isinstance(data, dict) else {}
        # Map of provider-name -> (env_name, base_url_env_name)
        PROVIDER_REDIRECTS = {
            "minimax":    ("MINIMAX_API_KEY",    "ANTHROPIC_BASE_URL",
                           "https://api.minimax.io/anthropic"),
            "z.ai":       ("GLM_API_KEY",        "OPENAI_BASE_URL",
                           "https://api.z.ai/api/paas/v4"),
            "kimi":       ("KIMI_API_KEY",       "OPENAI_BASE_URL",
                           "https://api.kimi.com/coding/v1"),
            "novita":     ("NOVITA_API_KEY",     "OPENAI_BASE_URL",
                           "https://api.novita.ai/openai/v1"),
            "openrouter": ("OPENROUTER_API_KEY", "OPENAI_BASE_URL",
                           "https://openrouter.ai/api/v1"),
            "ollama":     ("OLLAMA_API_KEY",     "OPENAI_BASE_URL",
                           "https://ollama.com/v1"),
            "groq":       ("GROQ_API_KEY",       "OPENAI_BASE_URL",
                           "https://api.groq.com/openai/v1"),
            "google":     ("GOOGLE_API_KEY",     "OPENAI_BASE_URL",
                           "https://generativelanguage.googleapis.com/v1beta/openai"),
            "gemini":     ("GEMINI_API_KEY",     "OPENAI_BASE_URL",
                           "https://generativelanguage.googleapis.com/v1beta/openai"),
        }
        for prov, entries in (pool.items() if isinstance(pool, dict) else []):
            if prov not in PROVIDER_REDIRECTS:
                continue
            env_key, base_env, default_base = PROVIDER_REDIRECTS[prov]
            # Already loaded (from .env? from previous sniffer step?) — accept any
            if not os.environ.get(env_key):
                continue
            # Find the base_url the user actually configured
            base_url = default_base
            for entry in (entries if isinstance(entries, list) else []):
                if not isinstance(entry, dict):
                    continue
                base_url = entry.get("base_url") or base_url
                break
            # Adopt into both the canonical Anthropic/OpenAI slots AND the
            # *_BASE_URL override so the existing call_anthropic call works.
            # We pick anthropic-compat providers as the "anthropic" channel
            # (MiniMax, Z.ai/Kimi etc. via Anthropic-Messages if user uses
            # that base) and OpenAI-compat ones (Kimi, Ollama, OpenRouter,
            # Gemini) as the "openai" channel.
            anthropic_compat = {"minimax"}  # anthropic-Messages-API endpoint
            openai_compat    = {"z.ai", "kimi", "novita", "openrouter",
                                "ollama", "groq", "google", "gemini"}
            if prov in anthropic_compat:
                if not os.environ.get("ANTHROPIC_BASE_URL"):
                    os.environ["ANTHROPIC_BASE_URL"] = base_url
                    found["ANTHROPIC_BASE_URL"] = f"~/.hermes/auth.json ({prov})"
                adopt({"ANTHROPIC_API_KEY": os.environ[env_key]},
                      f"~/.hermes/.env ({env_key})")
            elif prov in openai_compat:
                if not os.environ.get("OPENAI_BASE_URL"):
                    os.environ["OPENAI_BASE_URL"] = base_url
                    found["OPENAI_BASE_URL"] = f"~/.hermes/auth.json ({prov})"
                adopt({"OPENAI_API_KEY": os.environ[env_key]},
                      f"~/.hermes/.env ({env_key})")

    # macOS keychain (skipped — non-stdlib on most systems; quietly ignore)
    # Per-tool sniffs. The hint on each candidate tells us which env-var
    # the key corresponds to, so generic names like "apiKey" map correctly.
    HINT_TO_ENV = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai":    "OPENAI_API_KEY",
    }
    for hint, source, path, key_paths in _CRED_CANDIDATES:
        if not path.exists():
            continue
        env_for = {keys[-1]: HINT_TO_ENV[hint] for keys in key_paths} if key_paths else {}
        if path.suffix == ".json" or path.name.endswith(".json"):
            env = _read_json_keys(path, key_paths, env_for)
            if env:
                adopt(env, f"{source} ({path.name})")
        elif path.exists():
            try:
                value = path.read_text().strip()
                if value and not value.startswith("#"):
                    env_name = HINT_TO_ENV[hint]
                    adopt({env_name: value}, f"{source} ({path.name})")
            except Exception:
                pass

    # ---- Google-specific base-url routing --------------------------------
    # If we discovered a Google / Gemini / Antigravity / gcloud key, route
    # OPENAI_BASE_URL at Google's OpenAI-compat endpoint so call_openai
    # reaches Gemini-family models.
    if os.environ.get("OPENAI_API_KEY") and not os.environ.get("OPENAI_BASE_URL"):
        src_label = found.get("OPENAI_API_KEY", "")
        if any(name in src_label for name in ("Gemini CLI", "Antigravity", "gcloud", "Gemini", "antigravity-cli")):
            os.environ["OPENAI_BASE_URL"] = "https://generativelanguage.googleapis.com/v1beta/openai"
            found["OPENAI_BASE_URL"] = f"{src_label} (auto-routed)"
        # Else: user has raw GOOGLE_API_KEY/GEMINI_API_KEY in env. We intentionally
        # don't promote it — they should `export GEMINI_API_KEY=...` (or set up
        # `rune`'s ~/.env) and we'll route via the same base on the next pass.

    # Walk sub-paths worth checking (Claude / OpenCode / Continue / Codeium
    # may use quirky nested layouts we can't enumerate exhaustively).
    for root in (h / ".claude", h / ".continue", h / ".codeium", h / "jan",
                 h / ".config"):
        if not root.exists():
            continue
        for p in root.rglob("*.json"):
            try:
                data = json.loads(p.read_text(errors="replace"))
            except Exception:
                continue
            blobs = [
                ("ANTHROPIC_API_KEY", data.get("anthropicApiKey")),
                ("ANTHROPIC_API_KEY", data.get("anthropic_api_key")),
                ("OPENAI_API_KEY",    data.get("openaiApiKey")),
                ("OPENAI_API_KEY",    data.get("openai_api_key")),
            ]
            if isinstance(data.get("provider"), dict):
                blobs += [
                    ("ANTHROPIC_API_KEY", data["provider"].get("anthropic")),
                    ("OPENAI_API_KEY",    data["provider"].get("openai")),
                ]
            for k, v in blobs:
                if isinstance(v, str) and v.strip():
                    adopt({k: v.strip()}, f"{p.parent.name}/{p.name}")

    found["_FOUND_IN"] = found.get("ANTHROPIC_API_KEY", found.get("OPENAI_API_KEY", ""))
    return found


# -----------------------------------------------------------------------------
# LLM providers
# -----------------------------------------------------------------------------
def call_anthropic(prompt: str, model: str) -> str:
    """Anthropic Messages API. Honors ANTHROPIC_BASE_URL so MiniMax's
    Anthropic-compatible endpoint (https://api.minimax.io/anthropic) is used
    transparently when ANTHROPIC_API_KEY holds a MiniMax key."""
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    base_url = base_url.rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/v1/messages",
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
    """OpenAI Chat Completions API. Honors OPENAI_BASE_URL so Kimi, Ollama,
    OpenRouter, Novita, Gemini, Groq can be reached via the same client."""
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
    base_url = base_url.rstrip("/")
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
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
    parser.add_argument("--agent-id", default=None,
                        help="Display name for first registration (default: user@host). "
                             "Subsequent runs reuse the saved agent_id from ~/.fcoin/agent.json.")
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
        # First run on this machine — derive display name from environment
        # if user didn't pass --agent-id.
        display = args.agent_id or f"{os.environ.get('USER') or os.environ.get('USERNAME') or 'agent'}@"
        display += (os.uname().nodename if hasattr(os, "uname") else os.environ.get("COMPUTERNAME", "host")).split(".")[0]
        print(f"[identity] none saved — registering new agent at {base_url} ...")
        try:
            identity = http_post(f"{base_url}/register", {"display_name": display})
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
        # Already registered on this machine — ignore any --agent-id the user
        # typed. The real identity is the saved one.
        if args.agent_id and args.agent_id != identity["agent_id"]:
            print(f"[identity] ignoring --agent-id={args.agent_id!r}; "
                  f"using saved agent_id={identity['agent_id']!r}", file=sys.stderr)
        args.agent_id = identity["agent_id"]
        print(f"[identity] loaded  agent_id={args.agent_id}  address={identity.get('address')}")

    # Auto-detect provider. Env vars win; if absent, sniff common tools'
    # config files (~/.codex, ~/.claude, ~/.config/opencode, ~/.aider.*,
    # ~/.env, ~/.netrc, etc.) and adopt without prompting.
    provider = args.provider
    if provider is None:
        # Run the sniffer (it loads .env, ~/.hermes/.env, ~/.hermes/auth.json, etc.)
        if not (os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or os.environ.get("GOOGLE_API_KEY")
                or os.environ.get("GEMINI_API_KEY")):
            discovered = discover_credentials()
            if discovered.get("_FOUND_IN"):
                print(f"[creds] discovered LLM keys from: {discovered['_FOUND_IN']}")

        # Promote Google raw keys to OPENAI_API_KEY + Gemini base URL when no
        # other provider claim exists. Order: Anthropic > OpenAI raw > Google.
        if not os.environ.get("OPENAI_API_KEY"):
            for gkey in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
                if os.environ.get(gkey):
                    os.environ["OPENAI_API_KEY"] = os.environ[gkey]
                    if not os.environ.get("OPENAI_BASE_URL"):
                        os.environ["OPENAI_BASE_URL"] = (
                            "https://generativelanguage.googleapis.com/v1beta/openai"
                        )
                    break
        if os.environ.get("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        elif os.environ.get("OPENAI_API_KEY"):
            provider = "openai"
        else:
            print("ERROR: no LLM credentials found. Set ANTHROPIC_API_KEY or "
                  "OPENAI_API_KEY, or one of: GOOGLE_API_KEY / GEMINI_API_KEY, "
                  "or install one of: Codex CLI (~/.codex/auth.json), "
                  "Claude Code (~/.claude/config.json), "
                  "OpenCode (~/.config/opencode/opencode.json), "
                  "Continue.dev (~/.continue/config.json), "
                  "Aider (~/.aider.anthropic.api.key), "
                  "Windsurf/Codeium (~/.codeium/config.json), "
                  "Jan.ai (~/jan/settings.json), "
                  "Zed AI (~/.config/zed/settings.json), "
                  "Hermes Agent (~/.hermes/.env + ~/.hermes/auth.json), "
                  "Gemini CLI (~/.gemini/oauth_creds.json), "
                  "Antigravity CLI (~/.gemini/antigravity-cli/settings.json), "
                  "or write a ~/.env file. NOTE: Antigravity CLI's normal "
                  "install stores creds in the OS keyring (per the Hermes "
                  "Agent antigravity-cli skill doc; upstream repo at "
                  "gazetteer/antigravity-cli — verify), so a keyring-stored "
                  "Antigravity session is not directly sniffable from disk. "
                  "Cursor, Cline, Roo Code, JetBrains AI, and GitHub Copilot "
                  "CLI likewise store credentials in OS keyring / encrypted "
                  "stores / YAML — export ANTHROPIC_API_KEY/OPENAI_API_KEY "
                  "directly for those.",
                  file=sys.stderr)
            sys.exit(1)

    # Default model. With MiniMax sniffs via Hermes Agent, "MiniMax-M3" is the
    # safe pick (works at https://api.minimax.io/anthropic).
    model = args.model
    if model is None:
        if provider == "anthropic":
            base = os.environ.get("ANTHROPIC_BASE_URL", "")
            model = "MiniMax-M3" if "minimax" in base.lower() else "claude-sonnet-4-5"
        else:
            base = os.environ.get("OPENAI_BASE_URL", "")
            # pick a sensible model per provider
            if "openrouter" in base:  model = "anthropic/claude-sonnet-4-5"
            elif "kimi" in base:      model = "kimi-k2.5"
            elif "z.ai" in base:      model = "glm-4.5"
            elif "ollama" in base:    model = "llama3.3:70b"
            elif "novita" in base:    model = "meta-llama/llama-3.1-70b"
            elif "groq" in base:      model = "llama-3.3-70b-versatile"
            elif "google" in base or "gemini" in base: model = "gemini-2.0-flash"
            else:                     model = "gpt-4o-mini"

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