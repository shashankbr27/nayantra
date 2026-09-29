"""
nayantra/ros2_adapter/fleet_adapter.py

Legacy single-robot Nav2 adapter (kept for the CLI below and old imports).

Despite the historical name it does not use Open-RMF's rmf_fleet_adapter. It
sends Nav2 NavigateToPose goals, follows /odom, and optionally publishes
rmf_fleet_msgs/RobotState if that package is installed. Without ROS 2 it
interpolates the pose (kinematic stub).

The multi-robot, traffic-coordinated path is nayantra/core/adapters/nav2.py,
which shares one rclpy node across robots. This class calls rclpy.init() per
instance, so it supports one robot per process.

Usage:
  python -m nayantra.ros2_adapter.fleet_adapter --fleet warehouse_fleet --robot carter1 [--ros2]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger("nayantra.fleet_adapter")

# ---------------------------------------------------------------------------
# RMF Robot Modes (matches rmf_fleet_msgs/RobotMode)
# ---------------------------------------------------------------------------


class RobotMode(IntEnum):
    IDLE = 0
    CHARGING = 1
    MOVING = 2
    PAUSED = 3
    WAITING = 4
    EMERGENCY = 5
    GOING_HOME = 6
    DOCKING = 7
    ADAPTER_ERROR = 8


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class RobotLocation:
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    level_name: str = "L1"
    t: float = field(default_factory=time.time)


@dataclass
class RobotState:
    name: str
    fleet_name: str
    mode: RobotMode = RobotMode.IDLE
    battery_percent: float = 100.0
    location: RobotLocation = field(default_factory=RobotLocation)
    task_id: str = ""
    path: list[RobotLocation] = field(default_factory=list)
    seq: int = 0


# ---------------------------------------------------------------------------
# Nav2 goal representation (used in stub mode)
# ---------------------------------------------------------------------------


@dataclass
class Nav2Goal:
    x: float
    y: float
    yaw: float
    frame_id: str = "map"
    label: str = ""


# ---------------------------------------------------------------------------
# Fleet Adapter
# ---------------------------------------------------------------------------


class RMFFleetAdapter:
    """
    Bridges OpenRMF task dispatch to Nav2 navigation actions.

    In stub mode (ROS2_ENABLED=false or rclpy unavailable):
      - Simulates robot movement by linearly interpolating pose over time
      - Publishes state updates to an internal callback (used by tests)
      - No actual ROS 2 infrastructure required

    In live mode (ROS2_ENABLED=true + rclpy available):
      - Creates a rclpy node
      - Subscribes to /odom for real pose updates
      - Sends NavigateToPose goals to Nav2
      - Publishes RobotState to /rmf_fleet/robot_state
    """

    def __init__(
        self,
        fleet_name: str = "turtlebot_fleet",
        robot_name: str = "turtlebot3_1",
        ros2_enabled: bool = False,
        namespace: str = "",
    ) -> None:
        self.fleet_name = fleet_name
        self.robot_name = robot_name
        self._ros2_enabled = ros2_enabled
        # Topic/action namespace for multi-robot setups, e.g. "/carter1".
        # Empty string = global namespace (/odom, /navigate_to_pose).
        self.namespace = namespace.rstrip("/")
        self._state = RobotState(name=robot_name, fleet_name=fleet_name)
        self._running = False
        self._nav_goal: Nav2Goal | None = None
        self._state_callbacks: list[Callable[[RobotState], None]] = []

        # ROS 2 node handles (populated in live mode)
        self._node = None
        self._nav_client = None
        self._state_publisher = None
        self._goal_handle = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the adapter and begin the control loop."""
        self._running = True
        if self._ros2_enabled:
            await self._init_ros2()
        logger.info(
            f"Fleet adapter started — fleet={self.fleet_name} "
            f"robot={self.robot_name} "
            f"ros2={'enabled' if self._ros2_enabled else 'stub'}"
        )
        await self._control_loop()

    async def stop(self) -> None:
        self._running = False
        if self._node:
            try:
                import rclpy

                self._node.destroy_node()
                rclpy.shutdown()
            except Exception:
                pass
        logger.info("Fleet adapter stopped")

    # ------------------------------------------------------------------
    # ROS 2 init (live mode)
    # ------------------------------------------------------------------

    async def _init_ros2(self) -> None:
        """Initialise rclpy node, publishers, and action clients."""
        try:
            import rclpy
            from rclpy.action import ActionClient

            rclpy.init()
            self._node = rclpy.create_node(f"rmf_fleet_adapter_{self.robot_name.replace('-', '_')}")

            # Odometry subscriber
            from nav_msgs.msg import Odometry  # type: ignore

            self._node.create_subscription(
                Odometry, f"{self.namespace}/odom", self._odom_callback, 10
            )

            # Nav2 action client
            from nav2_msgs.action import NavigateToPose  # type: ignore

            self._nav_client = ActionClient(
                self._node, NavigateToPose, f"{self.namespace}/navigate_to_pose"
            )

            # Robot state publisher
            from rmf_fleet_msgs.msg import RobotState as RmfRobotState  # type: ignore

            self._state_publisher = self._node.create_publisher(
                RmfRobotState, f"/rmf_fleet/{self.fleet_name}/robot_state", 10
            )

            logger.info("ROS 2 node initialised")
        except ImportError as exc:
            logger.warning(
                f"ROS 2 packages not available ({exc}). "
                "Falling back to stub mode. "
                "Source your ROS 2 workspace to enable live mode."
            )
            self._ros2_enabled = False

    # ------------------------------------------------------------------
    # Control loop
    # ------------------------------------------------------------------

    async def _control_loop(self) -> None:
        """Main async loop — updates state and publishes at 2 Hz."""
        while self._running:
            if self._ros2_enabled and self._node:
                import rclpy

                rclpy.spin_once(self._node, timeout_sec=0)
                self._check_live_arrival()
            else:
                self._step_simulation(dt=0.5)

            self._publish_state()
            await asyncio.sleep(0.5)

    def _check_live_arrival(self) -> None:
        """
        Arrival detection for live mode (stub mode handles it in
        _step_simulation). The authoritative signal is the Nav2 result
        callback; this proximity check is a fallback in case the result
        callback is missed (e.g. action server restart).
        """
        if self._nav_goal is None:
            return
        goal = self._nav_goal
        cur = self._state.location
        if math.hypot(goal.x - cur.x, goal.y - cur.y) < 0.35:
            self._on_goal_finished(success=True, source="proximity")

    def _on_goal_finished(self, success: bool, source: str = "nav2") -> None:
        """Clear the active goal and reset state (idempotent)."""
        if self._nav_goal is None:
            return
        goal = self._nav_goal
        self._nav_goal = None
        self._goal_handle = None
        self._state.mode = RobotMode.IDLE
        self._state.task_id = ""
        outcome = "arrived at" if success else "FAILED to reach"
        logger.info(f"[{source}] {self.robot_name} {outcome} ({goal.x:.2f}, {goal.y:.2f})")

    def _step_simulation(self, dt: float = 0.5) -> None:
        """
        Advance the simulated robot toward the current nav goal.
        Uses simple linear interpolation at 0.5 m/s.
        """
        if self._nav_goal is None:
            self._state.mode = RobotMode.IDLE
            return

        goal = self._nav_goal
        cur = self._state.location
        dx = goal.x - cur.x
        dy = goal.y - cur.y
        dist = math.hypot(dx, dy)

        SPEED = 0.5  # m/s

        if dist < 0.05:
            # Arrived
            cur.x, cur.y, cur.yaw = goal.x, goal.y, goal.yaw
            cur.t = time.time()
            self._nav_goal = None
            self._state.mode = RobotMode.IDLE
            self._state.task_id = ""
            logger.info(f"[SIM] {self.robot_name} arrived at ({goal.x:.2f}, {goal.y:.2f})")
        else:
            step = min(SPEED * dt, dist)
            cur.x += (dx / dist) * step
            cur.y += (dy / dist) * step
            cur.yaw = math.atan2(dy, dx)
            cur.t = time.time()
            self._state.mode = RobotMode.MOVING

        self._state.seq += 1

    # ------------------------------------------------------------------
    # ROS 2 callbacks (live mode)
    # ------------------------------------------------------------------

    def _odom_callback(self, msg: Any) -> None:
        """Update robot location from /odom topic."""
        pos = msg.pose.pose.position
        ori = msg.pose.pose.orientation
        # Convert quaternion → yaw
        siny = 2.0 * (ori.w * ori.z + ori.x * ori.y)
        cosy = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
        yaw = math.atan2(siny, cosy)

        self._state.location = RobotLocation(x=pos.x, y=pos.y, yaw=yaw)

    # ------------------------------------------------------------------
    # Navigation commands
    # ------------------------------------------------------------------

    async def navigate_to(self, x: float, y: float, yaw: float = 0.0, label: str = "") -> bool:
        """
        Command the robot to navigate to (x, y, yaw).

        In live mode: sends a Nav2 NavigateToPose action goal.
        In stub mode: sets the simulation target.

        Returns:
            True if the goal was accepted; False otherwise.
        """
        logger.info(
            f"[{self.robot_name}] Navigate to ({x:.2f}, {y:.2f}, {yaw:.2f}) label={label!r}"
        )

        self._nav_goal = Nav2Goal(x=x, y=y, yaw=yaw, label=label)
        self._state.mode = RobotMode.MOVING

        if not self._ros2_enabled or not self._nav_client:
            return True

        try:
            from geometry_msgs.msg import PoseStamped  # type: ignore
            from nav2_msgs.action import NavigateToPose  # type: ignore

            goal_msg = NavigateToPose.Goal()
            goal_msg.pose = PoseStamped()
            goal_msg.pose.header.frame_id = "map"
            goal_msg.pose.header.stamp = self._node.get_clock().now().to_msg()
            goal_msg.pose.pose.position.x = x
            goal_msg.pose.pose.position.y = y
            goal_msg.pose.pose.orientation.z = math.sin(yaw / 2)
            goal_msg.pose.pose.orientation.w = math.cos(yaw / 2)

            if not self._nav_client.wait_for_server(timeout_sec=5.0):
                logger.error("Nav2 action server not available after 5 s")
                self._on_goal_finished(success=False, source="nav2")
                return False

            # rclpy futures are NOT concurrent.futures — they complete only
            # while the node spins (our control loop calls spin_once at 2 Hz),
            # so poll with asyncio sleeps instead of asyncio.wrap_future().
            send_future = self._nav_client.send_goal_async(goal_msg)
            goal_handle = await self._await_rclpy_future(send_future, timeout=10.0)
            if goal_handle is None or not goal_handle.accepted:
                logger.error("Nav2 rejected the navigation goal")
                self._on_goal_finished(success=False, source="nav2")
                return False

            self._goal_handle = goal_handle
            result_future = goal_handle.get_result_async()
            result_future.add_done_callback(self._nav_result_callback)
            logger.info(f"Nav2 accepted goal for {self.robot_name}")
            return True
        except Exception as exc:
            logger.error(f"Nav2 goal failed: {exc}")
            self._on_goal_finished(success=False, source="nav2")
            return False

    async def _await_rclpy_future(self, future: Any, timeout: float) -> Any:
        """Await an rclpy future by polling (the control loop does the spinning)."""
        deadline = time.time() + timeout
        while not future.done():
            if time.time() > deadline:
                return None
            await asyncio.sleep(0.1)
        return future.result()

    def _nav_result_callback(self, future: Any) -> None:
        """Invoked (during spin_once) when Nav2 reports the goal finished."""
        try:
            status = future.result().status
            # GoalStatus.STATUS_SUCCEEDED == 4
            self._on_goal_finished(success=(status == 4), source="nav2")
        except Exception as exc:
            logger.error(f"Nav2 result callback error: {exc}")
            self._on_goal_finished(success=False, source="nav2")

    async def navigate_to_waypoint(
        self, waypoint_name: str, waypoint_map: dict[str, tuple]
    ) -> bool:
        """
        Navigate to a named waypoint using a name→(x, y, yaw) lookup map.

        Args:
            waypoint_name: e.g. "charging_dock"
            waypoint_map:  e.g. {"charging_dock": (-5.0, -2.0, 0.0)}
        """
        coords = waypoint_map.get(waypoint_name.lower().replace(" ", "_"))
        if not coords:
            logger.warning(f"Unknown waypoint: {waypoint_name!r}")
            return False
        x, y, yaw = coords if len(coords) == 3 else (*coords, 0.0)
        return await self.navigate_to(x, y, yaw, label=waypoint_name)

    def _cancel_nav2_goal(self) -> None:
        """Ask Nav2 to abort the in-flight goal (live mode only)."""
        if self._goal_handle is not None:
            try:
                self._goal_handle.cancel_goal_async()
            except Exception as exc:
                logger.warning(f"Nav2 cancel failed: {exc}")
            self._goal_handle = None

    async def pause(self) -> None:
        """Pause current navigation."""
        self._cancel_nav2_goal()
        self._state.mode = RobotMode.PAUSED
        self._nav_goal = None
        logger.info(f"[{self.robot_name}] Paused")

    async def resume(self) -> None:
        """Resume after pause."""
        if self._state.mode == RobotMode.PAUSED:
            self._state.mode = RobotMode.IDLE
            logger.info(f"[{self.robot_name}] Resumed")

    async def emergency_stop(self) -> None:
        """Trigger emergency stop."""
        self._cancel_nav2_goal()
        self._nav_goal = None
        self._state.mode = RobotMode.EMERGENCY
        self._state.task_id = ""
        logger.warning(f"[{self.robot_name}] EMERGENCY STOP")

    # ------------------------------------------------------------------
    # State publishing
    # ------------------------------------------------------------------

    def _publish_state(self) -> None:
        """Publish current robot state to all registered callbacks."""
        for cb in self._state_callbacks:
            try:
                cb(self._state)
            except Exception as exc:
                logger.error(f"State callback error: {exc}")

        if self._state_publisher:
            self._publish_ros2_state()

    def _publish_ros2_state(self) -> None:
        """Publish rmf_fleet_msgs/RobotState to ROS 2."""
        try:
            from rmf_fleet_msgs.msg import (
                Location,
            )
            from rmf_fleet_msgs.msg import (  # type: ignore
                RobotState as RmfRobotState,
            )

            msg = RmfRobotState()
            msg.name = self._state.name
            msg.model = "turtlebot3"
            msg.task_id = self._state.task_id
            msg.seq = self._state.seq
            msg.mode.mode = int(self._state.mode)
            msg.battery_percent = self._state.battery_percent
            loc = Location()
            loc.x = self._state.location.x
            loc.y = self._state.location.y
            loc.yaw = self._state.location.yaw
            loc.level_name = self._state.location.level_name
            loc.t.sec = int(self._state.location.t)
            msg.location = loc
            self._state_publisher.publish(msg)
        except Exception as exc:
            logger.debug(f"ROS 2 state publish skipped: {exc}")

    # ------------------------------------------------------------------
    # State subscription
    # ------------------------------------------------------------------

    def on_state_update(self, callback: Callable[[RobotState], None]) -> None:
        """Register a callback invoked every time robot state is published."""
        self._state_callbacks.append(callback)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def state(self) -> RobotState:
        return self._state

    def state_dict(self) -> dict[str, Any]:
        """Serialisable state snapshot."""
        loc = self._state.location
        return {
            "name": self._state.name,
            "fleet": self._state.fleet_name,
            "mode": self._state.mode.name,
            "battery_percent": self._state.battery_percent,
            "location": {
                "x": round(loc.x, 3),
                "y": round(loc.y, 3),
                "yaw": round(loc.yaw, 3),
                "level": loc.level_name,
            },
            "task_id": self._state.task_id,
            "seq": self._state.seq,
        }


# ---------------------------------------------------------------------------
# Warehouse waypoints — a read-only view of config/maps/isaac_warehouse.json.
# The authoritative, editable map lives in the Nayantra Core registry; this
# table only exists for the legacy single-robot CLI below and old imports.
# ---------------------------------------------------------------------------


def load_map_waypoints(path: Path | None = None) -> dict[str, tuple]:
    """{waypoint id and aliases (lower-case): (x, y, yaw)} from a map seed file."""
    import json

    path = path or Path(__file__).resolve().parents[2] / "config" / "maps" / "isaac_warehouse.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    table: dict[str, tuple] = {}
    for w in data.get("waypoints", []):
        coords = (float(w["x"]), float(w["y"]), float(w.get("yaw", 0.0)))
        for key in [w["id"], *w.get("aliases", [])]:
            table[key.lower()] = coords
    return table


WAREHOUSE_WAYPOINTS: dict[str, tuple] = load_map_waypoints()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Nayantra RMF Fleet Adapter")
    parser.add_argument("--fleet", default="turtlebot_fleet", help="Fleet name")
    parser.add_argument("--robot", default="turtlebot3_1", help="Robot name")
    parser.add_argument("--ros2", action="store_true", help="Enable live ROS 2 mode")
    args = parser.parse_args()

    adapter = RMFFleetAdapter(
        fleet_name=args.fleet,
        robot_name=args.robot,
        ros2_enabled=args.ros2,
    )

    def log_state(s: RobotState) -> None:
        logger.debug(f"{s.name} mode={s.mode.name} pos=({s.location.x:.2f},{s.location.y:.2f})")

    adapter.on_state_update(log_state)

    try:
        asyncio.run(adapter.start())
    except KeyboardInterrupt:
        asyncio.run(adapter.stop())


if __name__ == "__main__":
    main()
