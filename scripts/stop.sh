#!/usr/bin/env bash
# =============================================================================
# scripts/stop.sh — gracefully terminate all Nayantra services
#
# Reads every PID file in runtime/pids/ and sends SIGTERM, then SIGKILL on
# any that don't exit within 5 seconds.
# =============================================================================

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info() { echo -e "${GREEN}[stop]${NC} $*"; }
warn() { echo -e "${YELLOW}[stop]${NC} $*"; }

if [ ! -d "runtime/pids" ]; then
    info "No PID directory — nothing to stop"
    exit 0
fi

stopped=0
for pidfile in runtime/pids/*.pid; do
    [ -f "$pidfile" ] || continue
    name=$(basename "$pidfile" .pid)
    pid=$(cat "$pidfile")

    if kill -0 "$pid" 2>/dev/null; then
        info "Stopping $name (PID $pid)..."
        kill -TERM "$pid" 2>/dev/null || true
        for i in 1 2 3 4 5; do
            sleep 1
            kill -0 "$pid" 2>/dev/null || break
        done
        if kill -0 "$pid" 2>/dev/null; then
            warn "$name did not exit — sending SIGKILL"
            kill -KILL "$pid" 2>/dev/null || true
        fi
        stopped=$((stopped + 1))
    else
        warn "$name PID $pid not running"
    fi
    rm -f "$pidfile"
done

info "Stopped $stopped service(s)"
