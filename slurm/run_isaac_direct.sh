#!/usr/bin/env bash
# =============================================================================
# slurm/run_isaac_direct.sh — Isaac Sim + warehouse + robot, WebRTC
#                             (udocker, NO scheduler — runs directly on the box)
#
# This host (aidc-artgarage) has the GPUs attached directly and no Slurm, so we
# skip srun/salloc and run udocker straight on the node after loading the GPU
# modules.
#
# One-time setup first (see slurm/udocker_setup_isaac.sh):
#     export NGC_API_KEY=nvapi-...
#     bash slurm/udocker_setup_isaac.sh
#
# Then just:
#     bash slurm/run_isaac_direct.sh
#
# Override module names if they differ on your box:
#     CUDA_MODULE=cuda/12.4 GCC_MODULE=gcc/11.2 bash slurm/run_isaac_direct.sh
# =============================================================================

set -euo pipefail

# =============================================================================
# CONFIG (override via env)
# =============================================================================
CONTAINER_NAME="${CONTAINER_NAME:-isaac}"
export UDOCKER_DIR="${UDOCKER_DIR:-$HOME/.udocker}"
export PATH="$HOME/.local/bin:$PATH"

# Modules to load for GPU access. EDIT if your box names them differently.
CUDA_MODULE="${CUDA_MODULE:-cuda}"
GCC_MODULE="${GCC_MODULE:-gcc}"

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CACHE_ROOT="${CACHE_ROOT:-$HOME/isaac-cache}"

# Public HTTPS asset mirror (no Nucleus server needed).
ISAAC_ASSETS_ROOT="${ISAAC_ASSETS_ROOT:-https://omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/4.5}"

WEBRTC_SIGNALING_PORT=49100
# =============================================================================

log()  { echo -e "\033[0;32m[isaac]\033[0m $*"; }
warn() { echo -e "\033[1;33m[isaac]\033[0m $*"; }
err()  { echo -e "\033[0;31m[isaac]\033[0m $*" >&2; }

# --- Load GPU modules --------------------------------------------------------
if command -v module >/dev/null 2>&1; then
    log "Loading modules: $GCC_MODULE $CUDA_MODULE"
    module load "$GCC_MODULE" 2>/dev/null || warn "Could not load $GCC_MODULE (continuing)"
    module load "$CUDA_MODULE" 2>/dev/null || warn "Could not load $CUDA_MODULE (continuing)"
else
    warn "No 'module' command found — assuming CUDA is already on PATH."
fi

# --- Preflight ---------------------------------------------------------------
command -v udocker >/dev/null 2>&1 || { err "udocker not on PATH — run slurm/udocker_setup_isaac.sh first"; exit 1; }
udocker ps 2>/dev/null | awk '{print $NF}' | grep -qx "$CONTAINER_NAME" \
    || { err "Container '$CONTAINER_NAME' not found — run slurm/udocker_setup_isaac.sh first"; exit 1; }
[ -f "$REPO_ROOT/slurm/isaac_boot.py" ] || { err "Missing $REPO_ROOT/slurm/isaac_boot.py"; exit 1; }

if command -v nvidia-smi >/dev/null 2>&1; then
    log "GPU on host:"
    nvidia-smi --query-gpu=index,name,driver_version,memory.total --format=csv,noheader | sed 's/^/    /'
else
    err "nvidia-smi not found even after module load. Fix module names via CUDA_MODULE=... and retry."
    exit 1
fi

# --- Refresh NVIDIA libs inside the container (driver may differ per node) ---
log "Re-injecting NVIDIA host libraries into '$CONTAINER_NAME' (idempotent)"
udocker setup --nvidia "$CONTAINER_NAME" 2>/dev/null || warn "setup --nvidia returned non-zero"

# --- Verify the GPU is visible INSIDE the container before the heavy launch --
log "Verifying GPU visibility inside the container..."
if udocker run --nvidia "$CONTAINER_NAME" nvidia-smi -L >/dev/null 2>&1; then
    log "GPU is visible inside the container."
else
    err "GPU NOT visible inside the container. Run this to debug:"
    err "    udocker run --nvidia $CONTAINER_NAME nvidia-smi"
    err "If it fails, try: udocker setup --execmode=F4 $CONTAINER_NAME"
    exit 1
fi

# --- Host cache layout -------------------------------------------------------
mkdir -p \
  "$CACHE_ROOT"/kit "$CACHE_ROOT"/ov "$CACHE_ROOT"/pip \
  "$CACHE_ROOT"/glcache "$CACHE_ROOT"/computecache \
  "$CACHE_ROOT"/logs "$CACHE_ROOT"/data "$CACHE_ROOT"/documents

# --- Connection info ---------------------------------------------------------
NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "Host: $(hostname) ($NODE_IP)"
log "Once you see 'Stage ready. WebRTC stream is live.':"
log "  Isaac Sim WebRTC Streaming Client -> Server: ${NODE_IP}  (TCP ${WEBRTC_SIGNALING_PORT}, UDP 47998-48000)"

# --- Launch ------------------------------------------------------------------
log "Starting Isaac Sim + warehouse + robot. First boot builds shader caches (several min)."
exec udocker run \
    --nvidia \
    --env=ACCEPT_EULA=Y \
    --env=OMNI_KIT_ACCEPT_EULA=YES \
    --env=PRIVACY_CONSENT=Y \
    --env=OMNI_KIT_ALLOW_ROOT=1 \
    --env=ISAAC_ASSETS_ROOT="$ISAAC_ASSETS_ROOT" \
    --env=ROBOT_NAME="${ROBOT_NAME:-turtlebot3_1}" \
    --env=ROBOT_X="${ROBOT_X:-0.0}" \
    --env=ROBOT_Y="${ROBOT_Y:-0.0}" \
    --env=SCENE_USD="${SCENE_USD:-}" \
    --env=ROBOT_USD="${ROBOT_USD:-}" \
    --env=EXTRA_ROBOTS_JSON="${EXTRA_ROBOTS_JSON:-}" \
    --volume="$REPO_ROOT:/workspace" \
    --volume="$CACHE_ROOT/kit:/isaac-sim/kit/cache" \
    --volume="$CACHE_ROOT/ov:/root/.cache/ov" \
    --volume="$CACHE_ROOT/pip:/root/.cache/pip" \
    --volume="$CACHE_ROOT/glcache:/root/.cache/nvidia/GLCache" \
    --volume="$CACHE_ROOT/computecache:/root/.nv/ComputeCache" \
    --volume="$CACHE_ROOT/logs:/root/.nvidia-omniverse/logs" \
    --volume="$CACHE_ROOT/data:/root/.local/share/ov/data" \
    --volume="$CACHE_ROOT/documents:/root/Documents" \
    --workdir=/isaac-sim \
    "$CONTAINER_NAME" \
    bash -lc 'exec ./python.sh /workspace/slurm/isaac_boot.py'
