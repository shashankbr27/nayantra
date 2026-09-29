"""
simulation/gazebo/robots.py — the Gazebo fleet's robot list and per-robot wiring.

ROS-free so the launch file and the tests share one definition.
"""

from __future__ import annotations

from pathlib import Path

GZ_DIR = Path(__file__).resolve().parent
MODEL_TEMPLATE = GZ_DIR / "models" / "nayantra_ugv.sdf.in"

#: "name:x:y:yaw" — names are the ROS namespaces and the robot ids in
#: config/scenarios/gazebo_warehouse.json; x, y are the spawn waypoints' map coordinates.
DEFAULT_ROBOTS = "ugv_01:-10.5:0:0,ugv_02:10.5:0:3.14159"


def parse_robots(spec: str) -> list[tuple[str, float, float, float]]:
    robots = []
    for item in filter(None, (s.strip() for s in spec.split(","))):
        name, x, y, yaw = item.split(":")
        robots.append((name, float(x), float(y), float(yaw)))
    return robots


def render_model(name: str) -> str:
    """The robot SDF with @NAME@ replaced by the robot's namespace."""
    return MODEL_TEMPLATE.read_text(encoding="utf-8").replace("@NAME@", name)


def bridge_args(name: str) -> list[str]:
    """ros_gz_bridge arguments: `]` is ROS -> Gazebo, `[` is Gazebo -> ROS."""
    return [
        f"/{name}/cmd_vel@geometry_msgs/msg/Twist]gz.msgs.Twist",
        f"/{name}/odom@nav_msgs/msg/Odometry[gz.msgs.Odometry",
        f"/{name}/tf@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V",
        f"/{name}/scan@sensor_msgs/msg/LaserScan[gz.msgs.LaserScan",
    ]
