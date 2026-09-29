"""
nayantra/core/adapters/ros2_runtime.py

One rclpy context, one node ("/nayantra_core") and one executor thread shared
by every ROS 2 robot adapter in the process.

The old RMFFleetAdapter called rclpy.init() per robot, which fails for the
second robot. Here robots only add their own (namespaced) subscriptions and
action clients to the shared node.

rclpy is imported lazily: the core runs fine without ROS 2 installed (for
example in simulation), and a robot registered with protocol=ros2 then fails
its connection test with an explanation instead of crashing the server.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger("nayantra.core.ros2")


class Ros2Runtime:
    _instance: Ros2Runtime | None = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.available = False
        self.error = ""
        self.node: Any = None
        self.tf_buffer: Any = None
        self._executor: Any = None
        self._thread: threading.Thread | None = None
        try:
            import rclpy  # type: ignore
            from rclpy.executors import MultiThreadedExecutor  # type: ignore

            if not rclpy.ok():
                rclpy.init()
            self.node = rclpy.create_node("nayantra_core")
            self._executor = MultiThreadedExecutor(num_threads=4)
            self._executor.add_node(self.node)
            self._thread = threading.Thread(
                target=self._executor.spin, name="nayantra-ros2", daemon=True
            )
            self._thread.start()
            try:
                from tf2_ros import Buffer, TransformListener  # type: ignore

                self.tf_buffer = Buffer()
                self._tf_listener = TransformListener(self.tf_buffer, self.node)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"tf2_ros unavailable, poses fall back to odometry: {exc}")
            self.available = True
            logger.info(f"ROS 2 runtime up: node /nayantra_core, ROS_DOMAIN_ID={self.domain_id}")
        except Exception as exc:  # noqa: BLE001 — ImportError, RCLError …
            self.error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"ROS 2 runtime unavailable: {self.error}")

    @property
    def domain_id(self) -> str:
        return os.getenv("ROS_DOMAIN_ID", "0")

    @classmethod
    def get(cls) -> Ros2Runtime:
        with cls._lock:
            if cls._instance is None:
                cls._instance = Ros2Runtime()
            return cls._instance

    @classmethod
    def peek(cls) -> Ros2Runtime | None:
        return cls._instance

    def publisher_count(self, topic: str) -> int:
        try:
            return int(self.node.count_publishers(topic))
        except Exception:  # noqa: BLE001
            return 0

    def lookup_pose(self, target: str, source: str) -> tuple[float, float, float] | None:
        """(x, y, yaw) of `source` frame in `target` frame, or None."""
        if self.tf_buffer is None:
            return None
        try:
            import math

            from rclpy.time import Time  # type: ignore

            tf = self.tf_buffer.lookup_transform(target, source, Time())
            t, q = tf.transform.translation, tf.transform.rotation
            yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
            return (t.x, t.y, yaw)
        except Exception:  # noqa: BLE001
            return None

    def shutdown(self) -> None:
        try:
            if self._executor:
                self._executor.shutdown()
            if self.node:
                self.node.destroy_node()
            import rclpy  # type: ignore

            if rclpy.ok():
                rclpy.shutdown()
        except Exception:  # noqa: BLE001
            pass
        Ros2Runtime._instance = None
