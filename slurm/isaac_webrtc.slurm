#!/usr/bin/env bash
# =============================================================================
# slurm/isaac_webrtc.slurm — Isaac Sim headless + WebRTC on H200
#                            (Enroot + Pyxis variant)
#
# Launches the NGC Isaac Sim image via `srun --container-image=...`, with the
# WebRTC livestream server enabled and Isaac's writable cache dirs bound to
# persistent host paths.
#
# One-time setup (run once on the login node):
#     bash slurm/enroot_import_isaac.sh
#
# Run modes:
#
#   A) INTERACTIVE (recommended for bring-up):
#        salloc --partition=$PARTITION --gres=gpu:1 --cpus-per-task=16 \
#               --mem=64G --time=04:00:00
#        # then, on the compute-node shell:
#        bash slurm/isaac_webrtc.slurm
#
#   B) BATCH (once it works):
#        sbatch slurm/isaac_webrtc.slurm
#        tail -f isaac-webrtc-<jobid>.log
# =============================================================================

#SBATCH --job-name=isaac-webrtc
#SBATCH --partition=PARTITION_TODO     # <-- EDIT: your GPU/H200 partition
#SBATCH --gres=gpu:1                   # <-- EDIT: maybe gpu:h200:1 if GRES is typed
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=04:00:00
#SBATCH --output=isaac-webrtc-%j.log
# #SBATCH --account=ACCOUNT_TODO       # <-- UNCOMMENT + EDIT if required
# #SBATCH --qos=QOS_TODO               # <-- UNCOMMENT + EDIT if required

set -euo pipefail

# =============================================================================
# CONFIG — EDIT THESE (or override via environment when launching)
# =============================================================================
# Pre-imported NGC image bundle from slurm/enroot_import_isaac.sh.
SQSH_PATH="${SQSH_PATH:-$HOME/enroot/isaac-sim+4.5.0.sqsh}"
IMAGE_TAG="${IMAGE_TAG:-4.5.0}"

# Where Isaac's writable caches/logs live on the host (persists across jobs).
# Use $SCRATCH if you have fast node-local or parallel scratch.
CACHE_ROOT="${CACHE_ROOT:-$HOME/isaac-cache}"

# WebRTC ports are fixed by Isaac Sim 4.x: TCP 49100 (signaling) +
# UDP 47998-48000 (media). They are NOT configurable via this script;
# the laptop must be able to reach the compute node on those ports.
WEBRTC_SIGNALING_PORT=49100

# Optional: persistent enroot container instance name (warm shader caches
# across reruns of the SAME node). Leave empty to use ephemeral instance.
CONTAINER_NAME="${CONTAINER_NAME:-isaac-webrtc-$$}"
# =============================================================================

log() { echo -e "\033[0;32m[isaac]\033[0m $*"; }
err() { echo -e "\033[0;31m[isaac]\033[0m $*" >&2; }

# --- Preflight --------------------------------------------------------------
command -v srun >/dev/null 2>&1 || { err "srun not on PATH — am I really inside a Slurm allocation?"; exit 1; }
[ -f "$SQSH_PATH" ] || { err "Missing $SQSH_PATH — run: bash slurm/enroot_import_isaac.sh"; exit 1; }
log "Image: $SQSH_PATH (Isaac $IMAGE_TAG)"

# --- Writable cache layout (mirrors NVIDIA's documented Docker -v binds) ----
# The NGC Isaac Sim image runs as root inside the container, so we bind to
# /root/... paths (not $HOME inside the container).
mkdir -p \
  "$CACHE_ROOT"/kit \
  "$CACHE_ROOT"/ov \
  "$CACHE_ROOT"/pip \
  "$CACHE_ROOT"/glcache \
  "$CACHE_ROOT"/computecache \
  "$CACHE_ROOT"/logs \
  "$CACHE_ROOT"/data \
  "$CACHE_ROOT"/documents

CONTAINER_MOUNTS="\
${CACHE_ROOT}/kit:/isaac-sim/kit/cache,\
${CACHE_ROOT}/ov:/root/.cache/ov,\
${CACHE_ROOT}/pip:/root/.cache/pip,\
${CACHE_ROOT}/glcache:/root/.cache/nvidia/GLCache,\
${CACHE_ROOT}/computecache:/root/.nv/ComputeCache,\
${CACHE_ROOT}/logs:/root/.nvidia-omniverse/logs,\
${CACHE_ROOT}/data:/root/.local/share/ov/data,\
${CACHE_ROOT}/documents:/root/Documents"

# --- EULA / consent env vars (required for headless launch) ------------------
export ACCEPT_EULA=Y
export OMNI_KIT_ACCEPT_EULA=YES
export PRIVACY_CONSENT=Y
export OMNI_KIT_ALLOW_ROOT=1

# --- Report how the client should connect ------------------------------------
NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
NODE_HOST="$(hostname -f 2>/dev/null || hostname)"
log "Compute node: ${NODE_HOST} (${NODE_IP})"
log "Once Isaac is up ('app ready' in the log), connect from your laptop using"
log "the native 'Isaac Sim WebRTC Streaming Client' app (NOT a browser URL):"
log "    Server address: ${NODE_IP}   (TCP ${WEBRTC_SIGNALING_PORT}, UDP 47998-48000)"
log "Download: search 'Isaac Sim WebRTC Streaming Client' on the NVIDIA docs site."
log "If the H200 node is behind NAT relative to your laptop, also pass:"
log "    --/app/livestream/publicEndpointAddress=${NODE_IP}"

# --- In-container launch: prefer bundled runheadless.webrtc.sh ---------------
# The bundled script carries the right livestream flags for the image's Isaac
# version. We auto-detect to stay compatible across 4.x.
read -r -d '' LAUNCH_CMD <<'EOSH' || true
set -euo pipefail
cd /isaac-sim
if [ -f ./runheadless.webrtc.sh ]; then
    echo "[isaac] launching via runheadless.webrtc.sh"
    exec ./runheadless.webrtc.sh -v
elif [ -f ./runheadless.sh ]; then
    echo "[isaac] launching via runheadless.sh + livestream flags"
    exec ./runheadless.sh -v \
        --/app/livestream/enabled=true \
        --/app/livestream/proto=webrtc \
        --/app/livestream/webrtc/enabled=true
else
    echo "[isaac] ERROR: no runheadless*.sh found in /isaac-sim" >&2
    exit 1
fi
EOSH

log "Starting Isaac Sim (headless, WebRTC). First launch builds shader caches — can take several minutes."

# Pyxis flags:
#   --container-image       : path to .sqsh from enroot_import_isaac.sh
#   --container-name        : persistent instance name (warm caches across runs on same node)
#   --container-mounts      : host:container bind pairs (comma-separated)
#   --container-workdir     : initial PWD inside the container
#   --container-env         : env vars to propagate from host
# Network: Pyxis containers share the host network namespace by default,
# so the in-container 0.0.0.0:8211 lands directly on $NODE_IP:8211.
exec srun \
    --container-image="$SQSH_PATH" \
    --container-name="$CONTAINER_NAME" \
    --container-mounts="$CONTAINER_MOUNTS" \
    --container-workdir=/isaac-sim \
    --container-env=ACCEPT_EULA,OMNI_KIT_ACCEPT_EULA,PRIVACY_CONSENT,OMNI_KIT_ALLOW_ROOT \
    bash -lc "$LAUNCH_CMD"
