"""
simulation/gazebo/launch/gazebo_warehouse.launch.py

Gazebo (Harmonic) warehouse + one Nav2 stack per robot, for ROS 2 Humble or Jazzy.

  gz sim  ── nayantra_warehouse.sdf  (generated from config/maps/gazebo_warehouse.json)
     │  spawn  ros_gz_sim create  ── nayantra_ugv.sdf.in rendered per robot
     │  bridge ros_gz_bridge      ── /clock, /<ns>/{cmd_vel,odom,tf,scan}
     ▼
  per robot, under /<ns>:  static map->odom, base_link->lidar_link,
                           controller / smoother / planner / behavior / bt_navigator,
                           lifecycle manager

Nayantra Core then drives each robot through its Nav2Adapter (topics
/<ns>/odom, action /<ns>/navigate_through_poses). Nothing here talks to the core.

Run (ROS 2 sourced):
  ros2 launch simulation/gazebo/launch/gazebo_warehouse.launch.py
  # or:  bash scripts/run_gazebo.sh sim

Env:
  GZ_ROBOTS     "name:x:y:yaw,..."  (default: ugv_01 at the receiving dock,
                ugv_02 at the shipping dock). Names must match the robot ids in
                config/scenarios/gazebo_warehouse.json.
  GZ_HEADLESS   1 = server only, no GUI (lidar still renders, using EGL)
  GZ_WORLD      alternative world SDF
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from launch import LaunchDescription
from launch.actions import ExecuteProcess, SetEnvironmentVariable, TimerAction
from launch_ros.actions import Node

_HERE = Path(__file__).resolve().parent
_GZ = _HERE.parent
sys.path.insert(0, str(_GZ))

import nav2_params  # noqa: E402
from robots import DEFAULT_ROBOTS, bridge_args, parse_robots, render_model  # noqa: E402

WORLD_NAME = "nayantra_warehouse"
LIDAR_XYZ = ("0.25", "0", "0.2")
LIFECYCLE_NODES = [
    "controller_server",
    "smoother_server",
    "planner_server",
    "behavior_server",
    "bt_navigator",
]
TF_REMAPS = [("/tf", "tf"), ("/tf_static", "tf_static")]


def robot_nodes(name: str, params_file: str) -> list[Node]:
    sim_time = {"use_sim_time": True}

    def nav(pkg: str, exe: str, node_name: str) -> Node:
        return Node(
            package=pkg,
            executable=exe,
            name=node_name,
            namespace=name,
            output="screen",
            parameters=[params_file, sim_time],
            remappings=TF_REMAPS,
        )

    return [
        # No localization: map -> odom is identity, which is correct because the
        # odometry is published in the world frame (see the model SDF).
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="map_to_odom",
            namespace=name,
            arguments=["--frame-id", "map", "--child-frame-id", "odom"],
            parameters=[sim_time],
            remappings=TF_REMAPS,
        ),
        Node(
            package="tf2_ros",
            executable="static_transform_publisher",
            name="base_to_lidar",
            namespace=name,
            arguments=[
                "--x",
                LIDAR_XYZ[0],
                "--y",
                LIDAR_XYZ[1],
                "--z",
                LIDAR_XYZ[2],
                "--frame-id",
                "base_link",
                "--child-frame-id",
                "lidar_link",
            ],  # fmt: skip
            parameters=[sim_time],
            remappings=TF_REMAPS,
        ),
        nav("nav2_controller", "controller_server", "controller_server"),
        nav("nav2_smoother", "smoother_server", "smoother_server"),
        nav("nav2_planner", "planner_server", "planner_server"),
        nav("nav2_behaviors", "behavior_server", "behavior_server"),
        nav("nav2_bt_navigator", "bt_navigator", "bt_navigator"),
        Node(
            package="nav2_lifecycle_manager",
            executable="lifecycle_manager",
            name="lifecycle_manager_navigation",
            namespace=name,
            output="screen",
            parameters=[{"use_sim_time": True, "autostart": True, "node_names": LIFECYCLE_NODES}],
        ),
    ]


def generate_launch_description() -> LaunchDescription:
    robots = parse_robots(os.getenv("GZ_ROBOTS", DEFAULT_ROBOTS))
    world = os.getenv("GZ_WORLD", str(_GZ / "worlds" / "nayantra_warehouse.sdf"))
    headless = os.getenv("GZ_HEADLESS", "0").strip().lower() in ("1", "true", "yes")
    params_dir = Path(tempfile.mkdtemp(prefix="nayantra_nav2_"))

    gz_cmd = ["gz", "sim", "-r", world]
    if headless:
        gz_cmd = ["gz", "sim", "-r", "-s", "--headless-rendering", world]

    actions: list = [
        SetEnvironmentVariable("GZ_SIM_RESOURCE_PATH", str(_GZ / "models")),
        ExecuteProcess(cmd=gz_cmd, name="gazebo", output="screen"),
        Node(
            package="ros_gz_bridge",
            executable="parameter_bridge",
            name="clock_bridge",
            arguments=["/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock"],
            output="screen",
        ),
    ]
    for name, x, y, yaw in robots:
        actions.append(
            # Spawn after the world is up; the bridge and Nav2 wait for topics themselves.
            TimerAction(
                period=5.0,
                actions=[
                    Node(
                        package="ros_gz_sim",
                        executable="create",
                        name=f"spawn_{name}",
                        arguments=[
                            "-world",
                            WORLD_NAME,
                            "-name",
                            name,
                            "-string",
                            render_model(name),
                            "-x",
                            str(x),
                            "-y",
                            str(y),
                            "-z",
                            "0.0",
                            "-Y",
                            str(yaw),
                        ],  # fmt: skip
                        output="screen",
                    ),
                    Node(
                        package="ros_gz_bridge",
                        executable="parameter_bridge",
                        name=f"bridge_{name}",
                        arguments=bridge_args(name),
                        output="screen",
                    ),
                    *robot_nodes(name, str(nav2_params.write_params(name, params_dir))),
                ],
            )
        )
    return LaunchDescription(actions)
