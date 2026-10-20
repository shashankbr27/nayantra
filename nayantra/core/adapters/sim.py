"""
nayantra/core/adapters/sim.py

Nayantra's built-in **kinematic** simulator. It is a simulator, labelled as
such everywhere: poses are ground truth, dynamics are acceleration-limited
point kinematics, and no sensor noise or physics is modelled. It exists so the
whole orchestration stack (allocation, traffic, tasks, UI) runs with many
heterogeneous robots on a laptop. Isaac Sim / Gazebo robots connect through
the ROS 2 (Nav2) adapter instead and use the same fleet interfaces.

Per robot type:
  * UGV       — differential drive: turns in place for large heading errors
  * quadruped / humanoid — turns while walking, slower
  * UAV       — 3-D: climbs before cruising, cruises before descending;
                hovering drains the battery

Safety: every tick the engine runs a forward stop-zone check between robots
on the same map and layer (analogous to Nav2's collision_monitor). If
coordination ever failed, robots stop instead of overlapping, and a
SAFETY_STOP event is raised.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from nayantra.core.adapters.base import AdapterSnapshot, CheckResult, RobotAdapter
from nayantra.core.events import EventBus
from nayantra.core.geometry import angle_wrap
from nayantra.core.models import (
    EffectiveRobotConfig,
    EventType,
    Pose,
    Robot,
    RobotHealth,
    Severity,
    Velocity,
)

logger = logging.getLogger("nayantra.core.sim")

TICK_S = 0.05
PASS_TOL_M = 0.35  # intermediate path points: pass within this distance
ARRIVE_TOL_M = 0.05
STOP_ZONE_MARGIN_M = 0.25
DWELL_ACTIONS_S = {"dock": 3.0, "undock": 2.0}


class SimAdapter(RobotAdapter):
    kind = "simulation"
    native_actions = frozenset({"charge", "dock", "undock", "takeoff", "land"})

    def __init__(
        self,
        robot: Robot,
        config: EffectiveRobotConfig,
        spawn: Pose | None,
        engine: SimEngine,
        spawn_label: str = "",
    ) -> None:
        super().__init__(robot, config, spawn)
        self.engine = engine
        self.spawn_label = spawn_label
        self.pose = Pose(**self.spawn.model_dump())
        self.v = 0.0
        self.w = 0.0
        self.vz = 0.0
        self.battery = float(robot.spawn.battery_pct)
        self.path: list[Pose] = []
        self.limits: list[float] = []
        self.path_index = -1
        self.paused = False
        self.estop = False
        self.protective_stop = False
        self.action: str | None = None
        self.action_t = 0.0
        self.action_done = False
        self.charging = False
        self.online = False
        self.failed = ""
        self.odometer_m = 0.0

    # ------------------------------------------------------------------
    @property
    def is_air(self) -> bool:
        return self.config.layer == "air"

    @property
    def radius(self) -> float:
        return self.config.footprint.effective_radius

    async def connect(self) -> list[CheckResult]:
        self.engine.register(self)
        self.online = True
        self.failed = ""
        where = self.spawn_label or f"({self.pose.x:.1f}, {self.pose.y:.1f})"
        map_ok = self.config.map_id is not None
        return [
            CheckResult(
                "Robot discovered",
                True,
                f"kinematic simulator spawned {self.robot.name} at {where}",
            ),
            CheckResult("Connection established", True, "in-process built-in simulator (no ROS 2)"),
            CheckResult("Pose available", True, "ground truth from the simulator"),
            CheckResult("Odometry available", True, "simulated, noise-free"),
            CheckResult("Navigation interface available", True, "kinematic path follower"),
            CheckResult("Battery telemetry available", True, "simulated discharge model"),
            CheckResult(
                "Map available",
                map_ok,
                f"operating on map '{self.config.map_id}'"
                if map_ok
                else "no map assigned to this robot or its fleet",
            ),
        ]

    async def disconnect(self) -> None:
        self.online = False
        self.engine.unregister(self.robot_id)

    def reset(self) -> None:
        self.pose = Pose(**self.spawn.model_dump())
        self.v = self.w = self.vz = 0.0
        self.battery = float(self.robot.spawn.battery_pct)
        self.path, self.limits, self.path_index = [], [], -1
        self.paused = self.estop = self.protective_stop = False
        self.action, self.action_done, self.charging = None, False, False
        self.failed = ""

    def snapshot(self) -> AdapterSnapshot:
        if not self.online:
            state = "offline"
        elif self.failed:
            state = "failed"
        elif self.estop:
            state = "estop"
        elif self.paused:
            state = "paused"
        elif self.path and self.path_index < len(self.path) - 1:
            state = "moving"
        elif self.path:
            state = "arrived"
        else:
            state = "idle"
        detail = self.failed or (
            "protective stop: obstacle robot in stop zone" if self.protective_stop else ""
        )
        return AdapterSnapshot(
            online=self.online,
            pose=Pose(**self.pose.model_dump()),
            velocity=Velocity(
                linear=round(self.v, 3), angular=round(self.w, 3), vertical=round(self.vz, 3)
            ),
            battery_pct=round(self.battery, 2),
            charging=self.charging,
            nav_state=state,
            nav_detail=detail,
            path_index=self.path_index,
            path_len=len(self.path),
            action=self.action,
            action_done=self.action_done,
            health=RobotHealth(
                localization="ground_truth",
                navigation="ok (kinematic sim)" if not self.failed else "error",
                network="in-process",
                sensors={},
            ),
        )

    # ------------------------------------------------------------------
    async def follow_path(self, path: list[Pose], speed_limits: list[float] | None = None) -> None:
        if self.estop or self.failed:
            return
        self.path = [Pose(**p.model_dump()) for p in path]
        cap = self.config.max_speed
        self.limits = [min(cap, s) for s in (speed_limits or [cap] * len(path))]
        self.path_index = -1
        if self.charging:
            self.charging = False
            self.action = None

    async def stop(self) -> None:
        self.path, self.limits, self.path_index = [], [], -1
        if self.action and self.action != "charge":
            self.action = None

    async def pause(self) -> None:
        self.paused = True

    async def resume(self) -> None:
        self.paused = False

    async def emergency_stop(self) -> None:
        self.estop = True
        self.v = self.w = self.vz = 0.0
        self.path, self.limits, self.path_index = [], [], -1
        self.action, self.charging = None, False

    async def release_estop(self) -> None:
        self.estop = False

    async def start_action(self, action: str, params: dict[str, Any] | None = None) -> None:
        if action not in self.native_actions:
            raise NotImplementedError(f"simulated robots have no native '{action}' action")
        self.action = action
        self.action_t = 0.0
        self.action_done = False
        self.charging = action == "charge"

    async def cancel_action(self) -> None:
        self.action = None
        self.action_done = False
        self.charging = False

    # ------------------------------------------------------------------
    # Physics
    # ------------------------------------------------------------------

    def _drain(self, dt: float, moved_m: float) -> None:
        b = self.config.battery
        idle = b.drain_pct_per_min_idle
        if self.is_air and self.pose.z > 0.3:
            idle = max(idle, 0.02) * 12  # hovering is expensive
        self.battery -= idle * dt / 60.0 + b.drain_pct_per_m * moved_m
        if self.battery <= 0:
            self.battery = 0.0
            if not self.failed:
                self.failed = "battery depleted"
                self.path, self.path_index = [], -1
                self.v = self.vz = 0.0

    def _step_action(self, dt: float) -> None:
        b = self.config.battery
        self.action_t += dt
        if self.action == "charge":
            self.battery = min(100.0, self.battery + b.charge_rate_pct_per_min * dt / 60.0)
            if self.battery >= b.charged_pct:
                self.action_done = True
        elif self.action in DWELL_ACTIONS_S:
            if self.action_t >= DWELL_ACTIONS_S[self.action]:
                self.action_done = True
        elif self.action in ("takeoff", "land"):
            target = (self.config.cruise_altitude or 5.0) if self.action == "takeoff" else 0.0
            vmax = self.config.max_vertical_speed or 1.0
            dz = target - self.pose.z
            step = max(-vmax * dt, min(vmax * dt, dz))
            self.pose.z += step
            self.vz = step / dt if dt else 0.0
            if abs(target - self.pose.z) < 0.02:
                self.pose.z = target
                self.vz = 0.0
                self.action_done = True

    def _corner_distance(self) -> float:
        """Distance to the next point where we must slow right down (sharp turn / end)."""
        pts = self.path[self.path_index + 1 :]
        if not pts:
            return 0.0
        total = math.hypot(pts[0].x - self.pose.x, pts[0].y - self.pose.y)
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            prev = (
                a.x - (pts[i - 1].x if i else self.pose.x),
                a.y - (pts[i - 1].y if i else self.pose.y),
            )
            nxt = (b.x - a.x, b.y - a.y)
            turn = abs(angle_wrap(math.atan2(nxt[1], nxt[0]) - math.atan2(prev[1], prev[0])))
            if turn > 0.6 or abs(b.z - a.z) > 0.3:
                return total
            total += math.hypot(nxt[0], nxt[1])
        return total

    def step(self, dt: float) -> None:
        if not self.online:
            return
        if self.estop or self.failed:
            self.v = self.w = self.vz = 0.0
            self._drain(dt, 0.0)
            return
        if self.action and not self.action_done:
            self.v = self.w = 0.0
            self._step_action(dt)
            if self.action != "charge":
                self._drain(dt, 0.0)
            return
        if (
            self.paused
            or self.protective_stop
            or not self.path
            or self.path_index >= len(self.path) - 1
        ):
            decel = self.config.max_decel
            self.v = max(0.0, self.v - decel * dt)
            self.w = 0.0
            self.vz = 0.0
            self._drain(dt, 0.0)
            return

        target = self.path[self.path_index + 1]
        limit = self.limits[self.path_index + 1] if self.limits else self.config.max_speed
        is_last = self.path_index + 1 == len(self.path) - 1
        dx, dy = target.x - self.pose.x, target.y - self.pose.y
        dist = math.hypot(dx, dy)
        moved = 0.0

        if self.is_air:
            dz = target.z - self.pose.z
            vmax_z = self.config.max_vertical_speed or 1.0
            climb_first = dz > 0.3 and dist > 0.1
            if climb_first or (abs(dz) > 0.02 and dist <= 0.2):
                stepz = max(-vmax_z * dt, min(vmax_z * dt, dz))
                self.pose.z += stepz
                self.vz = stepz / dt
                self.v = max(0.0, self.v - self.config.max_decel * dt)
                moved = abs(stepz)
            else:
                self.vz = 0.0
                if dist > 1e-6:
                    self.pose.yaw = math.atan2(dy, dx)
                # descending: altitude is held until horizontally over the target
                moved = self._advance(dt, dist, dx, dy, limit)
            arrived = (
                dist < (ARRIVE_TOL_M if is_last else PASS_TOL_M)
                and abs(target.z - self.pose.z) < 0.05
            )
        else:
            heading = math.atan2(dy, dx) if dist > 1e-6 else self.pose.yaw
            err = angle_wrap(heading - self.pose.yaw)
            yaw_rate = self.config.max_yaw_rate
            turn = max(-yaw_rate * dt, min(yaw_rate * dt, err * 3.0 * dt))
            self.pose.yaw = angle_wrap(self.pose.yaw + turn)
            self.w = turn / dt
            turn_in_place = self.config.robot_type == "ugv" and abs(err) > 0.5 and dist > 0.2
            if turn_in_place:
                self.v = max(0.0, self.v - self.config.max_decel * dt)
            else:
                scale = max(0.0, math.cos(min(abs(err), math.pi / 2)))
                moved = self._advance(dt, dist, dx, dy, limit * max(scale, 0.3))
            arrived = dist < (ARRIVE_TOL_M if is_last else PASS_TOL_M)

        self.odometer_m += moved
        self._drain(dt, moved)
        if arrived:
            self.path_index += 1
            if is_last:
                self.pose.x, self.pose.y = target.x, target.y
                if self.is_air:
                    self.pose.z = target.z
                self.v = 0.0

    def _advance(self, dt: float, dist: float, dx: float, dy: float, limit: float) -> float:
        cfg = self.config
        brake_d = self._corner_distance()
        v_brake = math.sqrt(max(0.0, 2 * cfg.max_decel * max(0.0, brake_d - 0.02)))
        v_target = min(limit, v_brake + 0.15)
        if v_target > self.v:
            self.v = min(v_target, self.v + cfg.max_accel * dt)
        else:
            self.v = max(v_target, self.v - cfg.max_decel * dt)
        step = min(self.v * dt, dist)
        if dist > 1e-9:
            self.pose.x += dx / dist * step
            self.pose.y += dy / dist * step
        return step


class SimEngine:
    """Steps every simulated robot; owns sim clock controls and the stop-zone check."""

    def __init__(self, bus: EventBus, environment: str = "Built-in kinematic simulator") -> None:
        self.bus = bus
        self.environment = environment
        self.adapters: dict[str, SimAdapter] = {}
        self.running = True
        self.speed = 1.0
        self.sim_time = 0.0
        self._task: asyncio.Task | None = None
        self._last_safety_event: dict[tuple[str, str], float] = {}
        self.on_reset = None  # set by the fleet manager

    def register(self, adapter: SimAdapter) -> None:
        self.adapters[adapter.robot_id] = adapter

    def unregister(self, robot_id: str) -> None:
        self.adapters.pop(robot_id, None)

    async def start_loop(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="nayantra-sim")

    async def stop_loop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        last = time.monotonic()
        while True:
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            dt = min(0.25, now - last)
            last = now
            if self.running:
                try:
                    self.step(dt * self.speed)
                except Exception:  # noqa: BLE001 — the sim must never die
                    logger.exception("sim step failed")

    def step(self, dt: float) -> None:
        self.sim_time += dt
        self._stop_zones()
        # sub-step fast sims to keep kinematics stable
        n = max(1, math.ceil(dt / 0.1))
        for _ in range(n):
            for adapter in list(self.adapters.values()):
                adapter.step(dt / n)

    def _stop_zones(self) -> None:
        robots = [a for a in self.adapters.values() if a.online]
        for a in robots:
            a.protective_stop = False
        now = time.monotonic()
        for a in robots:
            if a.v < 0.02 and abs(a.vz) < 0.02:
                continue
            ca, sa = math.cos(a.pose.yaw), math.sin(a.pose.yaw)
            for b in robots:
                if b is a or b.config.map_id != a.config.map_id or b.config.layer != a.config.layer:
                    continue
                rx, ry, rz = b.pose.x - a.pose.x, b.pose.y - a.pose.y, b.pose.z - a.pose.z
                d = math.sqrt(rx * rx + ry * ry + (rz * rz if a.is_air else 0.0))
                limit = a.radius + b.radius + STOP_ZONE_MARGIN_M
                if d >= limit or d < 1e-6:
                    continue
                ahead = (rx * ca + ry * sa) / d
                if ahead > 0.3:
                    a.protective_stop = True
                    key = (a.robot_id, b.robot_id)
                    if now - self._last_safety_event.get(key, 0.0) > 10.0:
                        self._last_safety_event[key] = now
                        self.bus.emit(
                            EventType.SAFETY_STOP,
                            f"{a.robot.name} protective stop: {b.robot.name} {d:.2f} m ahead "
                            f"(stop zone {limit:.2f} m)",
                            Severity.WARNING,
                            robot_id=a.robot_id,
                            data={"other": b.robot_id, "distance_m": round(d, 2)},
                        )
                    break

    # -- controls ----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "speed": self.speed,
            "sim_time_s": round(self.sim_time, 1),
            "environment": self.environment,
            "engine": "kinematic",
            "robots": sorted(self.adapters),
        }

    def set_running(self, running: bool) -> None:
        self.running = running
        self.bus.emit(EventType.SIMULATION, f"Simulation {'running' if running else 'paused'}")
        self.bus.publish("sim", self.status())

    def set_speed(self, speed: float) -> None:
        self.speed = max(0.1, min(8.0, speed))
        self.bus.publish("sim", self.status())

    def reset(self) -> None:
        for adapter in self.adapters.values():
            adapter.reset()
        self.sim_time = 0.0
        self.bus.emit(EventType.SIMULATION, "Simulation reset: robots returned to spawn poses")
        self.bus.publish("sim", self.status())
