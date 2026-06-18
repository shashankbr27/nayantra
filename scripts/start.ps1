# =============================================================================
# scripts/start.ps1 -- launch the Nayantra stack (Windows / PowerShell)
#
# Mirrors scripts/start.sh. Starts three background processes:
#   1. RMF stub server      (port 8000)
#   2. MCP server           (port 7000)
#   3. Agent API v2         (port 8080)
#
# PIDs are written to runtime\pids\*.pid; logs to runtime\logs\*.log.
# Use scripts\stop.ps1 to terminate all services cleanly.
#
# Usage:
#   .\scripts\start.ps1                # full stack with stub
#   .\scripts\start.ps1 -NoStub        # skip the RMF stub
#   .\scripts\start.ps1 -V1            # use agent API v1 (no WebSocket)
# =============================================================================

param(
    [switch]$NoStub,
    [switch]$V1
)

$ErrorActionPreference = "Stop"
$PROJECT_ROOT = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $PROJECT_ROOT

function Info  { param($m) Write-Host "[start] $m" -ForegroundColor Green }
function Warn  { param($m) Write-Host "[start] $m" -ForegroundColor Yellow }
function Fail  { param($m) Write-Host "[start] $m" -ForegroundColor Red; exit 1 }

# --- Preflight ---------------------------------------------------------------
if (-not (Test-Path ".venv\Scripts\python.exe")) { Fail ".venv not found, run scripts\setup.ps1 first" }
if (-not (Test-Path ".env"))                     { Fail ".env not found, run scripts\setup.ps1 first" }

New-Item -ItemType Directory -Force -Path "runtime\logs","runtime\pids" | Out-Null
$venvPy = Join-Path $PROJECT_ROOT ".venv\Scripts\python.exe"

$apiModule = if ($V1) { "nayantra.agent.api" } else { "nayantra.agent.api_v2" }

function Start-NayantraService {
    param(
        [string]$Name,
        [string[]]$ArgList,
        [int]$Port
    )
    $pidfile = "runtime\pids\$Name.pid"
    $outLog  = "runtime\logs\$Name.out.log"
    $errLog  = "runtime\logs\$Name.err.log"

    if (Test-Path $pidfile) {
        $existing = (Get-Content $pidfile -ErrorAction SilentlyContinue) -as [int]
        if ($existing -and (Get-Process -Id $existing -ErrorAction SilentlyContinue)) {
            Warn "$Name already running (PID $existing)"
            return
        }
    }

    Info "Starting $Name on port $Port ..."
    $proc = Start-Process -FilePath $venvPy -ArgumentList $ArgList `
            -NoNewWindow -PassThru `
            -WorkingDirectory $PROJECT_ROOT `
            -RedirectStandardOutput $outLog `
            -RedirectStandardError  $errLog
    $proc.Id | Out-File $pidfile -Encoding ASCII

    Start-Sleep -Seconds 1
    if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
        Warn "$Name failed to start, last 20 lines of $errLog :"
        Get-Content $errLog -Tail 20 | ForEach-Object { Write-Host "    $_" }
    }
}

function Wait-Healthy {
    param([string]$Name, [string]$Url, [int]$TimeoutSec = 30)
    Info "Waiting for $Name @ $Url ..."
    for ($i = 0; $i -lt $TimeoutSec; $i++) {
        try {
            $r = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($r.StatusCode -eq 200) { Info "$Name is healthy"; return }
        } catch { Start-Sleep -Seconds 1 }
    }
    Warn "$Name did not become healthy within ${TimeoutSec}s, check runtime\logs\$Name.*.log"
}

# --- 1. RMF stub -------------------------------------------------------------
if (-not $NoStub) {
    Start-NayantraService "rmf-stub" @("docker\rmf_stub_server.py") 8000
    Wait-Healthy "rmf-stub" "http://localhost:8000/health"
}

# --- 2. MCP server -----------------------------------------------------------
Start-NayantraService "mcp-server" @("-m","nayantra.mcp.server") 7000
Wait-Healthy "mcp-server" "http://localhost:7000/health"

# --- 3. Agent API ------------------------------------------------------------
Start-NayantraService "agent-api" @("-m","uvicorn","${apiModule}:app","--host","127.0.0.1","--port","8080") 8080
Wait-Healthy "agent-api" "http://localhost:8080/health"

Write-Host ""
Info "All services launched."
Write-Host ""
Write-Host "  Dashboard:    http://localhost:8080/"
Write-Host "  Agent API:    http://localhost:8080/docs"
Write-Host "  MCP Server:   http://localhost:7000/tools"
Write-Host "  WebSocket:    ws://localhost:8080/ws/fleet"
Write-Host ""
Write-Host "  Logs:   runtime\logs\*.log"
Write-Host "  Stop:   .\scripts\stop.ps1"
