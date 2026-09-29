"""
nayantra/mcp/tools.py

MCP tool registry — the only actions the LLM can take.

Design rules (docs/platform_architecture.md, D5):
  * High-level operations only. Nothing here publishes velocities or raw
    motion: the LLM creates *tasks* and the core allocates, plans, reserves
    traffic and drives.
  * Every call is schema-validated (Pydantic) before it runs; unknown tools
    and invalid parameters are rejected with an explanation.
  * The LLM does not pick robots. `create_task` lets the task manager choose.
    `robot` is only for when the operator named one.
  * Dangerous operations (stop/e-stop all, remove a robot, restricted
    destinations) come back as `confirmation_required`. An operator decides
    in the dashboard; there is no confirm tool.
  * Results are compact summaries of live core state, so the model reasons
    over current data and not over stale prompt text.

Tools are registered as (schema, params model, handler). Legacy names from the
Open-RMF-shaped v1 (move_robot, get_task_state, …) stay executable for old
clients but are hidden from the tool list the LLM sees.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nayantra.config import settings
from nayantra.mcp.core_client import CoreClient, CoreError, Trace
from nayantra.rmf_client.client import OpenRMFClient

logger = logging.getLogger("nayantra.tools")


@dataclass
class ToolContext:
    core: CoreClient
    rmf: OpenRMFClient | None = None
    trace: Trace = field(default_factory=Trace)


class ToolParamError(Exception):
    """Parameters failed schema validation."""


Handler = Callable[[ToolContext, Any], Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    params: type[BaseModel]
    handler: Handler
    risk: Literal["read", "act", "dangerous"] = "read"
    legacy: bool = False
    endpoint: str = ""

    def schema(self) -> dict[str, Any]:
        js = self.params.model_json_schema()
        props = js.get("properties", {})
        for p in props.values():
            p.pop("title", None)
        return {
            "name": self.name,
            "description": self.description,
            "parameters": props,
            "required": js.get("required", []),
            "risk": self.risk,
            "api_endpoint": self.endpoint,
        }


TOOL_REGISTRY: dict[str, ToolSpec] = {}


def _tool(
    name: str,
    description: str,
    params: type[BaseModel],
    risk: str = "read",
    legacy: bool = False,
    endpoint: str = "",
):
    def deco(fn: Handler) -> Handler:
        TOOL_REGISTRY[name] = ToolSpec(name, description, params, fn, risk, legacy, endpoint)  # type: ignore[arg-type]
        return fn

    return deco


def get_all_tools(include_legacy: bool = False) -> list[dict[str, Any]]:
    return [t.schema() for t in TOOL_REGISTRY.values() if include_legacy or not t.legacy]


async def execute_tool(ctx: ToolContext, tool_name: str, params: dict[str, Any]) -> Any:
    """Validate and run one tool. Core-side rejections become {"ok": false, "error": …}."""
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        raise KeyError(f"Unknown tool: {tool_name}")
    try:
        validated = spec.params.model_validate(params or {})
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'params'}: {e['msg']}" for e in exc.errors()
        )
        raise ToolParamError(f"Invalid parameters for {tool_name}: {problems}") from exc
    ctx.trace.tool = tool_name
    logger.info(f"tool {tool_name} {validated.model_dump(exclude_none=True)}")
    try:
        return await spec.handler(ctx, validated)
    except CoreError as exc:
        return {"ok": False, "error": exc.message, "details": exc.details}


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoParams(Params):
    pass


# ---------------------------------------------------------------------------
# Compact views (what the LLM reads)
# ---------------------------------------------------------------------------


def _robot_brief(r: dict[str, Any], wp_names: dict[str, str] | None = None) -> dict[str, Any]:
    st = r.get("state") or {}
    names = wp_names or {}
    return {
        "id": r["id"],
        "name": r["name"],
        "fleet": r["fleet_id"],
        "type": r["effective"]["robot_type"],
        "online": st.get("online", False),
        "status": st.get("mode", "offline"),
        "battery_pct": round(st["battery_pct"]) if st.get("battery_pct") is not None else None,
        "at": names.get(st.get("current_waypoint"), st.get("current_waypoint")),
        "task": st.get("current_task_id"),
        "why": st.get("status_reason") or None,
        "capabilities": r["effective"]["capabilities"],
    }


def _task_brief(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": t["id"],
        "type": t["type"],
        "status": t["status"],
        "robot": t.get("assigned_robot"),
        "progress_pct": round(100 * t.get("progress", 0)),
        "steps": [s["label"] for s in t.get("steps", [])],
        "why": t.get("status_reason"),
        "allocation": (t.get("allocation") or {}).get("explanation"),
    }


async def _wp_names(ctx: ToolContext) -> dict[str, str]:
    names: dict[str, str] = {}
    for m in await ctx.core.get("/maps", trace=ctx.trace):
        for w in await ctx.core.get(f"/maps/{m['id']}/waypoints", trace=ctx.trace):
            names[w["id"]] = w["name"]
    return names


def _confirmation_or(result: Any, ok: Callable[[Any], Any]) -> Any:
    if isinstance(result, dict) and result.get("confirmation_required"):
        c = result["confirmation"]
        return {
            "confirmation_required": True,
            "message": f"{c['summary']} — waiting for the operator to confirm in the dashboard (id {c['id']}). "
            "Tell the operator; do not retry.",
        }
    return ok(result)


# ===========================================================================
# Fleets, robots, maps — read
# ===========================================================================


@_tool(
    "list_fleets",
    "List all fleets with robot type, capabilities and live status counts.",
    NoParams,
    endpoint="GET /fleets",
)
async def _list_fleets(ctx: ToolContext, p: NoParams) -> Any:
    fleets = await ctx.core.get("/fleets", trace=ctx.trace)
    return [
        {
            "id": f["id"],
            "name": f["name"],
            "robot_type": f["robot_type"],
            "capabilities": f["capabilities"],
            "maps": f["map_ids"],
            "robots": len(f["robots"]),
            "stats": f["stats"],
        }
        for f in fleets
    ]


class FleetParams(Params):
    fleet_id: str = Field(..., description="Fleet id (from list_fleets)")


@_tool(
    "get_fleet_state",
    "Live state of one fleet and each of its robots.",
    FleetParams,
    endpoint="GET /fleets/{id}",
)
async def _get_fleet_state(ctx: ToolContext, p: FleetParams) -> Any:
    f = await ctx.core.get(f"/fleets/{p.fleet_id}", trace=ctx.trace)
    robots = await ctx.core.get("/robots", params={"fleet_id": p.fleet_id}, trace=ctx.trace)
    names = await _wp_names(ctx)
    return {
        "fleet": {"id": f["id"], "name": f["name"], "stats": f["stats"]},
        "robots": [_robot_brief(r, names) for r in robots],
    }


class ListRobotsParams(Params):
    fleet_id: str | None = Field(None, description="Only this fleet")
    status: (
        Literal[
            "idle",
            "moving",
            "waiting",
            "paused",
            "charging",
            "acting",
            "error",
            "emergency_stop",
            "offline",
        ]
        | None
    ) = Field(None, description="Only robots in this status (e.g. 'charging')")


@_tool(
    "list_robots",
    "List robots with live status, battery, location, current task and why they are in that state. "
    "Use this to answer 'which robots are available / charging / busy'.",
    ListRobotsParams,
    endpoint="GET /robots",
)
async def _list_robots(ctx: ToolContext, p: ListRobotsParams) -> Any:
    robots = await ctx.core.get(
        "/robots", params={"fleet_id": p.fleet_id, "status": p.status}, trace=ctx.trace
    )
    names = await _wp_names(ctx)
    return [_robot_brief(r, names) for r in robots]


class RobotParams(Params):
    robot: str = Field(..., description="Robot id or name, e.g. 'ugv_03' or 'UGV-03'")


@_tool(
    "get_robot_state",
    "Full live state of one robot: status, why it is waiting, route, ETA, battery, health, current task.",
    RobotParams,
    endpoint="GET /robots/{id}",
)
async def _get_robot_state(ctx: ToolContext, p: RobotParams) -> Any:
    r = await ctx.core.get(f"/robots/{p.robot}", trace=ctx.trace)
    names = await _wp_names(ctx)
    st = r.get("state") or {}
    brief = _robot_brief(r, names)
    brief.update(
        {
            "destination": names.get(st.get("destination"), st.get("destination")),
            "route": [names.get(n, n) for n in st.get("route", [])],
            "eta_s": st.get("eta_s"),
            "position": {k: round(v, 2) for k, v in (st.get("pose") or {}).items()},
            "health": {
                k: v for k, v in (st.get("health") or {}).items() if v not in (None, {}, "")
            },
            "queue": st.get("queue", []),
            "connection": st.get("connection_detail") or st.get("connection"),
        }
    )
    return brief


class MapParams(Params):
    map_id: str | None = Field(None, description="Map id; default = the map most robots use")


async def _default_map(ctx: ToolContext) -> str:
    maps = await ctx.core.get("/maps", trace=ctx.trace)
    if not maps:
        raise CoreError(404, "No maps are defined")
    return max(maps, key=lambda m: m["counts"]["waypoints"])["id"]


@_tool(
    "get_map",
    "The map: named places (waypoints), areas (zones such as 'receiving', 'storage', restricted areas) and graph size. "
    "Use it to resolve place names before creating tasks.",
    MapParams,
    endpoint="GET /maps/{id}",
)
async def _get_map(ctx: ToolContext, p: MapParams) -> Any:
    map_id = p.map_id or await _default_map(ctx)
    m = await ctx.core.get(f"/maps/{map_id}", trace=ctx.trace)
    return {
        "id": m["id"],
        "name": m["name"],
        "places": [
            {
                "id": w["id"],
                "name": w["name"],
                "type": w["type"],
                "layer": w["layer"],
                "aliases": w["aliases"],
            }
            for w in m["waypoints"]
            if w["type"] != "intersection"
        ],
        "areas": [{"name": z["name"], "type": z["type"]} for z in m["zones"]],
        "lanes": len(m["lanes"]),
        "other_maps": [
            x["id"] for x in await ctx.core.get("/maps", trace=ctx.trace) if x["id"] != m["id"]
        ],
    }


class WaypointListParams(Params):
    map_id: str | None = None
    type: (
        Literal[
            "location",
            "charger",
            "pickup",
            "dropoff",
            "dock",
            "parking",
            "landing_pad",
            "inspection",
            "intersection",
        ]
        | None
    ) = None


@_tool(
    "list_waypoints",
    "Named waypoints on a map, optionally filtered by type (e.g. 'charger').",
    WaypointListParams,
    endpoint="GET /maps/{id}/waypoints",
)
async def _list_waypoints(ctx: ToolContext, p: WaypointListParams) -> Any:
    map_id = p.map_id or await _default_map(ctx)
    wps = await ctx.core.get(f"/maps/{map_id}/waypoints", params={"type": p.type}, trace=ctx.trace)
    return [{"id": w["id"], "name": w["name"], "type": w["type"], "layer": w["layer"]} for w in wps]


# ===========================================================================
# Tasks
# ===========================================================================


class CreateTaskParams(Params):
    task_type: Literal[
        "navigate", "delivery", "patrol", "inspect", "charge", "dock", "takeoff", "land"
    ] = Field(..., description="What to do")
    destination: str | None = Field(None, description="navigate: waypoint or area name")
    pickup: str | None = Field(
        None, description="delivery: pickup waypoint or area (e.g. 'receiving')"
    )
    dropoff: str | None = Field(
        None, description="delivery: dropoff waypoint or area (e.g. 'storage')"
    )
    payload: str | None = Field(None, description="delivery: what is carried")
    waypoints: list[str] | None = Field(None, description="patrol: places in order")
    rounds: int | None = Field(None, ge=1, le=20, description="patrol: repetitions")
    target: str | None = Field(
        None, description="inspect/charge/dock/land: specific place (optional for charge/dock/land)"
    )
    count: int = Field(1, ge=1, le=20, description="How many identical tasks (e.g. 3 packages → 3)")
    priority: int = Field(5, ge=0, le=9, description="Traffic/queue priority, 9 = most urgent")
    fleet_id: str | None = Field(None, description="Restrict to one fleet (optional)")
    robot: str | None = Field(
        None,
        description="ONLY if the operator named a specific robot; otherwise leave empty so Nayantra allocates",
    )


def _task_params(p: CreateTaskParams) -> dict[str, Any]:
    t = p.task_type
    out: dict[str, Any] = {}
    if t == "navigate":
        out["destination"] = p.destination or p.target
    elif t == "delivery":
        out.update(pickup=p.pickup, dropoff=p.dropoff, payload=p.payload)
    elif t == "patrol":
        out.update(waypoints=p.waypoints, rounds=p.rounds or 1)
    elif t == "inspect":
        out["target"] = p.target or p.destination
    elif t == "charge":
        out["charger"] = p.target
    elif t == "dock":
        out["dock"] = p.target
    elif t == "land":
        out["pad"] = p.target
    return {k: v for k, v in out.items() if v is not None}


@_tool(
    "create_task",
    "Create a structured task; Nayantra chooses the robot (capability, distance, battery, workload, traffic), plans "
    "the route and coordinates traffic. Places may be waypoint names or areas like 'receiving'/'storage'.",
    CreateTaskParams,
    risk="act",
    endpoint="POST /tasks",
)
async def _create_task(ctx: ToolContext, p: CreateTaskParams) -> Any:
    body = {
        "type": p.task_type,
        "params": _task_params(p),
        "priority": p.priority,
        "requirements": {"fleet_id": p.fleet_id, "robot_id": p.robot},
        "created_by": "llm",
        "trace": {
            "source": "llm",
            "command": ctx.trace.command,
            "mission_id": ctx.trace.mission_id,
            "tool": "create_task",
        },
    }
    res = await ctx.core.post("/tasks", body, params={"count": p.count}, trace=ctx.trace)
    return _confirmation_or(
        res, lambda r: {"ok": True, "tasks": [_task_brief(t) for t in r["tasks"]]}
    )


class TaskIdParams(Params):
    task_id: str = Field(..., description="Task id, e.g. 'T-0007'")


@_tool(
    "get_task_status",
    "Status of a task with the reasons: why it is waiting, why this robot was selected, steps, recent trace.",
    TaskIdParams,
    endpoint="GET /tasks/{id}/trace",
)
async def _get_task_status(ctx: ToolContext, p: TaskIdParams) -> Any:
    tr = await ctx.core.get(f"/tasks/{p.task_id}/trace", trace=ctx.trace)
    brief = _task_brief(tr["task"])
    brief["origin"] = tr["origin"]
    brief["recent_events"] = [e["message"] for e in tr["events"][-8:]]
    return brief


class ListTasksParams(Params):
    status: (
        Literal[
            "active",
            "queued",
            "assigned",
            "executing",
            "waiting_for_traffic",
            "waiting_for_robot",
            "paused",
            "completed",
            "failed",
            "cancelled",
        ]
        | None
    ) = Field("active", description="'active' = every open task")
    robot: str | None = None
    limit: int = Field(20, ge=1, le=100)


@_tool(
    "list_tasks",
    "List tasks (open ones by default) with status and reasons.",
    ListTasksParams,
    endpoint="GET /tasks",
)
async def _list_tasks(ctx: ToolContext, p: ListTasksParams) -> Any:
    tasks = await ctx.core.get(
        "/tasks",
        params={"status": p.status, "robot_id": p.robot, "limit": p.limit},
        trace=ctx.trace,
    )
    return [_task_brief(t) for t in tasks]


@_tool(
    "pause_task",
    "Pause a task (the robot stops where it is; resume later).",
    TaskIdParams,
    risk="act",
    endpoint="POST /tasks/{id}/pause",
)
async def _pause_task(ctx: ToolContext, p: TaskIdParams) -> Any:
    return _task_brief(await ctx.core.post(f"/tasks/{p.task_id}/pause", trace=ctx.trace))


@_tool(
    "resume_task",
    "Resume a paused task.",
    TaskIdParams,
    risk="act",
    endpoint="POST /tasks/{id}/resume",
)
async def _resume_task(ctx: ToolContext, p: TaskIdParams) -> Any:
    return _task_brief(await ctx.core.post(f"/tasks/{p.task_id}/resume", trace=ctx.trace))


@_tool(
    "cancel_task", "Cancel a task.", TaskIdParams, risk="act", endpoint="POST /tasks/{id}/cancel"
)
async def _cancel_task(ctx: ToolContext, p: TaskIdParams) -> Any:
    return _task_brief(await ctx.core.post(f"/tasks/{p.task_id}/cancel", trace=ctx.trace))


# ===========================================================================
# Robot commands (the operator named the robot)
# ===========================================================================


class NavigateParams(Params):
    robot: str = Field(..., description="Robot id or name the operator named")
    destination: str = Field(..., description="Waypoint or area name")
    priority: int = Field(5, ge=0, le=9)


@_tool(
    "navigate_robot",
    "Send a specific, operator-named robot to a place. Routing and traffic are handled by Nayantra. "
    "If the operator did not name a robot, use create_task instead.",
    NavigateParams,
    risk="act",
    endpoint="POST /robots/{id}/navigate",
)
async def _navigate_robot(ctx: ToolContext, p: NavigateParams) -> Any:
    res = await ctx.core.post(
        f"/robots/{p.robot}/navigate",
        {"waypoint": p.destination, "priority": p.priority},
        trace=ctx.trace,
    )
    return _confirmation_or(res, lambda t: {"ok": True, "task": _task_brief(t)})


@_tool(
    "stop_robot",
    "Stop one robot now and cancel its current task (queued work is re-allocated).",
    RobotParams,
    risk="act",
    endpoint="POST /robots/{id}/stop",
)
async def _stop_robot(ctx: ToolContext, p: RobotParams) -> Any:
    r = await ctx.core.post(
        f"/robots/{p.robot}/stop",
        {"reason": "Stopped by the agent on operator request"},
        trace=ctx.trace,
    )
    return {"ok": True, "robot": r["name"], "status": (r.get("state") or {}).get("mode")}


@_tool(
    "emergency_stop_robot",
    "Emergency-stop one robot immediately (safety). Only an operator can release it.",
    RobotParams,
    risk="act",
    endpoint="POST /robots/{id}/estop",
)
async def _estop_robot(ctx: ToolContext, p: RobotParams) -> Any:
    r = await ctx.core.post(
        f"/robots/{p.robot}/estop",
        {"reason": "Emergency stop requested via the agent"},
        trace=ctx.trace,
    )
    return {"ok": True, "robot": r["name"], "e_stop": (r.get("state") or {}).get("e_stop", True)}


class ScopeParams(Params):
    fleet_id: str | None = Field(None, description="Only this fleet; omit for every robot")


@_tool(
    "stop_all_robots",
    "Stop every robot (or one fleet). Requires operator confirmation in the dashboard.",
    ScopeParams,
    risk="dangerous",
    endpoint="POST /robots/stop-all",
)
async def _stop_all(ctx: ToolContext, p: ScopeParams) -> Any:
    path = f"/fleets/{p.fleet_id}/stop" if p.fleet_id else "/robots/stop-all"
    return _confirmation_or(
        await ctx.core.post(path, {"reason": "requested via the agent"}, trace=ctx.trace),
        lambda r: r,
    )


@_tool(
    "emergency_stop_all",
    "Emergency-stop every robot (or one fleet). Requires operator confirmation in the dashboard.",
    ScopeParams,
    risk="dangerous",
    endpoint="POST /robots/estop-all",
)
async def _estop_all(ctx: ToolContext, p: ScopeParams) -> Any:
    path = f"/fleets/{p.fleet_id}/estop" if p.fleet_id else "/robots/estop-all"
    return _confirmation_or(
        await ctx.core.post(path, {"reason": "requested via the agent"}, trace=ctx.trace),
        lambda r: r,
    )


@_tool(
    "send_to_charge",
    "Send a robot to the nearest free charger and charge it.",
    RobotParams,
    risk="act",
    endpoint="POST /robots/{id}/charge",
)
async def _charge(ctx: ToolContext, p: RobotParams) -> Any:
    return {
        "ok": True,
        "task": _task_brief(await ctx.core.post(f"/robots/{p.robot}/charge", trace=ctx.trace)),
    }


# ===========================================================================
# Traffic, events
# ===========================================================================


@_tool(
    "get_traffic_state",
    "Traffic coordination: who is waiting for whom and why, reservations per robot, recent conflicts and how they were resolved.",
    NoParams,
    endpoint="GET /traffic",
)
async def _traffic(ctx: ToolContext, p: NoParams) -> Any:
    t = await ctx.core.get("/traffic", trace=ctx.trace)
    held: dict[str, int] = {}
    for r in t["reservations"]:
        held[r["robot_id"]] = held.get(r["robot_id"], 0) + 1
    return {
        "waiting": [
            {"robot": w["robot_id"], "why": w["reason"], "for": w["holder"]} for w in t["waits"]
        ],
        "reservations_held": held,
        "planned_routes": [
            {"robot": i["robot_id"], "hops": len(i["route"]) - 1, "eta_s": i["eta"]}
            for i in t["itineraries"]
        ],
        "recent_conflicts": [
            {
                "kind": c["kind"],
                "where": c["location_name"],
                "robots": c["robots"],
                "resolution": (c.get("resolution") or {}).get("message"),
            }
            for c in t["conflicts"][-8:]
        ],
    }


@_tool(
    "get_active_conflicts",
    "Predicted/resolved traffic conflicts (head-on, intersection, bottleneck …) with resolutions.",
    NoParams,
    endpoint="GET /traffic/conflicts",
)
async def _conflicts(ctx: ToolContext, p: NoParams) -> Any:
    cs = await ctx.core.get("/traffic/conflicts", trace=ctx.trace)
    return [
        {
            "kind": c["kind"],
            "where": c["location_name"],
            "robots": c["robots"],
            "message": c["message"],
            "resolution": (c.get("resolution") or {}).get("message"),
        }
        for c in cs
    ]


class EventsParams(Params):
    robot: str | None = None
    task_id: str | None = None
    limit: int = Field(20, ge=1, le=100)


@_tool(
    "list_events",
    "Recent events (optionally for one robot or task) — the observability trail.",
    EventsParams,
    endpoint="GET /events",
)
async def _events(ctx: ToolContext, p: EventsParams) -> Any:
    evs = await ctx.core.get(
        "/events",
        params={"robot_id": p.robot, "task_id": p.task_id, "limit": p.limit},
        trace=ctx.trace,
    )
    return [{"t": e["ts"], "type": e["type"], "message": e["message"]} for e in evs]


@_tool(
    "list_alerts",
    "Active alerts (battery low, offline, e-stop, restricted zone, task failures).",
    NoParams,
    endpoint="GET /alerts",
)
async def _alerts(ctx: ToolContext, p: NoParams) -> Any:
    return [
        {"severity": a["severity"], "title": a["title"], "message": a["message"]}
        for a in await ctx.core.get("/alerts", trace=ctx.trace)
    ]


# ===========================================================================
# Registration
# ===========================================================================


class RegisterRobotParams(Params):
    name: str = Field(..., description="Display name, e.g. 'UGV-05'")
    fleet_id: str = Field(..., description="Existing fleet id")
    protocol: Literal["simulation", "ros2", "isaac_demo"] = Field(
        "simulation", description="How Nayantra talks to it"
    )
    spawn_waypoint: str | None = Field(None, description="simulation: waypoint id to spawn at")
    namespace: str | None = Field(None, description="ros2: namespace such as /ugv_05")
    endpoint: str | None = Field(None, description="isaac_demo: control API URL")
    manufacturer: str | None = None
    model: str | None = None


@_tool(
    "register_robot",
    "Register a robot into an existing fleet and run its connection test. (The dashboard wizard offers the full form.)",
    RegisterRobotParams,
    risk="act",
    endpoint="POST /robots",
)
async def _register_robot(ctx: ToolContext, p: RegisterRobotParams) -> Any:
    body = {
        "name": p.name,
        "fleet_id": p.fleet_id,
        "manufacturer": p.manufacturer or "",
        "model": p.model or "",
        "communication": {
            "protocol": p.protocol,
            "endpoint": p.endpoint or "",
            "namespace": p.namespace or "",
        },
        "navigation": {"namespace": p.namespace or ""},
        "spawn": {"waypoint_id": p.spawn_waypoint},
    }
    r = await ctx.core.post("/robots", body, trace=ctx.trace)
    return {
        "ok": True,
        "robot": r["id"],
        "checks": [f"{'✓' if c['ok'] else '✗'} {c['name']}: {c['detail']}" for c in r["checks"]],
    }


class CreateFleetParams(Params):
    name: str
    robot_type: Literal["ugv", "uav", "quadruped", "humanoid", "other"]
    capabilities: list[str] = Field(default_factory=lambda: ["navigate"])
    map_id: str | None = None
    max_speed: float | None = Field(None, gt=0, le=6)


@_tool(
    "create_fleet",
    "Create a fleet (robot type, capabilities, map, speed limit).",
    CreateFleetParams,
    risk="act",
    endpoint="POST /fleets",
)
async def _create_fleet(ctx: ToolContext, p: CreateFleetParams) -> Any:
    body: dict[str, Any] = {
        "name": p.name,
        "robot_type": p.robot_type,
        "capabilities": p.capabilities,
        "map_ids": [p.map_id] if p.map_id else [],
        "layer": "air" if p.robot_type == "uav" else "ground",
    }
    if p.max_speed:
        body["limits"] = {"max_speed": p.max_speed}
    f = await ctx.core.post("/fleets", body, trace=ctx.trace)
    return {"ok": True, "fleet": f["id"]}


@_tool(
    "remove_robot",
    "Remove a robot from its fleet. Requires operator confirmation in the dashboard.",
    RobotParams,
    risk="dangerous",
    endpoint="DELETE /robots/{id}",
)
async def _remove_robot(ctx: ToolContext, p: RobotParams) -> Any:
    return _confirmation_or(
        await ctx.core.delete(f"/robots/{p.robot}", trace=ctx.trace), lambda r: r
    )


# ===========================================================================
# Legacy names (v1 / Open-RMF-shaped) — executable, hidden from the LLM list
# ===========================================================================


class LegacyMoveParams(Params):
    model_config = ConfigDict(extra="ignore")
    waypoint: str
    robot_name: str | None = None
    fleet_name: str | None = None


@_tool(
    "move_robot",
    "Deprecated: use navigate_robot / create_task.",
    LegacyMoveParams,
    risk="act",
    legacy=True,
)
async def _legacy_move(ctx: ToolContext, p: LegacyMoveParams) -> Any:
    if p.robot_name:
        return await _navigate_robot(
            ctx, NavigateParams(robot=p.robot_name, destination=p.waypoint)
        )
    return await _create_task(
        ctx, CreateTaskParams(task_type="navigate", destination=p.waypoint, fleet_id=p.fleet_name)
    )


class LegacyDispatchParams(Params):
    model_config = ConfigDict(extra="ignore")
    category: str
    description: dict[str, Any] = Field(default_factory=dict)
    fleet_name: str | None = None
    robot_name: str | None = None
    priority: int = Field(5, ge=0, le=9)


@_tool(
    "dispatch_task", "Deprecated: use create_task.", LegacyDispatchParams, risk="act", legacy=True
)
async def _legacy_dispatch(ctx: ToolContext, p: LegacyDispatchParams) -> Any:
    d = p.description

    def place(v: Any) -> Any:
        return v.get("place") or v.get("waypoint") if isinstance(v, dict) else v

    if p.category in ("navigate_to_waypoint", "navigate", "go_to_place"):
        req = CreateTaskParams(
            task_type="navigate",
            destination=place(d.get("waypoint") or d.get("place") or d.get("destination")),
        )
    elif p.category == "delivery":
        req = CreateTaskParams(
            task_type="delivery",
            pickup=place(d.get("pickup")),
            dropoff=place(d.get("dropoff")),
            payload=d.get("payload"),
        )
    elif p.category in ("patrol", "loop"):
        req = CreateTaskParams(
            task_type="patrol",
            waypoints=[place(x) for x in d.get("places", d.get("waypoints", []))],
            rounds=int(d.get("rounds", 1)),
        )
    else:
        return {"ok": False, "error": f"Unsupported category {p.category!r}"}
    req.priority, req.fleet_id, req.robot = p.priority, p.fleet_name, p.robot_name
    return await _create_task(ctx, req)


@_tool("get_task_state", "Deprecated: use get_task_status.", TaskIdParams, legacy=True)
async def _legacy_task_state(ctx: ToolContext, p: TaskIdParams) -> Any:
    return await _get_task_status(ctx, p)


class LegacyRobotParams(Params):
    model_config = ConfigDict(extra="ignore")
    robot_name: str
    fleet_name: str | None = None


@_tool("get_robot_status", "Deprecated: use get_robot_state.", LegacyRobotParams, legacy=True)
async def _legacy_robot_status(ctx: ToolContext, p: LegacyRobotParams) -> Any:
    return await _get_robot_state(ctx, RobotParams(robot=p.robot_name))


@_tool("get_building_map", "Deprecated: use get_map.", NoParams, legacy=True)
async def _legacy_map(ctx: ToolContext, p: NoParams) -> Any:
    return await _get_map(ctx, MapParams())


@_tool(
    "decommission_robot",
    "Deprecated: take a robot out of service.",
    LegacyRobotParams,
    risk="act",
    legacy=True,
)
async def _legacy_decommission(ctx: ToolContext, p: LegacyRobotParams) -> Any:
    r = await ctx.core.patch(f"/robots/{p.robot_name}", {"enabled": False}, trace=ctx.trace)
    return {"ok": True, "robot": r["id"], "enabled": r["enabled"]}


@_tool(
    "recommission_robot",
    "Deprecated: return a robot to service.",
    LegacyRobotParams,
    risk="act",
    legacy=True,
)
async def _legacy_recommission(ctx: ToolContext, p: LegacyRobotParams) -> Any:
    r = await ctx.core.patch(f"/robots/{p.robot_name}", {"enabled": True}, trace=ctx.trace)
    return {"ok": True, "robot": r["id"], "enabled": r["enabled"]}


# ===========================================================================
# Open-RMF infrastructure (doors, lifts, dispensers, fire alarm) — only for
# sites with a real rmf-web api-server (OPENRMF_INFRA_TOOLS=true).
# ===========================================================================


class DoorParams(Params):
    door_name: str


class DoorControlParams(DoorParams):
    mode: Literal[0, 2] = Field(..., description="0 = closed, 2 = open")


class LiftParams(Params):
    lift_name: str


class LiftRequestParams(LiftParams):
    destination_floor: str
    door_state: Literal[0, 2] = 2


def _need_rmf(ctx: ToolContext) -> OpenRMFClient:
    if ctx.rmf is None:
        raise CoreError(
            503,
            "Open-RMF infrastructure tools need OPENRMF_API_URL to point at an rmf-web api-server",
        )
    return ctx.rmf


def register_infra_tools() -> None:
    @_tool("list_doors", "Open-RMF: list doors.", NoParams, endpoint="GET /doors")
    async def _doors(ctx: ToolContext, p: NoParams) -> Any:
        return await _need_rmf(ctx).get_doors()

    @_tool(
        "get_door_state", "Open-RMF: door state.", DoorParams, endpoint="GET /doors/{name}/state"
    )
    async def _door_state(ctx: ToolContext, p: DoorParams) -> Any:
        return await _need_rmf(ctx).get_door_state(p.door_name)

    @_tool(
        "control_door",
        "Open-RMF: open (2) or close (0) a door.",
        DoorControlParams,
        risk="act",
        endpoint="POST /doors/{name}/request",
    )
    async def _door_ctl(ctx: ToolContext, p: DoorControlParams) -> Any:
        return await _need_rmf(ctx).post_door_request(p.door_name, {"mode": p.mode})

    @_tool("list_lifts", "Open-RMF: list lifts.", NoParams, endpoint="GET /lifts")
    async def _lifts(ctx: ToolContext, p: NoParams) -> Any:
        return await _need_rmf(ctx).get_lifts()

    @_tool(
        "get_lift_state", "Open-RMF: lift state.", LiftParams, endpoint="GET /lifts/{name}/state"
    )
    async def _lift_state(ctx: ToolContext, p: LiftParams) -> Any:
        return await _need_rmf(ctx).get_lift_state(p.lift_name)

    @_tool(
        "request_lift",
        "Open-RMF: call a lift to a floor.",
        LiftRequestParams,
        risk="act",
        endpoint="POST /lifts/{name}/request",
    )
    async def _lift_req(ctx: ToolContext, p: LiftRequestParams) -> Any:
        return await _need_rmf(ctx).post_lift_request(
            p.lift_name, {"destination_floor": p.destination_floor, "door_state": p.door_state}
        )

    @_tool("list_dispensers", "Open-RMF: dispensers.", NoParams, endpoint="GET /dispensers")
    async def _disp(ctx: ToolContext, p: NoParams) -> Any:
        return await _need_rmf(ctx).get_dispensers()

    @_tool("list_ingestors", "Open-RMF: ingestors.", NoParams, endpoint="GET /ingestors")
    async def _ing(ctx: ToolContext, p: NoParams) -> Any:
        return await _need_rmf(ctx).get_ingestors()

    @_tool(
        "get_fire_alarm_state",
        "Open-RMF: fire alarm state.",
        NoParams,
        endpoint="GET /fire_alarm_trigger",
    )
    async def _fire(ctx: ToolContext, p: NoParams) -> Any:
        return await _need_rmf(ctx).get_previous_fire_alarm_trigger()


if settings.OPENRMF_INFRA_TOOLS:
    register_infra_tools()


# ===========================================================================
# Isaac demo (scripts/isaac_demo.py) — legacy direct path, opt-in via
# ISAAC_DEMO_URL. It bypasses traffic coordination; prefer registering the
# Isaac robot in the core with protocol "isaac_demo" so it is coordinated.
# ===========================================================================


async def _isaac_demo_request(method: str, path: str, **kw: Any) -> Any:
    """Short-lived httpx call to the isaac_demo control API."""
    if not settings.ISAAC_DEMO_URL:
        raise RuntimeError(
            "ISAAC_DEMO_URL is not configured. Set it in .env "
            "(e.g. http://172.25.60.165:8900) to enable the Isaac demo tools."
        )
    url = f"{settings.ISAAC_DEMO_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.request(method, url, **kw)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}


class IsaacWaypointParams(Params):
    waypoint: str


if settings.ISAAC_DEMO_URL:

    @_tool(
        "isaac_list_waypoints", "Isaac demo (uncoordinated): waypoints of isaac_demo.py.", NoParams
    )
    async def _isaac_list_waypoints(ctx: ToolContext, p: NoParams) -> Any:
        return await _isaac_demo_request("GET", "/waypoints")

    @_tool(
        "isaac_goto_waypoint",
        "Isaac demo (uncoordinated, single robot): glide Carter to a named waypoint. Use only when the operator asks for the Isaac demo.",
        IsaacWaypointParams,
        risk="act",
    )
    async def _isaac_goto_waypoint(ctx: ToolContext, p: IsaacWaypointParams) -> Any:
        return await _isaac_demo_request("POST", "/goto", params={"waypoint": p.waypoint})

    class IsaacXYParams(Params):
        x: float = Field(..., description="Target x in metres")
        y: float = Field(..., description="Target y in metres")

    @_tool(
        "isaac_goto_xy",
        "Isaac demo (uncoordinated, single robot): glide Carter to absolute (x, y). Only on explicit operator request.",
        IsaacXYParams,
        risk="act",
    )
    async def _isaac_goto_xy(ctx: ToolContext, p: IsaacXYParams) -> Any:
        return await _isaac_demo_request("POST", "/goto", params={"x": p.x, "y": p.y})

    @_tool("isaac_get_robot_state", "Isaac demo: Carter's state.", NoParams)
    async def _isaac_get_robot_state(ctx: ToolContext, p: NoParams) -> Any:
        return await _isaac_demo_request("GET", "/state")

    logger.info(f"Isaac Demo tools registered (target: {settings.ISAAC_DEMO_URL})")
