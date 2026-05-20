# =============================================================================
# scripts/stop.ps1 -- gracefully terminate all Nayantra services (Windows)
#
# Reads every PID file in runtime\pids\ and sends Stop-Process.
# =============================================================================

$ErrorActionPreference = "Continue"
$PROJECT_ROOT = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $PROJECT_ROOT

function Info { param($m) Write-Host "[stop] $m" -ForegroundColor Green }
function Warn { param($m) Write-Host "[stop] $m" -ForegroundColor Yellow }

if (-not (Test-Path "runtime\pids")) {
    Info "No PID directory, nothing to stop"
    exit 0
}

$stopped = 0
Get-ChildItem "runtime\pids\*.pid" -ErrorAction SilentlyContinue | ForEach-Object {
    $name   = $_.BaseName
    $procId = (Get-Content $_.FullName -ErrorAction SilentlyContinue) -as [int]

    if ($procId) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            Info "Stopping $name (PID $procId)..."
            try {
                Stop-Process -Id $procId -Force -ErrorAction Stop
                $stopped++
            } catch {
                Warn "Failed to stop $name : $_"
            }
        } else {
            Warn "$name PID $procId not running"
        }
    }
    Remove-Item $_.FullName -ErrorAction SilentlyContinue
}

Info "Stopped $stopped service(s)"
