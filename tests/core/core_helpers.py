"""Shared helpers for the Nayantra Core tests (imported by conftest and test modules)."""

from __future__ import annotations

from nayantra.core.events import EventBus
from nayantra.core.models import Bounds, LaneCreate, MapCreate, WaypointCreate
from nayantra.core.routing import GraphCache, RouteProfile
from nayantra.core.store import DocumentStore
from nayantra.core.traffic import TrafficCoordinator
from nayantra.core.world import WorldRegistry


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TrafficWorld:
    """A tiny map + traffic coordinator built from a compact spec."""

    def __init__(self, nodes, lanes, zones=(), types=None) -> None:
        self.store = DocumentStore()
        self.bus = EventBus(self.store)
        self.reg = WorldRegistry(self.store, self.bus)
        self.reg.create_map(
            MapCreate(id="m", name="M", bounds=Bounds(min_x=-100, min_y=-100, max_x=100, max_y=100))
        )
        types = types or {}
        for nid, (x, y) in nodes.items():
            self.reg.create_waypoint(
                "m", WaypointCreate(id=nid, name=nid, x=x, y=y, type=types.get(nid, "intersection"))
            )
        for spec in lanes:
            if isinstance(spec, tuple):
                spec = {"from_id": spec[0], "to_id": spec[1]}
            self.reg.create_lane("m", LaneCreate(**spec))
        for z in zones:
            self.reg.create_zone("m", z)
        self.graphs = GraphCache(self.reg)
        self.clock = FakeClock()
        self.traffic = TrafficCoordinator(self.graphs, self.bus, clock=self.clock)

    @property
    def graph(self):
        return self.graphs.get("m")

    def add_robot(
        self, rid: str, node: str, speed: float = 1.0, priority: int = 5, radius: float = 0.3
    ):
        self.traffic.set_profile(
            rid,
            RouteProfile(robot_id=rid, fleet_id="f", max_speed=speed, footprint_radius=radius),
            priority,
        )
        self.traffic.set_parked(rid, "m", node)

    def route(self, rid: str, goal: str, **kw):
        node, _ = self.traffic.location(rid)
        return self.traffic.request_route(rid, "m", node, goal, **kw)

    def drive(self, rid: str) -> None:
        """Execute a robot's whole plan instantly (for staging scenarios)."""
        plan = self.traffic.plans[rid]
        for k in range(plan.started + 1, len(plan.steps) + 1):
            self.traffic.start_step(rid, k)
            self.traffic.complete_step(rid, k)
        self.traffic.finish_route(rid)
