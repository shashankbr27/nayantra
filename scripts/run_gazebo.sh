#!/usr/bin/env bash
# =============================================================================
# scripts/run_gazebo.sh — bring up the Gazebo warehouse + Nav2, then the core.
#
#   Terminal 1:  bash scripts/run_gazebo.sh sim     # Gazebo + bridge + Nav2 per robot
#   Terminal 2:  bash scripts/run_gazebo.sh core    # nayantra-core --scenario gazebo_warehouse
#   Any time:    bash scripts/run_gazebo.sh check   # ROS 2 / Gazebo / Nav2 / topics
#                bash scripts/run_gazebo.sh world   # regenerate the world SDF from the map
#
# Works with ROS 2 Jazzy (Gazebo Harmonic ships with ros-jazzy-ros-gz) and
# Humble (needs Gazebo Harmonic + ros-humble-ros-gzharmonic). Use the same
# ROS_DOMAIN_ID in both terminals.
#
# Override env: ROS_SETUP, ROS_DOMAIN_ID, GZ_ROBOTS, GZ_HEADLESS (see the launch file).
# =============================================================================
set -eo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
# Jazzy (Ubuntu 24.04) or Humble (22.04): use whichever is installed, Jazzy first.
if [ -z "${ROS_SETUP:-}" ]; then
  for d in jazzy humble; do
    [ -f "/opt/ros/$d/setup.bash" ] && ROS_SETUP="/opt/ros/$d/setup.bash" && break
  done
fi
ROS_SETUP="${ROS_SETUP:-/opt/ros/jazzy/setup.bash}"
export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"

log() { echo -e "\033[0;32m[gazebo]\033[0m $*"; }
err() { echo -e "\033[0;31m[gazebo]\033[0m $*" >&2; }

source_ros() {
  [ -f "$ROS_SETUP" ] || { err "ROS 2 not found at $ROS_SETUP (set ROS_SETUP)"; exit 1; }
  # shellcheck disable=SC1090
  source "$ROS_SETUP"
  log "ROS_DISTRO=${ROS_DISTRO:-?}  ROS_DOMAIN_ID=$ROS_DOMAIN_ID"
}

case "${1:-}" in
  world)
    python3 "$REPO/simulation/gazebo/generate_world.py"
    ;;
  sim)
    source_ros
    command -v gz >/dev/null 2>&1 || { err "gz not found. Install Gazebo Harmonic + ros_gz (see docs/gazebo_architecture.md)"; exit 1; }
    ros2 pkg prefix nav2_bringup >/dev/null 2>&1 || { err "Nav2 missing: sudo apt install ros-${ROS_DISTRO}-navigation2 ros-${ROS_DISTRO}-nav2-bringup"; exit 1; }
    ros2 pkg prefix ros_gz_sim >/dev/null 2>&1 || { err "ros_gz missing (Jazzy: ros-jazzy-ros-gz, Humble: ros-humble-ros-gzharmonic)"; exit 1; }
    python3 "$REPO/simulation/gazebo/generate_world.py" --check || python3 "$REPO/simulation/gazebo/generate_world.py"
    exec ros2 launch "$REPO/simulation/gazebo/launch/gazebo_warehouse.launch.py"
    ;;
  core)
    source_ros
    exec nayantra-core --scenario gazebo_warehouse "${@:2}"
    ;;
  check)
    source_ros
    printf "  gz:          "; command -v gz >/dev/null 2>&1 && gz sim --versions 2>/dev/null | head -1 || echo "MISSING"
    printf "  nav2_bringup:"; ros2 pkg prefix nav2_bringup >/dev/null 2>&1 && echo " OK" || echo " MISSING"
    printf "  ros_gz_sim:  "; ros2 pkg prefix ros_gz_sim >/dev/null 2>&1 && echo "OK" || echo "MISSING"
    for ns in ugv_01 ugv_02; do
      printf "  /%s/odom: " "$ns"; ros2 topic info "/$ns/odom" 2>/dev/null | grep -m1 "Publisher count" || echo "no publisher"
    done
    ;;
  *)
    sed -n 2,12p "${BASH_SOURCE[0]}"
    exit 1
    ;;
esac
