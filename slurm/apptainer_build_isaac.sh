#!/usr/bin/env bash
# =============================================================================
# slurm/apptainer_build_isaac.sh — one-time build of the Isaac Sim .sif
#
# Apptainer is available on this box via `module load apptainer/1.4.1`.
# It builds a read-only .sif from the NGC Isaac Sim Docker image. The .sif
# carries Ubuntu 22.04 / glibc 2.34, sidestepping the host's RHEL 8 glibc 2.28.
#
# Prereqs:
#   - NGC API key:  export NGC_API_KEY=nvapi-...
#                   (https://ngc.nvidia.com/setup/api-key)
#   - ~30 GB free disk for the .sif + build cache
#
# Usage:
#   export NGC_API_KEY=nvapi-...
#   bash slurm/apptainer_build_isaac.sh
# =============================================================================

set -euo pipefail

APPTAINER_MODULE="${APPTAINER_MODULE:-apptainer/1.4.1}"
IMAGE_TAG="${IMAGE_TAG:-4.5.0}"
IMAGE_URL="${IMAGE_URL:-docker://nvcr.io/nvidia/isaac-sim:${IMAGE_TAG}}"
SIF_DIR="${SIF_DIR:-$HOME/apptainer}"
SIF_PATH="${SIF_PATH:-$SIF_DIR/isaac-sim-${IMAGE_TAG}.sif}"

# Keep build scratch + layer cache in $HOME, NOT /tmp (HPC /tmp is often tiny).
export APPTAINER_TMPDIR="${APPTAINER_TMPDIR:-$HOME/apptainer/tmp}"
export APPTAINER_CACHEDIR="${APPTAINER_CACHEDIR:-$HOME/apptainer/cache}"

log()  { echo -e "\033[0;32m[apptainer]\033[0m $*"; }
warn() { echo -e "\033[1;33m[apptainer]\033[0m $*"; }
err()  { echo -e "\033[0;31m[apptainer]\033[0m $*" >&2; }

# --- Load apptainer module ---------------------------------------------------
if command -v module >/dev/null 2>&1; then
    log "Loading module: $APPTAINER_MODULE"
    module load "$APPTAINER_MODULE"
fi
command -v apptainer >/dev/null 2>&1 || { err "apptainer not on PATH after module load"; exit 1; }
log "apptainer: $(apptainer --version)"

# --- NGC credentials ---------------------------------------------------------
if [ -z "${NGC_API_KEY:-}" ]; then
    err "NGC_API_KEY not set. Run:  export NGC_API_KEY=nvapi-...  then re-run."
    exit 1
fi
# Apptainer reads registry creds from these env vars.
export APPTAINER_DOCKER_USERNAME='$oauthtoken'
export APPTAINER_DOCKER_PASSWORD="$NGC_API_KEY"

mkdir -p "$SIF_DIR" "$APPTAINER_TMPDIR" "$APPTAINER_CACHEDIR"

if [ -f "$SIF_PATH" ]; then
    log "SIF already exists: $SIF_PATH"
    log "Delete it and re-run to rebuild."
    exit 0
fi

# --- Build -------------------------------------------------------------------
log "Building $SIF_PATH from $IMAGE_URL"
log "(downloads ~15 GB + converts to SIF; 15-30 min first time)"
apptainer build "$SIF_PATH" "$IMAGE_URL"

log "Done: $SIF_PATH"
echo ""
echo "Next:  SIF_PATH=$SIF_PATH bash slurm/run_isaac_apptainer.sh"
