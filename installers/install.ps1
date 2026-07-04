# fcoin agent installer — Windows PowerShell
# Creates a `rune` command that runs the fcoin prompt-market agent.
#
# Usage (from an elevated or normal PowerShell, recommended Run as Administrator
# only if you want it installed for ALL users):
#   irm https://raw.githubusercontent.com/viprocket1/fcoin/master/installers/install.ps1 | iex
#
# Or locally:
#   powershell -ExecutionPolicy Bypass -File install.ps1 [-Uninstall] [-Dir PATH]
#
# Switches:
#   -Uninstall          remove the install
#   -Dir PATH           install location (default: $HOME\.fcoin\agent)
#   -NoShell            don't append to $PROFILE
#   -Help               this message
#
$ErrorActionPreference = 'Stop'

$Repo     = 'viprocket1/fcoin'
$Branch   = 'master'
$PyMin    = '3.10'

function Say  ([string]$m) { Write-Host "[fcoin] $m" -ForegroundColor Cyan }
function Warn ([string]$m) { Write-Host "[fcoin] $m" -ForegroundColor Yellow }
function Die  ([string]$m) { Write-Host "[fcoin] $m" -ForegroundColor Red; exit 1 }

# ---- args --------------------------------------------------------------------
param(
    [switch]$Uninstall = $false,
    [switch]$NoShell   = $false,
    [switch]$Help      = $false,
    [string]$Dir       = "$HOME\.fcoin\agent"
)

if ($Help) {
    Get-Content $MyInvocation.MyCommand.Path | Select-Object -First 12
    exit 0
}

$InstallDir = $Dir
$BinDir     = "$HOME\.local\bin"
$Profile    = $PROFILE
$Remote     = "https://raw.githubusercontent.com/$Repo/$Branch/agent_runner.py"

# ---- uninstall ---------------------------------------------------------------
if ($Uninstall) {
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    $rune = Join-Path $BinDir 'rune.cmd'
    if (Test-Path $rune) { Remove-Item -Force $rune }
    Say "removed $InstallDir and $rune"
    Say "delete the fcoin block in $Profile if present"
    exit 0
}

# ---- pick python -------------------------------------------------------------
function Pick-Python {
    $candidates = @('python3.exe', 'python.exe', 'py.exe')
    foreach ($cmd in $candidates) {
        $path = (Get-Command $cmd -ErrorAction SilentlyContinue).Source
        if (-not $path) { continue }
        try {
            $ver = & $cmd -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($ver -and ([version]$ver -ge [version]$PyMin)) { return $cmd }
        } catch {}
    }
    return $null
}

$PyBin = Pick-Python
if (-not $PyBin) {
    Warn "Python >= $PyMin not found. Attempting to install via winget..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements | Out-Null
        $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
        $PyBin = Pick-Python
    }
    if (-not $PyBin) {
        Die "install Python $PyMin+ from https://python.org/downloads and re-run"
    }
}
Say "using $(& $PyBin --version)"

# ---- fetch agent_runner.py ---------------------------------------------------
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Say "downloading agent_runner.py from $Repo@$Branch ..."
try {
    Invoke-WebRequest -UseBasicParsing -Uri $Remote -OutFile "$InstallDir\agent_runner.py"
} catch {
    Die "download failed: $($_.Exception.Message)"
    exit 1
}

# ---- venv (only if missing) --------------------------------------------------
if (-not (Test-Path "$InstallDir\venv")) {
    Say "creating venv at $InstallDir\venv ..."
    & $PyBin -m venv "$InstallDir\venv" | Out-Null
}
# agent_runner.py is stdlib-only — no pip install needed.

# ---- API key -----------------------------------------------------------------
if (-not $env:ANTHROPIC_API_KEY -and -not $env:OPENAI_API_KEY) {
    Warn "no LLM key in env - `rune` will sniff existing tools:"
    Say '  Codex CLI $HOME\.codex\auth.json, Claude Code $HOME\.claude\config.json,'
    Say '  OpenCode $HOME\.config\opencode\opencode.json, Aider $HOME\.aider.*.api.key,'
    Say '  or a $HOME\.env file with ANTHROPIC_API_KEY / OPENAI_API_KEY.'
}

# ---- install `rune` shim -----------------------------------------------------
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$runePath = Join-Path $BinDir 'rune.cmd'

# Routing: any subcommand (`update`, `uninstall`, `version`, `status`) goes
# through the installer itself; everything else is forwarded to agent_runner.py.
$RepoUrl     = "https://raw.githubusercontent.com/$Repo/$Branch"
$InstallerUrl = "$RepoUrl/installers/install.ps1"
$RunnerUrl    = "$RepoUrl/agent_runner.py"

# Auto-update: in PowerShell, we use a small PowerShell shim `rune-update.ps1`
# alongside rune.cmd. The cmd calls into it for subcommands only, and on every
# run, calls `_maybe_update` before forwarding args.

$updateScript = @"
`$ErrorActionPreference = 'SilentlyContinue'
`$runner = '$RunnerUrl'
`$dest   = Join-Path '$InstallDir' 'agent_runner.py'
`$bak    = "`$dest.bak.`$((Get-Date -UFormat %s))"
`$new    = "`$dest.new"
`$interval = [int](`$env:RUNE_UPDATE_INTERVAL_SECS)
if (`$interval -le 0) { `$interval = 21600 }

function Test-Stale(`$p, `$secs) {
    if (-not (Test-Path `$p)) { return `$true }
    `$mt = (Get-ItemItem `$p 2>`$null).LastWriteTimeUtc
    if (-not `$mt) { return `$false }
    `$age = ((Get-Date).ToUniversalTime() - `$mt).TotalSeconds
    return (`$age -gt `$secs)
}

if (-not `$env:RUNE_OFFLINE -and -not `$env:RUNE_NO_AUTO_UPDATE) {
    if ((Test-Stale `$dest `$interval) -or `$env:RUNE_FORCE_UPDATE) {
        Write-Host "[rune] checking for updates ..."
        try {
            Invoke-WebRequest -UseBasicParsing -Uri `$runner -OutFile `$new -TimeoutSec 30
            if ((Test-Path `$new) -and ((Get-Item `$new).Length -gt 0)) {
                `$cur = if (Test-Path `$dest) { (Get-Content `$dest -Raw) } else { "" }
                `$incoming = Get-Content `$new -Raw
                if (`$cur -ne `$incoming) {
                    if (Test-Path `$dest) { Move-Item `$dest `$bak -Force }
                    Move-Item `$new `$dest -Force
                    Write-Host "[rune] updated agent_runner.py (backup: `$bak)"
                } else {
                    Remove-Item `$new -Force
                    if (`$env:RUNE_FORCE_UPDATE) { Write-Host "[rune] already up-to-date" }
                }
            }
        } catch {
            if (Test-Path `$new) { Remove-Item `$new -Force }
            if (`$env:RUNE_FORCE_UPDATE) { Write-Host "[rune] upstream unreachable (kept local copy)" }
        }
    }
}
"@

# Inline powershell block — Windows .cmd can't call back into PS easily.
# Embed the same logic in a `PowerShell -Command` line.
$maybeUpdateCmd = 'powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference=\'SilentlyContinue\';$d=Join-Path \'' + $InstallDir + '\' \'agent_runner.py\';$b=\'$d\'.Replace(\'?','a');$n=$d+\'.new\';$i=[int]($env:RUNE_UPDATE_INTERVAL_SECS);if($i -le 0){$i=21600};if(!(Test-Path $d) -or $env:RUNE_FORCE_UPDATE -or (([math]::Round((New-TimeSpan -Start (Get-ItemItem $d).LastWriteTimeUtc -End (Get-Date).ToUniversalTime()).TotalSeconds)) -gt $i)){try{$Env:RUNE_OFFLINE=$null;$Env:RUNE_NO_AUTO_UPDATE=$null;if((-not $Env:RUNE_OFFLINE) -and (-not $Env:RUNE_NO_AUTO_UPDATE)){Invoke-WebRequest -UseBasicParsing -Uri \'' + $RunnerUrl + '\' -OutFile $n -TimeoutSec 30;if(Test-Path $n){if((Get-Content $d -Raw) -ne (Get-Content $n -Raw)){$ts=(Get-Date -UFormat %s);Move-Item -Force $d \"$d.bak.$ts\";Move-Item -Force $n $d;Write-Host \"[rune] updated agent_runner.py (backup: $d.bak.$ts)\"}else{Remove-Item -Force $n}}}}catch{if(Test-Path $n){Remove-Item -Force $n}}}"'

# The single-line PS gets messy; instead, write a sibling .ps1 helper.
$updaterPath = Join-Path $BinDir 'rune-update.ps1'
$updatePs = @"
`$ErrorActionPreference = 'SilentlyContinue'
`$runner = '$RunnerUrl'
`$dest   = Join-Path '$InstallDir' 'agent_runner.py'
`$bak    = "`$dest.bak.`$((Get-Date -UFormat %s))"
`$new    = "`$dest.new"
`$interval = if (`$env:RUNE_UPDATE_INTERVAL_SECS) { [int]`$env:RUNE_UPDATE_INTERVAL_SECS } else { 21600 }
if (`$env:RUNE_OFFLINE -or `$env:RUNE_NO_AUTO_UPDATE) { exit 0 }
if (`$env:RUNE_FORCE_UPDATE) { }  # fall through to fetch
else {
    if (Test-Path `$dest) {
        `$mt  = (Get-Item `$dest).LastWriteTimeUtc
        `$age = ((Get-Date).ToUniversalTime() - `$mt).TotalSeconds
        if (`$age -lt `$interval) { exit 0 }
    }
}
`$ok = `$false
try { Invoke-WebRequest -UseBasicParsing -Uri `$runner -OutFile `$new -TimeoutSec 30 ; `$ok = `$true } catch { }
if (-not `$ok) {
    if (Test-Path `$new) { Remove-Item `$new -Force }
    if (`$env:RUNE_FORCE_UPDATE) { Write-Host '[rune] upstream unreachable (kept local copy)' }
    exit 0
}
`$cur = if (Test-Path `$dest) { Get-Content `$dest -Raw } else { '' }
`$inc = Get-Content `$new -Raw
if (`$cur -eq `$inc) {
    Remove-Item `$new -Force
    if (`$env:RUNE_FORCE_UPDATE) { Write-Host '[rune] already up-to-date' }
    exit 0
}
if (Test-Path `$dest) { Move-Item `$dest `$bak -Force }
Move-Item `$new `$dest -Force
Write-Host "[rune] updated agent_runner.py (backup: `$bak)"
"@
$updatePs | Set-Content -Path $updaterPath -Encoding ASCII

@"
@echo off
rem fcoin agent launcher + self-updater — generated by install.ps1
setlocal
set "INSTALLER_URL=$InstallerUrl"
set "RUNNER_URL=$RunnerUrl"
set "INSTALL_DIR=$InstallDir"
set "PYBIN=$PyBin"
set "UPDATER=%BIN_DIR_PLACEHOLDER%\rune-update.ps1"
if "%1"=="" goto :help
if /I "%1"=="update"        goto :rune_update
if /I "%1"=="force-update"  goto :rune_force_update
if /I "%1"=="uninstall"     goto :rune_uninstall
if /I "%1"=="version"       goto :rune_version
if /I "%1"=="status"        goto :rune_status
if /I "%1"=="--update"      goto :rune_update
if /I "%1"=="--force-update" goto :rune_force_update
if /I "%1"=="--uninstall"   goto :rune_uninstall
if /I "%1"=="--version"     goto :rune_version
if /I "%1"=="--status"      goto :rune_status
if /I "%1"=="--help"        goto :help
if /I "%1"=="-h"            goto :help
goto :run_agent

:rune_update
echo [rune] fetching latest installer from $Repo@$Branch ...
powershell -ExecutionPolicy Bypass -Command "irm '$InstallerUrl' | iex"
goto :eof

:rune_force_update
echo [rune] force-refreshing agent_runner.py ...
set RUNE_FORCE_UPDATE=1
powershell -NoProfile -ExecutionPolicy Bypass -File "%BIN_DIR_PLACEHOLDER%\rune-update.ps1"
set RUNE_FORCE_UPDATE=
goto :eof

:rune_uninstall
echo [rune] re-running installer with -Uninstall ...
powershell -ExecutionPolicy Bypass -Command "irm '$InstallerUrl' | iex" -Uninstall
goto :eof

:rune_version
echo fcoin agent_runner.py at: %INSTALL_DIR%\agent_runner.py
echo installer URL: %INSTALLER_URL%
echo auto-update: every 6h on each invocation. Disable with RUNE_NO_AUTO_UPDATE=1 or RUNE_OFFLINE=1.
if exist "%INSTALL_DIR%\agent_runner.py" (
    powershell -NoProfile -Command "$mt=(Get-Item '%INSTALL_DIR%\agent_runner.py').LastWriteTimeUtc;$age=[int]((New-TimeSpan -Start $mt -End (Get-Date).ToUniversalTime()).TotalSeconds);Write-Host ('local agent_runner.py age: ' + [int]($age/3600) + 'h ' + [int](($age%3600)/60) + 'm')"
)
goto :eof

:rune_status
echo INSTALL_DIR = %INSTALL_DIR%
echo AGENT_RUNNER = %PYBIN% %INSTALL_DIR%\agent_runner.py
"%PYBIN%" "%INSTALL_DIR%\agent_runner.py" --show-identity
goto :eof

:run_agent
powershell -NoProfile -ExecutionPolicy Bypass -File "%BIN_DIR_PLACEHOLDER%\rune-update.ps1"
"%PYBin%" "%INSTALL_DIR%\agent_runner.py" %*
goto :eof

:help
echo Usage: rune [command] [args]
echo.
echo Subcommands:
echo   update             re-run installer to fetch latest agent_runner.py
echo   force-update       refetch agent_runner.py right now
echo   uninstall          remove the agent + shim
echo   status             show install paths and saved agent identity
echo   version            print install paths + freshness
echo.
echo Default forwards all remaining args to agent_runner.py:
echo   rune --agent-id my-bot
echo   rune --show-identity
echo   rune --dry-run
echo   rune --reset
echo.
echo Auto-update every 6h on each invocation. Disable with RUNE_NO_AUTO_UPDATE=1.
"%PYBin%" "%INSTALL_DIR%\agent_runner.py" --help
endlocal
"@ | ForEach-Object { $_ -replace '%BIN_DIR_PLACEHOLDER%', $BinDir } | Set-Content -Path $runePath -Encoding ASCII

Say "installed $runePath"
Say "installed $updaterPath"

# Save the install URL alongside
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
@{
    INSTALL_URL    = $RepoUrl
    INSTALLER_URL  = $InstallerUrl
    RUNNER_URL     = $RunnerUrl
    BRANCH         = $Branch
    INSTALLED_AT   = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json | Set-Content -Path (Join-Path $InstallDir '.rune-update-url') -Encoding ASCII

# ---- ensure BinDir on PATH ---------------------------------------------------
$userPath = [Environment]::GetEnvironmentVariable('Path','User')
if ($userPath -notlike "*$BinDir*") {
    if (-not $NoShell) {
        # Append to PATH for current user
        [Environment]::SetEnvironmentVariable('Path', "$userPath;$BinDir", 'User')
        $env:Path = "$env:Path;$BinDir"
        Say "added $BinDir to PATH (user)"
    }
} else {
    Say "$BinDir already on PATH"
}

# ---- also write a PowerShell function for convenience -----------------------
if (-not $NoShell) {
    if (-not (Test-Path $Profile)) {
        New-Item -ItemType File -Force -Path $Profile | Out-Null
    }
    $marker = 'fcoin agent profile block'
    $snippet = @"

# >>> $marker >>>
function rune {
    & "$PyBin" "$InstallDir\agent_runner.py" @args
}
# <<< $marker <<<
"@
    $existing = Get-Content $Profile -Raw -ErrorAction SilentlyContinue
    if ($existing -notmatch [regex]::Escape($marker)) {
        Add-Content -Path $Profile -Value $snippet
        Say "added `rune` function to $Profile"
    }
}

# ---- done --------------------------------------------------------------------
@"
$(Write-Host "`n" -NoNewline)
$([char]27)[1;32mfcoin agent installed.$([char]27)[0m

Next steps:
  1) open a NEW PowerShell window  (existing windows won't see the new PATH)
  2) set your key (pick one) - usually unnecessary; `rune` sniffs existing tools:
       `$env:ANTHROPIC_API_KEY = "sk-ant-..."
       `$env:OPENAI_API_KEY   = "sk-..."
  3) run the agent:
       rune --agent-id my-bot

Options:
  rune                        # run the agent (auto-displays identity on first launch)
  rune --show-identity        # print saved wallet
  rune update                 # self-update: pulls latest agent_runner.py
  rune status                 # show install paths + saved identity
  rune uninstall              # remove the agent + shim

Uninstall:
  iex ((irm https://raw.githubusercontent.com/$Repo/$Branch/installers/install.ps1) -Replace 'iex','echo')
  # or locally:
  powershell -File install.ps1 -Uninstall
"@
