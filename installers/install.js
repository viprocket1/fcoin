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

  // shim
  fs.mkdirSync(BIN, { recursive: true });
  const rune = path.join(BIN, 'rune');
  fs.writeFileSync(rune, `#!/usr/bin/env bash\nexec "${py}" "${ROOT}/agent_runner.py" "$@"\n`);
  fs.chmodSync(rune, 0o755);
  say(`installed ${rune}`);

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
  node install.js --help
`);
})().catch(e => die(e.stack || e.message));
