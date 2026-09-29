#!/usr/bin/env bash
# =============================================================================
# scripts/run_rviz.sh — open RViz2 with the Nayantra preset.
#
# The GPU box is headless (SSH, no monitor), so RViz needs a display forwarded
# to your laptop. Pick ONE:
#
#   A) X11 forwarding (simplest). Connect with X forwarding enabled:
#         ssh -Y art5gpc8@172.25.61.209
#      On Windows you need a local X server first:
#         - MobaXterm (free) — has an X server + SSH built in, X forwarding is automatic, OR
#         - VcXsrv / X410 — start it, then `ssh -Y` from any terminal, OR
#         - WSL2 (Windows 11 has WSLg) — `ssh -Y` from inside WSL2.
#      Then run this script on the box; the RViz window opens on your laptop.
#
#   B) VNC — run a VNC server on the box and connect a VNC viewer. Heavier
#      setup but smoother 3D than X forwarding; ask if you want the steps.
#
# Prereqs: isaac_boot.py running on the SAME ROS_DOMAIN_ID (publishes /tf,/odom).
#
# Usage:
#   bash scripts/run_rviz.sh
#   ROS_DOMAIN_ID=0 FIXED_FRAME=map bash scripts/run_rviz.sh   # override frame
# =============================================================================
# NOTE: no `-u` (nounset) — ROS 2's setup.bash references unset vars and would
# abort under nounset. We guard our own ${VAR:-} expansions explicitly instead.
set -eo pipefail

# Jazzy (Ubuntu 24.04) or Humble (22.04): use whichever is installed, Jazzy first.
if [ -z "${ROS_SETUP:-}" ]; then
  for d in jazzy humble; do
    [ -f "/opt/ros/$d/setup.bash" ] && ROS_SETUP="/opt/ros/$d/setup.bash" && break
  done
fi
ROS_SETUP="${ROS_SETUP:-/opt/ros/jazzy/setup.bash}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RVIZ_CONFIG="${RVIZ_CONFIG:-$REPO_ROOT/config/nayantra.rviz}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"

log() { echo -e "\033[0;32m[rviz]\033[0m $*"; }
err() { echo -e "\033[0;31m[rviz]\033[0m $*" >&2; }

# --- Display check -----------------------------------------------------------
if [ -z "${DISPLAY:-}" ]; then
    err "\$DISPLAY is not set — RViz needs a graphical display."
    err "Reconnect with X forwarding:   ssh -Y art5gpc8@172.25.61.209"
    err "(On Windows, start an X server first: MobaXterm has one built in, or VcXsrv.)"
    err "Then re-run this script. See the header of this file for VNC as an alternative."
    exit 1
fi

# --- Source ROS 2 ------------------------------------------------------------
if [ ! -f "$ROS_SETUP" ]; then
    err "ROS setup not found at $ROS_SETUP (override with ROS_SETUP=/path/setup.bash)"
    exit 1
fi
# shellcheck disable=SC1090
source "$ROS_SETUP"

[ -f "$RVIZ_CONFIG" ] || { err "RViz config not found: $RVIZ_CONFIG"; exit 1; }

# Soft-GL fallback: indirect GLX over remote X often can't do hardware OpenGL.
# If RViz crashes with an OGRE/GLX error, re-run with SOFT_GL=1 for software GL.
if [ "${SOFT_GL:-0}" = "1" ]; then
    export LIBGL_ALWAYS_SOFTWARE=1
    log "SOFT_GL=1 — using software OpenGL (slower, but survives remote X)."
fi

log "DISPLAY=$DISPLAY  ROS_DOMAIN_ID=$ROS_DOMAIN_ID"
log "config=$RVIZ_CONFIG"
log "Reminder: isaac_boot.py must be running on ROS_DOMAIN_ID=$ROS_DOMAIN_ID to see /tf."
log "Quick topic check:"
ros2 topic list 2>/dev/null | grep -E '^/(tf|tf_static|odom|clock|scan|map|plan)$' || \
    log "  (no nav topics yet — start isaac_boot.py)"

# Optional override of the Fixed Frame without editing the .rviz file.
if [ -n "${FIXED_FRAME:-}" ]; then
    log "Fixed Frame override: $FIXED_FRAME"
    exec rviz2 -d "$RVIZ_CONFIG" --ros-args -p "fixed_frame:=$FIXED_FRAME"
fi

exec rviz2 -d "$RVIZ_CONFIG"
