"""
nayantra/core/traffic.py

Nayantra-native multi-robot traffic coordination (inspired by Open-RMF's
traffic schedule and negotiation; not linked to rmf_traffic).

Two layers, deliberately separate:

1. **Predictive planning (space–time).** Every route is planned with Safe
   Interval Path Planning (SIPP) against a reservation table holding every
   other robot's timed occupancy of nodes and lanes, including parked robots
   (which occupy their node indefinitely). The plan therefore contains *where*
   and *for how long* to wait, so conflicts are prevented, not reacted to. The
   same route planned without other robots is used to *detect* and classify
   the conflict (head-on, intersection …), which gives the explanation
   "UGV-03 delayed ≈4.2 s at Intersection 2 for UGV-01".

   Negotiation:
     * FCFS by default: a new route yields to existing plans.
     * Priority: a higher-priority robot may displace the *uncommitted* future
       of lower-priority plans; those robots are re-planned around it
       transactionally (all succeed or nothing changes).
     * Make-way: an *idle* robot parked on the only path or on the destination
       is asked to move to a free parking node.

2. **Execution (action dependency graph).** A robot may start a step (enter
   lane → next node) only when every robot planned to use any of those
   resources (or anything physically conflicting with them) *earlier* has
   released them, and nothing is physically held. Because ordering follows a
   collision-free timed plan, execution is collision- and deadlock-free no
   matter how late robots run (Hönig et al., "Persistent and robust execution
   of MAPF schedules", 2019). Timing only affects efficiency.

Robots never talk to each other. Everything goes through this coordinator,
which is the shared world state for motion.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import math
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from nayantra.core.events import EventBus
from nayantra.core.models import (
    Conflict,
    ConflictKind,
    ConflictResolution,
    EventType,
    Itinerary,
    ItineraryEntry,
    Reservation,
    Severity,
    TrafficState,
    WaitInfo,
    new_id,
)
from nayantra.core.routing import GraphCache, MapGraph, RouteProfile, lane_res, node_res

logger = logging.getLogger("nayantra.core.traffic")

INF = math.inf
PLAN_HORIZON_S = 900.0
PRIORITY_GAIN_S = 0.5  # a priority plan must beat the FCFS plan by this much (s)
STALE_WAIT_S = 15.0
WAIT_EVENT_AFTER_S = 2.0  # waits shorter than this are normal following, not news


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Occ:
    """A robot's (planned or current) occupancy of one resource."""

    robot_id: str
    resource: str
    t0: float
    t1: float
    acquire_step: int  # step at whose start the resource is taken (0 = already held)
    release_step: float  # step at whose completion it is freed (INF = never)
    direction: tuple[str, str] | None = None  # lanes: (from, to)


@dataclass
class PlanStep:
    index: int  # 1-based
    lane_id: str
    from_node: str
    to_node: str
    t_depart: float
    t_arrive: float
    wait_s: float = 0.0


@dataclass
class RobotPlan:
    robot_id: str
    map_id: str
    nodes: list[str]
    steps: list[PlanStep]
    occs: list[Occ]
    goal: str
    label: str = ""
    progress: int = 0  # completed steps (robot is at nodes[progress] when started == progress)
    started: int = 0  # started steps
    version: int = 0
    created_at: float = 0.0
    static_time: float = 0.0
    eta: float = 0.0

    @property
    def moving(self) -> bool:
        return self.started > self.progress

    @property
    def finished(self) -> bool:
        return self.progress >= len(self.steps)

    def remaining_nodes(self) -> list[str]:
        return self.nodes[self.progress :]

    def step(self, k: int) -> PlanStep:
        return self.steps[k - 1]


@dataclass
class Parked:
    map_id: str
    node_id: str | None  # None when frozen mid-lane
    lane_id: str | None = None
    lane_ends: tuple[str, str] | None = None
    busy: bool = False  # True while the robot is mid-task (don't ask it to make way)
    since: float = 0.0


@dataclass
class StepCheck:
    allowed: bool
    reason: str = ""
    blocker: str | None = None
    resource: str | None = None
    replan: bool = False


@dataclass
class RouteResult:
    status: Literal["planned", "blocked", "unreachable"]
    plan: RobotPlan | None = None
    reason: str = ""
    retry: bool = False
    conflict: Conflict | None = None


@dataclass
class _Table:
    """Reservation table snapshot: resource -> sorted busy intervals (other robots)."""

    busy: dict[str, list[tuple[float, float, str]]] = field(default_factory=dict)

    def add(self, occ: Occ) -> None:
        self.busy.setdefault(occ.resource, []).append((occ.t0, occ.t1, occ.robot_id))

    def finalize(self) -> None:
        for lst in self.busy.values():
            lst.sort()

    def intervals(self, resources: set[str], t_from: float) -> list[tuple[float, float]]:
        spans = []
        for r in resources:
            for t0, t1, _ in self.busy.get(r, ()):
                if t1 > t_from:
                    spans.append((max(t0, t_from), t1))
        spans.sort()
        merged: list[list[float]] = []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        return [(a, b) for a, b in merged]

    def first_overlap(
        self, resources: set[str], t0: float, t1: float
    ) -> tuple[str, float, float, str] | None:
        best = None
        for r in resources:
            for b0, b1, rid in self.busy.get(r, ()):
                if b0 < t1 and t0 < b1 and (best is None or b0 < best[1]):
                    best = (r, b0, b1, rid)
        return best


def _safe_intervals(busy: list[tuple[float, float]], t_from: float) -> list[tuple[float, float]]:
    out = []
    cur = t_from
    for a, b in busy:
        if b <= cur:
            continue
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
        if cur == INF:
            break
    if cur < INF:
        out.append((cur, INF))
    return out


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class TrafficCoordinator:
    def __init__(
        self,
        graphs: GraphCache,
        bus: EventBus,
        names: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.graphs = graphs
        self.bus = bus
        self.clock = clock
        self._names = names or (lambda rid: rid)
        self.plans: dict[str, RobotPlan] = {}
        self.parked: dict[str, Parked] = {}
        self.profiles: dict[str, RouteProfile] = {}
        self.priorities: dict[str, int] = {}
        self.conflicts: deque[Conflict] = deque(maxlen=100)
        self.waits: dict[str, WaitInfo] = {}
        self.needs_replan: set[str] = set()
        self.make_way_handler: Callable[[str, str, str], bool] | None = None
        self._version = itertools.count(1)
        self._make_way_pending: dict[str, float] = {}
        self._make_way_targets: dict[str, str] = {}  # robot asked to move -> target node
        # Blocked route requests: robot -> (map, start, goal, label). Used to
        # negotiate robots that block each other (e.g. swapping places).
        self.pending: dict[str, tuple[str, str, str, str]] = {}
        self.needs_replan_snapshot: set[str] = set()
        self._announced: set[str] = set()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def name(self, robot_id: str) -> str:
        return self._names(robot_id)

    def _element_name(self, graph: MapGraph, resource: str) -> str:
        kind, eid = resource.split(":", 1)
        if kind == "node" and eid in graph.nodes:
            return graph.nodes[eid].name
        if kind == "lane" and eid in graph.lanes:
            ln = graph.lanes[eid]
            a = graph.nodes[ln.from_id].name if ln.from_id in graph.nodes else ln.from_id
            b = graph.nodes[ln.to_id].name if ln.to_id in graph.nodes else ln.to_id
            return ln.name or f"lane {a} ↔ {b}"
        return eid

    def _point(self, graph: MapGraph, resource: str) -> tuple[float, float]:
        kind, eid = resource.split(":", 1)
        if kind == "node" and eid in graph.nodes:
            return graph.xy(eid)
        if kind == "lane" and eid in graph.lanes:
            ln = graph.lanes[eid]
            (ax, ay), (bx, by) = graph.xy(ln.from_id), graph.xy(ln.to_id)
            return ((ax + bx) / 2, (ay + by) / 2)
        return (0.0, 0.0)

    def _clearance_s(self, profile: RouteProfile) -> float:
        return min(2.5, max(0.5, (profile.footprint_radius + 0.4) / max(profile.max_speed, 0.2)))

    def _all_occs(self, map_id: str, exclude: set[str] | None = None) -> list[Occ]:
        exclude = exclude or set()
        now = self.clock()
        out: list[Occ] = []
        for rid, plan in self.plans.items():
            if rid in exclude or plan.map_id != map_id:
                continue
            out.extend(o for o in plan.occs if plan.progress < o.release_step)
        for rid, p in self.parked.items():
            if rid in exclude or p.map_id != map_id:
                continue
            out.extend(self._parked_occs(rid, p, now))
        return out

    def _parked_occs(self, rid: str, p: Parked, now: float) -> list[Occ]:
        since = min(p.since, now)
        if p.node_id:
            return [Occ(rid, node_res(p.node_id), since, INF, 0, INF)]
        occs = []
        if p.lane_id:
            occs.append(Occ(rid, lane_res(p.lane_id), since, INF, 0, INF, p.lane_ends))
        for n in p.lane_ends or ():
            occs.append(Occ(rid, node_res(n), since, INF, 0, INF))
        return occs

    def _table(
        self, map_id: str, exclude: set[str], drop_future_of: set[str] | None = None
    ) -> _Table:
        table = _Table()
        drop_future_of = drop_future_of or set()
        for occ in self._all_occs(map_id, exclude):
            if occ.robot_id in drop_future_of:
                plan = self.plans.get(occ.robot_id)
                if plan and occ.acquire_step > plan.started:
                    continue  # uncommitted future — may be displaced
            table.add(occ)
        table.finalize()
        return table

    # ------------------------------------------------------------------
    # Robot registration / location
    # ------------------------------------------------------------------

    def set_profile(self, robot_id: str, profile: RouteProfile, priority: int = 5) -> None:
        self.profiles[robot_id] = profile
        self.priorities[robot_id] = priority

    def set_parked(self, robot_id: str, map_id: str, node_id: str, busy: bool = False) -> None:
        self.plans.pop(robot_id, None)
        self.parked[robot_id] = Parked(
            map_id=map_id, node_id=node_id, busy=busy, since=self.clock()
        )
        self._flag_dependents(robot_id)

    def set_busy(self, robot_id: str, busy: bool) -> None:
        if robot_id in self.parked:
            self.parked[robot_id].busy = busy

    def remove_robot(self, robot_id: str) -> None:
        self.plans.pop(robot_id, None)
        self.parked.pop(robot_id, None)
        self.waits.pop(robot_id, None)
        self.profiles.pop(robot_id, None)
        self.needs_replan.discard(robot_id)
        self.pending.pop(robot_id, None)

    def location(self, robot_id: str) -> tuple[str | None, str | None]:
        """(node the robot is at, lane it is on) as far as traffic knows."""
        plan = self.plans.get(robot_id)
        if plan:
            if plan.moving:
                return None, plan.step(plan.started).lane_id
            return plan.nodes[plan.progress], None
        p = self.parked.get(robot_id)
        if p:
            return p.node_id, p.lane_id
        return None, None

    def _flag_dependents(self, robot_id: str) -> None:
        """A robot now sits somewhere indefinitely: plans that pass there must replan."""
        mine = {o.resource for o in self._all_occs_of(robot_id) if o.t1 == INF}
        if not mine:
            return
        map_id = self._map_of(robot_id)
        graph = self.graphs.get(map_id) if map_id else None
        expanded = set()
        for r in mine:
            expanded |= graph.expand(r) if graph else {r}
        for rid, plan in self.plans.items():
            if rid == robot_id or plan.map_id != map_id:
                continue
            if any(o.resource in expanded and o.acquire_step > plan.started for o in plan.occs):
                self.needs_replan.add(rid)

    def _all_occs_of(self, robot_id: str) -> list[Occ]:
        plan = self.plans.get(robot_id)
        if plan:
            return [o for o in plan.occs if plan.progress < o.release_step]
        p = self.parked.get(robot_id)
        return self._parked_occs(robot_id, p, self.clock()) if p else []

    def _map_of(self, robot_id: str) -> str | None:
        if robot_id in self.plans:
            return self.plans[robot_id].map_id
        if robot_id in self.parked:
            return self.parked[robot_id].map_id
        return None

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    def _sipp(
        self,
        graph: MapGraph,
        profile: RouteProfile,
        start: str,
        t_start: float,
        goal: str,
        table: _Table,
        park: bool = True,
        dwell_s: float = 0.0,
    ) -> list[PlanStep] | None:
        """Earliest-arrival space-time path (Safe Interval Path Planning)."""
        c = self._clearance_s(profile)
        gx, gy = graph.xy(goal)
        vmax = max(profile.max_speed, 0.05)
        node_si: dict[str, list[tuple[float, float]]] = {}
        lane_busy: dict[str, list[tuple[float, float]]] = {}

        def si(node: str) -> list[tuple[float, float]]:
            if node not in node_si:
                node_si[node] = _safe_intervals(
                    table.intervals(graph.expand(node_res(node)), t_start), t_start
                )
            return node_si[node]

        def lb(lane_id: str) -> list[tuple[float, float]]:
            if lane_id not in lane_busy:
                lane_busy[lane_id] = table.intervals(graph.expand(lane_res(lane_id)), t_start)
            return lane_busy[lane_id]

        def h(n: str) -> float:
            x, y = graph.xy(n)
            return math.hypot(gx - x, gy - y) / vmax

        start_si = si(start)
        idx0 = next((i for i, (a, b) in enumerate(start_si) if a <= t_start < b), None)
        if idx0 is None:
            # Someone else is booked on our node right now (shouldn't happen for a
            # robot physically there) — treat "now until next booking" as ours.
            nxt = next((a for a, _ in start_si if a > t_start), INF)
            start_si.insert(0, (t_start, nxt))
            idx0 = 0

        def goal_ok(interval: tuple[float, float], t_arr: float) -> bool:
            if park:
                return interval[1] == INF
            return interval[1] >= t_arr + dwell_s + c

        counter = itertools.count()
        best: dict[tuple[str, int], float] = {(start, idx0): t_start}
        parent: dict[tuple[str, int], tuple[tuple[str, int], str, float, float]] = {}
        heap = [(t_start + h(start), next(counter), start, idx0, t_start)]
        found: tuple[str, int] | None = None
        while heap:
            _, _, u, ui, g = heapq.heappop(heap)
            if g > best.get((u, ui), INF) + 1e-9:
                continue
            if u == goal and goal_ok(si(u)[ui], g):
                found = (u, ui)
                break
            if g - t_start > PLAN_HORIZON_S:
                continue
            iu = si(u)[ui]
            for e in graph.usable_edges(u, profile):
                tau = graph.traverse_time(e, profile)
                busy = lb(e.lane_id)
                for vj, (ja, jb) in enumerate(si(e.v)):
                    dep_min = max(g, ja + c - tau)
                    dep_max = min(iu[1] - c, jb - c - tau)
                    if dep_min > dep_max:
                        if ja + c - tau > iu[1] - c:
                            break  # later intervals of v are even further out
                        continue
                    t_dep = dep_min
                    for b0, b1 in busy:
                        if b1 <= t_dep:
                            continue
                        if b0 >= t_dep + tau:
                            break
                        t_dep = b1
                    if t_dep > dep_max:
                        continue
                    t_arr = t_dep + tau
                    key = (e.v, vj)
                    if t_arr < best.get(key, INF) - 1e-9:
                        best[key] = t_arr
                        parent[key] = ((u, ui), e.lane_id, t_dep, t_arr)
                        heapq.heappush(heap, (t_arr + h(e.v), next(counter), e.v, vj, t_arr))
        if found is None:
            return None
        chain = []
        cur = found
        while cur in parent:
            prev, lane_id, t_dep, t_arr = parent[cur]
            chain.append((prev[0], cur[0], lane_id, t_dep, t_arr))
            cur = prev
        chain.reverse()
        steps: list[PlanStep] = []
        t_prev_arrive = t_start
        for k, (u, v, lane_id, t_dep, t_arr) in enumerate(chain, 1):
            steps.append(
                PlanStep(
                    index=k,
                    lane_id=lane_id,
                    from_node=u,
                    to_node=v,
                    t_depart=t_dep,
                    t_arrive=t_arr,
                    wait_s=max(0.0, t_dep - t_prev_arrive),
                )
            )
            t_prev_arrive = t_arr
        return steps

    def _build_occs(
        self,
        robot_id: str,
        start: str,
        t_start: float,
        steps: list[PlanStep],
        profile: RouteProfile,
        park: bool = True,
        started_prefix: int = 0,
    ) -> list[Occ]:
        c = self._clearance_s(profile)
        occs: list[Occ] = []
        first_dep = steps[0].t_depart if steps else INF
        occs.append(
            Occ(
                robot_id,
                node_res(start),
                t_start - c,
                first_dep + c if steps else INF,
                0,
                1 if steps else INF,
            )
        )
        for i, s in enumerate(steps):
            k = s.index
            occs.append(
                Occ(
                    robot_id,
                    lane_res(s.lane_id),
                    s.t_depart,
                    s.t_arrive,
                    k,
                    k,
                    (s.from_node, s.to_node),
                )
            )
            last = i == len(steps) - 1
            t_leave = (
                INF
                if last and park
                else (steps[i + 1].t_depart + c if not last else s.t_arrive + c)
            )
            occs.append(
                Occ(
                    robot_id,
                    node_res(s.to_node),
                    s.t_arrive - c,
                    t_leave,
                    k,
                    INF if (last and park) else k + 1,
                )
            )
        return occs

    def _make_plan(
        self,
        robot_id: str,
        map_id: str,
        start: str,
        t_start: float,
        goal: str,
        steps: list[PlanStep],
        profile: RouteProfile,
        label: str,
        static_time: float,
    ) -> RobotPlan:
        nodes = [start] + [s.to_node for s in steps]
        return RobotPlan(
            robot_id=robot_id,
            map_id=map_id,
            nodes=nodes,
            steps=steps,
            occs=self._build_occs(robot_id, start, t_start, steps, profile),
            goal=goal,
            label=label,
            version=next(self._version),
            created_at=t_start,
            static_time=static_time,
            eta=steps[-1].t_arrive if steps else t_start,
        )

    def _classify(
        self,
        graph: MapGraph,
        my_resource: str,
        hit_resource: str,
        other: str,
        my_dir: tuple[str, str] | None,
        goal: str,
        t1_other: float,
    ) -> ConflictKind:
        kind, eid = my_resource.split(":", 1)
        other_occ_dir = None
        for o in self._all_occs_of(other):
            if o.resource == hit_resource:
                other_occ_dir = o.direction
                break
        if kind == "lane":
            if hit_resource != my_resource:
                return ConflictKind.CROSSING
            if other_occ_dir and my_dir and other_occ_dir == my_dir:
                return ConflictKind.SAME_LANE
            return ConflictKind.HEAD_ON
        if t1_other == INF:
            return ConflictKind.DESTINATION if eid == goal else ConflictKind.BOTTLENECK
        if hit_resource != my_resource:
            return ConflictKind.CLEARANCE
        # Through a node: compare where each robot comes from / goes to.
        other_prev = next(
            (
                o.direction[0]
                for o in self._all_occs_of(other)
                if o.direction and o.direction[1] == eid
            ),
            None,
        )
        my_prev, my_next = my_dir if my_dir else (None, None)
        if other_prev is not None and other_prev == my_next:
            return ConflictKind.HEAD_ON
        if graph.degree.get(eid, 0) >= 3:
            return ConflictKind.INTERSECTION
        if other_prev is not None and other_prev == my_prev:
            return ConflictKind.SAME_LANE
        return ConflictKind.BOTTLENECK

    def _naive_conflict(
        self,
        graph: MapGraph,
        robot_id: str,
        profile: RouteProfile,
        static_nodes: list[str],
        static_lanes: list[str],
        t_start: float,
        goal: str,
        table: _Table,
    ) -> dict | None:
        """First conflict the shortest route would hit if nobody coordinated."""
        c = self._clearance_s(profile)
        t = t_start
        checks: list[tuple[str, float, float, tuple[str, str] | None]] = []
        for i, (lane_id, u, v) in enumerate(
            zip(static_lanes, static_nodes, static_nodes[1:], strict=False)
        ):
            edge = next(e for e in graph.adj[u] if e.lane_id == lane_id and e.v == v)
            tau = graph.traverse_time(edge, profile)
            checks.append((lane_res(lane_id), t, t + tau, (u, v)))
            t_end = INF if v == goal else t + tau + c
            nxt = static_nodes[i + 2] if i + 2 < len(static_nodes) else None
            checks.append((node_res(v), t + tau - c, t_end, (u, nxt)))
            t += tau
        for res, a, b, direction in checks:
            hit = table.first_overlap(graph.expand(res), a, b)
            if hit:
                hit_res, b0, b1, other = hit
                return {
                    "resource": res,
                    "hit": hit_res,
                    "other": other,
                    "t": max(a, b0) - t_start,
                    "kind": self._classify(graph, res, hit_res, other, direction, goal, b1),
                }
        return None

    def request_route(
        self,
        robot_id: str,
        map_id: str,
        start: str,
        goal: str,
        label: str = "",
        allow_make_way: bool = True,
    ) -> RouteResult:
        """Plan (and reserve) a route from the robot's current node to `goal`."""
        profile = self.profiles.get(robot_id)
        if profile is None:
            return RouteResult("unreachable", reason=f"{robot_id} has no route profile")
        graph = self.graphs.get(map_id)
        if start not in graph.nodes or goal not in graph.nodes:
            return RouteResult("unreachable", reason="start or goal is not on this map's graph")
        now = self.clock()
        priority = self.priorities.get(robot_id, 5)
        self.needs_replan_snapshot = set(self.needs_replan)
        self.needs_replan.discard(robot_id)
        existing = self.plans.get(robot_id)
        if (
            existing is not None
            and existing.goal == goal
            and existing.nodes[existing.progress] == start
            and robot_id not in self.needs_replan_snapshot
        ):
            # Already planned for us (e.g. committed by a joint negotiation with
            # a partner): keep it — others' plans depend on it.
            self.pending.pop(robot_id, None)
            return RouteResult("planned", plan=existing)
        if existing is not None:
            if existing.moving:
                return RouteResult(
                    "blocked", reason="finishing the current lane before re-planning", retry=True
                )
            self.plans.pop(robot_id)
            self.parked[robot_id] = Parked(
                map_id, existing.nodes[existing.progress], busy=True, since=now
            )
        self.pending[robot_id] = (map_id, start, goal, label)

        if start == goal:
            self.pending.pop(robot_id, None)
            prev = self.parked.get(robot_id)
            self.set_parked(robot_id, map_id, goal, busy=prev.busy if prev else True)
            return RouteResult(
                "planned",
                plan=self._make_plan(robot_id, map_id, start, now, goal, [], profile, label, 0.0),
            )

        static = graph.plan_static(start, goal, profile)
        if static is None:
            return RouteResult(
                "unreachable", reason=graph.explain_unreachable(start, goal, profile)
            )

        table = self._table(map_id, exclude={robot_id})
        naive = self._naive_conflict(
            graph, robot_id, profile, static.nodes, static.lanes, now, goal, table
        )
        steps = self._sipp(graph, profile, start, now, goal, table)
        displaced: dict[str, tuple[float, float]] = {}

        # --- priority negotiation --------------------------------------
        lower = {
            rid
            for rid, p in self.plans.items()
            if rid != robot_id
            and p.map_id == map_id
            and self.priorities.get(rid, 5) < priority
            and any(o.acquire_step > p.started for o in p.occs)
        }
        if lower:
            table_b = self._table(map_id, exclude={robot_id}, drop_future_of=lower)
            steps_b = self._sipp(graph, profile, start, now, goal, table_b)
            eta_a = steps[-1].t_arrive if steps else INF
            if steps_b and steps_b[-1].t_arrive + PRIORITY_GAIN_S < eta_a:
                outcome = self._try_priority(
                    robot_id, map_id, start, now, goal, steps_b, profile, label, static.time, lower
                )
                if outcome is not None:
                    steps = steps_b
                    displaced = outcome

        paired: str | None = None
        if steps is None:
            paired = self._try_pair(
                robot_id, map_id, start, goal, static.nodes, profile, now, label
            )
            if paired is None:
                return self._blocked(
                    robot_id, map_id, graph, static.nodes, goal, allow_make_way, naive
                )

        if displaced or paired:
            plan = self.plans[robot_id]  # committed by _try_priority / _try_pair
        else:
            plan = self._make_plan(
                robot_id, map_id, start, now, goal, steps, profile, label, static.time
            )
            self.plans[robot_id] = plan
        self.parked.pop(robot_id, None)
        self.waits.pop(robot_id, None)
        self.pending.pop(robot_id, None)
        if paired:
            self.bus.emit(
                EventType.TRAFFIC_CONFLICT_RESOLVED,
                f"{self.name(robot_id)} and {self.name(paired)} were blocking each other; "
                "planned jointly so both can proceed",
                robot_id=robot_id,
                map_id=map_id,
            )

        route_names = " → ".join(graph.nodes[n].name for n in plan.nodes)
        delay = plan.eta - (now + static.time)
        conflict = None
        if naive:
            conflict = self._record_conflict(graph, robot_id, plan, static, naive, delay, displaced)
        elif displaced:
            conflict = self._record_priority_only(graph, robot_id, plan, displaced)
        self.bus.emit(
            EventType.ROUTE_PLANNED,
            f"{self.name(robot_id)} route: {route_names} (ETA {plan.eta - now:.0f} s"
            + (f", +{delay:.1f} s for traffic" if delay > 0.5 else "")
            + ")",
            robot_id=robot_id,
            map_id=map_id,
            data={
                "nodes": plan.nodes,
                "eta_s": round(plan.eta - now, 1),
                "delay_s": round(delay, 1),
            },
        )
        return RouteResult("planned", plan=plan, conflict=conflict)

    def _try_priority(
        self,
        robot_id: str,
        map_id: str,
        start: str,
        now: float,
        goal: str,
        steps_b: list[PlanStep],
        profile: RouteProfile,
        label: str,
        static_time: float,
        lower: set[str],
    ) -> dict[str, tuple[float, float]] | None:
        graph = self.graphs.get(map_id)
        plan_b = self._make_plan(
            robot_id, map_id, start, now, goal, steps_b, profile, label, static_time
        )
        mine = {}
        for o in plan_b.occs:
            for r in graph.expand(o.resource):
                mine.setdefault(r, []).append((o.t0, o.t1))
        victims = []
        for rid in lower:
            plan = self.plans[rid]
            for o in plan.occs:
                if o.acquire_step <= plan.started:
                    continue
                if any(a < o.t1 and o.t0 < b for a, b in mine.get(o.resource, ())):
                    victims.append(rid)
                    break
        if not victims:
            return None
        saved_plans = {rid: self.plans[rid] for rid in victims}
        saved_parked = self.parked.get(robot_id)
        self.plans[robot_id] = plan_b
        self.parked.pop(robot_id, None)
        result: dict[str, tuple[float, float]] = {}
        victims.sort(key=lambda r: -self.priorities.get(r, 5))
        for rid in victims:
            old = saved_plans[rid]
            new = self._replan_existing(rid, old)
            if new is None:
                # Roll back: everything stays as it was.
                self.plans.update(saved_plans)
                self.plans.pop(robot_id, None)
                if saved_parked:
                    self.parked[robot_id] = saved_parked
                return None
            self.plans[rid] = new
            result[rid] = (old.eta, new.eta)
        return result

    def _replan_existing(self, rid: str, old: RobotPlan) -> RobotPlan | None:
        """Re-plan a robot's remaining route from its committed position."""
        profile = self.profiles[rid]
        graph = self.graphs.get(old.map_id)
        now = self.clock()
        table = self._table(old.map_id, exclude={rid})
        if old.moving:
            cur = old.step(old.started)
            t_start = max(now, cur.t_arrive)
            tail = self._sipp(graph, profile, cur.to_node, t_start, old.goal, table)
            if tail is None:
                return None
            prefix = PlanStep(1, cur.lane_id, cur.from_node, cur.to_node, cur.t_depart, t_start)
            steps = [prefix] + [
                PlanStep(
                    s.index + 1, s.lane_id, s.from_node, s.to_node, s.t_depart, s.t_arrive, s.wait_s
                )
                for s in tail
            ]
            plan = self._make_plan(
                rid,
                old.map_id,
                cur.from_node,
                cur.t_depart,
                old.goal,
                steps,
                profile,
                old.label,
                old.static_time,
            )
            plan.started = 1
            return plan
        start = old.nodes[old.progress]
        steps = self._sipp(graph, profile, start, now, old.goal, table)
        if steps is None:
            return None
        return self._make_plan(
            rid, old.map_id, start, now, old.goal, steps, profile, old.label, old.static_time
        )

    def _try_pair(
        self,
        robot_id: str,
        map_id: str,
        start: str,
        goal: str,
        static_nodes: list[str],
        profile: RouteProfile,
        now: float,
        label: str,
    ) -> str | None:
        """Robots stuck in each other's way (swaps, rotations): plan them jointly.

        Collects the chain of parked robots with pending requests that block
        each other (up to 4), then tries planning orders and finally a *yield*
        plan where one robot pulls into a side node while the others pass.
        All-or-nothing; on success every plan is committed and the id of one
        partner is returned.
        """
        graph = self.graphs.get(map_id)
        group: dict[str, tuple] = {robot_id: (robot_id, profile, start, goal, label)}
        frontier = [(robot_id, static_nodes)]
        while frontier and len(group) < 4:
            _, nodes = frontier.pop(0)
            for rid, p in list(self.parked.items()):
                if rid in group or p.map_id != map_id or p.node_id not in nodes[1:]:
                    continue
                req = self.pending.get(rid)
                prof = self.profiles.get(rid)
                if req is None or prof is None or req[0] != map_id or req[1] != p.node_id:
                    continue
                group[rid] = (rid, prof, p.node_id, req[2], req[3])
                st = graph.plan_static(p.node_id, req[2], prof)
                if st is not None:
                    frontier.append((rid, st.nodes))
                if len(group) >= 4:
                    break
        if len(group) < 2:
            return None
        joint = self._joint_plan(graph, list(group.values()), now)
        if joint is None:
            return None
        for (rid_x, prof_x, start_x, goal_x, lbl), steps_x in joint:
            static_x = graph.plan_static(start_x, goal_x, prof_x)
            self.plans[rid_x] = self._make_plan(
                rid_x,
                map_id,
                start_x,
                now,
                goal_x,
                steps_x,
                prof_x,
                lbl,
                static_x.time if static_x else 0.0,
            )
            self.parked.pop(rid_x, None)
            if rid_x != robot_id:
                self.pending.pop(rid_x, None)
        return next(r for r in group if r != robot_id)

    def _joint_plan(self, graph: MapGraph, robots: list[tuple], now: float):
        """Plan several robots together; robots = [(rid, profile, start, goal, label)]."""
        base = self._table(graph.map_id, exclude={r[0] for r in robots})

        def with_occs(table: _Table, who: tuple, st: str, t0: float, steps, park: bool) -> _Table:
            t = _Table(busy={k: list(v) for k, v in table.busy.items()})
            for o in self._build_occs(who[0], st, t0, steps, who[1], park=park):
                t.add(o)
            t.finalize()
            return t

        def sequential(order: list[tuple], table: _Table):
            out = []
            for who in order:
                steps = self._sipp(graph, who[1], who[2], now, who[3], table)
                if steps is None:
                    return None
                out.append((who, steps))
                table = with_occs(table, who, who[2], now, steps, True)
            return out, table

        orders = list(itertools.permutations(robots))[:24]
        for order in orders:
            res = sequential(list(order), base)
            if res is not None:
                return res[0]

        occupied = {
            r.split(":", 1)[1]
            for r, spans in base.busy.items()
            if r.startswith("node:") and any(t1 == INF for _, t1, _ in spans)
        }
        for yielder in robots:
            others = [r for r in robots if r is not yielder]
            avoid = set(occupied) | {r[2] for r in robots} | {r[3] for r in robots}
            for o in others:
                st = graph.plan_static(o[2], o[3], o[1])
                if st is not None:
                    avoid |= set(st.nodes)
            candidates = []
            for w in graph.nodes.values():
                if w.id in avoid or w.layer != yielder[1].layer:
                    continue
                r = graph.plan_static(yielder[2], w.id, yielder[1])
                if r is not None:
                    candidates.append((r.time, w.id))
            for _, side in sorted(candidates)[:5]:
                y1 = self._sipp(graph, yielder[1], yielder[2], now, side, base)
                if not y1:
                    continue
                t_y1 = with_occs(base, yielder, yielder[2], now, y1, True)
                for order in list(itertools.permutations(others))[:6]:
                    res = sequential(list(order), t_y1)
                    if res is None:
                        continue
                    planned, t_all = res
                    y2 = self._sipp(graph, yielder[1], side, y1[-1].t_arrive, yielder[3], t_all)
                    if y2 is None:
                        continue
                    composed = list(y1)
                    prev_arrive = y1[-1].t_arrive
                    for sidx, st in enumerate(y2, len(y1) + 1):
                        composed.append(
                            PlanStep(
                                sidx,
                                st.lane_id,
                                st.from_node,
                                st.to_node,
                                st.t_depart,
                                st.t_arrive,
                                max(0.0, st.t_depart - prev_arrive),
                            )
                        )
                        prev_arrive = st.t_arrive
                    return planned + [(yielder, composed)]
        return None

    def _blocked(
        self,
        robot_id: str,
        map_id: str,
        graph: MapGraph,
        static_nodes: list[str],
        goal: str,
        allow_make_way: bool,
        naive: dict | None,
    ) -> RouteResult:
        """No space-time route: find who sits in the way and negotiate."""
        parked_on_path = [
            (rid, p)
            for rid, p in self.parked.items()
            if rid != robot_id and p.map_id == map_id and p.node_id in static_nodes[1:]
        ]
        # The destination first, then the nearest blocker along the route. Ask
        # every idle blocker to make way (not just the first one we meet).
        parked_on_path.sort(
            key=lambda rp: (rp[1].node_id != goal, static_nodes.index(rp[1].node_id))
        )
        reasons: list[str] = []
        for rid, p in parked_on_path:
            where = graph.nodes[p.node_id].name
            what = "the destination" if p.node_id == goal else "the only route"
            if p.busy or not allow_make_way or self.make_way_handler is None:
                reasons.append(
                    f"{self.name(rid)} is working at {where} ({what}); waiting for it to leave"
                )
                continue
            if self._make_way_pending.get(rid, 0) > self.clock():
                reasons.append(f"waiting for {self.name(rid)} to make way at {where}")
                continue
            target = self._make_way_target(rid, map_id, avoid=set(static_nodes))
            if target is None:
                reasons.append(f"{self.name(rid)} blocks {where} and has nowhere free to move")
                continue
            if not self.make_way_handler(
                rid, target, f"make way for {self.name(robot_id)} at {where}"
            ):
                reasons.append(f"{self.name(rid)} blocks {where} and cannot move right now")
                continue
            self._make_way_pending[rid] = self.clock() + 30.0
            self._make_way_targets[rid] = target
            self._record_simple_conflict(
                graph,
                ConflictKind.DESTINATION if p.node_id == goal else ConflictKind.BOTTLENECK,
                [robot_id, rid],
                node_res(p.node_id),
                f"{self.name(rid)} is parked at {where}, {what} of {self.name(robot_id)}",
                ConflictResolution(
                    action="make_way",
                    robot_id=rid,
                    message=f"{self.name(rid)} asked to move to {graph.nodes[target].name}",
                ),
            )
            reasons.append(f"waiting for {self.name(rid)} to make way at {where}")
        if reasons:
            more = f" (+{len(reasons) - 1} more)" if len(reasons) > 1 else ""
            return RouteResult("blocked", reason=reasons[0] + more, retry=True)
        reason = "no conflict-free route within the planning horizon"
        if naive:
            reason = (
                f"route blocked by {self.name(naive['other'])} at "
                f"{self._element_name(graph, naive['hit'])}"
            )
        return RouteResult("blocked", reason=reason, retry=True)

    def _make_way_target(self, rid: str, map_id: str, avoid: set[str]) -> str | None:
        graph = self.graphs.get(map_id)
        p = self.parked[rid]
        profile = self.profiles.get(rid)
        if profile is None or p.node_id is None:
            return None
        occupied = {
            o.resource.split(":", 1)[1]
            for o in self._all_occs(map_id, exclude={rid})
            if o.resource.startswith("node:") and o.t1 == INF
        }
        occupied |= {t for r, t in self._make_way_targets.items() if r != rid}
        occupied |= {req[2] for r, req in self.pending.items() if r != rid and req[0] == map_id}
        preference = {"parking": 0, "charger": 1, "location": 2, "dock": 3, "inspection": 3}
        best = None
        for w in graph.nodes.values():
            if w.layer != profile.layer or w.id in avoid or w.id in occupied or w.id == p.node_id:
                continue
            if any(node_res(w.id) in graph.expand(node_res(o)) for o in occupied):
                continue
            route = graph.plan_static(p.node_id, w.id, profile)
            if route is None:
                continue
            score = route.time + 10.0 * preference.get(w.type, 5)
            if best is None or score < best[0]:
                best = (score, w.id)
        return best[1] if best else None

    # ------------------------------------------------------------------
    # Conflict records
    # ------------------------------------------------------------------

    def _record_conflict(self, graph, robot_id, plan, static, naive, delay, displaced) -> Conflict:
        other = naive["other"]
        where = self._element_name(graph, naive["hit"])
        kind: ConflictKind = naive["kind"]
        label = kind.value.replace("_", "-")
        message = (
            f"Potential {label} conflict between {self.name(robot_id)} and {self.name(other)} "
            f"at {where} in ≈{max(0.0, naive['t']):.0f} s"
        )
        if displaced:
            parts = [
                f"{self.name(r)} +{max(0.0, new - old):.1f} s"
                for r, (old, new) in displaced.items()
            ]
            res = ConflictResolution(
                action="priority",
                robot_id=robot_id,
                message=f"{self.name(robot_id)} has priority; re-planned " + ", ".join(parts),
            )
        elif plan.nodes != static.nodes:
            via = [graph.nodes[n].name for n in plan.nodes if n not in static.nodes][:2]
            res = ConflictResolution(
                action="reroute",
                robot_id=robot_id,
                delay_s=round(max(0.0, delay), 1),
                message=f"{self.name(robot_id)} rerouted via {', '.join(via) or 'another lane'}"
                + (f" (+{delay:.1f} s)" if delay > 0.05 else ""),
            )
        else:
            waits = [s for s in plan.steps if s.wait_s > 0.05]
            at = max(waits, key=lambda s: s.wait_s) if waits else None
            where_wait = graph.nodes[at.from_node].name if at else where
            res = ConflictResolution(
                action="delay",
                robot_id=robot_id,
                delay_s=round(max(0.0, delay), 1),
                message=f"{self.name(robot_id)} delayed ≈{max(0.0, delay):.1f} s at {where_wait} "
                f"to let {self.name(other)} pass",
            )
        return self._record_simple_conflict(
            graph, kind, [robot_id, other], naive["hit"], message, res, naive["t"]
        )

    def _record_priority_only(self, graph, robot_id, plan, displaced) -> Conflict:
        rid, (old, new) = next(iter(displaced.items()))
        return self._record_simple_conflict(
            graph,
            ConflictKind.SAME_LANE,
            [robot_id, *displaced],
            node_res(plan.goal),
            f"Priority conflict: {self.name(robot_id)} (priority {self.priorities.get(robot_id, 5)}) "
            f"and {self.name(rid)}",
            ConflictResolution(
                action="priority",
                robot_id=robot_id,
                message=f"{self.name(rid)} re-planned (+{max(0.0, new - old):.1f} s)",
            ),
        )

    def _record_simple_conflict(
        self,
        graph: MapGraph,
        kind: ConflictKind,
        robots: list[str],
        resource: str,
        message: str,
        resolution: ConflictResolution,
        t_ahead: float | None = None,
    ) -> Conflict:
        conflict = Conflict(
            id=new_id("cf"),
            kind=kind,
            map_id=graph.map_id,
            robots=robots,
            element_id=resource.split(":", 1)[1],
            location_name=self._element_name(graph, resource),
            point=self._point(graph, resource),
            predicted_at_s=round(t_ahead, 1) if t_ahead is not None else None,
            status="resolved",
            message=message,
            resolution=resolution,
            resolved_at=time.time(),
        )
        self.conflicts.append(conflict)
        self.bus.emit(
            EventType.TRAFFIC_CONFLICT_DETECTED,
            message,
            Severity.WARNING,
            robot_id=robots[0],
            map_id=graph.map_id,
            data={"conflict": conflict.model_dump(mode="json")},
        )
        self.bus.emit(
            EventType.TRAFFIC_CONFLICT_RESOLVED,
            resolution.message,
            robot_id=resolution.robot_id or robots[0],
            map_id=graph.map_id,
            data={"conflict_id": conflict.id, "action": resolution.action},
        )
        self.bus.publish("conflict", conflict.model_dump(mode="json"))
        return conflict

    # ------------------------------------------------------------------
    # Execution (action dependency graph)
    # ------------------------------------------------------------------

    def check_step(self, robot_id: str, k: int | None = None) -> StepCheck:
        plan = self.plans.get(robot_id)
        if plan is None:
            return StepCheck(False, "no active route", replan=True)
        if robot_id in self.needs_replan:
            return StepCheck(False, "re-planning around a changed schedule", replan=True)
        k = k or plan.started + 1
        if k > len(plan.steps):
            return StepCheck(True)
        graph = self.graphs.get(plan.map_id)
        step = plan.step(k)
        c = self._clearance_s(self.profiles[robot_id])
        needs = [
            (lane_res(step.lane_id), step.t_depart),
            (node_res(step.to_node), step.t_arrive - c),
        ]
        for res, my_t0 in needs:
            expanded = graph.expand(res)
            for rid in list(self.plans) + list(self.parked):
                if rid == robot_id or self._map_of(rid) != plan.map_id:
                    continue
                other_plan = self.plans.get(rid)
                for o in self._all_occs_of(rid):
                    if o.resource not in expanded:
                        continue
                    active = other_plan is None or other_plan.started >= o.acquire_step
                    earlier = o.t0 < my_t0 - 1e-6 or (abs(o.t0 - my_t0) <= 1e-6 and rid < robot_id)
                    if active or earlier:
                        what = self._element_name(graph, o.resource)
                        verb = "is on" if active else "goes first through"
                        reason = f"waiting for {self.name(rid)}, which {verb} {what}"
                        self._note_wait(robot_id, res, rid, reason)
                        return StepCheck(False, reason, blocker=rid, resource=o.resource)
        self._clear_wait(robot_id)
        return StepCheck(True)

    def _note_wait(self, robot_id: str, resource: str, holder: str, reason: str) -> None:
        existing = self.waits.get(robot_id)
        if existing and existing.holder == holder and existing.resource == resource:
            return
        self.waits[robot_id] = WaitInfo(
            robot_id=robot_id,
            resource=resource,
            element_id=resource.split(":", 1)[1],
            holder=holder,
            reason=reason,
            since=self.clock(),
        )
        self._announced.discard(robot_id)  # logged by tick() if it lasts

    def _clear_wait(self, robot_id: str) -> None:
        self.waits.pop(robot_id, None)

    def start_step(self, robot_id: str, k: int) -> None:
        plan = self.plans.get(robot_id)
        if plan and k == plan.started + 1:
            plan.started = k
            self._clear_wait(robot_id)

    def complete_step(self, robot_id: str, k: int) -> None:
        plan = self.plans.get(robot_id)
        if plan and plan.progress < k <= plan.started:
            plan.progress = k

    def finish_route(self, robot_id: str, busy: bool = False) -> None:
        plan = self.plans.pop(robot_id, None)
        if plan is not None:
            node = plan.nodes[min(plan.progress, len(plan.nodes) - 1)]
            self.parked[robot_id] = Parked(plan.map_id, node, busy=busy, since=self.clock())
        self._make_way_pending.pop(robot_id, None)
        self._make_way_targets.pop(robot_id, None)
        self._clear_wait(robot_id)

    def freeze(self, robot_id: str, busy: bool = True) -> None:
        """The robot stopped where it is (pause, e-stop, cancel, failure)."""
        plan = self.plans.pop(robot_id, None)
        if plan is None:
            if robot_id in self.parked:
                self.parked[robot_id].busy = busy
            return
        if plan.moving:
            s = plan.step(plan.started)
            self.parked[robot_id] = Parked(
                plan.map_id,
                None,
                lane_id=s.lane_id,
                lane_ends=(s.from_node, s.to_node),
                busy=busy,
                since=self.clock(),
            )
        else:
            self.parked[robot_id] = Parked(
                plan.map_id, plan.nodes[plan.progress], busy=busy, since=self.clock()
            )
        self._clear_wait(robot_id)
        self._flag_dependents(robot_id)

    def rejoin_node(self, robot_id: str) -> str | None:
        """Where a robot frozen mid-lane should head to get back on the graph."""
        p = self.parked.get(robot_id)
        if p is None:
            return None
        if p.node_id:
            return p.node_id
        return p.lane_ends[1] if p.lane_ends else None

    def settle_on_node(self, robot_id: str, node_id: str) -> None:
        p = self.parked.get(robot_id)
        if p:
            p.node_id = node_id
            p.lane_id = None
            p.lane_ends = None

    # ------------------------------------------------------------------
    # Housekeeping + observability
    # ------------------------------------------------------------------

    def tick(self) -> None:
        """Log lasting waits, detect deadlocks and waits on robots that no longer move."""
        now = self.clock()
        for rid, w in self.waits.items():
            if rid not in self._announced and now - w.since >= WAIT_EVENT_AFTER_S:
                self._announced.add(rid)
                plan = self.plans.get(rid)
                self.bus.emit(
                    EventType.TRAFFIC_WAIT,
                    f"{self.name(rid)} {w.reason}",
                    robot_id=rid,
                    map_id=plan.map_id if plan else None,
                    data={"holder": w.holder, "resource": w.resource},
                )
        graph_wait = {rid: w.holder for rid, w in self.waits.items() if w.holder}
        for start in list(graph_wait):
            seen = []
            cur = start
            while cur in graph_wait and cur not in seen:
                seen.append(cur)
                cur = graph_wait[cur]
            if cur in seen:
                cycle = seen[seen.index(cur) :]
                victim = min(cycle, key=lambda r: (self.priorities.get(r, 5), r))
                if victim not in self.needs_replan:
                    names = ", ".join(self.name(r) for r in cycle)
                    self.bus.emit(
                        EventType.TRAFFIC_DEADLOCK,
                        f"Wait cycle between {names}; re-planning {self.name(victim)}",
                        Severity.WARNING,
                        robot_id=victim,
                    )
                    self.needs_replan.add(victim)
        for rid, w in list(self.waits.items()):
            holder_plan = self.plans.get(w.holder) if w.holder else None
            holder_stuck = w.holder in self.parked or (holder_plan is None)
            if holder_stuck and now - w.since > STALE_WAIT_S and rid not in self.needs_replan:
                self.needs_replan.add(rid)

    def snapshot(self) -> TrafficState:
        now = self.clock()
        wall = time.time()
        reservations = []
        itineraries = []
        for rid, plan in self.plans.items():
            for o in plan.occs:
                if plan.started >= o.acquire_step and plan.progress < o.release_step:
                    kind, eid = o.resource.split(":", 1)
                    reservations.append(
                        Reservation(
                            resource=o.resource,
                            kind=kind,
                            element_id=eid,
                            map_id=plan.map_id,
                            robot_id=rid,
                            since=wall,
                        )
                    )
            entries = [
                ItineraryEntry(
                    resource=o.resource,
                    kind=o.resource.split(":", 1)[0],
                    element_id=o.resource.split(":", 1)[1],
                    t_enter=round(o.t0 - now, 2),
                    t_exit=round(o.t1 - now, 2) if o.t1 != INF else -1,
                )
                for o in plan.occs
                if plan.progress < o.release_step
            ]
            itineraries.append(
                Itinerary(
                    robot_id=rid,
                    map_id=plan.map_id,
                    route=plan.remaining_nodes(),
                    entries=entries,
                    eta=round(max(0.0, plan.eta - now), 1),
                )
            )
        for rid, p in self.parked.items():
            for o in self._parked_occs(rid, p, now):
                kind, eid = o.resource.split(":", 1)
                reservations.append(
                    Reservation(
                        resource=o.resource,
                        kind=kind,
                        element_id=eid,
                        map_id=p.map_id,
                        robot_id=rid,
                        since=wall,
                    )
                )
        precedences = []
        for rid, w in self.waits.items():
            if w.holder:
                precedences.append({"first": w.holder, "then": rid, "resource": w.resource})
        return TrafficState(
            reservations=reservations,
            itineraries=itineraries,
            conflicts=list(self.conflicts)[-30:],
            waits=list(self.waits.values()),
            precedences=precedences,
        )
