"""
nayantra/core/adapters/nav2.py

ROS 2 + Nav2 robot adapter (Isaac Sim, Gazebo or real hardware).

Per robot, under its namespace (e.g. "/ugv_01" → "/ugv_01/odom"):
  subscribes  odom            nav_msgs/Odometry
              battery_state   sensor_msgs/BatteryState       (optional)
  actions     navigate_through_poses  nav2_msgs/NavigateThroughPoses (preferred)
              navigate_to_pose        nav2_msgs/NavigateToPose       (fallback)
  publishes   cmd_vel         geometry_msgs/Twist or TwistStamped (matched to
                              the robot, see ros_compat) — ONLY a zero twist
                              on emergency stop (safety controller). Motion is
                              always delegated to Nav2; nothing upstream can
                              command velocities.

Pose: TF map→base_frame when available, else odometry (valid when map→odom
is identity, as in scripts/nav2_demo.launch.py).

ROS 2 Humble and Jazzy are both supported (ros_compat.py); the connection test
reports which distro is sourced.

Status: written against the rclpy / nav2_msgs APIs but NOT yet run
against a live Nav2 stack (CI and the dev laptop have no ROS 2). Validate on
the Isaac + Nav2 workstation before relying on it; the connection test
reports each interface separately to make that bring-up quick.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import Any

from nayantra.core.adapters import ros_compat
from nayantra.core.adapters.base import AdapterSnapshot, CheckResult, RobotAdapter
from nayantra.core.adapters.ros2_runtime import Ros2Runtime
from nayantra.core.models import EffectiveRobotConfig, Pose, Robot, RobotHealth, Velocity

logger = logging.getLogger("nayantra.core.nav2")

PASS_TOL_M = 0.6
ARRIVE_TOL_M = 0.35
ODOM_STALE_S = 2.0


class Nav2Adapter(RobotAdapter):
    kind = "ros2_nav2"

    def __init__(self, robot: Robot, config: EffectiveRobotConfig, spawn: Pose | None) -> None:
        super().__init__(robot, config, spawn)
        ns = robot.navigation.namespace or config.communication.namespace or ""
        self.ns = ("/" + ns.strip("/")) if ns.strip("/") else ""
        self.base_frame = robot.navigation.base_frame or "base_link"
        self.map_frame = robot.navigation.frame or "map"
        self._rt: Ros2Runtime | None = None
        self._subs: list[Any] = []
        self._to_pose = None
        self._through = None
        self._cmd_pub = None
        self.pose = Pose(**self.spawn.model_dump())
        self.vel = Velocity()
        self.last_odom = 0.0
        self.battery: float | None = None
        self.charging = False
        self.online = False
        self.path: list[Pose] = []
        self.path_index = -1
        self.paused = False
        self.estop = False
        self.failed = ""
        self._goal_handle = None
        self._goal_task: asyncio.Task | None = None
        self._goal_seq = 0
        self._use_through = False
        self._stamped = False

    # ------------------------------------------------------------------
    def _topic(self, name: str) -> str:
        return f"{self.ns}/{name}"

    async def connect(self) -> list[CheckResult]:
        rt = await asyncio.to_thread(Ros2Runtime.get)
        self._rt = rt
        if not rt.available:
            self.online = False
            return [
                CheckResult(
                    "ROS 2 available",
                    False,
                    f"rclpy could not start in the core process ({rt.error}). Start nayantra-core "
                    "from a shell with ROS 2 sourced (source /opt/ros/jazzy/setup.bash) and the "
                    "same ROS_DOMAIN_ID as the robot.",
                )
            ]
        distro_ok, distro_detail = ros_compat.distro_support(ros_compat.ros_distro())
        checks = [
            CheckResult(
                "ROS 2 available", True, f"node /nayantra_core, ROS_DOMAIN_ID={rt.domain_id}"
            ),
            CheckResult("ROS 2 distribution", distro_ok, distro_detail, required=False),
        ]
        try:
            self._create_interfaces(rt)
        except Exception as exc:  # noqa: BLE001
            self.online = False
            return checks + [
                CheckResult(
                    "ROS 2 interfaces", False, f"could not create subscriptions/clients: {exc}"
                )
            ]

        odom_topic = self._topic("odom")
        pubs = rt.publisher_count(odom_topic)
        checks.append(
            CheckResult(
                "Robot discovered",
                pubs > 0,
                f"{pubs} publisher(s) on {odom_topic}"
                if pubs
                else f"nothing publishes {odom_topic} — is the robot / simulator running with namespace '{self.ns or '/'}'?",
            )
        )
        deadline = time.monotonic() + 3.0
        while self.last_odom == 0.0 and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        checks.append(
            CheckResult(
                "Odometry available",
                self.last_odom > 0,
                f"receiving {odom_topic}"
                if self.last_odom
                else f"no message on {odom_topic} within 3 s",
            )
        )
        tf_pose = rt.lookup_pose(self.map_frame, self.base_frame)
        checks.append(
            CheckResult(
                "TF available",
                tf_pose is not None,
                f"{self.map_frame} → {self.base_frame}"
                if tf_pose
                else f"no transform {self.map_frame} → {self.base_frame}; using odometry as map pose",
                required=False,
            )
        )
        nav_ok = await asyncio.to_thread(self._wait_nav, 3.0)
        checks.append(
            CheckResult(
                "Navigation interface available",
                nav_ok,
                (
                    "Nav2 "
                    + ("navigate_through_poses" if self._use_through else "navigate_to_pose")
                    + f" under '{self.ns or '/'}'"
                )
                if nav_ok
                else f"no Nav2 action server at {self._topic('navigate_to_pose')} — is Nav2 running and active?",
            )
        )
        bat_pubs = rt.publisher_count(self._topic("battery_state"))
        checks.append(
            CheckResult(
                "Battery telemetry available",
                bat_pubs > 0,
                f"{self._topic('battery_state')}"
                if bat_pubs
                else "no sensor_msgs/BatteryState published (battery shown as not reported)",
                required=False,
            )
        )
        map_pubs = rt.publisher_count("/map")
        checks.append(
            CheckResult(
                "Map available",
                map_pubs > 0 or self.config.map_id is not None,
                ("/map published by map_server" if map_pubs else "no /map topic; ")
                + (
                    f" fleet routing uses Nayantra map '{self.config.map_id}'"
                    if self.config.map_id
                    else ""
                ),
                required=False,
            )
        )
        self.online = self.last_odom > 0 and nav_ok
        return checks

    def _create_interfaces(self, rt: Ros2Runtime) -> None:
        from geometry_msgs.msg import Twist, TwistStamped  # type: ignore
        from nav2_msgs.action import NavigateThroughPoses, NavigateToPose  # type: ignore
        from nav_msgs.msg import Odometry  # type: ignore
        from rclpy.action import ActionClient  # type: ignore

        node = rt.node
        if not self._subs:
            self._subs.append(
                node.create_subscription(Odometry, self._topic("odom"), self._on_odom, 10)
            )
            try:
                from sensor_msgs.msg import BatteryState  # type: ignore

                self._subs.append(
                    node.create_subscription(
                        BatteryState, self._topic("battery_state"), self._on_battery, 10
                    )
                )
            except Exception:  # noqa: BLE001
                pass
            self._to_pose = ActionClient(node, NavigateToPose, self._topic("navigate_to_pose"))
            self._through = ActionClient(
                node, NavigateThroughPoses, self._topic("navigate_through_poses")
            )
            cmd_topic = self._topic("cmd_vel")
            try:
                seen = [
                    t
                    for name, ts in node.get_topic_names_and_types()
                    if name == cmd_topic
                    for t in ts
                ]
            except Exception:  # noqa: BLE001
                seen = []
            self._stamped = ros_compat.use_stamped_cmd_vel(
                seen, os.getenv("NAYANTRA_CMD_VEL_STAMPED")
            )
            self._cmd_pub = node.create_publisher(
                TwistStamped if self._stamped else Twist, cmd_topic, 10
            )
        self._Twist = Twist
        self._TwistStamped = TwistStamped
        self._NavToPose = NavigateToPose
        self._NavThrough = NavigateThroughPoses

    def _wait_nav(self, timeout: float) -> bool:
        if self._through is not None and self._through.wait_for_server(timeout_sec=timeout / 2):
            self._use_through = True
            return True
        return bool(
            self._to_pose is not None and self._to_pose.wait_for_server(timeout_sec=timeout / 2)
        )

    # -- callbacks (executor thread) --------------------------------------
    def _on_odom(self, msg: Any) -> None:
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        yaw = math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))
        self.pose = Pose(x=p.x, y=p.y, z=p.z, yaw=yaw)
        t = msg.twist.twist
        self.vel = Velocity(
            linear=math.hypot(t.linear.x, t.linear.y), angular=t.angular.z, vertical=t.linear.z
        )
        self.last_odom = time.monotonic()

    def _on_battery(self, msg: Any) -> None:
        pct = msg.percentage
        if pct == pct:  # not NaN
            self.battery = pct * 100.0 if pct <= 1.0 else pct
        self.charging = int(getattr(msg, "power_supply_status", 0)) == 1  # CHARGING

    # ------------------------------------------------------------------
    def _map_pose(self) -> Pose:
        if self._rt is not None:
            tf = self._rt.lookup_pose(self.map_frame, self.base_frame)
            if tf is not None:
                return Pose(x=tf[0], y=tf[1], z=self.pose.z, yaw=tf[2])
        return self.pose

    def snapshot(self) -> AdapterSnapshot:
        pose = self._map_pose()
        fresh = self.last_odom > 0 and time.monotonic() - self.last_odom < ODOM_STALE_S
        # progress by proximity along the commanded path
        while self.path and self.path_index < len(self.path) - 1:
            nxt = self.path[self.path_index + 1]
            last = self.path_index + 1 == len(self.path) - 1
            if math.hypot(nxt.x - pose.x, nxt.y - pose.y) < (ARRIVE_TOL_M if last else PASS_TOL_M):
                self.path_index += 1
            else:
                break
        if not self.online:
            state = "offline"
        elif self.estop:
            state = "estop"
        elif self.failed:
            state = "failed"
        elif self.paused:
            state = "paused"
        elif self.path and self.path_index < len(self.path) - 1:
            state = "moving"
        elif self.path:
            state = "arrived"
        else:
            state = "idle"
        localization = None
        if self._rt is not None:
            localization = (
                "ok" if self._rt.lookup_pose(self.map_frame, self.base_frame) else "odometry only"
            )
        return AdapterSnapshot(
            online=self.online and fresh,
            pose=pose,
            velocity=self.vel,
            battery_pct=self.battery,
            charging=self.charging,
            nav_state=state if fresh or not self.online else "offline",
            nav_detail=self.failed or ("" if fresh else "odometry stale"),
            path_index=self.path_index,
            path_len=len(self.path),
            health=RobotHealth(
                localization=localization,
                navigation="ok"
                if self.online and not self.failed
                else ("error" if self.failed else None),
                network=f"ROS 2 DDS (domain {self._rt.domain_id})" if self._rt else None,
            ),
        )

    # ------------------------------------------------------------------
    def _pose_stamped(self, p: Pose) -> Any:
        from geometry_msgs.msg import PoseStamped  # type: ignore

        ps = PoseStamped()
        ps.header.frame_id = self.map_frame
        ps.header.stamp = self._rt.node.get_clock().now().to_msg()
        ps.pose.position.x = float(p.x)
        ps.pose.position.y = float(p.y)
        ps.pose.orientation.z = math.sin(p.yaw / 2.0)
        ps.pose.orientation.w = math.cos(p.yaw / 2.0)
        return ps

    async def follow_path(self, path: list[Pose], speed_limits: list[float] | None = None) -> None:
        if self.estop or not self.online:
            return
        self.path = list(path)
        self.path_index = -1
        self.failed = ""
        self.paused = False
        await self._send_remaining()

    async def _send_remaining(self) -> None:
        remaining = self.path[self.path_index + 1 :]
        if not remaining:
            return
        self._goal_seq += 1
        seq = self._goal_seq
        if self._use_through and len(remaining) > 1:
            goal = self._NavThrough.Goal()
            goal.poses = [self._pose_stamped(p) for p in remaining]
            client = self._through
            target_idx = len(self.path) - 1
        elif self._use_through:
            goal = self._NavToPose.Goal()
            goal.pose = self._pose_stamped(remaining[-1])
            client = self._to_pose
            target_idx = len(self.path) - 1
        else:
            # NavigateToPose only: go point by point along the cleared path.
            goal = self._NavToPose.Goal()
            goal.pose = self._pose_stamped(remaining[0])
            client = self._to_pose
            target_idx = self.path_index + 1
        if self._goal_task and not self._goal_task.done():
            self._goal_task.cancel()
        self._goal_task = asyncio.create_task(self._run_goal(client, goal, seq, target_idx))

    async def _await(self, fut: Any, timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        while not fut.done():
            if time.monotonic() > deadline:
                return None
            await asyncio.sleep(0.05)
        return fut.result()

    async def _run_goal(self, client: Any, goal: Any, seq: int, target_idx: int) -> None:
        try:
            handle = await self._await(client.send_goal_async(goal), 10.0)
            if seq != self._goal_seq:
                return
            if handle is None or not handle.accepted:
                self.failed = "Nav2 rejected the goal"
                return
            self._goal_handle = handle
            result = await self._await(handle.get_result_async(), 3600.0)
            if seq != self._goal_seq:
                return
            status = getattr(result, "status", None)
            if status == 4:  # SUCCEEDED
                self.path_index = max(self.path_index, target_idx)
                if self.path_index < len(self.path) - 1:
                    await self._send_remaining()
            elif status in (5, 6):  # CANCELED, ABORTED
                if status == 6 and not self.paused:
                    self.failed = "Nav2 aborted the goal (planner/controller failure)"
            elif status is not None:
                self.failed = f"Nav2 goal ended with status {status}"
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.failed = f"Nav2 goal error: {exc}"

    def _cancel_goal(self) -> None:
        self._goal_seq += 1
        if self._goal_handle is not None:
            try:
                self._goal_handle.cancel_goal_async()
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{self.robot.name}: Nav2 cancel failed: {exc}")
            self._goal_handle = None

    async def stop(self) -> None:
        self._cancel_goal()
        self.path, self.path_index = [], -1

    async def pause(self) -> None:
        self.paused = True
        self._cancel_goal()

    async def resume(self) -> None:
        self.paused = False
        await self._send_remaining()

    async def emergency_stop(self) -> None:
        self.estop = True
        self._cancel_goal()
        self.path, self.path_index = [], -1
        if self._cmd_pub is not None:
            for _ in range(3):
                try:
                    self._cmd_pub.publish(self._zero_cmd())
                except Exception:  # noqa: BLE001
                    break
                await asyncio.sleep(0.05)

    def _zero_cmd(self) -> Any:
        if not self._stamped:
            return self._Twist()
        msg = self._TwistStamped()
        msg.header.frame_id = self.base_frame
        msg.header.stamp = self._rt.node.get_clock().now().to_msg()
        return msg

    async def release_estop(self) -> None:
        self.estop = False

    async def disconnect(self) -> None:
        self._cancel_goal()
        self.online = False
        if self._rt is not None and self._rt.node is not None:
            for sub in self._subs:
                try:
                    self._rt.node.destroy_subscription(sub)
                except Exception:  # noqa: BLE001
                    pass
        self._subs = []

    async def restart_navigation(self) -> list[CheckResult]:
        self.failed = ""
        return await self.connect()
