"""
nayantra/core/tasks.py

TaskManager — structured tasks with an explicit state machine.

    QUEUED → ASSIGNED → PLANNING → EXECUTING ⇄ WAITING_FOR_TRAFFIC → COMPLETED
       ↘ WAITING_FOR_ROBOT (nobody eligible right now; retried)
    any active state → PAUSED → (resume) … ; any non-terminal → CANCELLED / FAILED

A task is built from a type plus parameters. Places may be waypoints
("Receiving Bay 2", "loading_zone") or zones ("receiving", "storage"). For a
zone, the free waypoint inside it is picked when the robot gets there. Every
transition is recorded with a human-readable reason; `status_reason` always
answers "why is this task in this state?".
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nayantra.core.allocation import allocate
from nayantra.core.errors import Conflict, Invalid, NotFound
from nayantra.core.events import EventBus
from nayantra.core.geometry import point_in_polygon
from nayantra.core.models import (
    TERMINAL_TASK_STATUSES,
    EventType,
    Severity,
    Task,
    TaskCreate,
    TaskHistoryEntry,
    TaskStatus,
    TaskStep,
    TaskType,
    Waypoint,
    Zone,
)
from nayantra.core.store import DocumentStore
from nayantra.core.world import WorldRegistry, norm_name

if TYPE_CHECKING:
    from nayantra.core.fleet import FleetManager

logger = logging.getLogger("nayantra.core.tasks")

S = TaskStatus
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    S.QUEUED: {S.ASSIGNED, S.WAITING_FOR_ROBOT, S.PAUSED, S.CANCELLED, S.FAILED},
    S.WAITING_FOR_ROBOT: {S.ASSIGNED, S.QUEUED, S.PAUSED, S.CANCELLED, S.FAILED},
    S.ASSIGNED: {S.PLANNING, S.QUEUED, S.WAITING_FOR_ROBOT, S.PAUSED, S.CANCELLED, S.FAILED},
    S.PLANNING: {S.EXECUTING, S.WAITING_FOR_TRAFFIC, S.PAUSED, S.COMPLETED, S.CANCELLED, S.FAILED},
    S.EXECUTING: {S.PLANNING, S.WAITING_FOR_TRAFFIC, S.PAUSED, S.COMPLETED, S.CANCELLED, S.FAILED},
    S.WAITING_FOR_TRAFFIC: {S.EXECUTING, S.PLANNING, S.PAUSED, S.CANCELLED, S.FAILED},
    S.PAUSED: {S.QUEUED, S.ASSIGNED, S.PLANNING, S.EXECUTING, S.CANCELLED, S.FAILED},
    S.COMPLETED: set(),
    S.FAILED: set(),
    S.CANCELLED: set(),
}

ACTIVE = {S.PLANNING, S.EXECUTING, S.WAITING_FOR_TRAFFIC}

# Capabilities each task type needs (robots are matched on these, not on type).
TASK_CAPABILITIES: dict[TaskType, list[str]] = {
    TaskType.NAVIGATE: ["navigate"],
    TaskType.DELIVERY: ["navigate", "carry_payload"],
    TaskType.PATROL: ["navigate", "patrol"],
    TaskType.INSPECT: ["navigate", "inspect"],
    TaskType.CHARGE: ["charge"],
    TaskType.DOCK: ["dock"],
    TaskType.TAKEOFF: ["takeoff"],
    TaskType.LAND: ["land"],
    TaskType.MAKE_WAY: ["navigate"],
}

# What kind of waypoint to prefer inside a zone for each purpose.
PURPOSE_TYPES: dict[str, list[str]] = {
    "pickup": ["pickup"],
    "dropoff": ["dropoff"],
    "charge": ["charger", "landing_pad"],
    "dock": ["dock"],
    "land": ["landing_pad"],
    "inspect": ["inspection"],
    "goto": [
        "location",
        "parking",
        "pickup",
        "dropoff",
        "dock",
        "inspection",
        "charger",
        "landing_pad",
    ],
}

DWELL_S = {"pickup": 4.0, "dropoff": 4.0, "inspect": 8.0, "manipulate": 6.0}

# Parameter contract per task type — served to the UI and the MCP layer so
# both build exactly what build_steps() accepts. "place" = waypoint or zone.
TASK_SPECS: dict[str, dict[str, Any]] = {
    "navigate": {"label": "Go to", "params": {"destination": {"type": "place", "required": True}}},
    "delivery": {
        "label": "Delivery",
        "params": {
            "pickup": {"type": "place", "required": True},
            "dropoff": {"type": "place", "required": True},
            "payload": {"type": "string", "required": False},
        },
    },
    "patrol": {
        "label": "Patrol",
        "params": {
            "waypoints": {"type": "place[]", "required": True},
            "rounds": {"type": "integer", "required": False, "default": 1},
        },
    },
    "inspect": {
        "label": "Inspect",
        "params": {
            "target": {"type": "place", "required": True},
            "duration_s": {"type": "number", "required": False, "default": 8},
        },
    },
    "charge": {"label": "Charge", "params": {"charger": {"type": "place", "required": False}}},
    "dock": {"label": "Dock", "params": {"dock": {"type": "place", "required": False}}},
    "takeoff": {"label": "Take off", "params": {}},
    "land": {"label": "Land", "params": {"pad": {"type": "place", "required": False}}},
}


@dataclass
class Place:
    waypoint: str | None
    candidates: list[str]
    zone_id: str | None
    label: str
    map_id: str


class TaskManager:
    def __init__(self, registry: WorldRegistry, bus: EventBus, store: DocumentStore) -> None:
        self.registry = registry
        self.bus = bus
        self.store = store
        self.fleet: FleetManager | None = None
        self.tasks: dict[str, Task] = {}
        seq = 0
        for raw in store.list("tasks"):
            try:
                t = Task.model_validate(raw)
            except Exception:  # noqa: BLE001
                continue
            self.tasks[t.id] = t
            try:
                seq = max(seq, int(t.id.split("-")[-1]))
            except ValueError:
                pass
        self._seq = itertools.count(seq + 1)
        self._retry_task: asyncio.Task | None = None
        # Tasks active when the core stopped are not resumed silently.
        for t in self.tasks.values():
            if t.status not in TERMINAL_TASK_STATUSES:
                t.status = S.CANCELLED
                t.status_reason = "Nayantra core restarted while this task was open; re-submit it"
                t.history.append(TaskHistoryEntry(status=S.CANCELLED, message=t.status_reason))
                self._persist(t)

    # ------------------------------------------------------------------
    def attach(self, fleet: FleetManager) -> None:
        self.fleet = fleet

    async def start(self) -> None:
        self._retry_task = asyncio.create_task(self._retry_loop(), name="nayantra-task-retry")

    async def stop(self) -> None:
        if self._retry_task:
            self._retry_task.cancel()

    def _persist(self, task: Task) -> None:
        self.store.put("tasks", task.id, task.model_dump(mode="json"), map_id=task.map_id)

    def _publish(self, task: Task) -> None:
        self._persist(task)
        self.bus.publish("task", task.model_dump(mode="json"))

    def get(self, task_id: str) -> Task:
        t = self.tasks.get(task_id)
        if t is None:
            raise NotFound(f"Unknown task {task_id!r}")
        return t

    def list(
        self, status: str | None = None, robot_id: str | None = None, limit: int = 200
    ) -> list[Task]:
        items = [
            t
            for t in self.tasks.values()
            if (
                status is None
                or t.status == status
                or (status == "active" and t.status not in TERMINAL_TASK_STATUSES)
            )
            and (robot_id is None or t.assigned_robot == robot_id)
        ]
        items.sort(key=lambda t: t.created_at, reverse=True)
        return items[:limit]

    def open_tasks_using(self, waypoint_id: str) -> list[str]:
        return [
            t.id
            for t in self.tasks.values()
            if t.status not in TERMINAL_TASK_STATUSES
            and any(s.target == waypoint_id or waypoint_id in s.candidates for s in t.steps)
        ]

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def transition(self, task: Task, new: TaskStatus, reason: str = "") -> None:
        cur = TaskStatus(task.status)
        new = TaskStatus(new)
        if cur == new:
            if reason and reason != task.status_reason:
                task.status_reason = reason
                self._publish(task)
            return
        if new not in TRANSITIONS[cur]:
            raise Conflict(f"Task {task.id} cannot go from {cur.value} to {new.value}")
        task.status = new
        task.status_reason = reason
        now = time.time()
        if new == S.ASSIGNED:
            task.assigned_at = now
        if new in ACTIVE and task.started_at is None:
            task.started_at = now
        if new in TERMINAL_TASK_STATUSES:
            task.completed_at = now
            if new == S.COMPLETED:
                task.progress = 1.0
        task.history.append(TaskHistoryEntry(status=new, message=reason or new.value))
        self._publish(task)
        robot = task.assigned_robot
        rname = self.registry.robots[robot].name if robot in self.registry.robots else robot
        if new == S.ASSIGNED:
            self.bus.emit(
                EventType.ROBOT_TASK_ASSIGNED,
                f"{task.id} ({task.type}) assigned to {rname}",
                robot_id=robot,
                fleet_id=task.assigned_fleet,
                task_id=task.id,
                data={"explanation": task.allocation.explanation if task.allocation else ""},
            )
        elif new == S.PLANNING and cur == S.ASSIGNED:
            self.bus.emit(
                EventType.ROBOT_TASK_STARTED,
                f"{rname} started {task.id} ({task.type})",
                robot_id=robot,
                task_id=task.id,
            )
        elif new == S.COMPLETED:
            dur = f" in {now - task.started_at:.0f} s" if task.started_at else ""
            self.bus.emit(
                EventType.ROBOT_TASK_COMPLETED,
                f"{rname} completed {task.id}{dur}",
                robot_id=robot,
                task_id=task.id,
            )
        elif new == S.FAILED:
            self.bus.emit(
                EventType.ROBOT_TASK_FAILED,
                f"{task.id} failed: {reason}",
                Severity.ERROR,
                robot_id=robot,
                task_id=task.id,
            )
            self.bus.raise_alert(
                f"task_failed:{task.id}",
                Severity.ERROR,
                f"Task {task.id} failed",
                reason,
                robot_id=robot,
                task_id=task.id,
            )
        else:
            self.bus.emit(
                EventType.TASK_STATUS_CHANGED,
                f"{task.id} → {new.value.replace('_', ' ')}" + (f": {reason}" if reason else ""),
                robot_id=robot,
                task_id=task.id,
            )

    def note(self, task: Task, message: str) -> None:
        task.history.append(TaskHistoryEntry(message=message))
        self._publish(task)

    def set_progress(self, task: Task, step_index: int, fraction: float) -> None:
        n = max(1, len(task.steps))
        task.current_step = step_index
        task.progress = round(min(1.0, (step_index + max(0.0, min(1.0, fraction))) / n), 3)
        self.bus.publish(
            "task_progress", {"id": task.id, "progress": task.progress, "current_step": step_index}
        )

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------

    def resolve_place(self, ref: Any, map_id: str | None, purpose: str) -> Place:
        if isinstance(ref, dict):
            ref = ref.get("place") or ref.get("waypoint") or ref.get("name") or ref.get("zone")
        if not isinstance(ref, str) or not ref.strip():
            raise Invalid(f"Missing place for {purpose}")
        try:
            w = self.registry.resolve_waypoint(ref, map_id)
            return Place(w.id, [w.id], None, w.name, w.map_id)
        except NotFound as nf:
            zone = self._find_zone(ref, map_id)
            if zone is None:
                raise nf from None
            members = [
                w
                for w in self.registry.list_waypoints(zone.map_id)
                if point_in_polygon((w.x, w.y), zone.polygon) and w.type != "intersection"
            ]
            preferred = [w for w in members if w.type in PURPOSE_TYPES.get(purpose, [])]
            pool = preferred or members
            if not pool:
                raise Invalid(
                    f"Zone '{zone.name}' contains no usable waypoints for {purpose}"
                ) from None
            return Place(None, [w.id for w in pool], zone.id, zone.name, zone.map_id)

    def _find_zone(self, ref: str, map_id: str | None) -> Zone | None:
        key = norm_name(ref)
        zones = self.registry.list_zones(map_id)
        for z in zones:
            if key in (norm_name(z.id), norm_name(z.name)):
                return z
        for z in zones:  # "storage" → the storage zone, "receiving area" → receiving
            if key == z.type or key.replace("_area", "").replace("_zone", "") in (
                z.type,
                norm_name(z.name),
            ):
                return z
        return None

    def _typed_candidates(self, map_id: str, types: list[str]) -> list[str]:
        return [w.id for w in self.registry.list_waypoints(map_id) if w.type in types]

    def _infer_map(self, req: TaskCreate) -> str:
        if req.map_id:
            self.registry.get_map(req.map_id)
            return req.map_id
        r = req.requirements
        if r.robot_id:
            robot = self.registry.find_robot(r.robot_id)
            cfg = self.registry.effective_config(robot.id)
            if cfg.map_id:
                return cfg.map_id
        if r.fleet_id:
            f = self.registry.get_fleet(r.fleet_id)
            if f.map_ids:
                return f.map_ids[0]
        refs = [
            v
            for k, v in req.params.items()
            if k in ("destination", "waypoint", "target", "pickup", "dropoff", "place")
        ]
        refs += list(req.params.get("waypoints") or [])
        for ref in refs:
            try:
                return self.resolve_place(ref, None, "goto").map_id
            except (NotFound, Invalid):
                continue
            except Conflict:
                break
        if len(self.registry.maps) == 1:
            return next(iter(self.registry.maps))
        # Nothing resolved: use the map most robots work on, so the operator gets
        # a precise "unknown place — did you mean …" error rather than a vague one.
        usage: dict[str, int] = {}
        for rid in self.registry.robots:
            m = self.registry.effective_config(rid).map_id
            if m:
                usage[m] = usage.get(m, 0) + 1
        if usage:
            return max(usage, key=usage.get)
        raise Invalid(
            "Cannot tell which map this task is on; pass map_id",
            {"maps": sorted(self.registry.maps)},
        )

    def build_steps(
        self, ttype: TaskType, params: dict[str, Any], map_id: str
    ) -> tuple[list[TaskStep], list[Waypoint]]:
        """Validate type-specific params and turn them into steps."""
        steps: list[TaskStep] = []
        fixed: list[Waypoint] = []

        def go(ref: Any, purpose: str, label_verb: str) -> None:
            place = self.resolve_place(ref, map_id, purpose)
            if place.waypoint:
                fixed.append(self.registry.waypoints[place.waypoint])
            steps.append(
                TaskStep(
                    kind="go_to",
                    label=f"{label_verb} {place.label}",
                    target=place.waypoint,
                    candidates=place.candidates if place.waypoint is None else [],
                    zone_id=place.zone_id,
                )
            )

        def act(action: str, label: str, duration: float | None = None) -> None:
            steps.append(TaskStep(kind="action", label=label, action=action, duration_s=duration))

        p = params
        if ttype == TaskType.NAVIGATE:
            dest = p.get("destination") or p.get("waypoint") or p.get("target") or p.get("place")
            go(dest, "goto", "Go to")
        elif ttype == TaskType.MAKE_WAY:
            go(p.get("target"), "goto", "Make way → ")
        elif ttype == TaskType.DELIVERY:
            payload = p.get("payload") or "payload"
            go(p.get("pickup"), "pickup", "Go to")
            act("pickup", f"Pick up {payload}", float(p.get("pickup_s", DWELL_S["pickup"])))
            go(p.get("dropoff"), "dropoff", "Deliver to")
            act("dropoff", f"Drop off {payload}", float(p.get("dropoff_s", DWELL_S["dropoff"])))
        elif ttype == TaskType.PATROL:
            route = p.get("waypoints") or p.get("route") or p.get("places")
            if not isinstance(route, list) or len(route) < 1:
                raise Invalid("patrol needs params.waypoints (a list of places)")
            rounds = int(p.get("rounds", 1))
            if not 1 <= rounds <= 20:
                raise Invalid("patrol rounds must be between 1 and 20")
            for r in range(rounds):
                for ref in route:
                    go(ref, "goto", f"Patrol {r + 1}/{rounds}:")
        elif ttype == TaskType.INSPECT:
            go(p.get("target") or p.get("waypoint") or p.get("destination"), "inspect", "Go to")
            act(
                "inspect",
                f"Inspect ({p.get('what', 'visual')})",
                float(p.get("duration_s", DWELL_S["inspect"])),
            )
        elif ttype == TaskType.CHARGE:
            ref = p.get("charger") or p.get("target")
            if ref:
                go(ref, "charge", "Go to")
            else:
                cands = self._typed_candidates(map_id, PURPOSE_TYPES["charge"])
                if not cands:
                    raise Invalid(f"Map {map_id!r} has no chargers")
                steps.append(
                    TaskStep(kind="go_to", label="Go to nearest free charger", candidates=cands)
                )
            act("charge", "Charge")
        elif ttype == TaskType.DOCK:
            ref = p.get("dock") or p.get("target")
            if ref:
                go(ref, "dock", "Go to")
            else:
                cands = self._typed_candidates(map_id, ["dock"])
                if not cands:
                    raise Invalid(f"Map {map_id!r} has no docks")
                steps.append(
                    TaskStep(kind="go_to", label="Go to nearest free dock", candidates=cands)
                )
            act("dock", "Dock")
        elif ttype == TaskType.TAKEOFF:
            act("takeoff", "Take off")
        elif ttype == TaskType.LAND:
            ref = p.get("pad") or p.get("target")
            if ref:
                go(ref, "land", "Fly to")
            else:
                cands = self._typed_candidates(map_id, ["landing_pad"])
                if not cands:
                    raise Invalid(f"Map {map_id!r} has no landing pads")
                steps.append(
                    TaskStep(kind="go_to", label="Fly to nearest free pad", candidates=cands)
                )
            act("land", "Land")
        else:
            raise Invalid(f"Unsupported task type {ttype!r}")
        return steps, fixed

    def validate(self, req: TaskCreate) -> tuple[str, list[TaskStep], list[Waypoint]]:
        map_id = self._infer_map(req)
        steps, fixed = self.build_steps(TaskType(req.type), req.params, map_id)
        return map_id, steps, fixed

    def create(self, req: TaskCreate, count: int = 1) -> list[Task]:
        if not 1 <= count <= 20:
            raise Invalid("count must be between 1 and 20")
        map_id, steps, _ = self.validate(req)
        created = []
        for _ in range(count):
            caps = list(
                dict.fromkeys(
                    TASK_CAPABILITIES[TaskType(req.type)] + list(req.requirements.capabilities)
                )
            )
            requirements = req.requirements.model_copy(update={"capabilities": caps})
            if requirements.robot_id:
                requirements.robot_id = self.registry.find_robot(requirements.robot_id).id
            task = Task(
                id=f"T-{next(self._seq):04d}",
                type=req.type,
                params=req.params,
                priority=req.priority,
                requirements=requirements,
                map_id=map_id,
                created_by=req.created_by,
                trace=req.trace,
                allow_restricted=req.allow_restricted,
                steps=[s.model_copy(deep=True) for s in steps],
            )
            source = task.trace.source
            task.history.append(
                TaskHistoryEntry(
                    status=S.QUEUED,
                    message=f"Created by {task.created_by} via {source}"
                    + (f" from command {task.trace.command!r}" if task.trace.command else "")
                    + (f" (tool {task.trace.tool})" if task.trace.tool else ""),
                )
            )
            self.tasks[task.id] = task
            self._publish(task)
            self.bus.emit(
                EventType.TASK_CREATED,
                f"{task.id} created: {self.describe(task)}",
                task_id=task.id,
                map_id=map_id,
                data={"source": source, "created_by": task.created_by},
            )
            created.append(task)
        for task in created:
            self.try_allocate(task)
        return created

    def describe(self, task: Task) -> str:
        gos = [s.label for s in task.steps if s.kind == "go_to"]
        if task.type == TaskType.DELIVERY and len(gos) >= 2:
            return f"deliver {task.params.get('payload', 'payload')} {gos[0].replace('Go to ', 'from ')} {gos[1].replace('Deliver to ', 'to ')}"
        return f"{task.type}: " + " → ".join(gos or [s.label for s in task.steps])

    # ------------------------------------------------------------------
    # Allocation
    # ------------------------------------------------------------------

    def try_allocate(self, task: Task) -> bool:
        if self.fleet is None or task.status not in (S.QUEUED, S.WAITING_FOR_ROBOT):
            return False
        allocation, retry = allocate(task, self.registry, self.fleet)
        task.allocation = allocation
        if allocation.selected:
            robot = self.registry.robots[allocation.selected]
            task.assigned_robot = robot.id
            task.assigned_fleet = robot.fleet_id
            self.transition(task, S.ASSIGNED, allocation.explanation)
            self.fleet.enqueue(robot.id, task)
            return True
        if retry:
            if task.status != S.WAITING_FOR_ROBOT:
                self.transition(task, S.WAITING_FOR_ROBOT, allocation.explanation)
                self.bus.emit(
                    EventType.TASK_UNASSIGNABLE,
                    f"{task.id} is waiting for a robot: {allocation.explanation}",
                    Severity.WARNING,
                    task_id=task.id,
                )
            elif allocation.explanation != task.status_reason:
                task.status_reason = allocation.explanation
                self._publish(task)
        else:
            self.transition(task, S.FAILED, allocation.explanation)
        return False

    async def _retry_loop(self) -> None:
        while True:
            await asyncio.sleep(2.0)
            try:
                for task in sorted(self.tasks.values(), key=lambda t: (-t.priority, t.created_at)):
                    if task.status in (S.QUEUED, S.WAITING_FOR_ROBOT):
                        self.try_allocate(task)
            except Exception:  # noqa: BLE001
                logger.exception("allocation retry failed")

    def requeue(self, task: Task, reason: str) -> None:
        """A robot can no longer run a task it was assigned but had not started."""
        if task.status in (S.ASSIGNED, S.PAUSED):
            task.assigned_robot = None
            task.assigned_fleet = None
            self.transition(task, S.QUEUED, reason)
            self.try_allocate(task)

    # ------------------------------------------------------------------
    # Operator controls
    # ------------------------------------------------------------------

    async def pause(self, task_id: str, reason: str = "Paused by operator") -> Task:
        task = self.get(task_id)
        if task.status in TERMINAL_TASK_STATUSES:
            raise Conflict(f"Task {task_id} is already {task.status}")
        if task.status == S.PAUSED:
            return task
        if task.status in ACTIVE and self.fleet:
            await self.fleet.control_task(task, "pause", reason)
        else:
            self.transition(task, S.PAUSED, reason)
        return task

    async def resume(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task.status != S.PAUSED:
            raise Conflict(f"Task {task_id} is not paused (status {task.status})")
        rt = (
            self.fleet.runtimes.get(task.assigned_robot)
            if (self.fleet and task.assigned_robot)
            else None
        )
        if rt and rt.current_task == task.id:
            await self.fleet.control_task(task, "resume", "Resumed by operator")
        elif task.assigned_robot and rt and task.id in rt.queue:
            self.transition(task, S.ASSIGNED, "Resumed by operator")
            rt.wake.set()
        else:
            task.assigned_robot = None
            self.transition(task, S.QUEUED, "Resumed by operator")
            self.try_allocate(task)
        return task

    async def cancel(self, task_id: str, reason: str = "Cancelled by operator") -> Task:
        task = self.get(task_id)
        if task.status in TERMINAL_TASK_STATUSES:
            return task
        rt = (
            self.fleet.runtimes.get(task.assigned_robot)
            if (self.fleet and task.assigned_robot)
            else None
        )
        if rt and rt.current_task == task.id:
            await self.fleet.control_task(task, "cancel", reason)
        else:
            if rt and task.id in rt.queue:
                rt.queue.remove(task.id)
            self.transition(task, S.CANCELLED, reason)
        return task
