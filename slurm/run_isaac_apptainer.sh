#!/usr/bin/env bash
# =============================================================================
# slurm/run_isaac_apptainer.sh — Isaac Sim + warehouse + robot, WebRTC
#                                (Apptainer, NO scheduler — direct on the box)
#
# This box has GPUs attached directly, no Slurm, and Apptainer via modules.
# Runs slurm/isaac_boot.py inside the .sif with --nv GPU passthrough.
#
# One-time build first:
#     export NGC_API_KEY=nvapi-...
#     bash slurm/apptainer_build_isaac.sh
#
# Then:
#     bash slurm/run_isaac_apptainer.sh
#
# Override modules / robot pose:
#     CUDA_MODULE=cuda/12.6 ROBOT_X=3 bash slurm/run_isaac_apptainer.sh
# =============================================================================

set -euo pipefail

# =============================================================================
# CONFIG (override via env)
# =============================================================================
APPTAINER_MODULE="${APPTAINER_MODULE:-apptainer/1.4.1}"
CUDA_MODULE="${CUDA_MODULE:-cuda/12.6}"

IMAGE_TAG="${IMAGE_TAG:-4.5.0}"
SIF_PATH="${SIF_PATH:-$HOME/apptainer/isaac-sim-${IMAGE_TAG}.sif}"

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CACHE_ROOT="${CACHE_ROOT:-$HOME/isaac-cache}"

# Public HTTPS asset mirror (no Nucleus server needed).
ISAAC_ASSETS_ROOT="${ISAAC_ASSETS_ROOT:-https://omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/4.5}"

WEBRTC_SIGNALING_PORT=49100
# =============================================================================

log()  { echo -e "\033[0;32m[isaac]\033[0m $*"; }
warn() { echo -e "\033[1;33m[isaac]\033[0m $*"; }
err()  { echo -e "\033[0;31m[isaac]\033[0m $*" >&2; }

# --- Load modules ------------------------------------------------------------
if command -v module >/dev/null 2>&1; then
    log "Loading modules: $APPTAINER_MODULE $CUDA_MODULE"
    module load "$APPTAINER_MODULE" 2>/dev/null || { err "Could not load $APPTAINER_MODULE"; exit 1; }
    module load "$CUDA_MODULE" 2>/dev/null || warn "Could not load $CUDA_MODULE (continuing)"
fi
command -v apptainer >/dev/null 2>&1 || { err "apptainer not on PATH"; exit 1; }

# --- Preflight ---------------------------------------------------------------
[ -f "$SIF_PATH" ]                          || { err "Missing $SIF_PATH — run slurm/apptainer_build_isaac.sh"; exit 1; }
[ -f "$REPO_ROOT/slurm/isaac_boot.py" ]     || { err "Missing $REPO_ROOT/slurm/isaac_boot.py"; exit 1; }
log "SIF:        $SIF_PATH"
log "Repo root:  $REPO_ROOT -> /workspace"
log "Asset root: $ISAAC_ASSETS_ROOT"

if command -v nvidia-smi >/dev/null 2>&1; then
    log "GPU on host:"
    nvidia-smi --query-gpu=index,name,driver_version,memory.total --format=csv,noheader | sed 's/^/    /'
else
    err "nvidia-smi not found after module load — fix CUDA_MODULE and retry."
    exit 1
fi

# --- Writable cache dirs (SIF is read-only; Isaac needs writable cache) ------
mkdir -p \
  "$CACHE_ROOT"/kit "$CACHE_ROOT"/ov "$CACHE_ROOT"/pip \
  "$CACHE_ROOT"/glcache "$CACHE_ROOT"/computecache \
  "$CACHE_ROOT"/logs "$CACHE_ROOT"/data "$CACHE_ROOT"/documents "$CACHE_ROOT"/tmp

# Apptainer runs as the invoking user (NOT root), and shares the host $HOME.
# So cache binds target $HOME/.cache/... ; the in-image /isaac-sim/kit/cache
# needs an explicit writable bind.
BINDS=(
  -B "$REPO_ROOT:/workspace"
  -B "$CACHE_ROOT/kit:/isaac-sim/kit/cache"
  -B "$CACHE_ROOT/ov:$HOME/.cache/ov"
  -B "$CACHE_ROOT/pip:$HOME/.cache/pip"
  -B "$CACHE_ROOT/glcache:$HOME/.cache/nvidia/GLCache"
  -B "$CACHE_ROOT/computecache:$HOME/.nv/ComputeCache"
  -B "$CACHE_ROOT/logs:$HOME/.nvidia-omniverse/logs"
  -B "$CACHE_ROOT/data:$HOME/.local/share/ov/data"
  -B "$CACHE_ROOT/documents:$HOME/Documents"
  -B "$CACHE_ROOT/tmp:/tmp"
)

# --- Connection info ---------------------------------------------------------
NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "Host: $(hostname) ($NODE_IP)"
log "When the log says 'Stage ready. WebRTC stream is live.':"
log "  Isaac Sim WebRTC Streaming Client -> Server: ${NODE_IP}  (TCP ${WEBRTC_SIGNALING_PORT}, UDP 47998-48000)"

# --- Launch ------------------------------------------------------------------
# --nv         : NVIDIA GPU passthrough (libs auto-detected, no manual setup)
# --pwd        : initial working dir inside the container
# --env        : pass scene/robot config into isaac_boot.py
log "Starting Isaac Sim + warehouse + robot. First boot builds shader caches (several min)."
exec apptainer exec --nv \
    --pwd /isaac-sim \
    "${BINDS[@]}" \
    --env ACCEPT_EULA=Y \
    --env OMNI_KIT_ACCEPT_EULA=YES \
    --env PRIVACY_CONSENT=Y \
    --env OMNI_KIT_ALLOW_ROOT=1 \
    --env ISAAC_ASSETS_ROOT="$ISAAC_ASSETS_ROOT" \
    --env ROBOT_NAME="${ROBOT_NAME:-turtlebot3_1}" \
    --env ROBOT_X="${ROBOT_X:-0.0}" \
    --env ROBOT_Y="${ROBOT_Y:-0.0}" \
    --env SCENE_USD="${SCENE_USD:-}" \
    --env ROBOT_USD="${ROBOT_USD:-}" \
    --env EXTRA_ROBOTS_JSON="${EXTRA_ROBOTS_JSON:-}" \
    "$SIF_PATH" \
    bash -lc 'exec ./python.sh /workspace/slurm/isaac_boot.py'
