#!/usr/bin/env bash
# =============================================================================
# slurm/gpu02_bootstrap.sh — one-shot bootstrap that runs INSIDE a tmux
# server on gpu02 (survives ssh disconnects):
#   1. extracts the Isaac SIF to a sandbox dir on LOCAL NVMe (fast, no FUSE)
#   2. launches Isaac Sim headless WebRTC from the sandbox
# All progress -> ~/bootstrap.log (shared home, readable from artgarage).
# =============================================================================
exec > ~/bootstrap.log 2>&1
set -x
date; hostname

APPTAINER="$HOME/apptainer/bin/apptainer"
SIF="$HOME/sif/isaac-sim_4.5.0.sif"
LOCAL="/tmp/shashank"
SANDBOX="$LOCAL/isaac-sandbox"
PUBLIC_IP="${PUBLIC_IP:-172.25.60.80}"

mkdir -p "$LOCAL/kitcache"

# --- 1. sandbox on local disk (one-time, ~5-10 min) -------------------------
if [ ! -d "$SANDBOX/isaac-sim" ]; then
    echo "[bootstrap] extracting sandbox to $SANDBOX ..."
    rm -rf "$SANDBOX"
    "$APPTAINER" build --sandbox "$SANDBOX" "$SIF" || { echo "[bootstrap] SANDBOX BUILD FAILED"; exit 1; }
fi
echo "[bootstrap] sandbox ready: $(du -sh "$SANDBOX" 2>/dev/null | cut -f1)"

# --- 2. launch Isaac Sim headless WebRTC ------------------------------------
export ACCEPT_EULA=Y
export PRIVACY_CONSENT=Y
export OMNI_KIT_ACCEPT_EULA=YES
export CUDA_VISIBLE_DEVICES="${GPU_ID:-0}"

echo "[bootstrap] launching Isaac Sim at $(date)"
"$APPTAINER" exec --nv \
    --bind "$LOCAL/kitcache:/isaac-sim/kit/cache" \
    "$SANDBOX" \
    bash -c "cd /isaac-sim && ./runheadless.sh -v \
        --/app/livestream/publicEndpointAddress=$PUBLIC_IP \
        --/app/window/width=1280 --/app/window/height=720" 2>&1 | tee ~/isaac-run.log
echo "[bootstrap] isaac exited rc=$? at $(date)"
