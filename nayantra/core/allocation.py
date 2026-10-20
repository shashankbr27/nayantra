"""
nayantra/core/allocation.py

Capability- and cost-based task allocation (inspired by Open-RMF task
bidding). The LLM never picks the robot. It describes the task, and this
module decides, with an explanation the operator can read:

  Selected UGV-02: nearest capable robot (≈14 s to Receiving Bay 1, battery 86%, idle).
  Next best UGV-01 (≈31 s: busy ≈18 s). Not eligible: UAV-01 — lacks carry_payload …

Eligibility (hard): capability, robot type, fleet, map, layer, payload,
reachability, commissioned/online/not e-stopped, queue length, and battery
after the task above the fleet minimum.
Cost (seconds): time until free + travel to the start + a congestion penalty
from the traffic schedule + a small battery term.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from nayantra.core.models import Allocation, AllocationCandidate, Task, TaskType
from nayantra.core.routing import RouteProfile, node_res

if TYPE_CHECKING:
    from nayantra.core.fleet import FleetManager
    from nayantra.core.world import WorldRegistry

MAX_QUEUE = 3
BATTERY_WEIGHT_S = 0.15  # seconds of cost per % of battery missing


@dataclass
class _Eval:
    robot_id: str
    eligible: bool
    permanent: bool
    reasons: list[str]
    score: float | None = None
    estimates: dict | None = None


def task_distance_estimate(task: Task, graph, profile: RouteProfile) -> tuple[float, float]:
    """(metres, seconds) between consecutive fixed targets of the task (zones: first candidate)."""
    pts = []
    for step in task.steps:
        if step.kind == "go_to":
            target = step.target or (step.candidates[0] if step.candidates else None)
            if target and target in graph.nodes:
                pts.append(target)
    metres = seconds = 0.0
    for a, b in zip(pts, pts[1:], strict=False):
        r = graph.plan_static(a, b, profile)
        if r:
            metres += r.length
            seconds += r.time
    for step in task.steps:
        if step.kind == "action" and step.duration_s:
            seconds += step.duration_s
    return metres, seconds


def allocate(task: Task, registry: WorldRegistry, fleet: FleetManager) -> tuple[Allocation, bool]:
    """Returns (allocation, retry_later). allocation.selected is None if nobody fits."""
    req = task.requirements
    evals: list[_Eval] = []
    for robot in registry.robots.values():
        if req.robot_id and robot.id != req.robot_id:
            continue
        if req.fleet_id and robot.fleet_id != req.fleet_id:
            continue
        evals.append(_evaluate(task, robot.id, registry, fleet))

    candidates = [
        AllocationCandidate(
            robot_id=e.robot_id,
            eligible=e.eligible,
            score=round(e.score, 1) if e.score is not None else None,
            reasons=e.reasons,
            estimates=e.estimates or {},
        )
        for e in evals
    ]
    eligible = sorted((e for e in evals if e.eligible), key=lambda e: e.score)
    name = lambda rid: registry.robots[rid].name if rid in registry.robots else rid  # noqa: E731
    if eligible:
        best = eligible[0]
        est = best.estimates or {}
        why = [f"≈{est.get('to_start_s', 0):.0f} s to {est.get('start_name', 'start')}"]
        if est.get("battery_pct") is not None:
            why.append(f"battery {est['battery_pct']:.0f}%")
        why.append("idle" if est.get("busy_s", 0) < 0.5 else f"free in ≈{est['busy_s']:.0f} s")
        if est.get("congestion_s", 0) >= 1:
            why.append(f"traffic ≈{est['congestion_s']:.0f} s")
        text = f"Selected {name(best.robot_id)}: " + ", ".join(why) + "."
        if len(eligible) > 1:
            nb = eligible[1]
            text += (
                f" Next best {name(nb.robot_id)} (cost ≈{nb.score:.0f} s vs ≈{best.score:.0f} s)."
            )
        rejected = [e for e in evals if not e.eligible]
        if rejected:
            shown = "; ".join(f"{name(e.robot_id)} — {e.reasons[0]}" for e in rejected[:3])
            more = f" (+{len(rejected) - 3} more)" if len(rejected) > 3 else ""
            text += f" Not eligible: {shown}{more}."
        return Allocation(selected=best.robot_id, explanation=text, candidates=candidates), False

    if not evals:
        scope = (
            f"robot {req.robot_id}"
            if req.robot_id
            else (f"fleet {req.fleet_id}" if req.fleet_id else "any fleet")
        )
        return Allocation(explanation=f"No robots registered in {scope}.", candidates=[]), False
    retry = any(not e.permanent for e in evals)
    summary = "; ".join(f"{name(e.robot_id)} — {e.reasons[0]}" for e in evals[:4])
    more = f" (+{len(evals) - 4} more)" if len(evals) > 4 else ""
    prefix = "Waiting for a robot" if retry else "No robot can perform this task"
    return Allocation(explanation=f"{prefix}: {summary}{more}.", candidates=candidates), retry


def _evaluate(task: Task, robot_id: str, registry: WorldRegistry, fleet: FleetManager) -> _Eval:
    robot = registry.robots[robot_id]
    cfg = registry.effective_config(robot_id)
    req = task.requirements
    reasons: list[str] = []

    missing = [c for c in req.capabilities if c not in cfg.capabilities]
    if missing:
        reasons.append(f"lacks capability {', '.join(missing)}")
    if req.robot_type and req.robot_type != cfg.robot_type:
        reasons.append(f"is a {cfg.robot_type}, task needs a {req.robot_type}")
    if task.map_id and cfg.map_id != task.map_id:
        reasons.append(f"operates on map '{cfg.map_id}', task is on '{task.map_id}'")
    if req.payload_kg and (cfg.payload_kg or 0) < req.payload_kg:
        reasons.append(f"payload {cfg.payload_kg or 0:g} kg < {req.payload_kg:g} kg")

    graph = fleet.graphs.get(cfg.map_id) if cfg.map_id and cfg.map_id in registry.maps else None
    profile = fleet.routing_profile(robot_id)
    start_node = fleet.planning_origin(robot_id)
    first_targets = (
        _first_targets(task, graph, cfg.layer, fleet.taken_targets(robot_id)) if graph else []
    )
    if graph and not reasons:
        if task.steps and task.steps[0].kind == "go_to" and not first_targets:
            reasons.append(f"no destination on the {cfg.layer} layer")
    if reasons:
        return _Eval(robot_id, False, True, reasons)

    to_start_s, to_start_m, start_name = 0.0, 0.0, "its position"
    if graph and first_targets and start_node:
        best = None
        for t in first_targets:
            r = graph.plan_static(start_node, t, profile)
            if r and (best is None or r.time < best[0].time):
                best = (r, t)
        if best is None:
            reason = graph.explain_unreachable(start_node, first_targets[0], profile)
            return _Eval(robot_id, False, True, [reason])
        to_start_s, to_start_m = best[0].time, best[0].length
        start_name = graph.nodes[best[1]].name

    rt = fleet.runtimes.get(robot_id)
    state = rt.state if rt else None
    temp: list[str] = []
    if not robot.enabled:
        temp.append("is decommissioned")
    if rt is None or state is None or not state.online:
        detail = f" ({state.connection_detail})" if state and state.connection_detail else ""
        temp.append(f"is offline{detail}")
    elif state.e_stop:
        temp.append("has its emergency stop engaged")
    elif rt.held:
        temp.append("is held by the operator (paused)")
    if rt and len(rt.queue) >= MAX_QUEUE:
        temp.append(f"queue is full ({len(rt.queue)} tasks)")

    task_m, task_s = task_distance_estimate(task, graph, profile) if graph else (0.0, 0.0)
    battery = state.battery_pct if state else None
    if battery is not None and task.type not in (TaskType.CHARGE, TaskType.MAKE_WAY):
        need = (to_start_m + task_m) * cfg.battery.drain_pct_per_m
        need += (to_start_s + task_s) / 60.0 * cfg.battery.drain_pct_per_min_idle
        if battery - need < cfg.battery.min_pct:
            temp.append(
                f"battery {battery:.0f}% − ≈{need:.1f}% for this task is below the "
                f"{cfg.battery.min_pct:.0f}% minimum"
            )
    if temp:
        return _Eval(robot_id, False, False, temp)

    busy_s = fleet.busy_estimate(robot_id)
    congestion_s = fleet.congestion_estimate(robot_id, first_targets[0] if first_targets else None)
    battery_s = (100.0 - (battery if battery is not None else 100.0)) * BATTERY_WEIGHT_S
    score = busy_s + to_start_s + congestion_s + battery_s
    return _Eval(
        robot_id,
        True,
        False,
        [],
        score=score,
        estimates={
            "to_start_s": round(to_start_s, 1),
            "to_start_m": round(to_start_m, 1),
            "task_s": round(task_s, 1),
            "busy_s": round(busy_s, 1),
            "congestion_s": round(congestion_s, 1),
            "battery_pct": battery,
            "start_name": start_name,
            "total_s": round(busy_s + to_start_s + task_s + congestion_s, 1),
        },
    )


def _first_targets(task: Task, graph, layer: str, taken: set[str]) -> list[str]:
    for step in task.steps:
        if step.kind != "go_to":
            continue
        pool = [step.target] if step.target else list(step.candidates)
        pool = [t for t in pool if t in graph.nodes and graph.nodes[t].layer == layer]
        if step.target:
            return pool
        # A zone: estimate to a free spot, as the executor will pick one (all taken → it waits)
        return [t for t in pool if t not in taken] or pool
    return []


def congestion_penalty(traffic, robot_id: str, map_id: str, nodes: list[str]) -> float:
    """≈ seconds of expected waiting: other robots' planned use of these nodes."""
    wanted = {node_res(n) for n in nodes}
    penalty = 0.0
    for rid, plan in traffic.plans.items():
        if rid == robot_id or plan.map_id != map_id:
            continue
        hits = sum(1 for o in plan.occs if o.resource in wanted and o.t1 != math.inf)
        penalty += 2.0 * hits
    return penalty
