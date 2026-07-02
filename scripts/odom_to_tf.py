#!/usr/bin/env python3
"""
scripts/odom_to_tf.py — broadcast odom->base_link TF from the /odom topic.

Isaac Sim publishes the /odom TOPIC (nav_msgs/Odometry), but Nav2 needs that pose
as a TF (odom -> base_link) to complete its transform tree:

    map -> odom -> base_link

This tiny standalone ROS 2 node subscribes /odom and rebroadcasts it on /tf. We
use a standard tf2 broadcaster rather than an Isaac OmniGraph TF node because it's
version-stable and easy to verify. (map -> odom is published separately as a
static identity transform by the launch file — we run no localization here, so
odom effectively IS the map frame.)

Frames are taken from the Odometry message itself (header.frame_id ->
child_frame_id), so they always match whatever Isaac is publishing.

Run (ROS 2 sourced, same ROS_DOMAIN_ID as Isaac):
    python3 scripts/odom_to_tf.py
Env: ODOM_TOPIC (default /odom).
"""

from __future__ import annotations

import os

import rclpy
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy
from tf2_ros import TransformBroadcaster


class OdomToTf(Node):
    def __init__(self) -> None:
        super().__init__("odom_to_tf")
        self._br = TransformBroadcaster(self)
        topic = os.getenv("ODOM_TOPIC", "/odom")
        # Best-effort subscriber receives from either a best-effort OR a reliable
        # publisher, so it matches Isaac's /odom regardless of its QoS.
        qos = QoSProfile(depth=10)
        qos.reliability = ReliabilityPolicy.BEST_EFFORT
        self.create_subscription(Odometry, topic, self._cb, qos)
        self._n = 0
        self.get_logger().info(f"odom_to_tf: republishing {topic} as TF (odom->base_link)")

    def _cb(self, msg: Odometry) -> None:
        t = TransformStamped()
        t.header = msg.header  # frame_id (parent, e.g. 'odom') + stamp
        t.child_frame_id = msg.child_frame_id or "base_link"
        t.transform.translation.x = msg.pose.pose.position.x
        t.transform.translation.y = msg.pose.pose.position.y
        t.transform.translation.z = msg.pose.pose.position.z
        t.transform.rotation = msg.pose.pose.orientation
        self._br.sendTransform(t)
        self._n += 1
        if self._n % 200 == 1:
            self.get_logger().info(
                f"TF {t.header.frame_id}->{t.child_frame_id} "
                f"@ ({t.transform.translation.x:.2f}, {t.transform.translation.y:.2f})"
            )


def main() -> None:
    rclpy.init()
    node = OdomToTf()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
