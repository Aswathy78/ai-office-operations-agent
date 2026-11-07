<#
.SYNOPSIS
  Start, stop or check the whole project the one reliable way.

.DESCRIPTION
  Replaces the "which terminal, which Python, which port" guesswork that caused most
  set-up errors:
    * always uses .venv-py311 (the other environments are missing packages);
    * checks the set-up first (scripts\dev.ps1 -Check shows the same report on its own);
    * stops old copies of the API and front end, so only one of each is running;
    * the API restarts itself when a .py file OR backend\.env is saved;
    * the front end is pointed at whatever port the API was started on.

.EXAMPLE
  .\scripts\dev.ps1                 # start the API and the front end
  .\scripts\dev.ps1 -Check          # only check the set-up (add -Mail to test the e-mail login once)
  .\scripts\dev.ps1 -Stop           # stop them
  .\scripts\dev.ps1 -DryRun         # show what it would do
#>
[CmdletBinding()]
param(
    [switch]$Check,        # only run the set-up check
    [switch]$Mail,         # with -Check: also try the e-mail login once (nothing is sent)
    [switch]$Stop,         # only stop the servers
    [switch]$DryRun,       # say what would happen, change nothing
    [switch]$KeepOthers,   # only stop what is on our ports; leave other copies running
    [switch]$NoFrontend,   # start only the API
    [int]$ApiPort = 8000,
    [int]$FrontendPort = 5173
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root 'backend'
$python = Join-Path $root '.venv-py311\Scripts\python.exe'

function Say([string]$Text) { Write-Host $Text }
function Fail([string]$Text) { Write-Host "ERROR: $Text" -ForegroundColor Red; exit 1 }

# ---- finding and stopping our servers --------------------------------------------------
# A stop script must never be able to hit the wrong process. So it only ever touches
# python / node / cmd processes, matches their command lines strictly, and treats a parent as
# a parent only if it was started before the child (Windows reuses process ids, so a stale
# parent id can point at something unrelated).

function Get-ListenerPids([int]$Port) {
    $ids = @()
    try {
        $ids += @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop |
                  ForEach-Object { [int]$_.OwningProcess })
    } catch { }
    if ($ids.Count -eq 0) {   # not always allowed in a restricted shell; netstat always works
        $ids += @(& netstat.exe -ano -p tcp | ForEach-Object {
            if ($_ -match "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)") { [int]$Matches[1] }
        })
    }
    return @($ids | Where-Object { $_ -gt 0 } | Sort-Object -Unique)
}

function Test-InProject([string]$CommandLine) {
    return [bool]$CommandLine -and $CommandLine.IndexOf($root, [StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Test-ServerProcess($Proc, $ById) {
    # True only for THIS project's API or front end. Another project's `uvicorn main:app`
    # must never match, so the project folder has to appear in the process's command line
    # or in its launcher's (uvicorn's worker is started by a launcher that carries the path).
    if (-not $Proc -or -not $Proc.CommandLine) { return $false }
    if ($Proc.Name -notmatch '^(python|node|cmd)\.exe$') { return $false }
    $line = $Proc.CommandLine
    if ($Proc.Name -eq 'python.exe' -and $line -match 'uvicorn\s+main:app') {
        if (Test-InProject $line) { return $true }
        $parent = $ById[[int]$Proc.ParentProcessId]
        return [bool]($parent -and $parent.CreationDate -le $Proc.CreationDate -and (Test-InProject $parent.CommandLine))
    }
    if ($Proc.Name -eq 'node.exe' -and $line -match 'vite' -and (Test-InProject $line)) { return $true }
    return $false
}

function Get-ServerTree([int]$ListenerPid, $All) {
    # The listener, the launcher processes above it and every process below those.
    $byId = @{}; foreach ($p in $All) { $byId[[int]$p.ProcessId] = $p }
    $top = $byId[$ListenerPid]
    if (-not $top) { return @() }
    while ($true) {
        $parent = $byId[[int]$top.ParentProcessId]
        $isParent = $parent -and ($parent.CreationDate -le $top.CreationDate) -and `
                    ($parent.Name -match '^(python|node|cmd)\.exe$') -and `
                    ($parent.CommandLine -match 'uvicorn|vite|npm-cli\.js|/c\s+vite')
        if ($isParent) { $top = $parent } else { break }
    }
    # A plain array: wrapping a generic List in @() throws "Argument types do not match"
    # in Windows PowerShell 5.1.
    $tree = @($top)
    for ($i = 0; $i -lt $tree.Count; $i++) {
        $current = $tree[$i]
        foreach ($child in @($All | Where-Object {
                $_.ParentProcessId -eq $current.ProcessId -and $_.CreationDate -ge $current.CreationDate -and
                $_.Name -match '^(python|node|cmd)\.exe$' })) { $tree += $child }
    }
    return $tree
}

function Stop-ProjectServers {
    # Returns the ids of processes that hold one of our ports but are NOT this project's
    # servers: they are left alone, and the caller reports that the port is taken.
    $all = @(Get-CimInstance Win32_Process)
    $byId = @{}; foreach ($proc in $all) { $byId[[int]$proc.ProcessId] = $proc }
    $listeners = @()
    foreach ($port in @($ApiPort, $FrontendPort)) { $listeners += Get-ListenerPids $port }
    if (-not $KeepOthers) {   # also any other copy of our API or front end, on any port
        $listeners += @($all | Where-Object { Test-ServerProcess $_ $byId } | ForEach-Object { [int]$_.ProcessId })
    }
    $victims = @{}
    $foreign = @()
    foreach ($id in @($listeners | Sort-Object -Unique)) {
        $tree = @(Get-ServerTree $id $all)
        # Ours only if the process, or the launcher that started it, is one of our servers.
        if (-not @($tree | Where-Object { Test-ServerProcess $_ $byId })) {
            $owner = $byId[[int]$id]
            $name = if ($owner) { $owner.Name } else { 'another process' }
            Say "Port in use by $name (process $id), which is not one of this project's servers - leaving it alone."
            $foreign += $id
            continue
        }
        foreach ($proc in $tree) { $victims[[int]$proc.ProcessId] = $proc }
    }
    if ($victims.Count -eq 0) { Say 'No old servers to stop.' }
    foreach ($proc in @($victims.Values | Sort-Object ProcessId -Descending)) {
        $label = "$($proc.Name) $($proc.ProcessId)"
        if ($DryRun) { Say "[dry run] would stop $label"; continue }
        Say "Stopping $label ..."
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if (-not $DryRun -and $victims.Count -gt 0) { Start-Sleep -Seconds 2 }
    return $foreign
}

function Wait-Url([string]$Url, [int]$Seconds) {
    $until = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $until) {
        try { Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 | Out-Null; return $true } catch { Start-Sleep -Seconds 2 }
    }
    return $false
}

# --- 1. the right Python ----------------------------------------------------------------
if (-not (Test-Path $python)) {
    Fail ("The project's Python environment was not found at $python`n" +
          "Create it once with:`n  py -3.11 -m venv .venv-py311`n" +
          "  .venv-py311\Scripts\python.exe -m pip install -r backend\requirements.txt")
}

if ($Stop) { $null = Stop-ProjectServers; exit 0 }

# --- 2. check the set-up --------------------------------------------------------------
Push-Location $backend
try {
    $doctorArgs = @('-m', 'app.doctor')
    if ($Mail) { $doctorArgs += '--mail' }
    & $python @doctorArgs
    $doctorStatus = $LASTEXITCODE
} finally { Pop-Location }
if ($Check) { exit $doctorStatus }
if ($doctorStatus -ne 0) { Fail 'Fix the FAIL items above first (each says how), then run this again.' }

# --- 3. one copy of each server ---------------------------------------------------------
$blocked = @(Stop-ProjectServers)
if ($blocked.Count -gt 0 -and -not $DryRun) {
    Fail ("Port $ApiPort or $FrontendPort is held by something that is not this project's server (see above).`n" +
          "Stop it, or use other ports:  .\scripts\dev.ps1 -ApiPort 8001 -FrontendPort 5174")
}

if (-not (Test-Path (Join-Path $root 'node_modules\react-markdown'))) {
    if ($DryRun) { Say '[dry run] would run: npm install' }
    else { Say 'Installing front-end packages (first run only) ...'; Push-Location $root; npm install; Pop-Location }
}

# --- 4. start --------------------------------------------------------------------------
# --reload-include .env : saving backend\.env restarts the API, so a new password or key is
#                         never ignored. --timeout-graceful-shutdown : a restart cannot hang.
$apiArgs = @('-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', "$ApiPort",
             '--reload', '--reload-include', '.env', '--timeout-graceful-shutdown', '5')
if ($DryRun) {
    Say "[dry run] would start the API: $python $($apiArgs -join ' ')   (in $backend)"
    if (-not $NoFrontend) { Say "[dry run] would start the front end on port $FrontendPort pointed at http://127.0.0.1:$ApiPort" }
    exit 0
}

Say "Starting the API on port $ApiPort ..."
Start-Process -FilePath $python -ArgumentList $apiArgs -WorkingDirectory $backend | Out-Null

if (-not $NoFrontend) {
    Say "Starting the front end on port $FrontendPort ..."
    $env:VITE_API_BASE_URL = "http://127.0.0.1:$ApiPort"
    Start-Process -FilePath 'cmd.exe' -WorkingDirectory $root `
        -ArgumentList @('/c', 'npm', 'run', 'dev', '--', '--port', "$FrontendPort", '--strictPort') | Out-Null
}

# --- 5. wait until they answer ---------------------------------------------------------
$apiUp = Wait-Url "http://127.0.0.1:$ApiPort/docs" 90
$frontUp = $NoFrontend -or (Wait-Url "http://localhost:$FrontendPort/" 90)
Say ''
if ($apiUp) { Say "API:        http://127.0.0.1:$ApiPort   (docs at /docs)" } else { Say "API did not answer on port $ApiPort - see its window for the error." }
if (-not $NoFrontend) {
    if ($frontUp) { Say "Front end:  http://localhost:$FrontendPort" } else { Say "Front end did not answer on port $FrontendPort - see its window for the error." }
}
Say 'Demo logins are listed in backend\app\db\seed_accounts.py (they are created automatically in development).'
Say 'Stop everything with:  .\scripts\dev.ps1 -Stop'
if (-not ($apiUp -and $frontUp)) { exit 1 }
