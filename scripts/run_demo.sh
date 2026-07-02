#!/usr/bin/env bash
# =============================================================================
# scripts/run_demo.sh — bring up the NL -> Nav2 -> Isaac demo, one piece per
# terminal. Each subcommand sets up the right environment for that process.
#
# THE DEMO (3 terminals on the box, all ROS_DOMAIN_ID=0):
#   Terminal 1:  bash scripts/run_demo.sh isaac     # Isaac: stream + /odom + /cmd_vel
#   Terminal 2:  bash scripts/run_demo.sh nav2      # Nav2 + TF tree
#   Terminal 3:  bash scripts/run_demo.sh cli "take carter to the loading dock"
#
# Then connect the Isaac WebRTC client to this box and watch Carter drive.
#
#   bash scripts/run_demo.sh check                  # verify ROS 2 / Nav2 / topics
#
# Override env: VENV (isaacsim venv dir), ROS_SETUP, PUBLIC_IP, ROS_DOMAIN_ID.
# =============================================================================
set -eo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
VENV="${VENV:-$HOME/or_sig/isaacsim-env}"
ROS_SETUP="${ROS_SETUP:-/opt/ros/jazzy/setup.bash}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
export PUBLIC_IP="${PUBLIC_IP:-172.25.61.209}"
ROBOT_USD_DEFAULT="https://omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/6.0/Isaac/Robots/NVIDIA/Carter/carter_v1.usd"

log() { echo -e "\033[0;32m[demo]\033[0m $*"; }
err() { echo -e "\033[0;31m[demo]\033[0m $*" >&2; }
src_ros() { [ -f "$ROS_SETUP" ] && source "$ROS_SETUP" || { err "no ROS at $ROS_SETUP"; exit 1; }; }

cmd="${1:-help}"; shift || true

case "$cmd" in
  isaac)
    # Isaac: render + WebRTC stream + ROS 2 bridge + /cmd_vel drive + /odom.
    src_ros
    # shellcheck disable=SC1091
    source "$VENV/bin/activate"
    export OMNI_KIT_ACCEPT_EULA=YES PRIVACY_CONSENT=Y
    export ROBOT_USD="${ROBOT_USD:-$ROBOT_USD_DEFAULT}"
    log "Isaac STREAM mode. Connect the WebRTC client to PUBLIC_IP=$PUBLIC_IP after 'ROS2_READY'."
    exec env STREAM=1 python "$REPO/scripts/isaac_boot.py"
    ;;

  nav2)
    src_ros
    command -v ros2 >/dev/null || { err "ros2 not found"; exit 1; }
    ros2 pkg prefix nav2_bringup >/dev/null 2>&1 || {
      err "nav2_bringup not installed. Install Nav2 for Jazzy:"
      err "  sudo apt install ros-jazzy-navigation2 ros-jazzy-nav2-bringup"
      exit 1
    }
    log "Launching Nav2 + TF (map->odom static, odom->base_link from /odom)."
    exec ros2 launch "$REPO/scripts/nav2_demo.launch.py"
    ;;

  cli)
    src_ros
    # Use the isaac venv python if it has google-genai; else system python3.
    log "NL -> Nav2 CLI. (Set GEMINI_API_KEY for natural language; names/x y always work.)"
    exec python3 "$REPO/scripts/nav_cli.py" "$@"
    ;;

  activate)
    # Manually drive the Nav2 lifecycle (workaround for a broken/missing
    # lifecycle_manager, e.g. the diagnostic_updater ABI crash). Run AFTER
    # 'nav2' is up. Idempotent-ish; safe to re-run.
    src_ros
    NODES="controller_server smoother_server planner_server behavior_server velocity_smoother"
    for n in $NODES; do log "configure $n"; ros2 lifecycle set "/$n" configure || true; done
    for n in $NODES; do log "activate  $n"; ros2 lifecycle set "/$n" activate  || true; done
    ros2 lifecycle set /bt_navigator configure || true
    ros2 lifecycle set /bt_navigator activate  || true
    if ros2 action list 2>/dev/null | grep -q navigate_to_pose; then
      log "ACTION SERVER UP — /navigate_to_pose ready. Run: run_demo.sh cli 2 0"
    else
      err "still no /navigate_to_pose — paste 'ros2 lifecycle get /bt_navigator'"
    fi
    ;;

  check)
    src_ros
    log "ROS_DISTRO=${ROS_DISTRO:-?}  ROS_DOMAIN_ID=$ROS_DOMAIN_ID"
    printf "  ros2:        "; command -v ros2 >/dev/null && echo OK || echo MISSING
    printf "  nav2_bringup:"; ros2 pkg prefix nav2_bringup >/dev/null 2>&1 && echo " OK" || echo " MISSING (apt install ros-jazzy-nav2-bringup)"
    printf "  rviz2:       "; command -v rviz2 >/dev/null && echo OK || echo MISSING
    log "Live topics (need isaac running):"
    ros2 topic list 2>/dev/null | grep -E '^/(clock|odom|cmd_vel|tf|tf_static)$' || echo "  (start: run_demo.sh isaac)"
    log "Nav2 action server:"
    timeout 3 ros2 action list 2>/dev/null | grep navigate_to_pose || echo "  (start: run_demo.sh nav2)"
    ;;

  *)
    cat <<EOF
Usage: bash scripts/run_demo.sh <isaac|nav2|cli|check> [args]

  isaac          Terminal 1 — Isaac Sim: WebRTC stream + ROS 2 (/odom, /cmd_vel)
  nav2           Terminal 2 — Nav2 navigation stack + transform tree
  activate       After 'nav2' — manually activate Nav2 (if lifecycle_manager died)
  cli "<text>"   Terminal 3 — natural-language command -> Nav2 goal
  check          Sanity-check ROS 2 / Nav2 / live topics

Example:
  # T1: bash scripts/run_demo.sh isaac     (then connect WebRTC client to $PUBLIC_IP)
  # T2: bash scripts/run_demo.sh nav2
  # T3: bash scripts/run_demo.sh cli "take carter to the loading dock"
EOF
    ;;
esac
