"""
nayantra/core/fleet.py

FleetManager — owns every robot's adapter, live state and task queue, and runs
one deterministic executor per robot.

Executor loop for a go-to step:
  1. resolve the target (a zone → the nearest free waypoint in it)
  2. make sure the robot is on the graph (re-join after a mid-lane stop)
  3. ask the TrafficCoordinator for a space-time route
       blocked      → WAITING_FOR_TRAFFIC with the coordinator's reason, retry
       unreachable  → task FAILED with the reason
  4. drive it: before each lane the coordinator must clear the next step
     (look-ahead of 2 steps for smooth motion); the adapter only ever gets a
     path of cleared nodes; progress releases resources behind the robot
  5. pause / cancel / e-stop freeze the robot where it is in the schedule,
     and others re-plan around it

Robots never coordinate with each other; everything goes through the core.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from nayantra.core.adapters import CheckResult, RobotAdapter, SimEngine, create_adapter
from nayantra.core.allocation import congestion_penalty
from nayantra.core.errors import Conflict, NotFound
from nayantra.core.events import EventBus
from nayantra.core.geometry import point_in_polygon
from nayantra.core.models import (
    EventType,
    Pose,
    Robot,
    RobotMode,
    RobotState,
    Severity,
    TaskCreate,
    TaskRequirements,
    TaskStatus,
    TaskTrace,
    TaskType,
)
from nayantra.core.routing import GraphCache, MapGraph, RouteProfile
from nayantra.core.safety import SafetyGuard, speed_cap
from nayantra.core.traffic import RobotPlan, TrafficCoordinator
from nayantra.core.world import WorldRegistry

if TYPE_CHECKING:
    from nayantra.core.models import Task
    from nayantra.core.tasks import TaskManager

logger = logging.getLogger("nayantra.core.fleet")

LOOKAHEAD_STEPS = 2
CONTROL_PERIOD_S = 0.2
TELEMETRY_PERIOD_S = 0.2
SNAP_TO_NODE_M = 0.8
STALL_S = 45.0

S = TaskStatus


@dataclass
class RobotRuntime:
    robot_id: str
    adapter: RobotAdapter
    state: RobotState
    checks: list[dict] = field(default_factory=list)
    queue: list[str] = field(default_factory=list)
    current_task: str | None = None
    control: str | None = None  # pause | resume | cancel | estop
    control_reason: str = ""
    held: bool = False  # operator hold: finish nothing new
    activity: str | None = None  # waiting | acting | charging | paused
    reason: str = ""
    goal_node: str | None = None
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    worker: asyncio.Task | None = None
    battery_low: bool = False
    last_zone_alert: float = 0.0
    last_pose: tuple[float, float] | None = None
    stall_ref: tuple[float, float, float] | None = None
    sent_base: int = 0
    sent_last_index: dict[int, int] = field(default_factory=dict)


class FleetManager:
    def __init__(
        self,
        registry: WorldRegistry,
        bus: EventBus,
        graphs: GraphCache,
        traffic: TrafficCoordinator,
        sim: SimEngine,
        safety: SafetyGuard,
        tasks: TaskManager,
    ) -> None:
        self.registry = registry
        self.bus = bus
        self.graphs = graphs
        self.traffic = traffic
        self.sim = sim
        self.safety = safety
        self.tasks = tasks
        self.runtimes: dict[str, RobotRuntime] = {}
        self._loops: list[asyncio.Task] = []
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.registry.on_change(self._on_registry)
        self.traffic.make_way_handler = self.make_way
        self.sim.on_reset = self.sim_reset
        for robot in list(self.registry.robots.values()):
            await self._add(robot)
        self._loops = [
            asyncio.create_task(self._telemetry_loop(), name="nayantra-telemetry"),
            asyncio.create_task(self._supervise_loop(), name="nayantra-supervisor"),
        ]
        self._started = True

    async def stop(self) -> None:
        for t in self._loops:
            t.cancel()
        for rt in list(self.runtimes.values()):
            if rt.worker:
                rt.worker.cancel()
            try:
                await rt.adapter.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _on_registry(self, kind: str, op: str, obj: Any) -> None:
        if not self._started:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if kind == "robot":
            if op == "delete":
                loop.create_task(self._remove(obj.id))
            elif obj.id not in self.runtimes:
                loop.create_task(self._add(obj))
            else:
                loop.create_task(self._reconfigure(obj))
        elif kind == "fleet" and op != "delete":
            for rid in self.registry.fleet_robot_ids(obj.id):
                loop.create_task(self._reconfigure(self.registry.robots[rid]))

    # ------------------------------------------------------------------
    # Robot runtime management
    # ------------------------------------------------------------------

    def profile_for(self, robot_id: str) -> RouteProfile:
        if robot_id in self.traffic.profiles:
            return self.traffic.profiles[robot_id]
        return self._profile(robot_id)

    def trapped_in(self, robot_id: str) -> list:
        """Forbidden zones the robot is currently inside (it may always drive out)."""
        rt = self.runtimes.get(robot_id)
        if rt is None:
            return []
        cfg = rt.adapter.config
        pose = rt.adapter.snapshot().pose
        return self.safety.forbidden_zones_at(
            cfg.map_id, pose.x, pose.y, rt.state.fleet_id, cfg.layer
        )

    def routing_profile(self, robot_id: str) -> RouteProfile:
        """Profile for planning a route that starts where the robot is now."""
        base = self.profile_for(robot_id)
        if not base.allow_restricted and self.trapped_in(robot_id):
            return self._profile(robot_id, allow_restricted=True)
        return base

    def _profile(self, robot_id: str, allow_restricted: bool = False) -> RouteProfile:
        cfg = self.registry.effective_config(robot_id)
        robot = self.registry.robots[robot_id]
        return RouteProfile(
            robot_id=robot_id,
            fleet_id=robot.fleet_id,
            layer=cfg.layer,
            max_speed=min(cfg.max_speed, speed_cap(cfg.layer)),
            footprint_radius=cfg.footprint.effective_radius,
            max_vertical_speed=cfg.max_vertical_speed,
            allow_restricted=allow_restricted,
        )

    def _spawn(self, robot: Robot) -> tuple[Pose | None, str]:
        if robot.spawn.waypoint_id and robot.spawn.waypoint_id in self.registry.waypoints:
            w = self.registry.waypoints[robot.spawn.waypoint_id]
            return Pose(x=w.x, y=w.y, z=w.z, yaw=w.yaw), w.name
        if robot.spawn.pose:
            return robot.spawn.pose, ""
        return None, ""

    async def _add(self, robot: Robot) -> None:
        if robot.id in self.runtimes:
            return
        cfg = self.registry.effective_config(robot.id)
        spawn, label = self._spawn(robot)
        adapter = create_adapter(robot, cfg, spawn, self.sim, spawn_label=label)
        state = RobotState(
            robot_id=robot.id,
            fleet_id=robot.fleet_id,
            map_id=cfg.map_id,
            adapter=adapter.kind,
            pose=spawn or Pose(),
        )
        rt = RobotRuntime(robot_id=robot.id, adapter=adapter, state=state)
        self.runtimes[robot.id] = rt
        self.traffic.set_profile(robot.id, self._profile(robot.id), cfg.priority)
        if robot.enabled:
            await self.connect(robot.id)
        else:
            state.connection_detail = "decommissioned"
        rt.worker = asyncio.create_task(self._worker(rt), name=f"nayantra-exec-{robot.id}")

    async def _remove(self, robot_id: str) -> None:
        rt = self.runtimes.pop(robot_id, None)
        if rt is None:
            return
        for tid in list(rt.queue):
            t = self.tasks.tasks.get(tid)
            if t:
                self.tasks.requeue(t, "Robot was removed")
        if rt.current_task:
            t = self.tasks.tasks.get(rt.current_task)
            if t and t.status not in (S.COMPLETED, S.FAILED, S.CANCELLED):
                self.tasks.transition(t, S.CANCELLED, "Robot was removed from the fleet")
        if rt.worker:
            rt.worker.cancel()
        try:
            await rt.adapter.stop()
            await rt.adapter.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self.traffic.remove_robot(robot_id)
        self.bus.publish("robot_removed", {"robot_id": robot_id})

    async def _reconfigure(self, robot: Robot) -> None:
        rt = self.runtimes.get(robot.id)
        if rt is None:
            return
        cfg = self.registry.effective_config(robot.id)
        old = rt.adapter.config
        rebuild = (
            cfg.communication.protocol != old.communication.protocol
            or cfg.communication.endpoint != old.communication.endpoint
            or cfg.map_id != old.map_id
            or robot.fleet_id != rt.state.fleet_id
            or robot.navigation.namespace != rt.adapter.robot.navigation.namespace
        )
        if rebuild:
            await self._remove(robot.id)
            await self._add(robot)
            return
        rt.adapter.reconfigure(robot, cfg)
        self.traffic.set_profile(robot.id, self._profile(robot.id), cfg.priority)
        if robot.enabled and not rt.state.online:
            await self.connect(robot.id)
        elif not robot.enabled and rt.state.online:
            await self._decommission(rt)

    async def _decommission(self, rt: RobotRuntime) -> None:
        if rt.current_task:
            t = self.tasks.tasks.get(rt.current_task)
            if t:
                await self.control_task(t, "cancel", "Robot decommissioned")
        for tid in list(rt.queue):
            t = self.tasks.tasks.get(tid)
            rt.queue.remove(tid)
            if t:
                self.tasks.requeue(t, "Robot decommissioned")
        await rt.adapter.stop()
        await rt.adapter.disconnect()
        self.traffic.remove_robot(rt.robot_id)
        rt.state.online = False
        rt.state.connection = "disconnected"
        rt.state.connection_detail = "decommissioned"

    async def connect(self, robot_id: str) -> list[dict]:
        """(Re)connect a robot and return its connection-test checks."""
        rt = self._rt(robot_id)
        robot = self.registry.robots[robot_id]
        rt.state.connection = "connecting"
        try:
            checks: list[CheckResult] = await rt.adapter.connect()
        except Exception as exc:  # noqa: BLE001
            checks = [CheckResult("Connection", False, f"{type(exc).__name__}: {exc}")]
        rt.checks = [c.as_dict() for c in checks]
        ok = all(c.ok for c in checks if c.required)
        rt.state.connection = "connected" if ok else "error"
        failed = [c for c in checks if c.required and not c.ok]
        rt.state.connection_detail = failed[0].detail if failed else ""
        if ok:
            self.bus.emit(
                EventType.ROBOT_CONNECTED,
                f"{robot.name} connected ({rt.adapter.kind})",
                robot_id=robot_id,
                fleet_id=robot.fleet_id,
            )
            self.bus.resolve_alert(f"offline:{robot_id}")
            self._place_on_graph(rt)
            self._update_state(rt)  # live immediately, not at the next telemetry tick
            rt.wake.set()
        else:
            self.bus.emit(
                EventType.ROBOT_DISCONNECTED,
                f"{robot.name} connection test failed: {rt.state.connection_detail}",
                Severity.WARNING,
                robot_id=robot_id,
                fleet_id=robot.fleet_id,
                data={"checks": rt.checks},
            )
        return rt.checks

    def _place_on_graph(self, rt: RobotRuntime) -> None:
        cfg = rt.adapter.config
        if not cfg.map_id or cfg.map_id not in self.registry.maps:
            return
        node, lane = self.traffic.location(rt.robot_id)
        if node or lane:
            return
        graph = self.graphs.get(cfg.map_id)
        snap = rt.adapter.snapshot()
        nearest, d = graph.nearest_node(snap.pose.x, snap.pose.y, cfg.layer)
        if nearest and d <= SNAP_TO_NODE_M:
            for other, p in self.traffic.parked.items():
                if other != rt.robot_id and p.node_id == nearest:
                    self.bus.raise_alert(
                        f"colocated:{rt.robot_id}",
                        Severity.WARNING,
                        "Two robots on one waypoint",
                        f"{self._name(rt.robot_id)} and {self._name(other)} are both at "
                        f"{graph.nodes[nearest].name}",
                        robot_id=rt.robot_id,
                    )
            self.traffic.set_parked(rt.robot_id, cfg.map_id, nearest)

    def _rt(self, robot_id: str) -> RobotRuntime:
        rt = self.runtimes.get(robot_id)
        if rt is None:
            raise NotFound(f"Unknown robot {robot_id!r}")
        return rt

    def _name(self, robot_id: str) -> str:
        r = self.registry.robots.get(robot_id)
        return r.name if r else robot_id

    # ------------------------------------------------------------------
    # Queue + estimates (used by allocation)
    # ------------------------------------------------------------------

    def enqueue(self, robot_id: str, task: Task) -> None:
        rt = self._rt(robot_id)
        if task.id not in rt.queue:
            rt.queue.append(task.id)
        rt.state.queue = list(rt.queue)
        rt.wake.set()

    def planning_origin(self, robot_id: str) -> str | None:
        rt = self.runtimes.get(robot_id)
        if rt is None:
            return None
        for tid in reversed(rt.queue + ([rt.current_task] if rt.current_task else [])):
            t = self.tasks.tasks.get(tid)
            if t:
                for step in reversed(t.steps):
                    if step.kind == "go_to":
                        tgt = step.target or (step.candidates[0] if step.candidates else None)
                        if tgt:
                            return tgt
        node, lane = self.traffic.location(robot_id)
        if node:
            return node
        cfg = rt.adapter.config
        if lane and self.traffic.rejoin_node(robot_id):
            return self.traffic.rejoin_node(robot_id)
        if cfg.map_id and cfg.map_id in self.registry.maps:
            snap = rt.adapter.snapshot()
            n, _ = self.graphs.get(cfg.map_id).nearest_node(snap.pose.x, snap.pose.y, cfg.layer)
            return n
        return None

    def busy_estimate(self, robot_id: str) -> float:
        rt = self.runtimes.get(robot_id)
        if rt is None:
            return 0.0
        total = 0.0
        now = time.time()
        ids = ([rt.current_task] if rt.current_task else []) + list(rt.queue)
        for tid in ids:
            t = self.tasks.tasks.get(tid)
            if not t or not t.allocation:
                continue
            cand = next((c for c in t.allocation.candidates if c.robot_id == robot_id), None)
            est = (cand.estimates.get("total_s") if cand else None) or 30.0
            if tid == rt.current_task and t.started_at:
                est = max(5.0, est - (now - t.started_at))
            total += est
        return total

    def congestion_estimate(self, robot_id: str, target: str | None) -> float:
        rt = self.runtimes.get(robot_id)
        origin = self.planning_origin(robot_id)
        if rt is None or target is None or origin is None:
            return 0.0
        cfg = rt.adapter.config
        route = self.graphs.get(cfg.map_id).plan_static(origin, target, self.profile_for(robot_id))
        if route is None:
            return 0.0
        return congestion_penalty(self.traffic, robot_id, cfg.map_id, route.nodes)

    # ------------------------------------------------------------------
    # Executor
    # ------------------------------------------------------------------

    def _can_work(self, rt: RobotRuntime) -> bool:
        robot = self.registry.robots.get(rt.robot_id)
        return bool(
            robot and robot.enabled and rt.state.online and not rt.state.e_stop and not rt.held
        )

    def _next_task(self, rt: RobotRuntime) -> Task | None:
        best = None
        for tid in list(rt.queue):
            t = self.tasks.tasks.get(tid)
            if t is None or t.status in (S.COMPLETED, S.FAILED, S.CANCELLED):
                rt.queue.remove(tid)
                continue
            if t.status != S.ASSIGNED:
                continue
            if best is None or (-t.priority, t.created_at) < (-best.priority, best.created_at):
                best = t
        return best

    async def _worker(self, rt: RobotRuntime) -> None:
        while True:
            await rt.wake.wait()
            rt.wake.clear()
            while self._can_work(rt):
                task = self._next_task(rt)
                if task is None:
                    break
                rt.queue.remove(task.id)
                rt.state.queue = list(rt.queue)
                rt.current_task = task.id
                rt.state.current_task_id = task.id
                rt.control = None
                self.traffic.set_busy(rt.robot_id, True)
                try:
                    await self._execute(rt, task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"executor error on {rt.robot_id}/{task.id}")
                    if task.status not in (S.COMPLETED, S.FAILED, S.CANCELLED):
                        self.traffic.freeze(rt.robot_id, busy=False)
                        self.tasks.transition(task, S.FAILED, f"Internal executor error: {exc}")
                finally:
                    rt.current_task = None
                    rt.state.current_task_id = None
                    rt.control = None
                    rt.activity = None
                    rt.reason = ""
                    rt.goal_node = None
                    rt.state.destination = None
                    self.traffic.set_busy(rt.robot_id, False)
                    self.traffic.profiles[rt.robot_id] = self._profile(rt.robot_id)
                    # The task just ended: publish the pose it ended at, not the last telemetry tick's
                    try:
                        self._update_state(rt)
                    except Exception:  # noqa: BLE001
                        logger.exception(f"state refresh failed for {rt.robot_id}")

    async def _execute(self, rt: RobotRuntime, task: Task) -> None:
        self.tasks.transition(task, S.PLANNING, "Planning route")
        if task.allow_restricted:
            self.traffic.profiles[rt.robot_id] = self._profile(rt.robot_id, allow_restricted=True)
        for i in range(task.current_step, len(task.steps)):
            step = task.steps[i]
            step.status = "active"
            step.started_at = time.time()
            self.tasks.set_progress(task, i, 0.0)
            outcome = await (
                self._go_to(rt, task, i) if step.kind == "go_to" else self._act(rt, task, i)
            )
            if outcome != "done":
                step.status = "failed" if outcome == "failed" else "pending"
                return
            step.status = "done"
            step.finished_at = time.time()
            self.tasks.set_progress(task, i + 1, 0.0)
        self.tasks.transition(task, S.COMPLETED, f"Completed by {self._name(rt.robot_id)}")

    # -- control handling ----------------------------------------------
    async def control_task(self, task: Task, action: str, reason: str) -> None:
        rt = self.runtimes.get(task.assigned_robot or "")
        if rt is None or rt.current_task != task.id:
            if action == "cancel":
                self.tasks.transition(task, S.CANCELLED, reason)
            elif action == "pause":
                self.tasks.transition(task, S.PAUSED, reason)
            return
        rt.control = action
        rt.control_reason = reason
        rt.wake.set()
        # Give the executor a moment so the API returns the new state.
        for _ in range(15):
            await asyncio.sleep(0.05)
            if rt.control is None:
                break

    async def _checkpoint(self, rt: RobotRuntime, task: Task) -> str:
        """Handle pause/cancel/e-stop. Returns 'ok', 'resumed' or 'cancelled'."""
        if rt.control is None and not rt.state.e_stop:
            return "ok"
        if rt.control == "cancel":
            await rt.adapter.stop()
            await rt.adapter.cancel_action()
            self.traffic.freeze(rt.robot_id, busy=False)
            self.tasks.transition(task, S.CANCELLED, rt.control_reason or "Cancelled")
            rt.control = None
            return "cancelled"
        if rt.control in ("pause", "estop") or rt.state.e_stop:
            reason = rt.control_reason or (
                "Emergency stop engaged" if rt.state.e_stop else "Paused"
            )
            await rt.adapter.stop()
            await rt.adapter.cancel_action()
            self.traffic.freeze(rt.robot_id, busy=True)
            self.tasks.transition(task, S.PAUSED, reason)
            rt.activity = "paused"
            rt.reason = reason
            rt.control = None
            while True:
                await rt.wake.wait()
                rt.wake.clear()
                if rt.control == "cancel":
                    self.traffic.freeze(rt.robot_id, busy=False)
                    self.tasks.transition(task, S.CANCELLED, rt.control_reason or "Cancelled")
                    rt.control = None
                    rt.activity = None
                    return "cancelled"
                if rt.control == "resume":
                    rt.control = None
                    if rt.state.e_stop:
                        self.tasks.note(task, "Resume ignored: emergency stop still engaged")
                        continue
                    rt.activity = None
                    rt.reason = ""
                    self.tasks.transition(
                        task, S.PLANNING, "Resumed; re-planning from current position"
                    )
                    return "resumed"
                rt.control = None  # repeated pause / e-stop while already paused
        if rt.control == "resume":
            rt.control = None
        return "ok"

    async def _sleep(self, rt: RobotRuntime, seconds: float) -> None:
        try:
            await asyncio.wait_for(rt.wake.wait(), timeout=seconds)
        except TimeoutError:
            pass
        # leave the event set for control signals; clear if nothing pending
        if rt.control is None:
            rt.wake.clear()

    def _set_waiting(self, rt: RobotRuntime, task: Task, reason: str) -> None:
        rt.activity = "waiting"
        rt.reason = reason
        if task.status != S.WAITING_FOR_TRAFFIC:
            self.tasks.transition(task, S.WAITING_FOR_TRAFFIC, reason)
        elif task.status_reason != reason:
            task.status_reason = reason
            self.tasks.note(task, reason)

    # -- go to -----------------------------------------------------------
    def taken_targets(self, robot_id: str) -> set[str]:
        """Nodes another robot is parked on, planned to, or heading for."""
        taken = {
            p.node_id for rid, p in self.traffic.parked.items() if rid != robot_id and p.node_id
        }
        taken |= {plan.goal for rid, plan in self.traffic.plans.items() if rid != robot_id}
        taken |= {
            o.goal_node for rid, o in self.runtimes.items() if rid != robot_id and o.goal_node
        }
        return taken

    def _choose_candidate(self, rt: RobotRuntime, step, graph: MapGraph) -> str | None:
        layer = rt.adapter.config.layer
        taken = self.taken_targets(rt.robot_id)
        origin, _ = self.traffic.location(rt.robot_id)
        origin = origin or self.planning_origin(rt.robot_id)
        best = None
        for c in step.candidates:
            if c not in graph.nodes or graph.nodes[c].layer != layer or c in taken:
                continue
            if origin == c:
                return c
            r = graph.plan_static(origin, c, self.profile_for(rt.robot_id)) if origin else None
            cost = r.time if r else math.inf
            if best is None or cost < best[0]:
                best = (cost, c)
        return best[1] if best and best[0] < math.inf else None

    async def _go_to(self, rt: RobotRuntime, task: Task, i: int) -> str:
        step = task.steps[i]
        cfg = rt.adapter.config
        if not cfg.map_id or cfg.map_id not in self.registry.maps:
            self.tasks.transition(task, S.FAILED, f"{self._name(rt.robot_id)} has no map")
            return "failed"
        graph = self.graphs.get(cfg.map_id)
        while True:
            ctl = await self._checkpoint(rt, task)
            if ctl == "cancelled":
                return "cancelled"
            graph = self.graphs.get(cfg.map_id)
            target = step.target
            if target is None:
                target = self._choose_candidate(rt, step, graph)
                if target is None:
                    zone = self.registry.zones.get(step.zone_id) if step.zone_id else None
                    self._set_waiting(
                        rt,
                        task,
                        f"All destinations in {zone.name if zone else 'the target set'} are occupied; waiting for one to free up",
                    )
                    await self._sleep(rt, 1.0)
                    continue
            if target not in graph.nodes:
                self.tasks.transition(task, S.FAILED, f"Waypoint {target!r} no longer exists")
                return "failed"
            rt.goal_node = target
            rt.state.destination = target
            node, lane = self.traffic.location(rt.robot_id)
            if node is None:
                ok = await self._rejoin(rt, task, graph)
                if ok == "cancelled":
                    return "cancelled"
                if ok != "ok":
                    continue
                node, _ = self.traffic.location(rt.robot_id)
            if node == target:
                step.target = target
                self.traffic.finish_route(rt.robot_id, busy=True)
                return "done"
            # A robot already inside a zone it isn't cleared for may always drive out.
            trapped = self.trapped_in(rt.robot_id)
            if trapped and not task.allow_restricted:
                self.traffic.profiles[rt.robot_id] = self._profile(
                    rt.robot_id, allow_restricted=True
                )
                self.tasks.note(
                    task, f"Leaving restricted zone '{trapped[0].name}' by the shortest route"
                )
            res = self.traffic.request_route(rt.robot_id, cfg.map_id, node, target, label=task.id)
            if trapped and not task.allow_restricted:
                self.traffic.profiles[rt.robot_id] = self._profile(rt.robot_id)
            if res.status == "unreachable":
                self.bus.emit(
                    EventType.NAVIGATION_FAILED,
                    f"{self._name(rt.robot_id)} cannot reach {graph.nodes[target].name}: {res.reason}",
                    Severity.ERROR,
                    robot_id=rt.robot_id,
                    task_id=task.id,
                )
                self.tasks.transition(task, S.FAILED, res.reason)
                return "failed"
            if res.status == "blocked":
                self._set_waiting(rt, task, res.reason)
                await self._sleep(rt, 1.0)
                continue
            step.target = target
            step.detail = " → ".join(graph.nodes[n].name for n in res.plan.nodes)
            if res.conflict and res.conflict.resolution:
                self.tasks.note(
                    task, f"Traffic: {res.conflict.message}. {res.conflict.resolution.message}"
                )
            outcome = await self._follow(rt, task, i, res.plan, graph)
            if outcome == "arrived":
                self.bus.emit(
                    EventType.ROBOT_ARRIVED,
                    f"{self._name(rt.robot_id)} arrived at {graph.nodes[target].name}",
                    robot_id=rt.robot_id,
                    task_id=task.id,
                )
                return "done"
            if outcome in ("cancelled", "failed"):
                return outcome
            # replan / resumed → loop

    async def _rejoin(self, rt: RobotRuntime, task: Task, graph: MapGraph) -> str:
        cfg = rt.adapter.config
        node = self.traffic.rejoin_node(rt.robot_id)
        if node is None:
            snap = rt.adapter.snapshot()
            node, d = graph.nearest_node(snap.pose.x, snap.pose.y, cfg.layer)
            if node is None:
                self.tasks.transition(
                    task, S.FAILED, f"No {cfg.layer} waypoints on map {cfg.map_id}"
                )
                return "failed"
            if d <= SNAP_TO_NODE_M:
                self.traffic.set_parked(rt.robot_id, cfg.map_id, node, busy=True)
                return "ok"
            occupied = any(
                p.node_id == node for r, p in self.traffic.parked.items() if r != rt.robot_id
            )
            if occupied:
                self._set_waiting(
                    rt, task, f"Joining the graph at {graph.nodes[node].name}, which is occupied"
                )
                await self._sleep(rt, 1.0)
                return "retry"
            self.traffic.set_parked(rt.robot_id, cfg.map_id, node, busy=True)
        w = graph.nodes[node]
        self.tasks.transition(task, S.EXECUTING, f"Re-joining the route network at {w.name}")
        await rt.adapter.follow_path([Pose(x=w.x, y=w.y, z=w.z, yaw=w.yaw)])
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            ctl = await self._checkpoint(rt, task)
            if ctl == "cancelled":
                return "cancelled"
            if ctl == "resumed":
                return "retry"
            snap = rt.adapter.snapshot()
            if (
                snap.nav_state == "arrived"
                or math.hypot(snap.pose.x - w.x, snap.pose.y - w.y) < 0.4
            ):
                self.traffic.settle_on_node(rt.robot_id, node)
                return "ok"
            if snap.nav_state == "failed":
                self.tasks.transition(
                    task, S.FAILED, f"Could not re-join at {w.name}: {snap.nav_detail}"
                )
                return "failed"
            await asyncio.sleep(CONTROL_PERIOD_S)
        self.tasks.transition(task, S.FAILED, f"Timed out re-joining at {w.name}")
        return "failed"

    def _path_for(
        self, rt: RobotRuntime, plan: RobotPlan, graph: MapGraph
    ) -> tuple[list[Pose], list[float], dict[int, int]]:
        """Poses for the started-but-unfinished steps, plus index of each step's last pose."""
        profile = self.profile_for(rt.robot_id)
        poses: list[Pose] = []
        limits: list[float] = []
        last_index: dict[int, int] = {}
        cur_z = rt.adapter.snapshot().pose.z
        for k in range(plan.progress + 1, plan.started + 1):
            s = plan.step(k)
            lane = graph.lanes.get(s.lane_id)
            u, v = graph.nodes[s.from_node], graph.nodes[s.to_node]
            speed = graph.lane_speed(lane, profile) if lane else profile.max_speed
            if rt.adapter.config.layer == "air":
                alt = lane.altitude if lane and lane.altitude is not None else v.z
                if abs(alt - cur_z) > 0.3:
                    poses.append(Pose(x=u.x, y=u.y, z=alt, yaw=u.yaw))
                    limits.append(speed)
                poses.append(Pose(x=v.x, y=v.y, z=alt, yaw=v.yaw))
                limits.append(speed)
                if abs(v.z - alt) > 0.3:
                    poses.append(Pose(x=v.x, y=v.y, z=v.z, yaw=v.yaw))
                    limits.append(speed)
                cur_z = v.z
            else:
                poses.append(Pose(x=v.x, y=v.y, z=v.z, yaw=v.yaw))
                limits.append(speed)
            last_index[k] = len(poses) - 1
        return poses, limits, last_index

    async def _send_path(self, rt: RobotRuntime, plan: RobotPlan, graph: MapGraph) -> None:
        poses, limits, last_index = self._path_for(rt, plan, graph)
        rt.sent_base = plan.progress
        rt.sent_last_index = last_index
        if poses:
            await rt.adapter.follow_path(poses, limits)

    async def _follow(
        self, rt: RobotRuntime, task: Task, i: int, plan: RobotPlan, graph: MapGraph
    ) -> str:
        rid = rt.robot_id
        version = plan.version
        rt.sent_last_index = {}
        rt.stall_ref = None
        nav_retries = 0
        target_name = graph.nodes[plan.goal].name
        self.tasks.transition(task, S.EXECUTING, f"Driving to {target_name}")
        rt.activity = None
        rt.reason = ""
        while True:
            ctl = await self._checkpoint(rt, task)
            if ctl == "cancelled":
                return "cancelled"
            if ctl == "resumed":
                return "replan"
            plan = self.traffic.plans.get(rid)
            if plan is None:
                return "replan"
            if plan.version != version:
                version = plan.version
                self.bus.emit(
                    EventType.ROUTE_CHANGED,
                    f"{self._name(rid)} re-routed by traffic negotiation: "
                    + " → ".join(graph.nodes[n].name for n in plan.remaining_nodes()),
                    robot_id=rid,
                    task_id=task.id,
                )
                if plan.moving:
                    await self._send_path(rt, plan, graph)
            snap = rt.adapter.snapshot()
            # progress: release what the robot has passed
            if rt.sent_last_index and snap.path_index >= 0:
                for k in sorted(rt.sent_last_index):
                    if k <= plan.progress:
                        continue
                    if rt.sent_last_index[k] <= snap.path_index and k <= plan.started:
                        self.traffic.complete_step(rid, k)
                    else:
                        break
            if snap.nav_state == "failed":
                if nav_retries < 1 and "battery" not in snap.nav_detail:
                    nav_retries += 1
                    self.tasks.note(task, f"Navigation error ({snap.nav_detail}); retrying once")
                    await self._send_path(rt, plan, graph)
                else:
                    self.traffic.freeze(rid, busy=False)
                    self.bus.emit(
                        EventType.NAVIGATION_FAILED,
                        f"{self._name(rid)} navigation failed: {snap.nav_detail}",
                        Severity.ERROR,
                        robot_id=rid,
                        task_id=task.id,
                    )
                    self.tasks.transition(task, S.FAILED, f"Navigation failed: {snap.nav_detail}")
                    return "failed"
            if plan.finished:
                self.traffic.finish_route(rid, busy=True)
                return "arrived"
            # look-ahead clearance
            extended = False
            waiting_reason = ""
            while plan.started < len(plan.steps) and plan.started - plan.progress < LOOKAHEAD_STEPS:
                chk = self.traffic.check_step(rid)
                if chk.replan:
                    if plan.moving:
                        break
                    self.traffic.freeze(rid, busy=True)
                    await rt.adapter.stop()
                    return "replan"
                if not chk.allowed:
                    waiting_reason = chk.reason
                    break
                self.traffic.start_step(rid, plan.started + 1)
                extended = True
            if extended:
                await self._send_path(rt, plan, graph)
                rt.activity = None
                rt.reason = ""
                if task.status == S.WAITING_FOR_TRAFFIC:
                    self.tasks.transition(task, S.EXECUTING, f"Proceeding to {target_name}")
            elif waiting_reason and not plan.moving:
                self._set_waiting(rt, task, waiting_reason)
            # stall watchdog (e.g. protective stop that never clears)
            if plan.moving and not rt.state.e_stop:
                p = snap.pose
                if (
                    rt.stall_ref is None
                    or math.hypot(p.x - rt.stall_ref[0], p.y - rt.stall_ref[1]) > 0.1
                ):
                    rt.stall_ref = (p.x, p.y, time.monotonic())
                elif time.monotonic() - rt.stall_ref[2] > STALL_S:
                    rt.stall_ref = (p.x, p.y, time.monotonic())
                    self.bus.raise_alert(
                        f"stalled:{rid}",
                        Severity.WARNING,
                        f"{self._name(rid)} is not moving",
                        f"No progress for {STALL_S:.0f} s on the way to {target_name}"
                        + (f" ({snap.nav_detail})" if snap.nav_detail else ""),
                        robot_id=rid,
                        task_id=task.id,
                    )
            else:
                self.bus.resolve_alert(f"stalled:{rid}")
            progress = plan.progress / max(1, len(plan.steps))
            self.tasks.set_progress(task, i, progress)
            await asyncio.sleep(CONTROL_PERIOD_S)

    # -- actions -----------------------------------------------------------
    async def _act(self, rt: RobotRuntime, task: Task, i: int) -> str:
        step = task.steps[i]
        action = step.action or "wait"
        adapter = rt.adapter
        self.tasks.transition(task, S.EXECUTING, step.label)
        native = action in adapter.native_actions
        while True:
            ctl = await self._checkpoint(rt, task)
            if ctl == "cancelled":
                return "cancelled"
            rt.activity = "charging" if action == "charge" else "acting"
            rt.reason = step.label
            if native:
                start_batt = adapter.snapshot().battery_pct or 0.0
                try:
                    await adapter.start_action(action, task.params)
                except NotImplementedError as exc:
                    native = False
                    self.tasks.note(task, str(exc))
                    continue
                target = rt.adapter.config.battery.charged_pct
                while True:
                    ctl = await self._checkpoint(rt, task)
                    if ctl == "cancelled":
                        return "cancelled"
                    if ctl == "resumed":
                        break
                    snap = adapter.snapshot()
                    if snap.action_done:
                        await adapter.cancel_action()
                        rt.activity = None
                        return "done"
                    if snap.nav_state == "failed":
                        self.tasks.transition(task, S.FAILED, f"{action} failed: {snap.nav_detail}")
                        return "failed"
                    if action == "charge" and snap.battery_pct is not None:
                        frac = (snap.battery_pct - start_batt) / max(1.0, target - start_batt)
                        rt.reason = f"Charging {snap.battery_pct:.0f}% → {target:.0f}%"
                        self.tasks.set_progress(task, i, frac)
                    await asyncio.sleep(CONTROL_PERIOD_S)
                continue  # resumed after pause → restart the action
            duration = step.duration_s or 5.0
            note = ""
            if rt.adapter.kind != "simulation" and action in ("pickup", "dropoff"):
                note = f" (timed {duration:.0f} s dwell: the {rt.adapter.kind} adapter has no native {action})"
            if note:
                self.tasks.note(task, step.label + note)
            t0 = time.monotonic()
            while time.monotonic() - t0 < duration:
                ctl = await self._checkpoint(rt, task)
                if ctl == "cancelled":
                    return "cancelled"
                if ctl == "resumed":
                    t0 = time.monotonic()
                self.tasks.set_progress(task, i, (time.monotonic() - t0) / duration)
                await asyncio.sleep(CONTROL_PERIOD_S)
            rt.activity = None
            return "done"

    # ------------------------------------------------------------------
    # Telemetry + supervision
    # ------------------------------------------------------------------

    def _derive_mode(self, rt: RobotRuntime, snap) -> RobotMode:
        if not snap.online:
            return RobotMode.OFFLINE
        if rt.state.e_stop or snap.nav_state == "estop":
            return RobotMode.EMERGENCY_STOP
        if snap.nav_state == "failed":
            return RobotMode.ERROR
        if rt.activity == "paused" or rt.held:
            return RobotMode.PAUSED
        if rt.activity == "waiting":
            return RobotMode.WAITING
        if rt.activity == "charging" or snap.charging:
            return RobotMode.CHARGING
        if rt.activity == "acting":
            return RobotMode.ACTING
        if (
            snap.nav_state == "moving"
            or abs(snap.velocity.linear) > 0.02
            or abs(snap.velocity.vertical) > 0.02
        ):
            return RobotMode.MOVING
        return RobotMode.IDLE

    def _update_state(self, rt: RobotRuntime) -> None:
        snap = rt.adapter.snapshot()
        st = rt.state
        robot = self.registry.robots.get(rt.robot_id)
        was_online = st.online
        st.online = snap.online
        if st.connection == "connected" and not snap.online:
            st.connection_detail = snap.nav_detail or "no telemetry"
        st.pose = snap.pose
        st.velocity = snap.velocity
        st.battery_pct = snap.battery_pct
        st.charging = snap.charging
        st.health = snap.health
        st.mode = self._derive_mode(rt, snap)
        st.queue = list(rt.queue)
        node, _ = self.traffic.location(rt.robot_id)
        st.current_waypoint = node
        plan = self.traffic.plans.get(rt.robot_id)
        now = self.traffic.clock()
        if plan:
            remaining = plan.remaining_nodes()
            st.route = remaining
            graph = self.graphs.get(plan.map_id)
            st.path = [(round(snap.pose.x, 2), round(snap.pose.y, 2))] + [
                graph.xy(n) for n in remaining[1:] if n in graph.nodes
            ]
            st.route_progress = round(plan.progress / max(1, len(plan.steps)), 3)
            st.eta_s = round(max(0.0, plan.eta - now), 1)
        else:
            st.route, st.path, st.route_progress, st.eta_s = [], [], None, None
        reason = rt.reason
        if not reason and rt.current_task:
            t = self.tasks.tasks.get(rt.current_task)
            reason = t.status_reason if t else ""
        if not snap.online:
            reason = st.connection_detail or "offline"
        elif snap.nav_detail and "protective" in snap.nav_detail:
            reason = snap.nav_detail
        st.status_reason = reason
        st.updated_at = time.time()
        if robot and was_online and not st.online and robot.enabled:
            self.bus.emit(
                EventType.ROBOT_DISCONNECTED,
                f"{robot.name} lost connection: {st.connection_detail}",
                Severity.ERROR,
                robot_id=rt.robot_id,
            )
            self.bus.raise_alert(
                f"offline:{rt.robot_id}",
                Severity.ERROR,
                f"{robot.name} offline",
                st.connection_detail,
                robot_id=rt.robot_id,
            )
        self._battery_monitor(rt, robot)
        self._zone_monitor(rt, robot)

    def _battery_monitor(self, rt: RobotRuntime, robot: Robot | None) -> None:
        if robot is None or rt.state.battery_pct is None:
            return
        pol = rt.adapter.config.battery
        b = rt.state.battery_pct
        if not rt.battery_low and b < pol.min_pct:
            rt.battery_low = True
            self.bus.emit(
                EventType.ROBOT_BATTERY_LOW,
                f"{robot.name} battery {b:.0f}% (minimum {pol.min_pct:.0f}%)",
                Severity.WARNING,
                robot_id=robot.id,
            )
            self.bus.raise_alert(
                f"battery_low:{robot.id}",
                Severity.WARNING,
                f"{robot.name} battery low",
                f"{b:.0f}% — below {pol.min_pct:.0f}%",
                robot_id=robot.id,
            )
        elif rt.battery_low and b >= max(pol.recharge_pct, pol.min_pct + 5):
            rt.battery_low = False
            self.bus.resolve_alert(f"battery_low:{robot.id}")

    def _zone_monitor(self, rt: RobotRuntime, robot: Robot | None) -> None:
        """Stop a robot that *drives into* a zone it isn't cleared for.

        A robot that is already inside (it was placed there, or the zone was
        drawn around it) only raises a warning: it may always drive out.
        """
        if robot is None or not rt.state.online:
            return
        cfg = rt.adapter.config
        p = rt.state.pose
        prev = rt.last_pose or (p.x, p.y)
        rt.last_pose = (p.x, p.y)
        zones = self.safety.forbidden_zones_at(cfg.map_id, p.x, p.y, robot.fleet_id, cfg.layer)
        if not zones:
            self.bus.resolve_alert(f"zone:{robot.id}")
            return
        task = self.tasks.tasks.get(rt.current_task) if rt.current_task else None
        if task and task.allow_restricted:
            return
        entered = [z for z in zones if not point_in_polygon(prev, z.polygon)]
        z = (entered or zones)[0]
        if not entered:
            self.bus.raise_alert(
                f"zone:{robot.id}",
                Severity.WARNING,
                f"{robot.name} inside restricted zone",
                f"{z.name}: it may only drive out",
                robot_id=robot.id,
            )
            return
        now = time.monotonic()
        if now - rt.last_zone_alert > 15:
            rt.last_zone_alert = now
            self.bus.emit(
                EventType.ROBOT_ENTERED_RESTRICTED_ZONE,
                f"{robot.name} entered {z.type.replace('_', '-')} zone '{z.name}' without clearance — stopping",
                Severity.CRITICAL,
                robot_id=robot.id,
            )
            self.bus.raise_alert(
                f"zone:{robot.id}",
                Severity.CRITICAL,
                f"{robot.name} entered restricted zone",
                z.name,
                robot_id=robot.id,
            )
            if task:
                asyncio.get_running_loop().create_task(
                    self.control_task(task, "pause", f"Entered restricted zone '{z.name}'")
                )

    async def _telemetry_loop(self) -> None:
        tick = 0
        while True:
            await asyncio.sleep(TELEMETRY_PERIOD_S)
            tick += 1
            try:
                for rt in list(self.runtimes.values()):
                    self._update_state(rt)
                self.bus.publish(
                    "robots", [rt.state.model_dump(mode="json") for rt in self.runtimes.values()]
                )
                if tick % 3 == 0:
                    self.traffic.tick()
                    self.bus.publish("traffic", self.traffic.snapshot().model_dump(mode="json"))
            except Exception:  # noqa: BLE001
                logger.exception("telemetry loop error")

    async def _supervise_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                for rt in list(self.runtimes.values()):
                    await self._supervise(rt)
            except Exception:  # noqa: BLE001
                logger.exception("supervisor error")

    def _open_task_of_type(self, robot_id: str, types: set[str]) -> bool:
        return any(
            t.requirements.robot_id == robot_id
            and t.type in types
            and t.status not in (S.COMPLETED, S.FAILED, S.CANCELLED)
            for t in self.tasks.tasks.values()
        )

    async def _supervise(self, rt: RobotRuntime) -> None:
        if rt.current_task is None and rt.queue:
            rt.wake.set()
        if not self._can_work(rt) or rt.current_task or rt.queue:
            return
        cfg = rt.adapter.config
        snap = rt.adapter.snapshot()
        node, _ = self.traffic.location(rt.robot_id)
        graph = self.graphs.get(cfg.map_id) if cfg.map_id in self.registry.maps else None
        node_type = graph.nodes[node].type if graph and node in graph.nodes else None
        pol = cfg.battery
        can_charge = "charge" in cfg.capabilities
        # Idle on a charger → top up.
        if can_charge and node_type in ("charger", "landing_pad") and snap.battery_pct is not None:
            if (
                snap.battery_pct < pol.charged_pct - 0.5
                and not snap.charging
                and "charge" in rt.adapter.native_actions
            ):
                if not (cfg.layer == "air" and snap.pose.z > 0.3):
                    await rt.adapter.start_action("charge")
            elif snap.charging and snap.battery_pct >= pol.charged_pct:
                await rt.adapter.cancel_action()
            return
        # UAV hovering with nothing to do → land on a free pad.
        if cfg.layer == "air" and snap.pose.z > 0.3 and "land" in cfg.capabilities:
            if not self._open_task_of_type(rt.robot_id, {"land"}):
                self._system_task(
                    rt.robot_id, TaskType.LAND, {}, 7, "idle while airborne; returning to a pad"
                )
            return
        # Low battery → go charge.
        robot = self.registry.robots[rt.robot_id]
        fleet = self.registry.fleets.get(robot.fleet_id)
        if (
            can_charge
            and fleet
            and fleet.charging_required
            and snap.battery_pct is not None
            and snap.battery_pct < pol.recharge_pct
            and not self._open_task_of_type(rt.robot_id, {"charge"})
        ):
            self._system_task(
                rt.robot_id,
                TaskType.CHARGE,
                {},
                7,
                f"battery {snap.battery_pct:.0f}% below the {pol.recharge_pct:.0f}% recharge threshold",
            )

    def _system_task(
        self, robot_id: str, ttype: TaskType, params: dict, priority: int, reason: str
    ) -> None:
        try:
            self.tasks.create(
                TaskCreate(
                    type=ttype,
                    params=params,
                    priority=priority,
                    requirements=TaskRequirements(robot_id=robot_id),
                    created_by="system",
                    trace=TaskTrace(source="system", reason=reason),
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"system task {ttype} for {robot_id} not created: {exc}")

    # ------------------------------------------------------------------
    # Traffic negotiation hook
    # ------------------------------------------------------------------

    def make_way(self, robot_id: str, target: str, reason: str) -> bool:
        rt = self.runtimes.get(robot_id)
        if rt is None or rt.current_task or rt.queue or not self._can_work(rt):
            return False
        try:
            self.tasks.create(
                TaskCreate(
                    type=TaskType.MAKE_WAY,
                    params={"target": target},
                    priority=9,
                    requirements=TaskRequirements(robot_id=robot_id),
                    created_by="system",
                    trace=TaskTrace(source="system", reason=reason),
                )
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"make-way for {robot_id} failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Operator commands
    # ------------------------------------------------------------------

    async def stop_robot(self, robot_id: str, reason: str = "Stopped by operator") -> None:
        rt = self._rt(robot_id)
        if rt.current_task:
            t = self.tasks.tasks.get(rt.current_task)
            if t:
                await self.control_task(t, "cancel", reason)
        await rt.adapter.stop()
        for tid in list(rt.queue):
            rt.queue.remove(tid)
            t = self.tasks.tasks.get(tid)
            if t:
                self.tasks.requeue(t, f"{self._name(robot_id)} was stopped; re-allocating")

    async def hold_robot(self, robot_id: str, hold: bool) -> None:
        rt = self._rt(robot_id)
        rt.held = hold
        task = self.tasks.tasks.get(rt.current_task) if rt.current_task else None
        if hold and task and task.status != S.PAUSED:
            await self.control_task(task, "pause", "Robot paused by operator")
        elif not hold:
            if task and task.status == S.PAUSED:
                await self.control_task(task, "resume", "Robot resumed by operator")
            rt.wake.set()

    async def emergency_stop(
        self, robot_id: str, reason: str = "Emergency stop by operator"
    ) -> None:
        rt = self._rt(robot_id)
        await rt.adapter.emergency_stop()
        rt.state.e_stop = True
        name = self._name(robot_id)
        self.bus.emit(
            EventType.EMERGENCY_STOP,
            f"EMERGENCY STOP {name}: {reason}",
            Severity.CRITICAL,
            robot_id=robot_id,
        )
        self.bus.raise_alert(
            f"estop:{robot_id}",
            Severity.CRITICAL,
            f"{name} emergency stop",
            reason,
            robot_id=robot_id,
        )
        if rt.current_task:
            rt.control = "estop"
            rt.control_reason = reason
            rt.wake.set()
        else:
            self.traffic.freeze(robot_id, busy=False)

    async def release_emergency_stop(self, robot_id: str) -> None:
        rt = self._rt(robot_id)
        if not rt.state.e_stop:
            raise Conflict(f"{self._name(robot_id)} is not emergency-stopped")
        await rt.adapter.release_estop()
        rt.state.e_stop = False
        self.bus.emit(
            EventType.EMERGENCY_STOP_RELEASED,
            f"Emergency stop released on {self._name(robot_id)}",
            Severity.WARNING,
            robot_id=robot_id,
        )
        self.bus.resolve_alert(f"estop:{robot_id}")
        rt.wake.set()

    async def restart_navigation(self, robot_id: str) -> list[dict]:
        rt = self._rt(robot_id)
        checks = await rt.adapter.restart_navigation()
        return [c.as_dict() for c in checks]

    async def sim_reset(self) -> None:
        """Cancel work on simulated robots and put them back at their spawn poses."""
        for rt in self.runtimes.values():
            if rt.adapter.kind != "simulation":
                continue
            if rt.current_task:
                t = self.tasks.tasks.get(rt.current_task)
                if t:
                    await self.control_task(t, "cancel", "Simulation reset")
            for tid in list(rt.queue):
                rt.queue.remove(tid)
                t = self.tasks.tasks.get(tid)
                if t:
                    self.tasks.transition(t, S.CANCELLED, "Simulation reset")
            rt.state.e_stop = False
            self.traffic.remove_robot(rt.robot_id)
            self.traffic.set_profile(
                rt.robot_id, self._profile(rt.robot_id), rt.adapter.config.priority
            )
        self.sim.reset()
        for rt in self.runtimes.values():
            if rt.adapter.kind == "simulation" and rt.state.online:
                self._place_on_graph(rt)
