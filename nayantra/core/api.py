"""
nayantra/core/api.py

REST API of the Nayantra Core (/api/v1). Every body is a Pydantic schema;
every error is {"error", "message", "details"} with a meaningful status code.

Dangerous operations (stop/e-stop all, remove robot, delete map, send a robot
into a restricted zone, cancel all tasks) take `?confirm=true`. Without it —
or when the caller identifies as the MCP layer (X-Nayantra-Client: mcp) — the
request is parked as a pending confirmation and answered with 202. Single-robot
emergency stop is never delayed by a confirmation.

Every handler is `async def`. Core state belongs to the event loop (sim ticks,
executors, traffic), and a plain `def` handler would run on FastAPI's thread
pool and race it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from nayantra.config import settings
from nayantra.core.adapters.ros2_runtime import Ros2Runtime
from nayantra.core.allocation import allocate
from nayantra.core.errors import ConfirmationRequired, Conflict, Invalid, NotFound
from nayantra.core.models import (
    KNOWN_CAPABILITIES,
    FleetCreate,
    FleetPatch,
    LaneCreate,
    LanePatch,
    Localization,
    MapCreate,
    MapImage,
    MapPatch,
    MapSeed,
    NavStack,
    Protocol,
    RobotCreate,
    RobotPatch,
    RobotType,
    Task,
    TaskCreate,
    TaskRequirements,
    TaskTrace,
    TaskType,
    WaypointCreate,
    WaypointPatch,
    WaypointType,
    ZoneCreate,
    ZonePatch,
    ZoneType,
)
from nayantra.core.runtime import VERSION, NayantraCore
from nayantra.core.tasks import TASK_CAPABILITIES, TASK_SPECS

router = APIRouter(prefix="/api/v1")
IMAGE_DIR = Path(settings.NAYANTRA_DB_PATH).parent / "maps"


def core_of(request: Request) -> NayantraCore:
    return request.app.state.core


def client_of(request: Request) -> str:
    return request.headers.get("x-nayantra-client", "api").lower()


def trace_of(request: Request, tool: str | None = None) -> TaskTrace:
    client = client_of(request)
    source = {"mcp": "llm", "ui": "operator"}.get(client, "api")
    return TaskTrace(
        source=source,
        mission_id=request.headers.get("x-nayantra-mission"),
        command=urllib.parse.unquote(request.headers["x-nayantra-command"])
        if "x-nayantra-command" in request.headers
        else None,
        tool=request.headers.get("x-nayantra-tool") or tool,
    )


async def guarded(
    request: Request,
    confirm: bool,
    kind: str,
    summary: str,
    params: dict[str, Any],
    detail: str = "",
) -> Any:
    """Run a dangerous action now (operator-confirmed) or park it for confirmation."""
    core = core_of(request)
    client = client_of(request)
    if confirm and client != "mcp":
        return await core.confirmations.handlers[kind](params)
    action = core.confirmations.request(
        kind, summary, params, detail=detail, requested_by=client, trace=trace_of(request)
    )
    raise ConfirmationRequired(
        f"{summary} — needs operator confirmation (id {action.id})",
        action.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class Reason(BaseModel):
    reason: str | None = None


class NavigateBody(BaseModel):
    waypoint: str = Field(..., description="waypoint id, name, alias or zone")
    priority: int = Field(5, ge=0, le=9)


class DispatchBody(BaseModel):
    robot_ids: list[str] = Field(..., min_length=1)
    type: TaskType
    params: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(5, ge=0, le=9)


class ImageUpload(BaseModel):
    data_url: str = Field(..., description="data:image/png;base64,…")
    resolution: float = Field(0.05, gt=0)
    origin: list[float] = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    width_px: int = Field(..., gt=0)
    height_px: int = Field(..., gt=0)
    opacity: float = Field(0.6, ge=0, le=1)


class SimPatch(BaseModel):
    speed: float | None = Field(None, ge=0.1, le=8.0)
    running: bool | None = None


class ConfirmationCreate(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    detail: str = ""


class AgentCommand(BaseModel):
    command: str = Field(..., min_length=1, max_length=2000)


# ---------------------------------------------------------------------------
# System / meta / world
# ---------------------------------------------------------------------------


@router.get("/health")
async def health(request: Request):
    core = core_of(request)
    return {"status": "ok", "version": VERSION, "uptime_s": round(time.time() - core.started_at, 1)}


@router.get("/meta")
async def meta():
    """Enumerations for forms (registration wizard, map editor, task builder)."""
    return {
        "robot_types": [t.value for t in RobotType],
        "capabilities": KNOWN_CAPABILITIES,
        "protocols": [p.value for p in Protocol],
        "implemented_protocols": ["simulation", "ros2", "isaac_demo"],
        "nav_stacks": [n.value for n in NavStack],
        "localization": [loc.value for loc in Localization],
        "waypoint_types": [w.value for w in WaypointType],
        "zone_types": [z.value for z in ZoneType],
        "task_types": TASK_SPECS,
        "robot_type_defaults": {
            "ugv": {
                "capabilities": ["navigate", "carry_payload", "pick", "drop", "dock", "charge"],
                "max_speed": 1.2,
                "radius": 0.45,
                "layer": "ground",
            },
            "uav": {
                "capabilities": ["navigate", "inspect", "takeoff", "land", "charge"],
                "max_speed": 3.0,
                "radius": 0.5,
                "layer": "air",
            },
            "quadruped": {
                "capabilities": ["navigate", "patrol", "inspect", "follow"],
                "max_speed": 1.0,
                "radius": 0.35,
                "layer": "ground",
            },
            "humanoid": {
                "capabilities": ["navigate", "manipulate", "pick", "drop", "inspect"],
                "max_speed": 0.7,
                "radius": 0.3,
                "layer": "ground",
            },
            "other": {
                "capabilities": ["navigate"],
                "max_speed": 0.8,
                "radius": 0.4,
                "layer": "ground",
            },
        },
    }


@router.get("/world")
async def world(request: Request):
    return core_of(request).snapshot()


@router.get("/system")
async def system(request: Request):
    core = core_of(request)

    async def probe(url: str) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=1.5) as http:
                r = await http.get(url)
            return {
                "ok": r.status_code == 200,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "url": url,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__, "url": url}

    ros = Ros2Runtime.peek()
    key = {
        "anthropic": settings.ANTHROPIC_API_KEY,
        "openai": settings.OPENAI_API_KEY,
        "gemini": settings.GEMINI_API_KEY,
    }
    return {
        "core": {
            "ok": True,
            "version": VERSION,
            "uptime_s": round(time.time() - core.started_at, 1),
            "db": core.store.path,
        },
        "simulation": core.sim.status(),
        "ros2": (
            {
                "ok": ros.available,
                "detail": ros.error or f"node /nayantra_core, domain {ros.domain_id}",
            }
            if ros
            else {"ok": None, "detail": "not started (no ROS 2 robots connected)"}
        ),
        "agent": await probe(f"{settings.AGENT_API_URL.rstrip('/')}/health"),
        "mcp": await probe(f"{settings.MCP_SERVER_URL.rstrip('/')}/health"),
        "llm": {
            "provider": settings.LLM_PROVIDER,
            "configured": bool(key.get(settings.LLM_PROVIDER)),
        },
        "websocket_clients": core.bus.subscriber_count,
        "counts": {
            "maps": len(core.registry.maps),
            "fleets": len(core.registry.fleets),
            "robots": len(core.registry.robots),
            "open_tasks": len(core.tasks.list(status="active")),
        },
    }


# ---------------------------------------------------------------------------
# Maps, waypoints, lanes, zones
# ---------------------------------------------------------------------------


@router.get("/maps")
async def list_maps(request: Request):
    r = core_of(request).registry
    return [
        {
            **m.model_dump(mode="json"),
            "counts": {
                "waypoints": len(r.list_waypoints(m.id)),
                "lanes": len(r.list_lanes(m.id)),
                "zones": len(r.list_zones(m.id)),
            },
        }
        for m in r.maps.values()
    ]


@router.post("/maps", status_code=201)
async def create_map(request: Request, body: dict[str, Any]):
    r = core_of(request).registry
    if any(k in body for k in ("waypoints", "lanes", "zones")):
        seed = MapSeed.model_validate(body)
        m = r.import_map_seed(seed)
    else:
        m = r.create_map(MapCreate.model_validate(body))
    return r.map_detail(m.id)


@router.get("/maps/{map_id}")
async def get_map(request: Request, map_id: str):
    return core_of(request).registry.map_detail(map_id)


@router.patch("/maps/{map_id}")
async def patch_map(request: Request, map_id: str, body: MapPatch):
    return core_of(request).registry.update_map(map_id, body)


@router.delete("/maps/{map_id}")
async def delete_map(request: Request, map_id: str, confirm: bool = False):
    r = core_of(request).registry
    m = r.get_map(map_id)
    refs = r.map_references(map_id)
    if refs["fleets"] or refs["robots"]:
        raise Conflict(f"Map '{m.name}' is used by fleets/robots; reassign them first", refs)
    return await guarded(
        request,
        confirm,
        "delete_map",
        f"Delete map '{m.name}' and all its waypoints, lanes and zones?",
        {"map_id": map_id},
    )


@router.post("/maps/{map_id}/image")
async def upload_map_image(request: Request, map_id: str, body: ImageUpload):
    r = core_of(request).registry
    r.get_map(map_id)
    m = re.match(r"^data:image/(png|jpeg|jpg|webp);base64,(.+)$", body.data_url, re.S)
    if not m:
        raise Invalid("data_url must be a base64 PNG, JPEG or WebP data URL")
    raw = base64.b64decode(m.group(2))
    if len(raw) > 15 * 1024 * 1024:
        raise Invalid("map image larger than 15 MB")
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if m.group(1) in ("jpeg", "jpg") else m.group(1)
    for old in IMAGE_DIR.glob(f"{map_id}.*"):
        old.unlink()
    (IMAGE_DIR / f"{map_id}.{ext}").write_bytes(raw)
    image = MapImage(
        url=f"/api/v1/maps/{map_id}/image?v={int(time.time())}",
        resolution=body.resolution,
        origin=body.origin,
        width_px=body.width_px,
        height_px=body.height_px,
        opacity=body.opacity,
    )
    return r.update_map(map_id, MapPatch(image=image))


@router.get("/maps/{map_id}/image")
async def get_map_image(map_id: str):
    for p in IMAGE_DIR.glob(f"{map_id}.*"):
        return FileResponse(str(p))
    raise NotFound(f"Map {map_id!r} has no image")


@router.get("/maps/{map_id}/export/rmf-nav-graph", response_class=PlainTextResponse)
async def export_rmf_nav_graph(request: Request, map_id: str):
    """Nav graph in the rmf_fleet_adapter YAML layout (emitted in JSON flow style, valid YAML)."""
    r = core_of(request).registry
    m = r.get_map(map_id)
    wps = r.list_waypoints(map_id)
    index = {w.id: i for i, w in enumerate(wps)}
    vertices = [
        [
            w.x,
            w.y,
            {
                "name": w.name if w.type != "intersection" else "",
                "is_charger": w.type == "charger",
                "is_parking_spot": w.type == "parking",
                "is_holding_point": w.type == "parking",
                "pickup_dispenser": w.id if w.type == "pickup" else "",
                "dropoff_ingestor": w.id if w.type == "dropoff" else "",
            },
        ]
        for w in wps
    ]
    lanes = []
    for ln in r.list_lanes(map_id):
        if ln.layer != "ground":
            continue
        props = {"speed_limit": ln.speed_limit or 0.0}
        lanes.append([index[ln.from_id], index[ln.to_id], props])
        if ln.bidirectional:
            lanes.append([index[ln.to_id], index[ln.from_id], props])
    doc = {"building_name": m.name, "levels": {"L1": {"vertices": vertices, "lanes": lanes}}}
    return PlainTextResponse(json.dumps(doc, indent=1), media_type="application/x-yaml")


@router.get("/maps/{map_id}/waypoints")
async def list_waypoints(request: Request, map_id: str, type: str | None = None):
    r = core_of(request).registry
    r.get_map(map_id)
    return r.list_waypoints(map_id, type)


@router.post("/maps/{map_id}/waypoints", status_code=201)
async def create_waypoint(request: Request, map_id: str, body: WaypointCreate):
    return core_of(request).registry.create_waypoint(map_id, body)


@router.get("/waypoints/{wp_id}")
async def get_waypoint(request: Request, wp_id: str):
    return core_of(request).registry.get_waypoint(wp_id)


@router.patch("/waypoints/{wp_id}")
async def patch_waypoint(request: Request, wp_id: str, body: WaypointPatch):
    return core_of(request).registry.update_waypoint(wp_id, body)


@router.delete("/waypoints/{wp_id}")
async def delete_waypoint(request: Request, wp_id: str):
    core = core_of(request)
    core.registry.delete_waypoint(wp_id, in_use_by=core.tasks.open_tasks_using(wp_id))
    return {"deleted": wp_id}


@router.get("/maps/{map_id}/lanes")
async def list_lanes(request: Request, map_id: str):
    r = core_of(request).registry
    r.get_map(map_id)
    return r.list_lanes(map_id)


@router.post("/maps/{map_id}/lanes", status_code=201)
async def create_lane(request: Request, map_id: str, body: LaneCreate):
    return core_of(request).registry.create_lane(map_id, body)


@router.patch("/lanes/{lane_id}")
async def patch_lane(request: Request, lane_id: str, body: LanePatch):
    return core_of(request).registry.update_lane(lane_id, body)


@router.delete("/lanes/{lane_id}")
async def delete_lane(request: Request, lane_id: str):
    core_of(request).registry.delete_lane(lane_id)
    return {"deleted": lane_id}


@router.get("/maps/{map_id}/zones")
async def list_zones(request: Request, map_id: str):
    r = core_of(request).registry
    r.get_map(map_id)
    return r.list_zones(map_id)


@router.post("/maps/{map_id}/zones", status_code=201)
async def create_zone(request: Request, map_id: str, body: ZoneCreate):
    return core_of(request).registry.create_zone(map_id, body)


@router.patch("/zones/{zone_id}")
async def patch_zone(request: Request, zone_id: str, body: ZonePatch):
    return core_of(request).registry.update_zone(zone_id, body)


@router.delete("/zones/{zone_id}")
async def delete_zone(request: Request, zone_id: str):
    core_of(request).registry.delete_zone(zone_id)
    return {"deleted": zone_id}


# ---------------------------------------------------------------------------
# Fleets
# ---------------------------------------------------------------------------


@router.get("/fleets")
async def list_fleets(request: Request):
    core = core_of(request)
    return [core.fleet_view(f) for f in core.registry.fleets]


@router.post("/fleets", status_code=201)
async def create_fleet(request: Request, body: FleetCreate):
    core = core_of(request)
    f = core.registry.create_fleet(body)
    return core.fleet_view(f.id)


@router.get("/fleets/{fleet_id}")
async def get_fleet(request: Request, fleet_id: str):
    return core_of(request).fleet_view(fleet_id)


@router.patch("/fleets/{fleet_id}")
async def patch_fleet(request: Request, fleet_id: str, body: FleetPatch):
    core = core_of(request)
    core.registry.update_fleet(fleet_id, body)
    return core.fleet_view(fleet_id)


@router.delete("/fleets/{fleet_id}")
async def delete_fleet(request: Request, fleet_id: str):
    core_of(request).registry.delete_fleet(fleet_id)
    return {"deleted": fleet_id}


@router.post("/fleets/{fleet_id}/stop")
async def stop_fleet(
    request: Request, fleet_id: str, confirm: bool = False, body: Reason | None = None
):
    f = core_of(request).registry.get_fleet(fleet_id)
    return await guarded(
        request,
        confirm,
        "stop_all",
        f"Stop every robot in {f.name}?",
        {"fleet_id": fleet_id, "reason": body.reason if body else None},
    )


@router.post("/fleets/{fleet_id}/estop")
async def estop_fleet(
    request: Request, fleet_id: str, confirm: bool = False, body: Reason | None = None
):
    f = core_of(request).registry.get_fleet(fleet_id)
    return await guarded(
        request,
        confirm,
        "estop_all",
        f"Emergency-stop the entire {f.name}?",
        {"fleet_id": fleet_id, "reason": body.reason if body else None},
    )


@router.post("/fleets/{fleet_id}/dispatch", status_code=201)
async def dispatch_fleet(request: Request, fleet_id: str, body: DispatchBody):
    """One task per selected robot, each pinned to that robot."""
    core = core_of(request)
    core.registry.get_fleet(fleet_id)
    created = []
    for rid in body.robot_ids:
        robot = core.registry.find_robot(rid)
        if robot.fleet_id != fleet_id:
            raise Invalid(f"{robot.name} is not in fleet {fleet_id}")
        req = TaskCreate(
            type=body.type,
            params=body.params,
            priority=body.priority,
            requirements=TaskRequirements(robot_id=robot.id),
            created_by=client_of(request),
            trace=trace_of(request, "fleet_dispatch"),
        )
        created += core.tasks.create(req)
    return {"tasks": created}


# ---------------------------------------------------------------------------
# Robots
# ---------------------------------------------------------------------------


@router.get("/robots")
async def list_robots(request: Request, fleet_id: str | None = None, status: str | None = None):
    core = core_of(request)
    out = []
    for rid, robot in core.registry.robots.items():
        if fleet_id and robot.fleet_id != fleet_id:
            continue
        view = core.robot_view(rid)
        if status and (view["state"] or {}).get("mode") != status:
            continue
        out.append(view)
    return out


@router.post("/robots", status_code=201)
async def create_robot(request: Request, body: RobotCreate):
    core = core_of(request)
    robot = core.registry.create_robot(body)
    # The fleet manager adds (and connects) the robot via the registry listener;
    # wait briefly so the response includes the connection test.
    for _ in range(60):
        rt = core.fleet.runtimes.get(robot.id)
        if rt and rt.state.connection != "connecting" and (rt.checks or not robot.enabled):
            break
        await asyncio.sleep(0.05)
    return core.robot_view(robot.id)


@router.get("/robots/{robot_id}")
async def get_robot(request: Request, robot_id: str):
    core = core_of(request)
    return core.robot_view(core.registry.find_robot(robot_id).id)


@router.patch("/robots/{robot_id}")
async def patch_robot(request: Request, robot_id: str, body: RobotPatch):
    core = core_of(request)
    rid = core.registry.find_robot(robot_id).id
    core.registry.update_robot(rid, body)
    return core.robot_view(rid)


@router.delete("/robots/{robot_id}")
async def delete_robot(request: Request, robot_id: str, confirm: bool = False):
    core = core_of(request)
    robot = core.registry.find_robot(robot_id)
    return await guarded(
        request,
        confirm,
        "remove_robot",
        f"Remove {robot.name} from fleet {robot.fleet_id}?",
        {"robot_id": robot.id},
    )


@router.post("/robots/{robot_id}/connect")
async def connect_robot(request: Request, robot_id: str):
    core = core_of(request)
    rid = core.registry.find_robot(robot_id).id
    checks = await core.fleet.connect(rid)
    return {"robot_id": rid, "ok": all(c["ok"] for c in checks if c["required"]), "checks": checks}


def _robot_task(
    request: Request, robot_id: str, ttype: TaskType, params: dict, priority: int, tool: str
):
    core = core_of(request)
    robot = core.registry.find_robot(robot_id)
    req = TaskCreate(
        type=ttype,
        params=params,
        priority=priority,
        requirements=TaskRequirements(robot_id=robot.id),
        created_by=client_of(request),
        trace=trace_of(request, tool),
    )
    _check_restricted(core, req)
    return core.tasks.create(req)[0]


def _check_restricted(core: NayantraCore, req: TaskCreate) -> list[str]:
    _, _, fixed = core.tasks.validate(req)
    hits = core.safety.restricted_targets(fixed)
    return [f"{w.name} (in {z.name})" for w, z in hits]


@router.post("/robots/{robot_id}/navigate", status_code=201)
async def navigate_robot(
    request: Request, robot_id: str, body: NavigateBody, confirm: bool = False
):
    core = core_of(request)
    robot = core.registry.find_robot(robot_id)
    req = TaskCreate(
        type=TaskType.NAVIGATE,
        params={"destination": body.waypoint},
        priority=body.priority,
        requirements=TaskRequirements(robot_id=robot.id),
        created_by=client_of(request),
        trace=trace_of(request, "navigate_robot"),
    )
    hits = _check_restricted(core, req)
    if hits:
        return await guarded(
            request,
            confirm,
            "restricted_task",
            f"Send {robot.name} into restricted area {hits[0]}?",
            {"request": req.model_dump(mode="json")},
        )
    return core.tasks.create(req)[0]


@router.post("/robots/{robot_id}/stop")
async def stop_robot(request: Request, robot_id: str, body: Reason | None = None):
    core = core_of(request)
    rid = core.registry.find_robot(robot_id).id
    await core.fleet.stop_robot(
        rid, (body.reason if body and body.reason else None) or "Stopped by operator"
    )
    return core.robot_view(rid)


@router.post("/robots/{robot_id}/pause")
async def pause_robot(request: Request, robot_id: str):
    core = core_of(request)
    rid = core.registry.find_robot(robot_id).id
    await core.fleet.hold_robot(rid, True)
    return core.robot_view(rid)


@router.post("/robots/{robot_id}/resume")
async def resume_robot(request: Request, robot_id: str):
    core = core_of(request)
    rid = core.registry.find_robot(robot_id).id
    await core.fleet.hold_robot(rid, False)
    return core.robot_view(rid)


@router.post("/robots/{robot_id}/estop")
async def estop_robot(request: Request, robot_id: str, body: Reason | None = None):
    core = core_of(request)
    rid = core.registry.find_robot(robot_id).id
    await core.fleet.emergency_stop(
        rid,
        (body.reason if body and body.reason else None) or f"Emergency stop ({client_of(request)})",
    )
    return core.robot_view(rid)


@router.post("/robots/{robot_id}/release-estop")
async def release_estop(request: Request, robot_id: str):
    core = core_of(request)
    if client_of(request) == "mcp":
        raise Conflict(
            "Releasing an emergency stop requires the operator (dashboard or API), not the agent"
        )
    rid = core.registry.find_robot(robot_id).id
    await core.fleet.release_emergency_stop(rid)
    return core.robot_view(rid)


@router.post("/robots/{robot_id}/dock", status_code=201)
async def dock_robot(request: Request, robot_id: str):
    return _robot_task(request, robot_id, TaskType.DOCK, {}, 6, "dock_robot")


@router.post("/robots/{robot_id}/charge", status_code=201)
async def charge_robot(request: Request, robot_id: str):
    return _robot_task(request, robot_id, TaskType.CHARGE, {}, 6, "charge_robot")


@router.post("/robots/{robot_id}/cancel-task")
async def cancel_robot_task(request: Request, robot_id: str):
    core = core_of(request)
    rid = core.registry.find_robot(robot_id).id
    rt = core.fleet.runtimes.get(rid)
    if not rt or not rt.current_task:
        raise Conflict(f"{core.robot_name(rid)} has no active task")
    return await core.tasks.cancel(rt.current_task, "Cancelled by operator from the robot panel")


@router.post("/robots/{robot_id}/restart-navigation")
async def restart_navigation(request: Request, robot_id: str):
    core = core_of(request)
    rid = core.registry.find_robot(robot_id).id
    return {"robot_id": rid, "checks": await core.fleet.restart_navigation(rid)}


@router.post("/robots/stop-all")
async def stop_all(request: Request, confirm: bool = False, body: Reason | None = None):
    return await guarded(
        request, confirm, "stop_all", "Stop all robots?", {"reason": body.reason if body else None}
    )


@router.post("/robots/estop-all")
async def estop_all(request: Request, confirm: bool = False, body: Reason | None = None):
    return await guarded(
        request,
        confirm,
        "estop_all",
        "Emergency-stop ALL robots?",
        {"reason": body.reason if body else None},
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@router.get("/tasks")
async def list_tasks(
    request: Request,
    status: str | None = None,
    robot_id: str | None = None,
    limit: int = Query(200, le=1000),
):
    return core_of(request).tasks.list(status=status, robot_id=robot_id, limit=limit)


@router.post("/tasks", status_code=201)
async def create_task(
    request: Request, body: TaskCreate, count: int = Query(1, ge=1, le=20), confirm: bool = False
):
    core = core_of(request)
    if body.trace.source == "api" and body.trace.command is None:
        body = body.model_copy(update={"trace": trace_of(request, "create_task")})
    if body.created_by == "operator" and client_of(request) == "mcp":
        body = body.model_copy(update={"created_by": "llm"})
    hits = _check_restricted(core, body)
    if hits or body.allow_restricted:
        where = hits[0] if hits else "a restricted area"
        return await guarded(
            request,
            confirm,
            "restricted_task",
            f"Send a robot into restricted area {where}?",
            {"request": body.model_dump(mode="json"), "count": count},
        )
    return {"tasks": core.tasks.create(body, count=count)}


@router.post("/tasks/validate")
async def validate_task(request: Request, body: TaskCreate):
    """Dry run: how the task would be built and who would get it (nothing is created)."""
    core = core_of(request)
    map_id, steps, _ = core.tasks.validate(body)
    task = Task(
        id="preview",
        type=body.type,
        params=body.params,
        priority=body.priority,
        requirements=body.requirements.model_copy(),
        map_id=map_id,
        steps=steps,
    )
    task.requirements.capabilities = list(
        dict.fromkeys(TASK_CAPABILITIES[TaskType(body.type)] + task.requirements.capabilities)
    )
    allocation, _ = allocate(task, core.registry, core.fleet)
    return {
        "map_id": map_id,
        "steps": steps,
        "allocation": allocation,
        "restricted": _check_restricted(core, body),
    }


@router.get("/tasks/{task_id}")
async def get_task(request: Request, task_id: str):
    return core_of(request).tasks.get(task_id)


@router.get("/tasks/{task_id}/trace")
async def task_trace(request: Request, task_id: str):
    """Operator → intent → LLM/tool → task → allocation → route → robot, in one view."""
    core = core_of(request)
    task = core.tasks.get(task_id)
    events = core.bus.recent(300, task_id=task_id)
    return {
        "task": task,
        "origin": task.trace,
        "allocation": task.allocation,
        "events": list(reversed(events)),
        "why": {
            "selected": task.allocation.explanation if task.allocation else None,
            "status": task.status_reason,
        },
    }


@router.post("/tasks/{task_id}/pause")
async def pause_task(request: Request, task_id: str):
    return await core_of(request).tasks.pause(task_id)


@router.post("/tasks/{task_id}/resume")
async def resume_task(request: Request, task_id: str):
    return await core_of(request).tasks.resume(task_id)


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(request: Request, task_id: str):
    return await core_of(request).tasks.cancel(task_id, f"Cancelled by {client_of(request)}")


@router.post("/tasks/cancel-all")
async def cancel_all_tasks(request: Request, confirm: bool = False):
    return await guarded(request, confirm, "cancel_all_tasks", "Cancel every open task?", {})


# ---------------------------------------------------------------------------
# Traffic, events, alerts
# ---------------------------------------------------------------------------


@router.get("/traffic")
async def traffic(request: Request):
    return core_of(request).traffic.snapshot()


@router.get("/traffic/reservations")
async def reservations(request: Request):
    return core_of(request).traffic.snapshot().reservations


@router.get("/traffic/conflicts")
async def conflicts(request: Request):
    return core_of(request).traffic.snapshot().conflicts


@router.get("/events")
async def events(
    request: Request,
    limit: int = Query(200, le=2000),
    robot_id: str | None = None,
    task_id: str | None = None,
    type: str | None = None,
    min_severity: str | None = None,
):
    types = set(type.split(",")) if type else None
    return core_of(request).bus.recent(
        limit, robot_id=robot_id, task_id=task_id, types=types, min_severity=min_severity
    )


@router.get("/alerts")
async def alerts(request: Request, include_resolved: bool = False):
    return core_of(request).bus.alerts(include_resolved)


@router.post("/alerts/{alert_id}/ack")
async def ack_alert(request: Request, alert_id: str):
    a = core_of(request).bus.acknowledge_alert(alert_id)
    if a is None:
        raise NotFound(f"Unknown alert {alert_id!r}")
    return a


# ---------------------------------------------------------------------------
# Confirmations
# ---------------------------------------------------------------------------


@router.get("/confirmations")
async def list_confirmations(request: Request, pending_only: bool = True):
    return core_of(request).confirmations.list(pending_only)


@router.post("/confirmations", status_code=202)
async def request_confirmation(request: Request, body: ConfirmationCreate):
    core = core_of(request)
    if body.kind not in core.confirmations.handlers:
        raise Invalid(
            f"Unknown confirmation kind {body.kind!r}",
            {"kinds": sorted(core.confirmations.handlers)},
        )
    return core.confirmations.request(
        body.kind,
        body.summary or body.kind.replace("_", " "),
        body.params,
        body.detail,
        requested_by=client_of(request),
        trace=trace_of(request),
    )


@router.post("/confirmations/{action_id}/confirm")
async def confirm_action(request: Request, action_id: str):
    if client_of(request) == "mcp":
        raise Conflict("Only an operator can confirm; the agent cannot approve its own request")
    return await core_of(request).confirmations.confirm(action_id, by=client_of(request))


@router.post("/confirmations/{action_id}/reject")
async def reject_action(request: Request, action_id: str):
    return core_of(request).confirmations.reject(action_id, by=client_of(request))


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


@router.get("/sim")
async def sim_status(request: Request):
    return core_of(request).sim.status()


@router.post("/sim/start")
async def sim_start(request: Request):
    core_of(request).sim.set_running(True)
    return core_of(request).sim.status()


@router.post("/sim/pause")
async def sim_pause(request: Request):
    core_of(request).sim.set_running(False)
    return core_of(request).sim.status()


@router.post("/sim/stop")
async def sim_stop(request: Request):
    core = core_of(request)
    await core.fleet.sim_reset()
    core.sim.set_running(False)
    return core.sim.status()


@router.post("/sim/reset")
async def sim_reset(request: Request):
    core = core_of(request)
    await core.fleet.sim_reset()
    core.sim.set_running(True)
    return core.sim.status()


@router.patch("/sim")
async def sim_patch(request: Request, body: SimPatch):
    core = core_of(request)
    if body.speed is not None:
        core.sim.set_speed(body.speed)
    if body.running is not None:
        core.sim.set_running(body.running)
    return core.sim.status()


# ---------------------------------------------------------------------------
# Natural-language commands → agent (separate process; LLM isolated from the
# control plane). The core only relays; the agent acts through MCP tools.
# ---------------------------------------------------------------------------


@router.get("/agent/status")
async def agent_status():
    url = settings.AGENT_API_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=2.0) as http:
            r = await http.get(f"{url}/health")
        ok = r.status_code == 200
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "detail": f"agent API not reachable at {url} ({type(exc).__name__})",
        }
    key = {
        "anthropic": settings.ANTHROPIC_API_KEY,
        "openai": settings.OPENAI_API_KEY,
        "gemini": settings.GEMINI_API_KEY,
    }
    return {
        "available": ok,
        "provider": settings.LLM_PROVIDER,
        "llm_configured": bool(key.get(settings.LLM_PROVIDER)),
        "detail": "" if ok else f"agent API returned HTTP {r.status_code}",
    }


@router.post("/agent/command")
async def agent_command(body: AgentCommand):
    url = f"{settings.AGENT_API_URL.rstrip('/')}/stream"

    async def relay():
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=3.0)) as http:
                async with http.stream("GET", url, params={"command": body.command}) as resp:
                    if resp.status_code != 200:
                        yield _sse(
                            "done",
                            {"summary": f"Agent error: HTTP {resp.status_code}", "success": False},
                        )
                        return
                    async for chunk in resp.aiter_text():
                        yield chunk
        except httpx.ConnectError:
            yield _sse(
                "done",
                {
                    "summary": f"The agent is not running at {settings.AGENT_API_URL} — start it with `nayantra-api` "
                    "(and the MCP server with `nayantra-mcp-server`).",
                    "success": False,
                },
            )
        except Exception as exc:  # noqa: BLE001
            yield _sse("done", {"summary": f"Agent relay error: {exc}", "success": False})

    return StreamingResponse(
        relay(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def json_error(status: int, error: str, message: str, details: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": error, "message": message, "details": details or {}}
    )
