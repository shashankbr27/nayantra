#!/usr/bin/env python3
"""
scripts/nav2_demo.launch.py — minimal Nav2 for carter_v1 in Isaac Sim.

We launch ONLY the nodes a basic NavigateToPose needs (controller, smoother,
planner, behaviors, bt_navigator), each with our params, plus our own
lifecycle_manager managing exactly those. This deliberately avoids the extra
nodes nav2_bringup's navigation_launch.py now pulls in — route_server,
docking_server, waypoint_follower — which abort the whole bringup when they
lack params.

Transform tree:
  map -> odom          static identity (no localization in this demo)
  odom -> base_link    from odom_to_tf.py (rebroadcasts the /odom topic)
  base_link -> lidar   static (env LIDAR_FRAME/LIDAR_XYZ) so the costmap can
                       place /scan returns. Harmless when no lidar publishes.

Prereqs: ROS 2 Jazzy + Nav2; isaac_boot.py running (publishes /clock, /odom,
optionally /scan with LIDAR=1; subscribes /cmd_vel).

Run:
  ros2 launch scripts/nav2_demo.launch.py
  # or:
  bash scripts/run_demo.sh nav2

Env:
  USE_SIM_TIME        default true (Isaac publishes /clock). Set false if Nav2
                      hangs in 'configuring' (i.e. /clock is silent).
  LIDAR_FRAME         lidar TF frame (default carter_lidar — must match Isaac's
                      LIDAR_FRAME_ID)
  LIDAR_XYZ           "x y z" of the lidar relative to base_link (default
                      "0 0 0.4"; isaac_boot logs the exact offset at startup)
  COLLISION_MONITOR   1 = insert the collision_monitor safety guardrail between
                      the controller and the robot (controller/behaviors then
                      publish cmd_vel_raw; the monitor publishes /cmd_vel and
                      enforces the slowdown/stop polygons from carter_nav2.yaml).
                      Default 0 = wiring identical to before.
"""

import os
from pathlib import Path

from launch import LaunchDescription
from launch.actions import ExecuteProcess
from launch_ros.actions import Node

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent


def _env_flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")


def generate_launch_description() -> LaunchDescription:
    params = str(_REPO / "config" / "nav2" / "carter_nav2.yaml")
    odom_to_tf = str(_HERE / "odom_to_tf.py")
    use_sim_time = _env_flag("USE_SIM_TIME", "true")
    collision_monitor = _env_flag("COLLISION_MONITOR", "0")
    lidar_frame = os.getenv("LIDAR_FRAME", "carter_lidar")
    lidar_xyz = os.getenv("LIDAR_XYZ", "0 0 0.4").split()
    common = [params, {"use_sim_time": use_sim_time}]

    # With the collision monitor inserted, the controller and the recovery
    # behaviors publish to cmd_vel_raw; the monitor filters that into /cmd_vel.
    drive_remaps = [("cmd_vel", "cmd_vel_raw")] if collision_monitor else []

    lifecycle_nodes = [
        "controller_server",
        "smoother_server",
        "planner_server",
        "behavior_server",
        "bt_navigator",
    ]
    if collision_monitor:
        lifecycle_nodes.append("collision_monitor")

    def nav(pkg: str, exe: str, name: str, remap=()) -> Node:
        return Node(
            package=pkg,
            executable=exe,
            name=name,
            output="screen",
            parameters=common,
            remappings=list(remap),
        )

    nodes = [
        # map -> odom : static identity (no localization here).
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="map_to_odom",
            arguments=["--frame-id", "map", "--child-frame-id", "odom"],
            parameters=[{"use_sim_time": use_sim_time}],
            output="screen",
        ),
        # base_link -> lidar : static mount offset, so /scan returns can be
        # transformed by the costmap. tf2 tolerates the frame being unused.
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="base_to_lidar",
            arguments=[
                "--x",
                lidar_xyz[0],
                "--y",
                lidar_xyz[1],
                "--z",
                lidar_xyz[2],
                "--frame-id",
                "base_link",
                "--child-frame-id",
                lidar_frame,
            ],
            parameters=[{"use_sim_time": use_sim_time}],
            output="screen",
        ),
        # odom -> base_link : rebroadcast Isaac's /odom topic as TF.
        ExecuteProcess(cmd=["python3", odom_to_tf], name="odom_to_tf", output="screen"),
        # Core Nav2 servers.
        nav("nav2_controller", "controller_server", "controller_server", drive_remaps),
        nav("nav2_smoother", "smoother_server", "smoother_server"),
        nav("nav2_planner", "planner_server", "planner_server"),
        nav("nav2_behaviors", "behavior_server", "behavior_server", drive_remaps),
        nav("nav2_bt_navigator", "bt_navigator", "bt_navigator"),
    ]
    if collision_monitor:
        nodes.append(nav("nav2_collision_monitor", "collision_monitor", "collision_monitor"))
    nodes.append(
        # Our own lifecycle manager over exactly those nodes (autostart =
        # configure + activate in order).
        Node(
            package="nav2_lifecycle_manager",
            executable="lifecycle_manager",
            name="lifecycle_manager_navigation",
            output="screen",
            parameters=[
                {
                    "use_sim_time": use_sim_time,
                    "autostart": True,
                    "node_names": lifecycle_nodes,
                }
            ],
        )
    )
    return LaunchDescription(nodes)
