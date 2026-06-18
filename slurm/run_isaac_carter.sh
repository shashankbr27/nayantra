#!/usr/bin/env bash
# =============================================================================
# slurm/run_isaac_carter.sh — Carter warehouse scene + ROS2 bridge + WebRTC
# on gpu02 via unprivileged apptainer. The full demo runtime (stage 2,
# replaces the bare runheadless render test).
#
#   ssh -n gpu02 'nohup bash ~/run_isaac_carter.sh > ~/isaac-carter.log 2>&1 & disown'
#
# Stop:  ssh -n gpu02 'pkill -f isaac_carter_app'
# =============================================================================
set -uo pipefail

APPTAINER="$HOME/apptainer/bin/apptainer"
SIF="$HOME/sif/isaac-sim_4.5.0.sif"
export PUBLIC_IP="${PUBLIC_IP:-172.25.60.80}"
export ASSETS_ROOT="${ASSETS_ROOT:-$HOME/isaac-assets/Assets/Isaac/4.5}"
GPU_ID="${GPU_ID:-0}"

[ -x "$APPTAINER" ] || { echo "FATAL: no apptainer"; exit 1; }
[ -f "$SIF" ]       || { echo "FATAL: no sif"; exit 1; }
[ -d "$ASSETS_ROOT/Isaac" ] || { echo "FATAL: assets not at $ASSETS_ROOT (need .../Isaac subdir)"; exit 1; }

mkdir -p "$HOME/isaac-cache/kitcache"

export ACCEPT_EULA=Y
export PRIVACY_CONSENT=Y
export OMNI_KIT_ACCEPT_EULA=YES
export CUDA_VISIBLE_DEVICES="$GPU_ID"
# ROS 2 bridge: use the bundled humble libs (no system ROS inside container);
# fastrtps must match the Nav2 side (RoboStack also defaults to fastrtps).
export ROS_DISTRO=humble
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"

echo "[carter] node=$(hostname) gpu=$GPU_ID at $(date)"

exec "$APPTAINER" exec --nv \
  --bind "$HOME/isaac-cache/kitcache:/isaac-sim/kit/cache" \
  "$SIF" \
  bash -c '
    # bundled ros2 libs path differs between 4.x exts naming schemes
    for d in /isaac-sim/exts/isaacsim.ros2.bridge/humble/lib /isaac-sim/exts/omni.isaac.ros2_bridge/humble/lib; do
      [ -d "$d" ] && export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:-}:$d" && echo "[carter] ros2 libs: $d"
    done
    cd /isaac-sim && ./python.sh ~/isaac_carter_app.py
  '
