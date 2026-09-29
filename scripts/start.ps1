# =============================================================================
# scripts/start.ps1 -- launch the Nayantra stack (Windows / PowerShell)
#
# Mirrors scripts/start.sh. Starts three background processes:
#   1. Nayantra Core        (port 8000)  control plane + operator UI
#   2. MCP server           (port 7000)  the LLM's tools
#   3. Agent API v2         (port 8080)  natural-language agent
#
# PIDs are written to runtime\pids\*.pid; logs to runtime\logs\*.log.
# Use scripts\stop.ps1 to terminate all services cleanly.
#
# Usage:
#   .\scripts\start.ps1                # full stack
#   .\scripts\start.ps1 -NoCore        # core runs elsewhere (NAYANTRA_CORE_URL)
#   .\scripts\start.ps1 -V1            # use agent API v1 (no WebSocket)
#   (-NoStub is accepted as a deprecated alias of -NoCore)
# =============================================================================

param(
    [switch]$NoCore,
    [switch]$NoStub,
    [switch]$V1
)
if ($NoStub) { $NoCore = $true }

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
    # 120s: a cold start on Windows imports heavy libs (anthropic, openai,
    # google-genai, fastapi, uvicorn) which can take well over 30s the first time.
    param([string]$Name, [string]$Url, [int]$TimeoutSec = 120)
    Info "Waiting for $Name @ $Url (first start can take ~60-90s) ..."
    for ($i = 0; $i -lt $TimeoutSec; $i++) {
        try {
            $r = Invoke-WebRequest -Uri $Url -TimeoutSec 2 -UseBasicParsing -ErrorAction Stop
            if ($r.StatusCode -eq 200) { Info "$Name is healthy"; return }
        } catch { Start-Sleep -Seconds 1 }
    }
    Warn "$Name did not become healthy within ${TimeoutSec}s, check runtime\logs\$Name.*.log"
}

# --- Launch ALL services first (concurrently) --------------------------------
# They don't need each other UP to start importing: the MCP client and the
# agent connect lazily on first use. Launching all three at once overlaps their
# cold-start imports, so total startup ~= the slowest single one, not the sum.
if (-not $NoCore) {
    if (-not (Test-Path "web\dist\index.html") -and (Get-Command npm -ErrorAction SilentlyContinue)) {
        Info "Building the operator UI (first run) ..."
        npm --prefix web install --no-audit --no-fund | Out-Null
        npm --prefix web run build | Out-Null
    }
    Start-NayantraService "core" @("-m","nayantra.core.server") 8000
}
Start-NayantraService "mcp-server" @("-m","nayantra.mcp.server") 7000
Start-NayantraService "agent-api" @("-m","uvicorn","${apiModule}:app","--host","127.0.0.1","--port","8080") 8080

# --- Then wait for health (imports are now happening in parallel) ------------
if (-not $NoCore) {
    Wait-Healthy "core" "http://localhost:8000/api/v1/health"
}
Wait-Healthy "mcp-server" "http://localhost:7000/health"
Wait-Healthy "agent-api" "http://localhost:8080/health"

Write-Host ""
Info "All services launched."
Write-Host ""
Write-Host "  Operator UI:  http://localhost:8000/"
Write-Host "  Core API:     http://localhost:8000/docs"
Write-Host "  MCP Server:   http://localhost:7000/tools"
Write-Host "  Agent API:    http://localhost:8080/docs"
Write-Host ""
Write-Host "  Logs:   runtime\logs\*.log"
Write-Host "  Stop:   .\scripts\stop.ps1"
