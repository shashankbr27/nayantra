"""
nayantra/core/adapters/ros_compat.py

ROS 2 distribution differences the adapters have to care about, kept free of
rclpy so they can be unit-tested anywhere.

Supported: Humble (Ubuntu 22.04) and Jazzy (Ubuntu 24.04). The rclpy, tf2 and
nav2_msgs surfaces the Nav2 adapter uses are the same on both. What differs is
the type of the velocity topic: Nav2 can publish geometry_msgs/Twist or
TwistStamped depending on distro and on `enable_stamped_cmd_vel`, and a robot
base subscribes to exactly one of them. The emergency-stop zero command must
match, or the robot never sees it.
"""

from __future__ import annotations

import os

SUPPORTED_DISTROS = ("humble", "jazzy")
TWIST = "geometry_msgs/msg/Twist"
TWIST_STAMPED = "geometry_msgs/msg/TwistStamped"


def ros_distro() -> str:
    """The sourced ROS 2 distro (`ROS_DISTRO`), lower-case, or ''."""
    return os.getenv("ROS_DISTRO", "").strip().lower()


def distro_support(distro: str) -> tuple[bool, str]:
    """(ok, detail) for the connection test's "ROS 2 distribution" line."""
    if not distro:
        return False, "ROS_DISTRO is not set (source /opt/ros/<humble|jazzy>/setup.bash)"
    if distro in SUPPORTED_DISTROS:
        return True, f"ROS 2 {distro.capitalize()}"
    return False, (
        f"ROS 2 {distro.capitalize()} is untested; supported distributions are "
        + " and ".join(d.capitalize() for d in SUPPORTED_DISTROS)
    )


def use_stamped_cmd_vel(discovered_types: list[str], override: str | None = None) -> bool:
    """Should the e-stop publish TwistStamped rather than Twist on cmd_vel?

    Order of precedence:
      1. `NAYANTRA_CMD_VEL_STAMPED=true|false` (override), for robots whose
         cmd_vel has no other endpoint yet.
      2. What is already on the wire: if a subscriber or publisher on the
         topic uses TwistStamped and none uses Twist, publish TwistStamped.
      3. Twist, which is what Humble's Nav2 and most robot bases use.
    """
    if override is not None and override.strip():
        return override.strip().lower() in ("1", "true", "yes", "on")
    types = set(discovered_types)
    return TWIST_STAMPED in types and TWIST not in types
