"""
nayantra/core/legacy_rmf.py

Backward-compatible routes with the rmf-web api-server *shape* that the old
nayantra.rmf_bridge served on :8000 — kept so existing MCP deployments,
nayantra.rmf_client and scripts keep working. They are backed by the Nayantra
Core, which is NOT Open-RMF; the shape is only for compatibility.

Doors, lifts, dispensers and the fire alarm do not exist in the core's maps and
answer empty, never fabricated.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from nayantra.core.errors import CoreError
from nayantra.core.models import TaskCreate, TaskRequirements, TaskTrace, TaskType

router = APIRouter(tags=["legacy rmf-web shape"])

_MODE_TO_STATUS = {
    "idle": "idle",
    "moving": "working",
    "acting": "working",
    "waiting": "waiting",
    "paused": "paused",
    "charging": "charging",
    "error": "error",
    "emergency_stop": "emergency",
    "offline": "offline",
}
_STATUS_MAP = {
    "queued": "queued",
    "assigned": "queued",
    "waiting_for_robot": "queued",
    "planning": "underway",
    "executing": "underway",
    "waiting_for_traffic": "underway",
    "paused": "standby",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "canceled",
}


def _ok(data: Any) -> JSONResponse:
    return JSONResponse({"status": "ok", "data": data, "timestamp": int(time.time())})


def _core(request: Request):
    return request.app.state.core


def _robot_payload(core, rid: str) -> dict[str, Any]:
    rt = core.fleet.runtimes.get(rid)
    st = rt.state if rt else None
    robot = core.registry.robots[rid]
    return {
        "name": rid,
        "display_name": robot.name,
        "status": "decommissioned"
        if not robot.enabled
        else _MODE_TO_STATUS.get(st.mode if st else "offline", "idle"),
        "task_id": (st.current_task_id if st else "") or "",
        "battery": round((st.battery_pct or 0) / 100.0, 2)
        if st and st.battery_pct is not None
        else None,
        "location": {
            "x": round(st.pose.x, 3) if st else 0.0,
            "y": round(st.pose.y, 3) if st else 0.0,
            "yaw": round(st.pose.yaw, 3) if st else 0.0,
            "level_name": "L1",
        },
    }


def _task_state(t) -> dict[str, Any]:
    return {
        "task_id": t.id,
        "category": t.type,
        "status": _STATUS_MAP.get(t.status, t.status),
        "robot_name": t.assigned_robot,
        "fleet_name": t.assigned_fleet,
        "unix_millis_start_time": int(t.created_at * 1000),
        "detail": t.status_reason,
    }


@router.get("/health")
async def health(request: Request):
    core = _core(request)
    return {
        "status": "ok",
        "service": "nayantra-core",
        "robots": list(core.registry.robots),
        "waypoints": sorted(w.id for w in core.registry.waypoints.values()),
    }


@router.get("/fleets")
async def fleets(request: Request):
    core = _core(request)
    out = []
    for f in core.registry.fleets.values():
        out.append(
            {
                "name": f.id,
                "robots": {
                    rid: _robot_payload(core, rid) for rid in core.registry.fleet_robot_ids(f.id)
                },
            }
        )
    return _ok(out)


@router.get("/fleets/{fleet_name}/robots/{robot_name}")
async def robot(request: Request, fleet_name: str, robot_name: str):
    core = _core(request)
    r = core.registry.robots.get(robot_name)
    if r is None or r.fleet_id != fleet_name:
        raise HTTPException(404, f"No robot {robot_name!r} in fleet {fleet_name!r}")
    return _ok({**_robot_payload(core, robot_name), "fleet": fleet_name})


@router.get("/fleets/{fleet_name}/log")
async def fleet_log(request: Request, fleet_name: str):
    core = _core(request)
    ids = set(core.registry.fleet_robot_ids(fleet_name))
    log = [
        {"t": e.ts, "text": e.message} for e in reversed(core.bus.recent(300)) if e.robot_id in ids
    ]
    return _ok({"fleet": fleet_name, "log": log[-100:]})


@router.post("/fleets/{fleet_name}/decommission")
async def decommission(request: Request, fleet_name: str, payload: dict):
    from nayantra.core.models import RobotPatch

    core = _core(request)
    rid = payload.get("robot_name", "")
    core.registry.update_robot(core.registry.find_robot(rid).id, RobotPatch(enabled=False))
    return _ok({"fleet": fleet_name, "robot": rid, "action": "decommissioned"})


@router.post("/fleets/{fleet_name}/recommission")
async def recommission(request: Request, fleet_name: str, payload: dict):
    from nayantra.core.models import RobotPatch

    core = _core(request)
    rid = payload.get("robot_name", "")
    core.registry.update_robot(core.registry.find_robot(rid).id, RobotPatch(enabled=True))
    return _ok({"fleet": fleet_name, "robot": rid, "action": "recommissioned"})


def _place(value: Any) -> Any:
    if isinstance(value, dict):
        return value.get("place") or value.get("waypoint") or value.get("name")
    return value


@router.post("/tasks/dispatch_task")
async def dispatch(request: Request, payload: dict):
    core = _core(request)
    req = payload.get("request") or payload
    category = req.get("category", "")
    desc = req.get("description") or {}
    if category in ("navigate_to_waypoint", "go_to_place", "navigate"):
        ttype, params = (
            TaskType.NAVIGATE,
            {
                "destination": _place(
                    desc.get("waypoint") or desc.get("place") or desc.get("destination")
                )
            },
        )
    elif category == "delivery":
        ttype, params = (
            TaskType.DELIVERY,
            {"pickup": _place(desc.get("pickup")), "dropoff": _place(desc.get("dropoff"))},
        )
    elif category in ("patrol", "loop"):
        places = desc.get("places") or desc.get("waypoints") or []
        ttype, params = (
            TaskType.PATROL,
            {"waypoints": [_place(p) for p in places], "rounds": int(desc.get("rounds", 1))},
        )
    else:
        raise HTTPException(
            400, f"Unsupported category {category!r} (navigate_to_waypoint, delivery, patrol, loop)"
        )
    prio = req.get("priority")
    priority = (
        prio.get("value", 5) if isinstance(prio, dict) else (prio if isinstance(prio, int) else 5)
    )
    robot_name = req.get("robot_name")
    try:
        task = core.tasks.create(
            TaskCreate(
                type=ttype,
                params=params,
                priority=max(0, min(9, int(priority or 5))),
                requirements=TaskRequirements(
                    robot_id=robot_name or None, fleet_id=req.get("fleet_name") or None
                ),
                created_by="legacy_rmf",
                trace=TaskTrace(source="legacy_rmf", tool="dispatch_task"),
            )
        )[0]
    except CoreError as exc:
        raise HTTPException(
            exc.status_code if exc.status_code >= 400 else 400, exc.message
        ) from exc
    return _ok(
        {
            "task_id": task.id,
            "state": _STATUS_MAP.get(task.status, task.status),
            "robot": task.assigned_robot,
        }
    )


@router.get("/tasks")
async def tasks(request: Request):
    return _ok([_task_state(t) for t in _core(request).tasks.list()])


@router.get("/tasks/{task_id}/state")
async def task_state(request: Request, task_id: str):
    t = _core(request).tasks.tasks.get(task_id)
    if t is None:
        raise HTTPException(404, f"Unknown task {task_id!r}")
    return _ok(_task_state(t))


@router.get("/tasks/{task_id}/log")
async def task_log(request: Request, task_id: str):
    t = _core(request).tasks.tasks.get(task_id)
    if t is None:
        raise HTTPException(404, f"Unknown task {task_id!r}")
    return _ok({"task_id": task_id, "log": [{"t": h.ts, "text": h.message} for h in t.history]})


@router.post("/tasks/cancel_task")
@router.post("/tasks/kill_task")
@router.post("/tasks/interrupt_task")
async def cancel(request: Request, payload: dict):
    core = _core(request)
    tid = payload.get("task_id", "")
    t = core.tasks.tasks.get(tid)
    if t is None:
        raise HTTPException(404, f"Unknown task {tid!r}")
    was_open = t.status not in ("completed", "failed", "cancelled")
    await core.tasks.cancel(tid, "Cancelled via legacy RMF API")
    return _ok({"task_id": tid, "action": "cancelled" if was_open else "already_finished"})


@router.post("/tasks/resume_task")
async def resume(request: Request, payload: dict):
    core = _core(request)
    tid = payload.get("task_id", "")
    t = core.tasks.tasks.get(tid)
    if t is None:
        raise HTTPException(404, f"Unknown task {tid!r}")
    if t.status == "paused":
        await core.tasks.resume(tid)
        return _ok({"task_id": tid, "action": "resumed"})
    return _ok({"task_id": tid, "action": "noop", "status": t.status})


@router.get("/building_map")
async def building_map(request: Request):
    core = _core(request)
    levels = []
    for m in core.registry.maps.values():
        wps = core.registry.list_waypoints(m.id)
        index = {w.id: i for i, w in enumerate(wps)}
        levels.append(
            {
                "name": m.id,
                "elevation": 0.0,
                "nav_graphs": [
                    {
                        "name": "0",
                        "vertices": [{"x": w.x, "y": w.y, "name": w.id} for w in wps],
                        "edges": [
                            {
                                "v1_idx": index[ln.from_id],
                                "v2_idx": index[ln.to_id],
                                "edge_type": 0 if ln.bidirectional else 1,
                            }
                            for ln in core.registry.list_lanes(m.id)
                        ],
                    }
                ],
            }
        )
    return _ok({"name": "Nayantra", "levels": levels})


@router.get("/alerts")
async def alerts(request: Request):
    return _ok(
        [
            {"id": a.id, "title": a.title, "message": a.message, "tier": a.severity}
            for a in _core(request).bus.alerts()
        ]
    )


@router.get("/doors")
@router.get("/lifts")
@router.get("/dispensers")
@router.get("/ingestors")
async def empty_infra():
    return _ok([])


@router.get("/doors/{name}/state")
@router.post("/doors/{name}/request")
async def no_door(name: str):
    raise HTTPException(404, f"No door {name!r}: Nayantra maps have no doors")


@router.get("/lifts/{name}/state")
@router.post("/lifts/{name}/request")
async def no_lift(name: str):
    raise HTTPException(404, f"No lift {name!r}: Nayantra maps have no lifts")


@router.get("/fire_alarm_trigger")
async def fire_alarm():
    return _ok({"triggered": False})
