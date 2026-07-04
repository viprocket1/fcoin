#!/usr/bin/env bash
# fcoin agent installer — Linux / macOS / Termux (bash)
# Creates a `rune` command that runs the fcoin prompt-market agent.
#
# Usage:   curl -fsSL https://raw.githubusercontent.com/viprocket1/fcoin/master/installers/install.sh | bash
# Or:      bash install.sh [--uninstall] [--dir DIR] [--no-shell] [--no-update]
#
# Flags:
#   --uninstall         remove $INSTALL_DIR and $BIN_DIR/rune
#   --dir PATH          install location (default: $HOME/.fcoin/agent)
#   --no-shell          don't append to ~/.bashrc
#   --no-update         disable auto-update for this install (or set RUNE_NO_AUTO_UPDATE=1)
#
# Auto-update:
#   After install, every `rune` invocation checks if agent_runner.py is
#   older than RUNE_UPDATE_INTERVAL_SECS (default 21600 = 6h). If so, it
#   refetches the latest from the pinned repo URL. Old copy becomes
#   agent_runner.py.bak.<ts>. Disable with RUNE_NO_AUTO_UPDATE=1.
#
set -euo pipefail

REPO="viprocket1/fcoin"
BRANCH="master"
INSTALL_DIR="${HOME}/.fcoin/agent"
BIN_DIR="${HOME}/.local/bin"
SHELL_RC="${HOME}/.bashrc"
PY_MIN="3.10"

say()  { printf '\033[1;36m[fcoin]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[fcoin]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fcoin]\033[0m %s\n' "$*" >&2; exit 1; }

# ---- arg parsing -------------------------------------------------------------
UNINSTALL=0
EDIT_SHELL=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --uninstall)     UNINSTALL=1; shift ;;
    --dir)           INSTALL_DIR="$2"; shift 2 ;;
    --no-shell)      EDIT_SHELL=0; shift ;;
    --no-update)     export RUNE_NO_AUTO_UPDATE=1; shift ;;
    -h|--help)
      sed -n '2,8p' "$0"; exit 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

# ---- uninstall ---------------------------------------------------------------
if [[ "$UNINSTALL" == "1" ]]; then
  rm -rf "$INSTALL_DIR"
  rm -f  "$BIN_DIR/rune"
  say "removed $INSTALL_DIR and $BIN_DIR/rune"
  say "delete the fcoin block in $SHELL_RC if present"
  exit 0
fi

# ---- pick python -------------------------------------------------------------
pick_python() {
  for cmd in python3 python; do
    if command -v "$cmd" >/dev/null 2>&1; then
      local ver
      ver="$("$cmd" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo 0)"
      if [[ "$(echo "$ver >= $PY_MIN" | bc -l 2>/dev/null || echo 0)" == "1" ]]; then
        echo "$cmd"; return 0
      fi
    fi
  done
  return 1
}

if ! PY_BIN="$(pick_python)"; then
  warn "Python >= $PY_MIN not found. Attempting to install..."
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y && sudo apt-get install -y python3 python3-venv
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y python3 python3-virtualenv
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y python3 python3-virtualenv
  elif command -v brew >/dev/null 2>&1; then
    brew install python@3.12
  elif command -v pkg >/dev/null 2>&1; then
    pkg update -y && pkg install -y python
  else
    die "no supported package manager — install Python $PY_MIN+ manually"
  fi
  PY_BIN="$(pick_python)" || die "Python still not available after install"
fi
say "using $($PY_BIN --version) at $(command -v $PY_BIN)"

# ---- fetch agent_runner.py ---------------------------------------------------
RAW_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}/agent_runner.py"
mkdir -p "$INSTALL_DIR"
say "downloading agent_runner.py from $REPO@$BRANCH ..."
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$RAW_URL" -o "$INSTALL_DIR/agent_runner.py" \
    || die "download failed — check network or repo access"
elif command -v wget >/dev/null 2>&1; then
  wget -qO "$INSTALL_DIR/agent_runner.py" "$RAW_URL" \
    || die "download failed — check network or repo access"
else
  die "need curl or wget to fetch agent_runner.py"
fi
chmod +x "$INSTALL_DIR/agent_runner.py"

# ---- venv (only if missing) --------------------------------------------------
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  say "creating venv at $INSTALL_DIR/venv ..."
  "$PY_BIN" -m venv "$INSTALL_DIR/venv"
fi
# agent_runner.py uses stdlib only; no pip install needed.

# ---- API key -----------------------------------------------------------------
if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${OPENAI_API_KEY:-}" ]]; then
  warn "no LLM key in env — `rune` will sniff existing tools:"
  say "  Codex CLI ~/.codex/auth.json, Claude Code ~/.claude/config.json,"
  say "  OpenCode ~/.config/opencode/opencode.json, Continue.dev ~/.continue/config.json,"
  say "  Aider ~/.aider.*.api.key, Codeium ~/.codeium/config.json, Jan ~/jan/settings.json,"
  say "  Zed ~/.config/zed/settings.json, Hermes Agent ~/.hermes/.env + ~/.hermes/auth.json,"
  say "  Gemini CLI ~/.gemini/oauth_creds.json, or a ~/.env file with"
  say "  ANTHROPIC_API_KEY=... / OPENAI_API_KEY=..."
fi

# ---- install `rune` shim -----------------------------------------------------
# Routing:
#   - subcommands (update / uninstall / version / status) -> installer
#   - default (anything else) -> agent_runner.py
#
# Auto-update model:
#   - On every rune invocation, if the local agent_runner.py is older than
#     RUNE_UPDATE_INTERVAL_SECS (default 21600 = 6h), OR missing entirely,
#     OR the local script's `UPDATE_URL` differs from this installer's URL,
#     we re-fetch the latest from ${REPO}@${BRANCH}. Old file saved as
#     agent_runner.py.bak.<unix-ts>.
#   - Auto-update is suppressed when:
#       * RUNE_OFFLINE=1                   (no network at all)
#       * RUNE_NO_AUTO_UPDATE=1            (user explicitly disabled)
#   - Set RUNE_FORCE_UPDATE=1 to bypass the freshness check.
REPO_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}"
RAW_URL="${REPO_URL}/installers/install.sh"
RUNNER_URL="${REPO_URL}/agent_runner.py"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/rune" <<EOF
#!/usr/bin/env bash
# fcoin agent launcher + self-updater — generated by install.sh
set -e
INSTALLER_URL="${RAW_URL}"
RUNNER_URL="${RUNNER_URL}"
INSTALL_DIR="${INSTALL_DIR}"
PYBIN="${PY_BIN}"
UPDATE_INTERVAL_SECS="\${RUNE_UPDATE_INTERVAL_SECS:-21600}"
UPDATE_URL_TRACKER="\$INSTALL_DIR/.rune-update-url"
AGENT="\$INSTALL_DIR/agent_runner.py"

cmd="\${1:-}"

# Auto-update helper. Refetches \$AGENT and replaces the rune shim itself if
# stale. Never errors out — if the network fails, just runs the local copy.
_maybe_update() {
  if [[ "\${RUNE_OFFLINE:-0}" == "1" || "\${RUNE_NO_AUTO_UPDATE:-0}" == "1" ]]; then
    return 0
  fi
  # Force-update mode or no local file -> refetch unconditionally.
  local force="\${RUNE_FORCE_UPDATE:-0}"
  if [[ "\$force" == "1" || ! -f "\$AGENT" ]]; then
    _rune_refetch "\$force"
    return 0
  fi
  # Freshness check: mtime of agent_runner.py vs INTERVAL_SECS.
  local now=\$(date +%s)
  local mtime=\$(stat -c %Y "\$AGENT" 2>/dev/null || stat -f %m "\$AGENT" 2>/dev/null || echo \$now)
  local age=\$(( now - mtime ))
  if [[ \$age -gt \$UPDATE_INTERVAL_SECS ]]; then
    echo "[rune] local agent_runner.py is \$((age/3600))h old -> checking for updates ..."
    _rune_refetch 0
  fi
}

# Refetch the latest agent_runner.py. \$1 = force (1 = print success, 0 = silent on no-change)
_rune_refetch() {
  local tmpbak="\${AGENT}.bak.\$(date +%s)"
  local tmpnew="\${AGENT}.new"
  if curl -fsSL --max-time 30 "\$RUNNER_URL" -o "\$tmpnew" 2>/dev/null; then
    if [[ -s "\$tmpnew" ]] && ! cmp -s "\$tmpnew" "\$AGENT" 2>/dev/null; then
      # Local copy differs from upstream -> swap.
      [[ -f "\$AGENT" ]] && mv "\$AGENT" "\$tmpbak"
      mv "\$tmpnew" "\$AGENT"
      chmod +x "\$AGENT"
      echo "[rune] updated agent_runner.py (backup: \$tmpbak)"
    else
      rm -f "\$tmpnew"
      [[ "\${1:-0}" == "1" ]] && echo "[rune] agent_runner.py already up-to-date"
    fi
  else
    rm -f "\$tmpnew"
    # Silent when not forced; loud when forced.
    [[ "\${1:-0}" == "1" ]] && echo "[rune] upstream unreachable (kept local copy)" >&2
  fi
}

case "\$cmd" in
  update|--update|-u)
    echo "[rune] fetching latest installer from ${REPO}@${BRANCH} ..."
    exec curl -fsSL "\${INSTALLER_URL}" | bash -s -- "\${@:2}"
    ;;
  force-update|--force-update)
    echo "[rune] force-refreshing agent_runner.py from \${RUNNER_URL} ..."
    _rune_refetch 1
    exit 0
    ;;
  no-update|--no-update)
    echo "[rune] re-running installer with --no-update ..."
    exec curl -fsSL "\${INSTALLER_URL}" | bash -s -- --no-update
    ;;
  uninstall|--uninstall)
    echo "[rune] re-running installer with --uninstall ..."
    exec curl -fsSL "\${INSTALLER_URL}" | bash -s -- --uninstall
    ;;
  version|--version|-V)
    echo "fcoin agent_runner.py at: \$AGENT"
    echo "installer URL: \${INSTALLER_URL}"
    echo "auto-update: every \$((UPDATE_INTERVAL_SECS/3600))h  (set RUNE_NO_AUTO_UPDATE=1 or RUNE_OFFLINE=1 to disable)"
    if [[ -f "\$AGENT" ]]; then
      local now_v=\$(date +%s)
      local mt=\$(stat -c %Y "\$AGENT" 2>/dev/null || stat -f %m "\$AGENT" 2>/dev/null || echo \$now_v)
      local local_age=\$(( now_v - mt ))
      echo "local agent_runner.py age: \$((local_age/3600))h \$((local_age%3600/60))m"
    fi
    echo "Pinned base URL (in installer):  ${REPO}@${BRANCH}"
    echo "Pinned base URL (in tracker):"
    [[ -f "\$UPDATE_URL_TRACKER" ]] && head -1 "\$UPDATE_URL_TRACKER" || echo "  (no tracker found)"
    ;;
  status|doctor)
    echo "INSTALL_DIR = \$INSTALL_DIR"
    echo "AGENT_RUNNER = \$PYBIN \$AGENT"
    "\$0" --show-identity 2>/dev/null || true
    ;;
  --help|-h|"")
    echo "Usage: rune [command] [args]"
    echo ""
    echo "Subcommands:"
    echo "  update             re-run installer to fetch latest agent_runner.py"
    echo "  force-update       refetch agent_runner.py right now (no waiting)"
    echo "  uninstall          remove the agent + shim"
    echo "  status             show install paths and saved agent identity"
    echo "  version            print install paths, freshness, pinned URL"
    echo ""
    echo "Default: forwards all remaining args to agent_runner.py:"
    echo "  rune --agent-id my-bot"
    echo "  rune --show-identity"
    echo "  rune --dry-run"
    echo "  rune --reset"
    echo ""
    echo "Auto-update: every \$((UPDATE_INTERVAL_SECS/3600))h on each rune invocation,"
    echo "  unless RUNE_NO_AUTO_UPDATE=1 or RUNE_OFFLINE=1 is set in env."
    echo "  Set RUNE_UPDATE_INTERVAL_SECS to change the window."
    "\$0" --help
    ;;
  *)
    # Default: refresh-if-stale, then forward
    _maybe_update
    exec "\$PYBIN" "\$AGENT" "\$@"
    ;;
esac
EOF
chmod +x "$BIN_DIR/rune"
say "installed $BIN_DIR/rune"

# Save the install URL alongside for debugging / 'rune update' to consult
mkdir -p "$INSTALL_DIR"
{
  echo "INSTALL_URL=${REPO_URL}"          # base URL, not specific file
  echo "RUNNER_URL=${RUNNER_URL}"
  echo "INSTALLER_URL=${RAW_URL}"
  echo "BRANCH=${BRANCH}"
  echo "INSTALLED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "$INSTALL_DIR/.rune-update-url"

# ---- ensure BIN_DIR on PATH --------------------------------------------------
if [[ "$EDIT_SHELL" == "1" ]]; then
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;  # already on PATH
    *)
      touch "$SHELL_RC" 2>/dev/null || true
      if ! grep -q 'fcoin agent PATH' "$SHELL_RC" 2>/dev/null; then
        {
          echo ''
          echo '# >>> fcoin agent PATH >>>'
          echo 'export PATH="$HOME/.local/bin:$PATH"'
          echo '# <<< fcoin agent PATH <<<'
        } >> "$SHELL_RC"
        say "added $BIN_DIR to PATH in $SHELL_RC"
      fi
      ;;
  esac
fi

# ---- done --------------------------------------------------------------------
cat <<MSG

$(printf '\033[1;32m')fcoin agent installed.$(printf '\033[0m')

Next steps:
  1) restart your shell (or: source $SHELL_RC)
  2) set your key (pick one) — usually unnecessary, `rune` sniffs existing tools:
       export ANTHROPIC_API_KEY=sk-ant-...
       export OPENAI_API_KEY=sk-...
  3) run the agent:
       rune --agent-id my-bot

Options:
  rune                            # run the agent (auto-displays identity on first launch)
  rune --show-identity             # print saved wallet
  rune update                      # self-update: pulls latest agent_runner.py
  rune status                      # show install paths + saved identity
  rune uninstall                   # remove the agent + shim

Uninstall:
  rune uninstall
  # or
  curl -fsSL .../install.sh | bash -s -- --uninstall
MSG
