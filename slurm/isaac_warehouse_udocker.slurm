#!/usr/bin/env bash
# =============================================================================
# slurm/isaac_warehouse_udocker.slurm — Isaac Sim + warehouse + robot, WebRTC
#                                       (udocker variant, user-space container)
#
# Runs the NGC Isaac Sim image via udocker (no root, no daemon). Loads the
# warehouse, spawns a robot, and streams over WebRTC — using the same
# slurm/isaac_boot.py as the other variants.
#
# One-time setup (login node):
#     export NGC_API_KEY=nvapi-...
#     bash slurm/udocker_setup_isaac.sh
#
# Run:
#   salloc --partition=$PARTITION --gres=gpu:1 --cpus-per-task=16 --mem=64G --time=04:00:00
#   # then on the compute-node shell:
#   bash slurm/isaac_warehouse_udocker.slurm
# =============================================================================

#SBATCH --job-name=isaac-udocker
#SBATCH --partition=PARTITION_TODO     # <-- EDIT
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=04:00:00
#SBATCH --output=isaac-udocker-%j.log
# #SBATCH --account=ACCOUNT_TODO
# #SBATCH --qos=QOS_TODO

set -euo pipefail

# =============================================================================
# CONFIG (override via env)
# =============================================================================
CONTAINER_NAME="${CONTAINER_NAME:-isaac}"
export UDOCKER_DIR="${UDOCKER_DIR:-$HOME/.udocker}"
export PATH="$HOME/.local/bin:$PATH"

# Repo root on the host (must contain slurm/isaac_boot.py).
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# Host cache dir (persists shader caches across runs).
CACHE_ROOT="${CACHE_ROOT:-$HOME/isaac-cache}"

# Public HTTPS asset mirror so we don't need a Nucleus server.
ISAAC_ASSETS_ROOT="${ISAAC_ASSETS_ROOT:-https://omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/4.5}"

WEBRTC_SIGNALING_PORT=49100
# =============================================================================

log() { echo -e "\033[0;32m[isaac]\033[0m $*"; }
err() { echo -e "\033[0;31m[isaac]\033[0m $*" >&2; }

# --- Preflight ---------------------------------------------------------------
command -v udocker >/dev/null 2>&1 || { err "udocker not on PATH — run slurm/udocker_setup_isaac.sh first"; exit 1; }
udocker ps 2>/dev/null | awk '{print $NF}' | grep -qx "$CONTAINER_NAME" \
    || { err "Container '$CONTAINER_NAME' not found — run slurm/udocker_setup_isaac.sh first"; exit 1; }
[ -f "$REPO_ROOT/slurm/isaac_boot.py" ] || { err "Missing $REPO_ROOT/slurm/isaac_boot.py"; exit 1; }
command -v srun >/dev/null 2>&1 || { err "srun not on PATH — am I inside a Slurm allocation?"; exit 1; }
log "Container:  $CONTAINER_NAME  (UDOCKER_DIR=$UDOCKER_DIR)"
log "Repo root:  $REPO_ROOT -> /workspace in container"
log "Asset root: $ISAAC_ASSETS_ROOT"

# --- Host cache layout -------------------------------------------------------
mkdir -p \
  "$CACHE_ROOT"/kit "$CACHE_ROOT"/ov "$CACHE_ROOT"/pip \
  "$CACHE_ROOT"/glcache "$CACHE_ROOT"/computecache \
  "$CACHE_ROOT"/logs "$CACHE_ROOT"/data "$CACHE_ROOT"/documents

# --- Report client-connect info ----------------------------------------------
NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
NODE_HOST="$(hostname -f 2>/dev/null || hostname)"
log "Compute node: ${NODE_HOST} (${NODE_IP})"
log "Once the log says 'Stage ready. WebRTC stream is live.':"
log "  Open the Isaac Sim WebRTC Streaming Client on your laptop and use:"
log "      Server: ${NODE_IP}    (TCP ${WEBRTC_SIGNALING_PORT}, UDP 47998-48000)"

# --- Launch via srun -> udocker run -----------------------------------------
# srun ensures the GPU cgroup is attached to this step. udocker --nvidia uses
# the host driver libs injected during setup. Binds:
#   $REPO_ROOT -> /workspace (boot script)
#   cache dirs -> the same /root/... + /isaac-sim/kit/cache paths as enroot
log "Starting Isaac Sim + warehouse + robot. First boot builds shader caches."
exec srun --export=ALL udocker run \
    --nvidia \
    --env=ACCEPT_EULA=Y \
    --env=OMNI_KIT_ACCEPT_EULA=YES \
    --env=PRIVACY_CONSENT=Y \
    --env=OMNI_KIT_ALLOW_ROOT=1 \
    --env=ISAAC_ASSETS_ROOT="$ISAAC_ASSETS_ROOT" \
    --env=SCENE_USD="${SCENE_USD:-}" \
    --env=ROBOT_USD="${ROBOT_USD:-}" \
    --env=ROBOT_NAME="${ROBOT_NAME:-turtlebot3_1}" \
    --env=ROBOT_X="${ROBOT_X:-0.0}" \
    --env=ROBOT_Y="${ROBOT_Y:-0.0}" \
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
