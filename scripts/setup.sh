#!/usr/bin/env bash
# =============================================================================
# scripts/setup.sh — one-time bootstrap for Nayantra
#
# What this does:
#   1. Verifies Python 3.11+
#   2. Creates a .venv virtualenv
#   3. Installs all project dependencies
#   4. Creates the .env file from config/.env.example if missing
#   5. Creates runtime/ (logs + pids) directory
#
# Usage:   bash scripts/setup.sh
# =============================================================================

set -euo pipefail

# Resolve project root (scripts/ lives one level below)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# ─── Colours ─────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[setup]${NC} $*"; }
error() { echo -e "${RED}[setup]${NC} $*" >&2; }

# ─── 1. Python version check ─────────────────────────────────────────────────
info "Checking Python version..."
if ! command -v python3 >/dev/null 2>&1; then
    error "python3 is not on PATH. Install Python 3.11+ first."
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PY_MAJOR=$(echo "$PY_VERSION" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VERSION" | cut -d. -f2)

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    error "Python $PY_VERSION found; this project requires Python 3.11+."
    exit 1
fi
info "Python $PY_VERSION OK"

# ─── 2. Virtualenv ───────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    info "Creating virtualenv at .venv ..."
    python3 -m venv .venv
else
    info ".venv already exists — skipping creation"
fi

# shellcheck disable=SC1091
source .venv/bin/activate
info "Activated $(python -V)"

# ─── 3. Dependencies ─────────────────────────────────────────────────────────
info "Upgrading pip + installing project (this can take a few minutes) ..."
python -m pip install --upgrade pip --quiet
pip install -e ".[test]" --quiet
info "Dependencies installed"

# ─── 4. .env file ────────────────────────────────────────────────────────────
if [ ! -f ".env" ]; then
    if [ -f "config/.env.example" ]; then
        cp config/.env.example .env
        info "Created .env from config/.env.example"
        warn "→ Edit .env and fill in ANTHROPIC_API_KEY (or OPENAI_API_KEY) before starting"
    else
        warn ".env not created — config/.env.example missing"
    fi
else
    info ".env already exists — leaving untouched"
fi

# ─── 5. Runtime directory ────────────────────────────────────────────────────
mkdir -p runtime/logs runtime/pids data
info "Runtime directories created"

echo ""
info "Setup complete!"
echo ""
echo "Next steps:"
echo "  1. Edit .env and add your LLM API key"
echo "  2. (optional) Start Isaac Sim — see docs/getting_started.md"
echo "  3. Run: bash scripts/start.sh"
