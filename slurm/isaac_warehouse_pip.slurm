#!/usr/bin/env bash
# =============================================================================
# slurm/isaac_warehouse_pip.slurm — Isaac Sim + warehouse + robot, WebRTC
#                                   (pip-install variant, NO container)
#
# This is the no-container equivalent of slurm/isaac_warehouse.slurm. It uses
# the venv created by slurm/pip_install_isaac.sh, runs slurm/isaac_boot.py
# directly under that venv's Python, and otherwise behaves identically.
#
# One-time setup (login node):
#     bash slurm/pip_install_isaac.sh
#
# Run modes:
#
#   A) INTERACTIVE (recommended for bring-up):
#        salloc --partition=$PARTITION --gres=gpu:1 --cpus-per-task=16 \
#               --mem=64G --time=04:00:00
#        # then on the compute-node shell:
#        bash slurm/isaac_warehouse_pip.slurm
#
#   B) BATCH (once it works):
#        sbatch slurm/isaac_warehouse_pip.slurm
#        tail -f isaac-warehouse-pip-<jobid>.log
# =============================================================================

#SBATCH --job-name=isaac-warehouse-pip
#SBATCH --partition=PARTITION_TODO     # <-- EDIT
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=04:00:00
#SBATCH --output=isaac-warehouse-pip-%j.log
# #SBATCH --account=ACCOUNT_TODO
# #SBATCH --qos=QOS_TODO

set -euo pipefail

# =============================================================================
# CONFIG (override via env)
# =============================================================================
CONDA_ENV="${CONDA_ENV:-isaac310}"

# Where the slurm/ directory lives (must contain isaac_boot.py).
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BOOT_PY="${BOOT_PY:-$REPO_ROOT/slurm/isaac_boot.py}"

# Default to NVIDIA's public HTTPS asset mirror so we don't need a Nucleus
# server. Requires outbound HTTPS from the compute node. Override if you
# have a local Nucleus or downloaded asset mirror.
ISAAC_ASSETS_ROOT="${ISAAC_ASSETS_ROOT:-https://omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/4.5}"

# WebRTC ports — fixed by Isaac Sim 4.x, NOT configurable here.
WEBRTC_SIGNALING_PORT=49100
# =============================================================================

log() { echo -e "\033[0;32m[isaac]\033[0m $*"; }
err() { echo -e "\033[0;31m[isaac]\033[0m $*" >&2; }

# --- Preflight ---------------------------------------------------------------
command -v conda >/dev/null 2>&1 || { err "conda not on PATH"; exit 1; }
CONDA_BASE="$(conda info --base)"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"
conda env list | awk '{print $1}' | grep -qx "$CONDA_ENV" \
    || { err "Missing conda env '$CONDA_ENV' — run: bash slurm/pip_install_isaac.sh"; exit 1; }
[ -f "$BOOT_PY" ]        || { err "Missing $BOOT_PY"; exit 1; }
command -v srun >/dev/null 2>&1 || { err "srun not on PATH — am I inside a Slurm allocation?"; exit 1; }
PYBIN="$(conda run -n "$CONDA_ENV" which python)"
log "Conda env:   $CONDA_ENV → $PYBIN"
log "Boot script: $BOOT_PY"
log "Asset root:  $ISAAC_ASSETS_ROOT"

# --- EULA / consent (required for headless launch) ---------------------------
export ACCEPT_EULA=Y
export OMNI_KIT_ACCEPT_EULA=YES
export PRIVACY_CONSENT=Y
export OMNI_KIT_ALLOW_ROOT=1
export ISAAC_ASSETS_ROOT

# Propagate any boot-script overrides the user passed in.
for v in SCENE_USD ROBOT_USD ROBOT_NAME ROBOT_X ROBOT_Y EXTRA_ROBOTS_JSON \
         RENDER_WIDTH RENDER_HEIGHT RENDER_DT; do
    [ -n "${!v:-}" ] && export "$v"
done

# --- Report client-connect info ----------------------------------------------
NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
NODE_HOST="$(hostname -f 2>/dev/null || hostname)"
log "Compute node: ${NODE_HOST} (${NODE_IP})"
log "Once the log says 'Stage ready. WebRTC stream is live.':"
log "  Open the Isaac Sim WebRTC Streaming Client on your laptop and use:"
log "      Server: ${NODE_IP}    (TCP ${WEBRTC_SIGNALING_PORT}, UDP 47998-48000)"

# --- Launch via srun under the conda env's python ---------------------------
# We use srun (not bare python) so the GPU cgroup is attached correctly, even
# when this script is run inside an salloc shell. $PYBIN is the absolute path
# to the conda env's interpreter, so no in-srun `conda activate` is needed.
log "Starting Isaac Sim + warehouse + robot. First boot builds shader caches."
exec srun \
    --export=ALL \
    "$PYBIN" "$BOOT_PY"
