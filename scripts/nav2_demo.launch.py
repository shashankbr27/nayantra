#!/usr/bin/env python3
"""
scripts/nav2_demo.launch.py — minimal Nav2 for carter_v1 in Isaac Sim.

We launch ONLY the nodes a basic NavigateToPose needs (controller, smoother,
planner, behaviors, bt_navigator), each with our params, plus our own
lifecycle_manager managing exactly those. This deliberately avoids the extra
nodes nav2_bringup's navigation_launch.py now pulls in — collision_monitor,
route_server, docking_server, waypoint_follower — which abort the whole bringup
when they lack params (e.g. collision_monitor needs `observation_sources`).

Transform tree:
  map -> odom        static identity (no localization in this demo)
  odom -> base_link  from odom_to_tf.py (rebroadcasts the /odom topic)

Prereqs: ROS 2 Jazzy + Nav2; isaac_boot.py running (publishes /clock, /odom;
subscribes /cmd_vel).

Run:
  ros2 launch scripts/nav2_demo.launch.py
  # or:
  bash scripts/run_demo.sh nav2
Env: USE_SIM_TIME (default true — Isaac publishes /clock). Set false if Nav2
hangs in 'configuring' (i.e. /clock is silent).
"""

import os
from pathlib import Path

from launch import LaunchDescription
from launch.actions import ExecuteProcess
from launch_ros.actions import Node

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent


def generate_launch_description() -> LaunchDescription:
    params = str(_REPO / "config" / "nav2" / "carter_nav2.yaml")
    odom_to_tf = str(_HERE / "odom_to_tf.py")
    use_sim_time = os.getenv("USE_SIM_TIME", "true").lower() in ("1", "true", "yes")
    common = [params, {"use_sim_time": use_sim_time}]

    lifecycle_nodes = [
        "controller_server",
        "smoother_server",
        "planner_server",
        "behavior_server",
        "bt_navigator",
    ]

    def nav(pkg: str, exe: str, name: str) -> Node:
        return Node(package=pkg, executable=exe, name=name, output="screen", parameters=common)

    return LaunchDescription(
        [
            # map -> odom : static identity (no localization here).
            Node(
                package="tf2_ros",
                executable="static_transform_publisher",
                name="map_to_odom",
                arguments=["--frame-id", "map", "--child-frame-id", "odom"],
                parameters=[{"use_sim_time": use_sim_time}],
                output="screen",
            ),
            # odom -> base_link : rebroadcast Isaac's /odom topic as TF.
            ExecuteProcess(cmd=["python3", odom_to_tf], name="odom_to_tf", output="screen"),
            # Core Nav2 servers.
            nav("nav2_controller", "controller_server", "controller_server"),
            nav("nav2_smoother", "smoother_server", "smoother_server"),
            nav("nav2_planner", "planner_server", "planner_server"),
            nav("nav2_behaviors", "behavior_server", "behavior_server"),
            nav("nav2_bt_navigator", "bt_navigator", "bt_navigator"),
            # Our own lifecycle manager over exactly those nodes (autostart =
            # configure + activate in order). No collision_monitor -> no abort.
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
            ),
        ]
    )
