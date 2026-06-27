#!/usr/bin/env bash
# =============================================================================
# scripts/install_isaac_pip.sh — native (NO Docker) Isaac Sim 6.0 install
#
# For an Ubuntu 24.04 workstation with Python 3.12 and an NVENC+RT-core GPU
# (RTX 6000 Ada, etc.). Installs Isaac Sim from NVIDIA's pip index into a
# user-space venv — nothing system-level, no Docker, no sudo.
#
# Requirements: glibc >= 2.34 and Python 3.12 (Ubuntu 24.04 ships both).
# Older RHEL 8 / glibc 2.28 hosts cannot install these wheels.
#
# Usage:
#   bash install_isaac_pip.sh
#   # then:
#   source ~/isaacsim-env/bin/activate
#   export OMNI_KIT_ACCEPT_EULA=YES PRIVACY_CONSENT=Y
#   isaacsim isaacsim.exp.full.streaming --no-window
# =============================================================================

set -euo pipefail

ISAAC_VER="${ISAAC_VER:-6.0.0.1}"          # pip version (docs: 6.0.0.1). Override if pip says no match.
# Install EVERYTHING under BASE_DIR (default: current dir), NOT $HOME — the venv
# and the ~10-20 GB uv download cache both go here so home quota isn't touched.
# Run this from inside your project dir (e.g. .../or_sig) or pass BASE_DIR=/abs/path.
BASE_DIR="${BASE_DIR:-$PWD}"
ENV_DIR="${ENV_DIR:-$BASE_DIR/isaacsim-env}"
export UV_CACHE_DIR="${UV_CACHE_DIR:-$BASE_DIR/.uv-cache}"

log() { echo -e "\033[0;32m[install]\033[0m $*"; }
err() { echo -e "\033[0;31m[install]\033[0m $*" >&2; }

# --- Isaac pins a Python version per release: 6.x->3.12, 5.x->3.11, 4.x->3.10 -
case "$ISAAC_VER" in
    5.*) PYVER="${PYVER:-3.11}" ;;
    4.*) PYVER="${PYVER:-3.10}" ;;
    *)   PYVER="${PYVER:-3.12}" ;;
esac
log "Isaac $ISAAC_VER -> Python $PYVER"

# --- uv: fast, user-space venv manager (also fetches the right Python) -------
if ! command -v uv >/dev/null 2>&1; then
    log "Installing uv (user-space, no sudo)"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
command -v uv >/dev/null 2>&1 || { err "uv install failed; add ~/.local/bin to PATH"; exit 1; }

# --- Create the venv (clean) + install --------------------------------------
log "Creating Python $PYVER venv at $ENV_DIR (uv fetches $PYVER if not on the system)"
rm -rf "$ENV_DIR"                 # clean recreate (handles a Python-version switch)
uv venv --python "$PYVER" --seed "$ENV_DIR"
# shellcheck disable=SC1091
source "$ENV_DIR/bin/activate"

log "Installing isaacsim==$ISAAC_VER into $ENV_DIR (cache: $UV_CACHE_DIR; 10-20 min)"
# Flags that make uv as lenient as plain pip (which NVIDIA's docs assume):
#   --index-strategy unsafe-best-match : pull deps (mujoco-usd-converter) from
#       public PyPI when not on NVIDIA's index (not first-index-only).
#   --prerelease=allow                 : permit pinned pre-release deps
#       (tinyobjloader==2.0.0rc13, etc.).
if ! uv pip install "isaacsim[all,extscache]==$ISAAC_VER" \
        --extra-index-url https://pypi.nvidia.com \
        --index-strategy unsafe-best-match \
        --prerelease=allow; then
    err "Install failed for version $ISAAC_VER. List available versions with:"
    err "  uv pip install 'isaacsim==' --extra-index-url https://pypi.nvidia.com --index-strategy unsafe-best-match"
    err "then re-run:  ISAAC_VER=<x.y.z.w> bash $0"
    exit 1
fi

log "Done. Isaac Sim installed in $ENV_DIR"
echo ""
echo "Run headless WebRTC streaming:"
echo "  source $ENV_DIR/bin/activate"
echo "  export OMNI_KIT_ACCEPT_EULA=YES PRIVACY_CONSENT=Y"
echo "  isaacsim isaacsim.exp.full.streaming --no-window"
echo ""
echo "Then connect the Isaac Sim WebRTC Streaming Client to this host:"
echo "  Server: $(hostname -I 2>/dev/null | awk '{print $1}')"
