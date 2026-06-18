"""
nayantra/rmf_bridge/server.py

RMF-compatible control plane backed by the Nav2 fleet adapter.

This server speaks the same REST surface as the Open-RMF stub
(docker/rmf_stub_server.py) — so the MCP server and agent need zero
changes — but every dispatch actually drives a robot:

    MCP tools → THIS SERVER → RMFFleetAdapter → Nav2 → Isaac Sim / hardware

Task categories handled:
  navigate_to_waypoint  {"waypoint": "zone_a"}
  delivery              {"pickup": "...", "dropoff": "..."}  (or RMF-style
                         {"pickup": {"place": ...}, "dropoff": {"place": ...}})
  patrol                {"places": ["zone_a", "zone_b"], "rounds": 2}
  loop                  alias of patrol

Robot state in /fleets is live (from /odom in ROS 2 mode, or the built-in
kinematic simulation otherwise). Doors/lifts/dispensers return empty-but-valid
payloads — the warehouse scene has none.

Run:
    python -m nayantra.rmf_bridge.server
Env (see nayantra/config.py): ROS2_ENABLED, RMF_BRIDGE_HOST, RMF_BRIDGE_PORT,
FLEET_NAME, ROBOT_NAME, WAYPOINTS_FILE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from nayantra.config import settings
from nayantra.ros2_adapter.fleet_adapter import (
    WAREHOUSE_WAYPOINTS,
    RMFFleetAdapter,
    RobotMode,
)

logger = logging.getLogger("nayantra.rmf_bridge")

ARRIVAL_TIMEOUT_S = 300.0
ARRIVAL_POLL_S = 0.3
DWELL_S = 2.0  # simulated pickup/dropoff handling time

# RobotMode → the status strings the stub / dashboard already use
_MODE_TO_STATUS = {
    RobotMode.IDLE: "idle",
    RobotMode.CHARGING: "charging",
    RobotMode.MOVING: "working",
    RobotMode.PAUSED: "paused",
    RobotMode.WAITING: "waiting",
    RobotMode.EMERGENCY: "emergency",
    RobotMode.GOING_HOME: "working",
    RobotMode.DOCKING: "working",
    RobotMode.ADAPTER_ERROR: "error",
}


def _load_waypoints() -> dict[str, tuple]:
    """WAYPOINTS_FILE overrides the built-in map when present."""
    path = Path(settings.WAYPOINTS_FILE)
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            wps = {k.lower(): tuple(v) for k, v in raw.items()}
            logger.info(f"Loaded {len(wps)} waypoints from {path}")
            return wps
        except Exception as exc:
            logger.error(f"Bad waypoints file {path}: {exc} — using built-ins")
    return dict(WAREHOUSE_WAYPOINTS)


def _extract_place(value: Any) -> str | None:
    """Accept 'zone_a' or RMF-style {'place': 'zone_a'} / {'waypoint': ...}."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, dict):
        for key in ("place", "waypoint", "name"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key].strip()
    return None


class TaskRecord:
    """In-memory task state machine, RMF-ish on the wire."""

    def __init__(self, category: str, description: dict, robot: str, fleet: str):
        self.task_id = f"task-{uuid.uuid4().hex[:8]}"
        self.category = category
        self.description = description
        self.robot = robot
        self.fleet = fleet
        self.status = "queued"  # queued | underway | completed | canceled | failed
        self.created = time.time()
        self.log: list[dict[str, Any]] = []
        self._runner: asyncio.Task | None = None
        self.add_log(f"Task created: {category} -> {description}")

    def add_log(self, text: str) -> None:
        entry = {"t": round(time.time(), 2), "text": text}
        self.log.append(entry)
        logger.info(f"[{self.task_id}] {text}")

    def as_state(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "category": self.category,
            "status": self.status,
            "robot_name": self.robot,
            "fleet_name": self.fleet,
            "unix_millis_start_time": int(self.created * 1000),
            "detail": self.log[-1]["text"] if self.log else "",
        }


class RMFBridge:
    """Owns the fleet adapters, the waypoint map, and the task table."""

    def __init__(self) -> None:
        self.waypoints = _load_waypoints()
        self.tasks: dict[str, TaskRecord] = {}
        self.decommissioned: set[str] = set()
        self.adapters: dict[str, RMFFleetAdapter] = {}
        self._adapter_tasks: list[asyncio.Task] = []

        # Single-robot v1; add entries here (with namespaces) for multi-robot.
        adapter = RMFFleetAdapter(
            fleet_name=settings.FLEET_NAME,
            robot_name=settings.ROBOT_NAME,
            ros2_enabled=settings.ROS2_ENABLED,
        )
        self.adapters[settings.ROBOT_NAME] = adapter

    async def start(self) -> None:
        for name, adapter in self.adapters.items():
            self._adapter_tasks.append(asyncio.create_task(adapter.start()))
            logger.info(f"Adapter task started for {name} (ros2={settings.ROS2_ENABLED})")

    async def stop(self) -> None:
        for adapter in self.adapters.values():
            await adapter.stop()
        for t in self._adapter_tasks:
            t.cancel()

    # ------------------------------------------------------------------
    # Robot selection / state
    # ------------------------------------------------------------------

    def pick_robot(self, robot_name: str | None) -> RMFFleetAdapter:
        if robot_name:
            adapter = self.adapters.get(robot_name)
            if adapter is None:
                raise HTTPException(
                    404, f"Unknown robot {robot_name!r}. Known: {list(self.adapters)}"
                )
            if robot_name in self.decommissioned:
                raise HTTPException(409, f"Robot {robot_name!r} is decommissioned")
            return adapter
        # Prefer an idle, commissioned robot; fall back to the first one.
        for name, adapter in self.adapters.items():
            if name not in self.decommissioned and adapter.state.mode == RobotMode.IDLE:
                return adapter
        for name, adapter in self.adapters.items():
            if name not in self.decommissioned:
                return adapter
        raise HTTPException(409, "All robots are decommissioned")

    def robot_payload(self, adapter: RMFFleetAdapter) -> dict[str, Any]:
        s = adapter.state
        name = s.name
        status = (
            "decommissioned" if name in self.decommissioned else _MODE_TO_STATUS.get(s.mode, "idle")
        )
        return {
            "name": name,
            "status": status,
            "task_id": s.task_id,
            "battery": round(s.battery_percent / 100.0, 2),
            "location": {
                "x": round(s.location.x, 3),
                "y": round(s.location.y, 3),
                "yaw": round(s.location.yaw, 3),
                "level_name": s.location.level_name,
            },
        }

    # ------------------------------------------------------------------
    # Task execution
    # ------------------------------------------------------------------

    def resolve_waypoint(self, name: str) -> tuple:
        key = name.lower().replace(" ", "_")
        if key not in self.waypoints:
            raise HTTPException(
                400,
                f"Unknown waypoint {name!r}. Valid waypoints: {sorted(self.waypoints)}",
            )
        return self.waypoints[key]

    async def _drive_to(self, task: TaskRecord, adapter: RMFFleetAdapter, place: str) -> bool:
        """Send one nav goal and wait for arrival. Returns success."""
        self.resolve_waypoint(place)  # raise early on bad names
        task.add_log(f"Navigating to '{place}'")
        ok = await adapter.navigate_to_waypoint(place, self.waypoints)
        if not ok:
            task.add_log(f"Goal to '{place}' was rejected")
            return False
        adapter.state.task_id = task.task_id

        deadline = time.time() + ARRIVAL_TIMEOUT_S
        while time.time() < deadline:
            if task.status == "canceled":
                return False
            if adapter._nav_goal is None:  # cleared on arrival/failure/cancel
                arrived = adapter.state.mode != RobotMode.ADAPTER_ERROR
                task.add_log(f"{'Arrived at' if arrived else 'Failed to reach'} '{place}'")
                return arrived
            await asyncio.sleep(ARRIVAL_POLL_S)
        task.add_log(f"Timed out after {ARRIVAL_TIMEOUT_S:.0f}s heading to '{place}'")
        return False

    async def _run_task(self, task: TaskRecord, adapter: RMFFleetAdapter) -> None:
        task.status = "underway"
        try:
            if task.category == "navigate_to_waypoint":
                place = _extract_place(task.description.get("waypoint")) or _extract_place(
                    task.description.get("place")
                )
                if not place:
                    raise HTTPException(400, "navigate_to_waypoint needs description.waypoint")
                ok = await self._drive_to(task, adapter, place)

            elif task.category == "delivery":
                pickup = _extract_place(task.description.get("pickup"))
                dropoff = _extract_place(task.description.get("dropoff"))
                if not pickup or not dropoff:
                    raise HTTPException(
                        400, "delivery needs description.pickup and description.dropoff"
                    )
                ok = await self._drive_to(task, adapter, pickup)
                if ok:
                    task.add_log(f"Picking up payload ({DWELL_S:.0f}s)")
                    await asyncio.sleep(DWELL_S)
                    ok = await self._drive_to(task, adapter, dropoff)
                    if ok:
                        task.add_log(f"Dropping off payload ({DWELL_S:.0f}s)")
                        await asyncio.sleep(DWELL_S)

            elif task.category in ("patrol", "loop"):
                places = task.description.get("places") or []
                places = [p for p in (_extract_place(x) for x in places) if p]
                rounds = int(task.description.get("rounds", 1))
                if not places:
                    raise HTTPException(400, "patrol needs description.places (list of waypoints)")
                ok = True
                for _ in range(rounds):
                    for place in places:
                        ok = await self._drive_to(task, adapter, place)
                        if not ok or task.status == "canceled":
                            break
                    if not ok or task.status == "canceled":
                        break

            else:
                raise HTTPException(400, f"Unsupported category {task.category!r}")

            if task.status != "canceled":
                task.status = "completed" if ok else "failed"
                task.add_log(f"Task {task.status}")
        except HTTPException:
            task.status = "failed"
            raise
        except Exception as exc:
            task.status = "failed"
            task.add_log(f"Task error: {exc}")
        finally:
            if adapter.state.task_id == task.task_id:
                adapter.state.task_id = ""

    def dispatch(self, request: dict[str, Any]) -> TaskRecord:
        category = request.get("category", "")
        description = request.get("description") or {}
        adapter = self.pick_robot(request.get("robot_name"))

        # Validate waypoints BEFORE accepting the task so the LLM gets an
        # immediate, actionable error (with the list of valid names).
        if category == "navigate_to_waypoint":
            place = _extract_place(description.get("waypoint")) or _extract_place(
                description.get("place")
            )
            if not place:
                raise HTTPException(400, "navigate_to_waypoint needs description.waypoint")
            self.resolve_waypoint(place)
        elif category == "delivery":
            for leg in ("pickup", "dropoff"):
                place = _extract_place(description.get(leg))
                if not place:
                    raise HTTPException(400, f"delivery needs description.{leg}")
                self.resolve_waypoint(place)

        task = TaskRecord(
            category=category,
            description=description,
            robot=adapter.robot_name,
            fleet=adapter.fleet_name,
        )
        self.tasks[task.task_id] = task
        task._runner = asyncio.create_task(self._run_task(task, adapter))
        return task

    async def cancel(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, f"Unknown task {task_id!r}")
        if task.status in ("completed", "canceled", "failed"):
            return False
        task.status = "canceled"
        task.add_log("Cancel requested")
        adapter = self.adapters.get(task.robot)
        if adapter is not None:
            await adapter.pause()
            await adapter.resume()  # leave the robot IDLE, not PAUSED
        return True


bridge = RMFBridge()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bridge.start()
    yield
    await bridge.stop()


app = FastAPI(title="Nayantra RMF Bridge", version="1.0.0", lifespan=lifespan)


def _ok(data: Any) -> JSONResponse:
    return JSONResponse({"status": "ok", "data": data, "timestamp": int(time.time())})


# ---------------------------------------------------------------------------
# Health / fleets
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    return {
        "status": "ok",
        "mode": "ros2" if settings.ROS2_ENABLED else "kinematic-sim",
        "robots": list(bridge.adapters),
        "waypoints": sorted(bridge.waypoints),
    }


@app.get("/fleets")
def get_fleets():
    fleets: dict[str, dict] = {}
    for adapter in bridge.adapters.values():
        fleet = fleets.setdefault(adapter.fleet_name, {"name": adapter.fleet_name, "robots": {}})
        fleet["robots"][adapter.robot_name] = bridge.robot_payload(adapter)
    return _ok(list(fleets.values()))


@app.get("/fleets/{fleet_name}/robots/{robot_name}")
def get_robot(fleet_name: str, robot_name: str):
    adapter = bridge.adapters.get(robot_name)
    if adapter is None or adapter.fleet_name != fleet_name:
        raise HTTPException(404, f"No robot {robot_name!r} in fleet {fleet_name!r}")
    return _ok({**bridge.robot_payload(adapter), "fleet": fleet_name})


@app.get("/fleets/{fleet_name}/log")
def get_fleet_log(fleet_name: str):
    entries = [e for t in bridge.tasks.values() if t.fleet == fleet_name for e in t.log]
    return _ok({"fleet": fleet_name, "log": entries[-100:]})


@app.post("/fleets/{fleet_name}/decommission")
async def decommission(fleet_name: str, payload: dict):
    name = payload.get("robot_name", "")
    if name not in bridge.adapters:
        raise HTTPException(404, f"Unknown robot {name!r}")
    bridge.decommissioned.add(name)
    return _ok({"fleet": fleet_name, "robot": name, "action": "decommissioned"})


@app.post("/fleets/{fleet_name}/recommission")
async def recommission(fleet_name: str, payload: dict):
    name = payload.get("robot_name", "")
    bridge.decommissioned.discard(name)
    return _ok({"fleet": fleet_name, "robot": name, "action": "recommissioned"})


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@app.post("/tasks/dispatch_task")
async def dispatch_task(payload: dict):
    request = payload.get("request") or payload  # tolerate both shapes
    task = bridge.dispatch(request)
    return _ok({"task_id": task.task_id, "state": task.status, "robot": task.robot})


@app.get("/tasks")
def list_tasks():
    return _ok([t.as_state() for t in bridge.tasks.values()])


@app.get("/tasks/{task_id}/state")
def task_state(task_id: str):
    task = bridge.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, f"Unknown task {task_id!r}")
    return _ok(task.as_state())


@app.get("/tasks/{task_id}/log")
def task_log(task_id: str):
    task = bridge.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, f"Unknown task {task_id!r}")
    return _ok({"task_id": task_id, "log": task.log})


@app.post("/tasks/cancel_task")
async def cancel_task(payload: dict):
    task_id = payload.get("task_id", "")
    changed = await bridge.cancel(task_id)
    return _ok({"task_id": task_id, "action": "cancelled" if changed else "already_finished"})


@app.post("/tasks/resume_task")
async def resume_task(payload: dict):
    # Tasks are not pausable mid-flight in v1; report state honestly.
    task_id = payload.get("task_id", "")
    task = bridge.tasks.get(task_id)
    if task is None:
        raise HTTPException(404, f"Unknown task {task_id!r}")
    return _ok({"task_id": task_id, "action": "noop", "status": task.status})


@app.post("/tasks/interrupt_task")
async def interrupt_task(payload: dict):
    return await cancel_task(payload)


@app.post("/tasks/kill_task")
async def kill_task(payload: dict):
    return await cancel_task(payload)


# ---------------------------------------------------------------------------
# Building map — generated from the live waypoint table so the LLM can
# discover valid destination names by calling get_building_map.
# ---------------------------------------------------------------------------


@app.get("/building_map")
def building_map():
    vertices = [
        {"x": coords[0], "y": coords[1], "name": name}
        for name, coords in sorted(bridge.waypoints.items())
    ]
    return _ok(
        {
            "name": "IsaacWarehouse",
            "levels": [
                {
                    "name": "L1",
                    "elevation": 0.0,
                    "nav_graphs": [{"name": "0", "vertices": vertices, "edges": []}],
                }
            ],
        }
    )


# ---------------------------------------------------------------------------
# Infrastructure the warehouse scene doesn't have — empty but valid
# ---------------------------------------------------------------------------


@app.get("/doors")
def get_doors():
    return _ok([])


@app.get("/doors/{door_name}/state")
def door_state(door_name: str):
    raise HTTPException(404, f"No door {door_name!r} in this map")


@app.post("/doors/{door_name}/request")
def door_request(door_name: str, payload: dict):
    raise HTTPException(404, f"No door {door_name!r} in this map")


@app.get("/lifts")
def get_lifts():
    return _ok([])


@app.get("/lifts/{lift_name}/state")
def lift_state(lift_name: str):
    raise HTTPException(404, f"No lift {lift_name!r} in this map")


@app.post("/lifts/{lift_name}/request")
def lift_request(lift_name: str, payload: dict):
    raise HTTPException(404, f"No lift {lift_name!r} in this map")


@app.get("/alerts")
def get_alerts():
    return _ok([])


@app.get("/alerts/{alert_id}")
def get_alert(alert_id: str):
    raise HTTPException(404, f"Unknown alert {alert_id!r}")


@app.post("/alerts/{alert_id}/response")
def alert_response(alert_id: str, payload: dict):
    raise HTTPException(404, f"Unknown alert {alert_id!r}")


@app.get("/fire_alarm_trigger")
def fire_alarm():
    return _ok({"triggered": False})


@app.post("/fire_alarm_trigger/reset")
def fire_alarm_reset(payload: dict | None = None):
    return _ok({"action": "reset"})


@app.get("/dispensers")
def get_dispensers():
    return _ok([])


@app.get("/ingestors")
def get_ingestors():
    return _ok([])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=getattr(logging, settings.LOGGING_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info(
        f"RMF bridge starting on {settings.RMF_BRIDGE_HOST}:{settings.RMF_BRIDGE_PORT} "
        f"(ros2={settings.ROS2_ENABLED}, robot={settings.ROBOT_NAME})"
    )
    uvicorn.run(app, host=settings.RMF_BRIDGE_HOST, port=settings.RMF_BRIDGE_PORT)


if __name__ == "__main__":
    main()
