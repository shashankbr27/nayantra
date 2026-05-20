# =============================================================================
# scripts/setup.ps1 -- one-time bootstrap for Nayantra (Windows / PowerShell)
#
# Mirrors scripts/setup.sh. Run from any directory:
#   .\scripts\setup.ps1
#
# If you get an execution-policy error the first time, run once:
#   Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
# =============================================================================

$ErrorActionPreference = "Stop"
$PROJECT_ROOT = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $PROJECT_ROOT

function Info  { param($m) Write-Host "[setup] $m" -ForegroundColor Green }
function Warn  { param($m) Write-Host "[setup] $m" -ForegroundColor Yellow }
function Fail  { param($m) Write-Host "[setup] $m" -ForegroundColor Red; exit 1 }

# --- 1. Python version check -------------------------------------------------
Info "Checking Python version..."
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { Fail "python is not on PATH. Install Python 3.11+ first." }

$verStr = (& python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')").Trim()
$parts  = $verStr.Split('.')
$major  = [int]$parts[0]; $minor = [int]$parts[1]
if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 11)) {
    Fail "Python $verStr found; this project requires Python 3.11+."
}
Info "Python $verStr OK"

# --- 2. Virtualenv -----------------------------------------------------------
if (-not (Test-Path ".venv")) {
    Info "Creating virtualenv at .venv ..."
    & python -m venv .venv
} else {
    Info ".venv already exists, skipping creation"
}

$venvPy = Join-Path $PROJECT_ROOT ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) { Fail "venv python not found at $venvPy" }

# --- 3. Dependencies ---------------------------------------------------------
Info "Upgrading pip + installing project (this can take a few minutes) ..."
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -e ".[test]" --quiet
if ($LASTEXITCODE -ne 0) { Fail "pip install failed" }
Info "Dependencies installed"

# --- 4. .env file ------------------------------------------------------------
if (-not (Test-Path ".env")) {
    if (Test-Path "config\.env.example") {
        Copy-Item "config\.env.example" ".env"
        Info "Created .env from config/.env.example"
        Warn "  -> Edit .env and fill in ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY"
    } else {
        Warn ".env not created, config/.env.example missing"
    }
} else {
    Info ".env already exists, leaving untouched"
}

# --- 5. Runtime directories --------------------------------------------------
New-Item -ItemType Directory -Force -Path "runtime\logs","runtime\pids","data" | Out-Null
Info "Runtime directories created"

Write-Host ""
Info "Setup complete!"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Edit .env and add your LLM API key"
Write-Host "  2. Make sure ISAAC_SIM_ENABLED=false in .env for pure stub mode"
Write-Host "  3. Run: .\scripts\start.ps1"
