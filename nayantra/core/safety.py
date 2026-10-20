"""
nayantra/core/safety.py

Deterministic hard limits. Nothing here consults the LLM, and nothing can be
switched off from the MCP tools.

  * speed      — global caps per layer on top of fleet/robot/lane/zone limits
  * workspace  — goals must be inside the map bounds (enforced by the registry)
  * zones      — routes avoid restricted / no-fly zones (routing); a task whose
                 *destination* is inside one needs explicit operator
                 confirmation; a robot found inside one it isn't cleared for is
                 stopped and an alert is raised
  * battery    — allocation never assigns work that would drop below the
                 fleet minimum (allocation.py)
  * e-stop     — per robot and global; an e-stopped robot ignores all motion
                 until an operator releases it
  * collision  — the simulator's forward stop zone (adapters/sim.py) and Nav2's
                 collision_monitor on real robots are the last layer
"""

from __future__ import annotations

from nayantra.core.geometry import point_in_polygon
from nayantra.core.models import Waypoint, Zone
from nayantra.core.world import WorldRegistry

MAX_GROUND_SPEED_MPS = 2.0
MAX_AIR_SPEED_MPS = 6.0


def speed_cap(layer: str) -> float:
    return MAX_AIR_SPEED_MPS if layer == "air" else MAX_GROUND_SPEED_MPS


class SafetyGuard:
    def __init__(self, registry: WorldRegistry) -> None:
        self.registry = registry

    def forbidden_zones_at(
        self, map_id: str | None, x: float, y: float, fleet_id: str | None, layer: str
    ) -> list[Zone]:
        """Restricted / no-fly zones containing (x, y) that this fleet may not enter."""
        if not map_id:
            return []
        out = []
        for z in self.registry.list_zones(map_id):
            if z.type not in ("restricted", "no_fly"):
                continue
            if z.layer is not None and z.layer != layer:
                continue
            if z.type == "no_fly" and layer != "air":
                continue
            if fleet_id and fleet_id in z.allowed_fleets:
                continue
            if point_in_polygon((x, y), z.polygon):
                out.append(z)
        return out

    def restricted_targets(self, waypoints: list[Waypoint]) -> list[tuple[Waypoint, Zone]]:
        """Task destinations inside restricted zones (fleet-independent check)."""
        hits = []
        for w in waypoints:
            for z in self.registry.list_zones(w.map_id):
                if z.type in ("restricted", "no_fly") and point_in_polygon((w.x, w.y), z.polygon):
                    if z.layer is None or z.layer == w.layer:
                        hits.append((w, z))
        return hits
