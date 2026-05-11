#!/usr/bin/env bash
# =============================================================================
# scripts/start.sh — launch the Nayantra stack
#
# Starts (in this order, in the background):
#   1. RMF stub server      (port 8000)  — only if no real RMF backend
#   2. MCP server           (port 7000)
#   3. Agent API v2         (port 8080)  — includes WebSocket + dashboard
#
# PIDs are written to runtime/pids/*.pid; logs to runtime/logs/*.log.
# Use scripts/stop.sh to terminate all services cleanly.
#
# Usage:
#   bash scripts/start.sh              # full stack
#   bash scripts/start.sh --no-stub    # skip the RMF stub (use real RMF)
#   bash scripts/start.sh --v1         # use agent API v1 (no WebSocket)
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[start]${NC} $*"; }
warn()  { echo -e "${YELLOW}[start]${NC} $*"; }
error() { echo -e "${RED}[start]${NC} $*" >&2; }

# ─── Argument parsing ────────────────────────────────────────────────────────
START_STUB=1
API_MODULE="nayantra.agent.api_v2"
while [ $# -gt 0 ]; do
    case "$1" in
        --no-stub) START_STUB=0 ;;
        --v1)      API_MODULE="nayantra.agent.api" ;;
        -h|--help)
            sed -n '3,16p' "$0"; exit 0 ;;
        *) error "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

# ─── Preflight ───────────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    error ".venv not found — run scripts/setup.sh first"
    exit 1
fi

if [ ! -f ".env" ]; then
    error ".env not found — run scripts/setup.sh first"
    exit 1
fi

mkdir -p runtime/logs runtime/pids

# shellcheck disable=SC1091
source .venv/bin/activate

start_service() {
    local name="$1"
    local cmd="$2"
    local port="$3"
    local pidfile="runtime/pids/${name}.pid"
    local logfile="runtime/logs/${name}.log"

    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        warn "$name already running (PID $(cat "$pidfile"))"
        return 0
    fi

    info "Starting $name on port $port ..."
    nohup bash -c "$cmd" > "$logfile" 2>&1 &
    echo $! > "$pidfile"
    sleep 1

    if ! kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        error "$name failed to start — see $logfile"
        tail -n 20 "$logfile" >&2
        return 1
    fi
}

wait_for_health() {
    local name="$1"
    local url="$2"
    local timeout=30
    info "Waiting for $name @ $url ..."
    for i in $(seq 1 $timeout); do
        if curl -sf "$url" >/dev/null 2>&1; then
            info "$name is healthy"
            return 0
        fi
        sleep 1
    done
    warn "$name did not become healthy within ${timeout}s — check runtime/logs/${name}.log"
    return 1
}

# ─── 1. RMF stub server (optional) ───────────────────────────────────────────
if [ "$START_STUB" -eq 1 ]; then
    start_service "rmf-stub" "python docker/rmf_stub_server.py" 8000
    wait_for_health "rmf-stub" "http://localhost:8000/health" || true
fi

# ─── 2. MCP server ───────────────────────────────────────────────────────────
start_service "mcp-server" "python -m nayantra.mcp.server" 7000
wait_for_health "mcp-server" "http://localhost:7000/health" || true

# ─── 3. Agent API ────────────────────────────────────────────────────────────
start_service "agent-api" "python -m uvicorn ${API_MODULE}:app --host 0.0.0.0 --port 8080" 8080
wait_for_health "agent-api" "http://localhost:8080/health" || true

echo ""
info "All services launched."
echo ""
echo "  Dashboard:    http://localhost:8080/"
echo "  Agent API:    http://localhost:8080/docs"
echo "  MCP Server:   http://localhost:7000/tools"
echo "  WebSocket:    ws://localhost:8080/ws/fleet"
echo ""
echo "  Logs:   runtime/logs/*.log"
echo "  Stop:   bash scripts/stop.sh"
