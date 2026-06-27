#!/usr/bin/env bash
# =============================================================================
# scripts/run_isaac_workstation.sh — Isaac Sim + WebRTC on a GPU workstation
#
# For a normal Ubuntu workstation with an NVENC-capable GPU (RTX 6000 Ada,
# A6000, RTX, L40S, ...) and Docker. No scheduler, no container gymnastics.
#
# This GPU has NVENC + RT cores, so Isaac's built-in WebRTC livestream works:
# you connect the "Isaac Sim WebRTC Streaming Client" from your laptop and see
# the real photoreal viewport.
#
# One-time:
#   sudo usermod -aG docker $USER && newgrp docker     # docker without sudo
#   export NGC_API_KEY=nvapi-...
#   echo "$NGC_API_KEY" | docker login nvcr.io -u '$oauthtoken' --password-stdin
#   docker pull nvcr.io/nvidia/isaac-sim:4.5.0
#
# Run:
#   bash run_isaac_workstation.sh                 # stock empty stage (proves streaming)
#   bash run_isaac_workstation.sh --warehouse     # load warehouse + Carter via boot script
# =============================================================================

set -euo pipefail

# Isaac Sim 6.0.1 (latest stable, Jun 2026) — validated on the 580 driver branch,
# Ubuntu 24.04, and RT-core GPUs (RTX 6000 Ada qualifies). NOTE: driver 595 is
# reported to break CUDA detection, so keep the 580.x driver you have.
IMAGE="${IMAGE:-nvcr.io/nvidia/isaac-sim:6.0.1}"
CONTAINER_NAME="${CONTAINER_NAME:-isaac-sim}"
CACHE="${CACHE:-$HOME/docker/isaac-sim}"
WEBRTC_SIGNALING_PORT=49100

MODE="stock"
[ "${1:-}" = "--warehouse" ] && MODE="warehouse"

log() { echo -e "\033[0;32m[isaac]\033[0m $*"; }
err() { echo -e "\033[0;31m[isaac]\033[0m $*" >&2; }

# --- Preflight ---------------------------------------------------------------
command -v docker >/dev/null 2>&1 || { err "docker not on PATH"; exit 1; }
if ! docker info >/dev/null 2>&1; then
    err "Docker daemon not reachable. Add yourself to the docker group:"
    err "   sudo usermod -aG docker \$USER && newgrp docker"
    err "(or run this script with sudo)"
    exit 1
fi
docker image inspect "$IMAGE" >/dev/null 2>&1 || {
    err "Image $IMAGE not present. Pull it first:"
    err "   echo \$NGC_API_KEY | docker login nvcr.io -u '\$oauthtoken' --password-stdin"
    err "   docker pull $IMAGE"
    exit 1
}

# --- NVIDIA's documented writable cache layout -------------------------------
mkdir -p \
  "$CACHE"/kit \
  "$CACHE"/ov \
  "$CACHE"/pip \
  "$CACHE"/glcache \
  "$CACHE"/computecache \
  "$CACHE"/logs \
  "$CACHE"/data \
  "$CACHE"/documents

# --- Remove any prior instance (frees the WebRTC port) -----------------------
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true

# --- Choose the in-container command -----------------------------------------
if [ "$MODE" = "warehouse" ]; then
    # Load the warehouse + Carter and stream it. Uses scripts/isaac_boot.py
    # (headless ROS 2 publisher) when you want Nav2 control, OR
    # scripts/isaac_demo.py (kinematic motion + WebRTC + HTTP API) when you
    # want the photoreal stream + direct goto control. Default: isaac_demo.py.
    REPO_ROOT="${REPO_ROOT:-$HOME/nayantra}"
    BOOT_SCRIPT="${BOOT_SCRIPT:-scripts/isaac_demo.py}"
    EXTRA_MOUNT=(-v "$REPO_ROOT:/workspace:ro")
    IN_CMD="cd /isaac-sim && exec ./python.sh /workspace/${BOOT_SCRIPT}"
    log "Mode: warehouse + Carter. Repo: $REPO_ROOT  Script: $BOOT_SCRIPT"
else
    EXTRA_MOUNT=()
    # The headless WebRTC launcher script was renamed across Isaac versions
    # (4.x: runheadless.webrtc.sh; 5.x/6.x may use isaac-sim.streaming.sh or
    # runheadless.sh + livestream flags). Auto-detect so it works on 6.0.1.
    IN_CMD='cd /isaac-sim
if [ -f ./runheadless.webrtc.sh ]; then echo "[isaac] using runheadless.webrtc.sh"; exec ./runheadless.webrtc.sh -v
elif [ -f ./isaac-sim.streaming.sh ]; then echo "[isaac] using isaac-sim.streaming.sh"; exec ./isaac-sim.streaming.sh
elif [ -f ./runheadless.sh ]; then echo "[isaac] using runheadless.sh + livestream flags"; exec ./runheadless.sh -v --/app/livestream/enabled=true --/app/livestream/webrtc/enabled=true
else echo "[isaac] ERROR: no headless launcher found in /isaac-sim:"; ls -1 ./*.sh 2>/dev/null; exit 1; fi'
    log "Mode: stock headless WebRTC (empty stage — proves streaming works)"
fi

NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
log "Host: $(hostname) ($NODE_IP)"
log "When the log shows 'app ready' / streaming, connect from your LAPTOP:"
log "  Isaac Sim WebRTC Streaming Client -> Server: ${NODE_IP}  (TCP ${WEBRTC_SIGNALING_PORT})"
log "Starting Isaac Sim. First launch builds shader caches (several minutes)."

exec docker run --name "$CONTAINER_NAME" --rm \
    --gpus all \
    --network host \
    --entrypoint bash \
    -e ACCEPT_EULA=Y \
    -e OMNI_KIT_ACCEPT_EULA=YES \
    -e PRIVACY_CONSENT=Y \
    -e OMNI_KIT_ALLOW_ROOT=1 \
    "${EXTRA_MOUNT[@]}" \
    -v "$CACHE/kit":/isaac-sim/kit/cache:rw \
    -v "$CACHE/ov":/root/.cache/ov:rw \
    -v "$CACHE/pip":/root/.cache/pip:rw \
    -v "$CACHE/glcache":/root/.cache/nvidia/GLCache:rw \
    -v "$CACHE/computecache":/root/.nv/ComputeCache:rw \
    -v "$CACHE/logs":/root/.nvidia-omniverse/logs:rw \
    -v "$CACHE/data":/root/.local/share/ov/data:rw \
    -v "$CACHE/documents":/root/Documents:rw \
    "$IMAGE" \
    -lc "$IN_CMD"
