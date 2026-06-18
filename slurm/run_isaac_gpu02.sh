#!/usr/bin/env bash
# =============================================================================
# slurm/run_isaac_gpu02.sh — launch Isaac Sim (headless WebRTC) on gpu02
# via unprivileged apptainer. Run ON gpu02 (usually spawned from artgarage):
#
#   ssh -n gpu02 'nohup bash ~/run_isaac_gpu02.sh > ~/isaac-run.log 2>&1 & disown'
#
# Logs:   ~/isaac-run.log   (this script + kit stdout)
# Stop:   ssh -n gpu02 'pkill -f runheadless'
# =============================================================================
set -uo pipefail

APPTAINER="$HOME/apptainer/bin/apptainer"
SIF="$HOME/sif/isaac-sim_4.5.0.sif"
# ICE candidates must point at the jump host the laptop can reach;
# the relay on artgarage forwards TCP 49100 + UDP 47998-48000 here.
PUBLIC_IP="${PUBLIC_IP:-172.25.60.80}"
GPU_ID="${GPU_ID:-0}"

[ -x "$APPTAINER" ] || { echo "FATAL: apptainer not at $APPTAINER"; exit 1; }
[ -f "$SIF" ]       || { echo "FATAL: image not at $SIF"; exit 1; }

# Writable caches: /isaac-sim is read-only inside the SIF, so the kit cache
# must be bind-mounted out. Everything else lands in $HOME (Lustre) anyway.
mkdir -p "$HOME/isaac-cache/kitcache"

export ACCEPT_EULA=Y
export PRIVACY_CONSENT=Y
export OMNI_KIT_ACCEPT_EULA=YES
# Keep CUDA on one free GPU; Vulkan device is steered by --/renderer/activeGpu.
export CUDA_VISIBLE_DEVICES="$GPU_ID"

echo "[run] node=$(hostname) gpu=$GPU_ID public_ip=$PUBLIC_IP"
echo "[run] sif=$SIF"
echo "[run] starting Isaac Sim headless WebRTC at $(date)"

exec "$APPTAINER" exec --nv \
  --bind "$HOME/isaac-cache/kitcache:/isaac-sim/kit/cache" \
  "$SIF" \
  bash -c "cd /isaac-sim && ./runheadless.sh -v \
      --/app/livestream/publicEndpointAddress=$PUBLIC_IP \
      --/renderer/activeGpu=$GPU_ID \
      --/app/window/width=1280 --/app/window/height=720"
