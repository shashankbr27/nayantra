"""
nayantra/core/routing.py

Navigation-graph index and static route planning.

A MapGraph is built from the registry for one map: waypoints are vertices,
lanes are (possibly one-way) edges. It precomputes what the traffic layer
needs:

  * which lanes a given robot profile may use: layer, closure, lane width
    against footprint, restricted / no-fly zones and fleet boundaries
  * effective lane speed: robot limit, lane limit and slow-zone limits
  * resource conflict sets: lanes that cross or run closer than the clearance
    distance, and nodes too close together or sitting on another lane

Resources are named "node:<waypoint_id>" and "lane:<lane_id>". Ids are
globally unique, so resource names never collide across maps.
"""

from __future__ import annotations

import heapq
import itertools
import math
from dataclasses import dataclass, field

from nayantra.core.geometry import (
    dist,
    point_in_polygon,
    point_segment_distance,
    segment_intersects_polygon,
    segment_segment_distance,
)
from nayantra.core.models import Lane, Waypoint, Zone

GROUND_CLEARANCE_M = 1.2  # lanes/nodes closer than this conflict (≈ two robot radii)
AIR_CLEARANCE_M = 2.0
AIR_VERTICAL_SEPARATION_M = 3.0


def node_res(node_id: str) -> str:
    return f"node:{node_id}"


def lane_res(lane_id: str) -> str:
    return f"lane:{lane_id}"


@dataclass(frozen=True)
class RouteProfile:
    """Everything about a robot that affects where and how fast it may go."""

    robot_id: str
    fleet_id: str
    layer: str = "ground"
    max_speed: float = 1.0
    footprint_radius: float = 0.4
    max_vertical_speed: float | None = None
    allow_restricted: bool = False

    @property
    def min_lane_width(self) -> float:
        return 2 * self.footprint_radius


@dataclass(frozen=True)
class Edge:
    lane_id: str
    u: str
    v: str


@dataclass
class StaticRoute:
    nodes: list[str]
    lanes: list[str]
    length: float
    time: float

    @property
    def resources(self) -> list[str]:
        out = [node_res(self.nodes[0])]
        for lane_id, node in zip(self.lanes, self.nodes[1:], strict=True):
            out += [lane_res(lane_id), node_res(node)]
        return out


@dataclass
class MapGraph:
    map_id: str
    nodes: dict[str, Waypoint]
    lanes: dict[str, Lane]
    zones: list[Zone]
    adj: dict[str, list[Edge]] = field(default_factory=dict)
    degree: dict[str, int] = field(default_factory=dict)
    conflicts: dict[str, set[str]] = field(default_factory=dict)
    lane_zones: dict[str, list[Zone]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls, map_id: str, waypoints: list[Waypoint], lanes: list[Lane], zones: list[Zone]
    ) -> MapGraph:
        g = cls(
            map_id=map_id,
            nodes={w.id: w for w in waypoints},
            lanes={ln.id: ln for ln in lanes},
            zones=zones,
        )
        for w in waypoints:
            g.adj[w.id] = []
            g.degree[w.id] = 0
        for ln in lanes:
            if ln.from_id not in g.nodes or ln.to_id not in g.nodes:
                continue
            g.adj[ln.from_id].append(Edge(ln.id, ln.from_id, ln.to_id))
            if ln.bidirectional:
                g.adj[ln.to_id].append(Edge(ln.id, ln.to_id, ln.from_id))
            g.degree[ln.from_id] += 1
            g.degree[ln.to_id] += 1
            g.lane_zones[ln.id] = [
                z
                for z in zones
                if (z.layer is None or z.layer == ln.layer)
                and segment_intersects_polygon(g.xy(ln.from_id), g.xy(ln.to_id), z.polygon)
            ]
        g._compute_conflicts()
        return g

    def xy(self, node_id: str) -> tuple[float, float]:
        w = self.nodes[node_id]
        return (w.x, w.y)

    def _compute_conflicts(self) -> None:
        lanes = [
            ln for ln in self.lanes.values() if ln.from_id in self.nodes and ln.to_id in self.nodes
        ]
        for ln in lanes:
            self.conflicts.setdefault(lane_res(ln.id), set())
        for w in self.nodes.values():
            self.conflicts.setdefault(node_res(w.id), set())

        def close_lanes(a: Lane, b: Lane) -> bool:
            if a.layer != b.layer:
                return False
            if {a.from_id, a.to_id} & {b.from_id, b.to_id}:
                return False  # share a node — the node resource covers it
            if a.layer == "air":
                if (
                    a.altitude is not None
                    and b.altitude is not None
                    and abs(a.altitude - b.altitude) >= AIR_VERTICAL_SEPARATION_M
                ):
                    return False
                limit = AIR_CLEARANCE_M
            else:
                limit = GROUND_CLEARANCE_M
            d = segment_segment_distance(
                self.xy(a.from_id), self.xy(a.to_id), self.xy(b.from_id), self.xy(b.to_id)
            )
            return d < limit

        for a, b in itertools.combinations(lanes, 2):
            if close_lanes(a, b):
                self.conflicts[lane_res(a.id)].add(lane_res(b.id))
                self.conflicts[lane_res(b.id)].add(lane_res(a.id))

        nodes = list(self.nodes.values())
        for a, b in itertools.combinations(nodes, 2):
            if a.layer != b.layer:
                continue
            limit = AIR_CLEARANCE_M if a.layer == "air" else GROUND_CLEARANCE_M
            if dist((a.x, a.y), (b.x, b.y)) < limit:
                self.conflicts[node_res(a.id)].add(node_res(b.id))
                self.conflicts[node_res(b.id)].add(node_res(a.id))

        # A node sitting on (or next to) a lane it is not part of blocks that lane.
        for w in nodes:
            for ln in lanes:
                if w.id in (ln.from_id, ln.to_id) or w.layer != ln.layer:
                    continue
                d = point_segment_distance((w.x, w.y), self.xy(ln.from_id), self.xy(ln.to_id))
                if d < GROUND_CLEARANCE_M * 0.75:
                    self.conflicts[node_res(w.id)].add(lane_res(ln.id))
                    self.conflicts[lane_res(ln.id)].add(node_res(w.id))

    # ------------------------------------------------------------------
    # Lane rules
    # ------------------------------------------------------------------

    def lane_block_reason(self, lane: Lane, profile: RouteProfile) -> str | None:
        """Why this profile may not use the lane (None = usable)."""
        if lane.closed:
            return "lane closed"
        if lane.layer != profile.layer:
            return f"{lane.layer} lane"
        if lane.width is not None and lane.width + 1e-6 < profile.min_lane_width:
            return f"lane width {lane.width:.2f} m < robot footprint {profile.min_lane_width:.2f} m"
        for z in self.lane_zones.get(lane.id, []):
            if z.type in ("restricted", "no_fly"):
                if profile.fleet_id in z.allowed_fleets or profile.allow_restricted:
                    continue
                return f"passes through {z.type.replace('_', '-')} zone '{z.name}'"
        boundaries = [
            z
            for z in self.zones
            if z.type == "fleet_boundary" and profile.fleet_id in z.allowed_fleets
        ]
        if boundaries:
            a, b = self.xy(lane.from_id), self.xy(lane.to_id)
            if not any(
                point_in_polygon(a, z.polygon) and point_in_polygon(b, z.polygon)
                for z in boundaries
            ):
                return "outside the fleet's boundary zone"
        return None

    def lane_speed(self, lane: Lane, profile: RouteProfile) -> float:
        speed = profile.max_speed
        if lane.speed_limit:
            speed = min(speed, lane.speed_limit)
        for z in self.lane_zones.get(lane.id, []):
            if z.speed_limit:
                speed = min(speed, z.speed_limit)
        return max(speed, 0.05)

    def lane_length(self, lane_id: str) -> float:
        ln = self.lanes[lane_id]
        return dist(self.xy(ln.from_id), self.xy(ln.to_id))

    def traverse_time(self, edge: Edge, profile: RouteProfile) -> float:
        lane = self.lanes[edge.lane_id]
        t = self.lane_length(edge.lane_id) / self.lane_speed(lane, profile)
        if lane.layer == "air" and profile.max_vertical_speed:
            alt = lane.altitude if lane.altitude is not None else self.nodes[edge.v].z
            climb = abs(alt - self.nodes[edge.u].z) + abs(alt - self.nodes[edge.v].z)
            t += climb / profile.max_vertical_speed
        return t

    def usable_edges(self, node_id: str, profile: RouteProfile) -> list[Edge]:
        return [
            e
            for e in self.adj.get(node_id, [])
            if self.lane_block_reason(self.lanes[e.lane_id], profile) is None
        ]

    def expand(self, resource: str) -> set[str]:
        """A resource plus everything that physically conflicts with it."""
        return {resource} | self.conflicts.get(resource, set())

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def nearest_node(self, x: float, y: float, layer: str = "ground") -> tuple[str | None, float]:
        best, best_d = None, math.inf
        for w in self.nodes.values():
            if w.layer != layer:
                continue
            d = dist((x, y), (w.x, w.y))
            if d < best_d:
                best, best_d = w.id, d
        return best, best_d

    def plan_static(
        self,
        start: str,
        goal: str,
        profile: RouteProfile,
        avoid: set[str] | None = None,
    ) -> StaticRoute | None:
        """Time-optimal route ignoring other robots (A*). `avoid` = resources to skip."""
        if start not in self.nodes or goal not in self.nodes:
            return None
        if start == goal:
            return StaticRoute(nodes=[start], lanes=[], length=0.0, time=0.0)
        avoid = avoid or set()
        gx, gy = self.xy(goal)
        vmax = max(profile.max_speed, 0.05)

        def h(n: str) -> float:
            x, y = self.xy(n)
            return math.hypot(gx - x, gy - y) / vmax

        counter = itertools.count()
        open_heap = [(h(start), next(counter), start)]
        g_cost = {start: 0.0}
        parent: dict[str, tuple[str, str]] = {}
        closed: set[str] = set()
        while open_heap:
            _, _, u = heapq.heappop(open_heap)
            if u in closed:
                continue
            if u == goal:
                break
            closed.add(u)
            for e in self.usable_edges(u, profile):
                if lane_res(e.lane_id) in avoid or (e.v != goal and node_res(e.v) in avoid):
                    continue
                cand = g_cost[u] + self.traverse_time(e, profile)
                if cand < g_cost.get(e.v, math.inf):
                    g_cost[e.v] = cand
                    parent[e.v] = (u, e.lane_id)
                    heapq.heappush(open_heap, (cand + h(e.v), next(counter), e.v))
        if goal not in g_cost:
            return None
        nodes, lanes = [goal], []
        cur = goal
        while cur != start:
            prev, lane_id = parent[cur]
            nodes.append(prev)
            lanes.append(lane_id)
            cur = prev
        nodes.reverse()
        lanes.reverse()
        length = sum(self.lane_length(ln) for ln in lanes)
        return StaticRoute(nodes=nodes, lanes=lanes, length=length, time=g_cost[goal])

    def explain_unreachable(self, start: str, goal: str, profile: RouteProfile) -> str:
        """Human-readable reason why no route exists."""
        blocked = {}
        for ln in self.lanes.values():
            reason = self.lane_block_reason(ln, profile)
            if reason and ln.layer == profile.layer:
                blocked.setdefault(reason, []).append(ln.id)
        start_name = self.nodes[start].name if start in self.nodes else start
        goal_name = self.nodes[goal].name if goal in self.nodes else goal
        if goal in self.nodes and self.nodes[goal].layer != profile.layer:
            return (
                f"'{goal_name}' is on the {self.nodes[goal].layer} layer; this robot uses "
                f"the {profile.layer} layer"
            )
        if not blocked:
            return f"No lanes connect '{start_name}' to '{goal_name}'"
        detail = "; ".join(f"{len(v)} lane(s): {k}" for k, v in blocked.items())
        return f"No usable route from '{start_name}' to '{goal_name}' ({detail})"


class GraphCache:
    """Builds MapGraphs on demand; the registry invalidates entries on change."""

    def __init__(self, registry) -> None:
        self._registry = registry
        self._graphs: dict[str, MapGraph] = {}
        registry.on_change(self._on_change)

    def _on_change(self, kind: str, op: str, obj) -> None:
        if kind == "map":
            self._graphs.pop(obj.id, None)
        elif kind in ("waypoint", "lane", "zone"):
            self._graphs.pop(obj.map_id, None)

    def get(self, map_id: str) -> MapGraph:
        g = self._graphs.get(map_id)
        if g is None:
            r = self._registry
            g = MapGraph.build(
                map_id, r.list_waypoints(map_id), r.list_lanes(map_id), r.list_zones(map_id)
            )
            self._graphs[map_id] = g
        return g

    def invalidate(self, map_id: str | None = None) -> None:
        if map_id is None:
            self._graphs.clear()
        else:
            self._graphs.pop(map_id, None)
