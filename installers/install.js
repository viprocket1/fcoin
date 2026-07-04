#!/usr/bin/env node
/**
 * fcoin agent installer — Ubuntu (apt) / Debian-family
 * One-shot Node script with zero deps. Use when bash isn't a fit
 * (CI, sandboxed VMs). Equivalent to install.sh.
 *
 * Usage:
 *   node install.js
 *   node install.js --uninstall
 *   node install.js --dir ~/.fcoin/agent --no-shell
 *
 * Or fetch remotely:
 *   curl -fsSL https://raw.githubusercontent.com/viprocket1/fcoin/master/installers/install.js | node
 */

'use strict';
const { execSync, spawnSync } = require('child_process');
const fs   = require('fs');
const path = require('path');
const os   = require('os');
const https = require('https');

const REPO   = 'viprocket1/fcoin';
const BRANCH = 'master';
const PY_MIN = [3, 10];
const HOME   = os.homedir();
const ROOT   = process.env.AGENT_INSTALL_DIR || path.join(HOME, '.fcoin', 'agent');
const BIN    = path.join(HOME, '.local', 'bin');
const RC     = path.join(HOME, '.bashrc');

const args = process.argv.slice(2);
const opts = { uninstall: false, noShell: false, help: false, dir: ROOT };
for (let i = 0; i < args.length; i++) {
  switch (args[i]) {
    case '--uninstall': opts.uninstall = true; break;
    case '--no-shell':  opts.noShell   = true; break;
    case '-h':
    case '--help':      opts.help      = true; break;
    case '--dir':       opts.dir       = args[++i]; break;
    default:
      console.error(`[fcoin] unknown flag: ${args[i]}`);
      process.exit(1);
  }
}

const C = {
  reset: '\x1b[0m',
  cyan:  s => `\x1b[1;36m${s}\x1b[0m`,
  yellow:s => `\x1b[1;33m${s}\x1b[0m`,
  red:   s => `\x1b[1;31m${s}\x1b[0m`,
  green: s => `\x1b[1;32m${s}\x1b[0m`,
};
const say  = m => console.log(C.cyan('[fcoin]'), m);
const warn = m => console.error(C.yellow('[fcoin]'), m);
const die  = m => { console.error(C.red('[fcoin]'), m); process.exit(1); };

if (opts.help) {
  console.log([
    'fcoin agent installer — Ubuntu/Debian (Node)',
    '',
    'Usage:',
    '  node install.js',
    '  node install.js --uninstall',
    '  node install.js --dir ~/.fcoin/agent --no-shell',
  ].join('\n'));
  process.exit(0);
}

// ------------------------------------------------------------------ uninstall
function uninstall() {
  fs.rmSync(ROOT, { recursive: true, force: true });
  const r = path.join(BIN, 'rune');
  if (fs.existsSync(r)) fs.unlinkSync(r);
  say(`removed ${ROOT} and ${r}`);
  say(`delete the fcoin block in ${RC} if present`);
}

if (opts.uninstall) { uninstall(); process.exit(0); }

// ------------------------------------------------------------------ helpers
function run(cmd, useSudo = false) {
  say(`$ ${useSudo ? 'sudo ' : ''}${cmd}`);
  const full = useSudo ? `sudo ${cmd}` : cmd;
  const r = spawnSync('sh', ['-c', full], { stdio: 'inherit' });
  if (r.status !== 0) die(`${full} failed`);
}

function have(cmd) {
  const r = spawnSync('sh', ['-c', `command -v ${cmd}`], { stdio: 'pipe' });
  return r.status === 0;
}

function pyVer(p) {
  const r = spawnSync(p, ['-c', 'import sys;print("%d.%d" % sys.version_info[:2])']);
  if (r.status !== 0) return null;
  return r.stdout.toString().trim().split('.').map(Number);
}

function pickPython() {
  for (const c of ['python3', 'python']) {
    if (!have(c)) continue;
    const v = pyVer(c);
    if (v && (v[0] > PY_MIN[0] || (v[0] === PY_MIN[0] && v[1] >= PY_MIN[1]))) {
      return c;
    }
  }
  return null;
}

function download(url, dest) {
  return new Promise((res, rej) => {
    const get = u => https.get(u, r => {
      if (r.statusCode === 301 || r.statusCode === 302) {
        return get(r.headers.location);
      }
      if (r.statusCode !== 200) return rej(new Error(`HTTP ${r.statusCode} for ${u}`));
      const chunks = [];
      r.on('data', c => chunks.push(c));
      r.on('end',  () => { fs.writeFileSync(dest, Buffer.concat(chunks)); res(); });
    }).on('error', rej);
    get(url);
  });
}

// ------------------------------------------------------------------ main
(async () => {
  // python check
  let py = pickPython();
  if (!py) {
    warn(`Python >= ${PY_MIN.join('.')} not found — installing via apt`);
    run('apt-get update -y',  true);
    run('apt-get install -y python3 python3-venv python3-pip', true);
    py = pickPython();
    if (!py) die('install failed — try `apt install python3 python3-venv`');
  }
  say(`using ${py} (${(execSync(`${py} --version`)).toString().trim()})`);

  // fetch
  fs.mkdirSync(ROOT, { recursive: true });
  const url  = `https://raw.githubusercontent.com/${REPO}/${BRANCH}/agent_runner.py`;
  const dest = path.join(ROOT, 'agent_runner.py');
  say(`downloading agent_runner.py from ${REPO}@${BRANCH} ...`);
  try {
    await download(url, dest);
  } catch (e) {
    die(`download failed: ${e.message}`);
  }
  fs.chmodSync(dest, 0o755);

  // venv
  if (!fs.existsSync(path.join(ROOT, 'venv'))) {
    say('creating venv ...');
    run(`${py} -m venv ${ROOT}/venv`);
  }
  // agent_runner.py is stdlib-only

  // api key
  if (!process.env.ANTHROPIC_API_KEY && !process.env.OPENAI_API_KEY) {
    warn('no LLM key in env - `rune` will sniff existing tools:');
    console.log('  Codex CLI ~/.codex/auth.json, Claude Code ~/.claude/config.json,');
    console.log('  OpenCode ~/.config/opencode/opencode.json, Aider ~/.aider.*.api.key,');
    console.log('  or a ~/.env file with ANTHROPIC_API_KEY / OPENAI_API_KEY.');
  }

  // shim — bash dispatcher. Auto-update: refetches agent_runner.py on each
  // invocation if older than RUNE_UPDATE_INTERVAL_SECS (default 21600 = 6h).
  // Suppressed by RUNE_NO_AUTO_UPDATE=1 or RUNE_OFFLINE=1. Force with
  // RUNE_FORCE_UPDATE=1. We assemble the bash script as a plain string array
  // (no JS template literal) so bash's `${...}` are preserved verbatim.
  const REPO_URL = `https://raw.githubusercontent.com/${REPO}/${BRANCH}`;
  const INSTALLER_URL = `${REPO_URL}/installers/install.js`;
  const RUNNER_URL = `${REPO_URL}/agent_runner.py`;
  fs.mkdirSync(BIN, { recursive: true });
  const rune = path.join(BIN, 'rune');
  const shimLines = [
    '#!/usr/bin/env bash',
    `# fcoin agent launcher + self-updater — generated by install.js`,
    'set -e',
    `INSTALLER_URL='${INSTALLER_URL}'`,
    `RUNNER_URL='${RUNNER_URL}'`,
    `INSTALL_DIR='${ROOT}'`,
    `PYBIN='${py}'`,
    'UPDATE_INTERVAL_SECS="${RUNE_UPDATE_INTERVAL_SECS:-21600}"',
    'AGENT="$INSTALL_DIR/agent_runner.py"',
    '',
    '# Auto-update: refetch $AGENT if stale. Never errors out.',
    '_maybe_update() {',
    '  if [ "${RUNE_OFFLINE:-0}" = "1" ] || [ "${RUNE_NO_AUTO_UPDATE:-0}" = "1" ]; then return 0; fi',
    '  local force="${RUNE_FORCE_UPDATE:-0}"',
    '  if [ "$force" = "1" ] || [ ! -f "$AGENT" ]; then _rune_refetch "$force"; return 0; fi',
    '  local now=$(date +%s)',
    '  local mt=$(stat -c %Y "$AGENT" 2>/dev/null || stat -f %m "$AGENT" 2>/dev/null || echo $now)',
    '  local age=$((now - mt))',
    '  if [ "$age" -gt "$UPDATE_INTERVAL_SECS" ]; then',
    '    echo "[rune] local agent_runner.py is $((age/3600))h old -> checking for updates ..."',
    '    _rune_refetch 0',
    '  fi',
    '}',
    '',
    '_rune_refetch() {',
    '  local new="$AGENT.new"',
    '  local bak="$AGENT.bak.$(date +%s)"',
    '  if curl -fsSL --max-time 30 "$RUNNER_URL" -o "$new" 2>/dev/null; then',
    '    if [ -s "$new" ] && ! cmp -s "$new" "$AGENT" 2>/dev/null; then',
    '      [ -f "$AGENT" ] && mv "$AGENT" "$bak"',
    '      mv "$new" "$AGENT"',
    '      chmod +x "$AGENT"',
    '      echo "[rune] updated agent_runner.py (backup: $bak)"',
    '    else',
    '      rm -f "$new"',
    '      [ "${1:-0}" = "1" ] && echo "[rune] already up-to-date"',
    '    fi',
    '  else',
    '    rm -f "$new"',
    '    [ "${1:-0}" = "1" ] && echo "[rune] upstream unreachable (kept local copy)" >&2',
    '  fi',
    '}',
    '',
    'cmd="$1"',
    'if [ -z "$cmd" ]; then cmd="--help"; fi',
    'case "$cmd" in',
    '  update|--update|-u)',
    `    echo "[rune] fetching latest installer from ${REPO}@${BRANCH} ..."`,
    '    shift',
    `    exec bash -c "curl -fsSL '$INSTALLER_URL' | node -- \\$@"` ,
    '    ;;',
    '  force-update|--force-update)',
    '    echo "[rune] force-refreshing agent_runner.py ..."',
    '    _rune_refetch 1',
    '    exit 0',
    '    ;;',
    '  uninstall|--uninstall)',
    `    echo "[rune] re-running installer with --uninstall ..."`,
    `    exec bash -c "curl -fsSL '$INSTALLER_URL' | node -- --uninstall"`,
    '    ;;',
    '  version|--version|-V)',
    '    echo "fcoin agent_runner.py at: $AGENT"',
    '    echo "installer URL: $INSTALLER_URL"',
    '    echo "auto-update: every $((UPDATE_INTERVAL_SECS/3600))h  (set RUNE_NO_AUTO_UPDATE=1 or RUNE_OFFLINE=1 to disable)"',
    '    if [ -f "$AGENT" ]; then',
    '      local now=$(date +%s)',
    '      local mt=$(stat -c %Y "$AGENT" 2>/dev/null || stat -f %m "$AGENT" 2>/dev/null || echo $now)',
    '      local local_age=$((now - mt))',
    '      echo "local agent_runner.py age: $((local_age/3600))h $((local_age%3600/60))m"',
    '    fi',
    '    ;;',
    '  status|doctor)',
    '    echo "INSTALL_DIR = $INSTALL_DIR"',
    '    echo "AGENT_RUNNER = $PYBIN $AGENT"',
    `    "$PYBIN" "$AGENT" --show-identity 2>/dev/null || true`,
    '    ;;',
    '  --help|-h)',
    '    echo "Usage: rune [command] [args]"',
    '    echo ""',
    '    echo "Subcommands:"',
    '    echo "  update             re-run installer to fetch latest agent_runner.py"',
    '    echo "  force-update       refetch agent_runner.py right now"',
    '    echo "  uninstall          remove the agent + shim"',
    '    echo "  status             show install paths and saved agent identity"',
    '    echo "  version            print install paths + freshness"',
    '    echo ""',
    '    echo "Default: forwards all remaining args to agent_runner.py:"',
    '    echo "  rune --agent-id my-bot"',
    '    echo "  rune --show-identity"',
    '    echo "  rune --dry-run"',
    '    echo "  rune --reset"',
    '    echo ""',
    '    echo "Auto-update: every $((UPDATE_INTERVAL_SECS/3600))h on each invocation,"',
    '    echo "  unless RUNE_NO_AUTO_UPDATE=1 or RUNE_OFFLINE=1 is set in env."',
    `    exec "$PYBIN" "$AGENT" --help`,
    '    ;;',
    '  *)',
    '    _maybe_update',
    `    exec "$PYBIN" "$AGENT" "$@"`,
    '    ;;',
    'esac',
  ];
  fs.writeFileSync(rune, shimLines.join('\n') + '\n');
  fs.chmodSync(rune, 0o755);
  say(`installed ${rune}`);

  // Save the install URL alongside for debugging
  fs.mkdirSync(ROOT, { recursive: true });
  const tracker = [
    `INSTALL_URL=${REPO_URL}`,
    `RUNNER_URL=${RUNNER_URL}`,
    `INSTALLER_URL=${INSTALLER_URL}`,
    `BRANCH=${BRANCH}`,
    `INSTALLED_AT=${new Date().toISOString()}`,
  ].join('\n') + '\n';
  fs.writeFileSync(path.join(ROOT, '.rune-update-url'), tracker);

  // PATH
  const pathHasBin = (process.env.PATH || '').split(':').includes(BIN);
  if (!pathHasBin && !opts.noShell) {
    fs.mkdirSync(path.dirname(RC), { recursive: true });
    fs.appendFileSync(RC,
      `\n# >>> fcoin agent PATH >>>\n` +
      `export PATH="$HOME/.local/bin:$PATH"\n` +
      `# <<< fcoin agent PATH <<<\n`);
    say(`added ${BIN} to PATH in ${RC}`);
  }

  console.log(`
${C.green('fcoin agent installed.')}

Next steps:
  1) restart your shell (or: source ${RC})
  2) set your key (pick one) - usually unnecessary; rune sniffs existing tools:
       export ANTHROPIC_API_KEY=sk-ant-...
       export OPENAI_API_KEY=sk-...
  3) run the agent:
       rune --agent-id my-bot

Options:
  rune                          # run the agent (auto-displays identity on first launch)
  rune --show-identity          # print saved wallet
  rune update                   # self-update: pulls latest agent_runner.py
  rune status                   # show install paths + saved identity
  rune uninstall                # remove the agent + shim
`);
})().catch(e => die(e.stack || e.message));
