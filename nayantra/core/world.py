"""
nayantra/core/world.py

WorldRegistry — the authoritative registry of maps, waypoints (nav-graph
vertices), lanes (nav-graph edges), zones, fleets and robots.

Everything is held in memory and written through to SQLite on each change.
Referential integrity is enforced here, so nothing downstream has to
second-guess the data:

  * lanes connect two existing waypoints on the same map
  * waypoints lie inside their map bounds (workspace hard limit)
  * waypoint names are unique per map (they are what operators and the LLM say)
  * robots belong to an existing fleet and operate on a map the fleet allows

Change listeners (routing cache, fleet manager, WebSocket) are notified
synchronously after each committed change.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from nayantra.core.errors import Conflict, Invalid, NotFound
from nayantra.core.events import EventBus
from nayantra.core.geometry import point_in_polygon
from nayantra.core.models import (
    EffectiveRobotConfig,
    EventType,
    Fleet,
    FleetCreate,
    FleetPatch,
    Lane,
    LaneCreate,
    LanePatch,
    Map,
    MapCreate,
    MapDetail,
    MapPatch,
    MapSeed,
    NavStack,
    Robot,
    RobotCreate,
    RobotPatch,
    Waypoint,
    WaypointCreate,
    WaypointPatch,
    Zone,
    ZoneCreate,
    ZonePatch,
    slugify,
)
from nayantra.core.store import DocumentStore

logger = logging.getLogger("nayantra.core.world")

ChangeListener = Callable[[str, str, Any], None]  # (kind, op, obj)


def _merge(model: BaseModel, patch: BaseModel) -> dict[str, Any]:
    data = model.model_dump(mode="json")
    for key, value in patch.model_dump(mode="json", exclude_unset=True).items():
        data[key] = value
    return data


def norm_name(text: str) -> str:
    return slugify(text)


class WorldRegistry:
    def __init__(self, store: DocumentStore, bus: EventBus) -> None:
        self._store = store
        self._bus = bus
        self._listeners: list[ChangeListener] = []
        self.maps: dict[str, Map] = {}
        self.waypoints: dict[str, Waypoint] = {}
        self.lanes: dict[str, Lane] = {}
        self.zones: dict[str, Zone] = {}
        self.fleets: dict[str, Fleet] = {}
        self.robots: dict[str, Robot] = {}
        self._load()

    # ------------------------------------------------------------------
    # Persistence / notification
    # ------------------------------------------------------------------

    def _load(self) -> None:
        for raw in self._store.list("maps"):
            m = Map.model_validate(raw)
            self.maps[m.id] = m
        for raw in self._store.list("waypoints"):
            w = Waypoint.model_validate(raw)
            self.waypoints[w.id] = w
        for raw in self._store.list("lanes"):
            ln = Lane.model_validate(raw)
            self.lanes[ln.id] = ln
        for raw in self._store.list("zones"):
            z = Zone.model_validate(raw)
            self.zones[z.id] = z
        for raw in self._store.list("fleets"):
            f = Fleet.model_validate(raw)
            self.fleets[f.id] = f
        for raw in self._store.list("robots"):
            r = Robot.model_validate(raw)
            self.robots[r.id] = r

    def on_change(self, listener: ChangeListener) -> None:
        self._listeners.append(listener)

    def _commit(self, kind: str, op: str, obj: Any, collection: str, map_id: str | None = None):
        if op == "delete":
            self._store.delete(collection, obj.id)
        else:
            self._store.put(collection, obj.id, obj.model_dump(mode="json"), map_id=map_id)
        self._bus.publish(
            "registry",
            {"kind": kind, "op": op, "id": obj.id, "item": obj.model_dump(mode="json")},
        )
        for listener in list(self._listeners):
            try:
                listener(kind, op, obj)
            except Exception as exc:  # noqa: BLE001
                logger.exception(f"registry listener failed on {kind}/{op}: {exc}")

    def _unique_id(self, taken: dict[str, Any], base: str, keep_case: bool = False) -> str:
        base = (
            re.sub(r"[^A-Za-z0-9_.\-]+", "_", base).strip("_")[:60] if keep_case else slugify(base)
        )
        if base not in taken:
            return base
        n = 2
        while f"{base}_{n}" in taken:
            n += 1
        return f"{base}_{n}"

    # ------------------------------------------------------------------
    # Maps
    # ------------------------------------------------------------------

    def get_map(self, map_id: str) -> Map:
        m = self.maps.get(map_id)
        if m is None:
            raise NotFound(f"Unknown map {map_id!r}", {"known": sorted(self.maps)})
        return m

    def map_detail(self, map_id: str) -> MapDetail:
        m = self.get_map(map_id)
        return MapDetail(
            **m.model_dump(),
            waypoints=self.list_waypoints(map_id),
            lanes=self.list_lanes(map_id),
            zones=self.list_zones(map_id),
        )

    def create_map(self, req: MapCreate) -> Map:
        map_id = req.id or self._unique_id(self.maps, req.name)
        if map_id in self.maps:
            raise Conflict(f"Map {map_id!r} already exists")
        m = Map(id=map_id, **req.model_dump(exclude={"id"}))
        self.maps[map_id] = m
        self._commit("map", "upsert", m, "maps")
        self._bus.emit(EventType.MAP_UPDATED, f"Map '{m.name}' created", map_id=map_id)
        return m

    def update_map(self, map_id: str, patch: MapPatch) -> Map:
        m = self.get_map(map_id)
        data = _merge(m, patch)
        data["updated_at"] = time.time()
        updated = Map.model_validate(data)
        if patch.bounds is not None:
            outside = [
                w.name for w in self.list_waypoints(map_id) if not updated.bounds.contains(w.x, w.y)
            ]
            if outside:
                raise Invalid(
                    "New bounds would leave waypoints outside the map",
                    {"waypoints": outside},
                )
        self.maps[map_id] = updated
        self._commit("map", "upsert", updated, "maps")
        return updated

    def map_references(self, map_id: str) -> dict[str, list[str]]:
        return {
            "fleets": [f.id for f in self.fleets.values() if map_id in f.map_ids],
            "robots": [r.id for r in self.robots.values() if r.navigation.map_id == map_id],
        }

    def delete_map(self, map_id: str) -> None:
        m = self.get_map(map_id)
        refs = self.map_references(map_id)
        if refs["fleets"] or refs["robots"]:
            raise Conflict(f"Map {map_id!r} is used by fleets/robots; reassign them first", refs)
        for z in self.list_zones(map_id):
            self.zones.pop(z.id, None)
            self._commit("zone", "delete", z, "zones")
        for ln in self.list_lanes(map_id):
            self.lanes.pop(ln.id, None)
            self._commit("lane", "delete", ln, "lanes")
        for w in self.list_waypoints(map_id):
            self.waypoints.pop(w.id, None)
            self._commit("waypoint", "delete", w, "waypoints")
        self.maps.pop(map_id)
        self._commit("map", "delete", m, "maps")
        self._bus.emit(EventType.MAP_UPDATED, f"Map '{m.name}' deleted", map_id=map_id)

    # ------------------------------------------------------------------
    # Waypoints
    # ------------------------------------------------------------------

    def list_waypoints(self, map_id: str | None = None, type: str | None = None) -> list[Waypoint]:
        return [
            w
            for w in self.waypoints.values()
            if (map_id is None or w.map_id == map_id) and (type is None or w.type == type)
        ]

    def get_waypoint(self, wp_id: str) -> Waypoint:
        w = self.waypoints.get(wp_id)
        if w is None:
            raise NotFound(f"Unknown waypoint {wp_id!r}")
        return w

    def _check_waypoint_name(self, map_id: str, name: str, exclude: str | None = None) -> None:
        key = norm_name(name)
        for w in self.list_waypoints(map_id):
            if w.id == exclude:
                continue
            if norm_name(w.name) == key or key in {norm_name(a) for a in w.aliases}:
                raise Conflict(f"A waypoint named {name!r} already exists on map {map_id!r}")

    def _check_in_bounds(self, map_id: str, x: float, y: float) -> None:
        m = self.get_map(map_id)
        if not m.bounds.contains(x, y):
            raise Invalid(
                f"({x:.2f}, {y:.2f}) is outside the workspace of map {map_id!r}",
                {"bounds": m.bounds.model_dump()},
            )

    def create_waypoint(self, map_id: str, req: WaypointCreate) -> Waypoint:
        self.get_map(map_id)
        self._check_in_bounds(map_id, req.x, req.y)
        self._check_waypoint_name(map_id, req.name)
        wp_id = req.id or self._unique_id(self.waypoints, req.name)
        if wp_id in self.waypoints:
            raise Conflict(f"Waypoint id {wp_id!r} already exists")
        w = Waypoint(id=wp_id, map_id=map_id, **req.model_dump(exclude={"id"}))
        self.waypoints[wp_id] = w
        self._commit("waypoint", "upsert", w, "waypoints", map_id)
        self._bus.emit(
            EventType.MAP_UPDATED,
            f"Waypoint '{w.name}' added at ({w.x:.2f}, {w.y:.2f})",
            map_id=map_id,
            data={"waypoint_id": wp_id},
        )
        return w

    def update_waypoint(self, wp_id: str, patch: WaypointPatch) -> Waypoint:
        w = self.get_waypoint(wp_id)
        updated = Waypoint.model_validate(_merge(w, patch))
        self._check_in_bounds(w.map_id, updated.x, updated.y)
        if patch.name is not None or patch.aliases is not None:
            self._check_waypoint_name(w.map_id, updated.name, exclude=wp_id)
        self.waypoints[wp_id] = updated
        self._commit("waypoint", "upsert", updated, "waypoints", w.map_id)
        if (w.x, w.y) != (updated.x, updated.y):
            msg = f"Waypoint '{updated.name}' moved to ({updated.x:.2f}, {updated.y:.2f})"
        elif w.name != updated.name:
            msg = f"Waypoint '{w.name}' renamed to '{updated.name}'"
        else:
            msg = f"Waypoint '{updated.name}' updated"
        self._bus.emit(EventType.MAP_UPDATED, msg, map_id=w.map_id, data={"waypoint_id": wp_id})
        return updated

    def delete_waypoint(self, wp_id: str, in_use_by: list[str] | None = None) -> None:
        w = self.get_waypoint(wp_id)
        if in_use_by:
            raise Conflict(f"Waypoint '{w.name}' is used by active tasks", {"tasks": in_use_by})
        spawners = [r.id for r in self.robots.values() if r.spawn.waypoint_id == wp_id]
        if spawners:
            raise Conflict(
                f"Waypoint '{w.name}' is the spawn point of robots", {"robots": spawners}
            )
        for ln in [ln for ln in self.lanes.values() if wp_id in (ln.from_id, ln.to_id)]:
            self.lanes.pop(ln.id)
            self._commit("lane", "delete", ln, "lanes", w.map_id)
        self.waypoints.pop(wp_id)
        self._commit("waypoint", "delete", w, "waypoints", w.map_id)
        self._bus.emit(EventType.MAP_UPDATED, f"Waypoint '{w.name}' deleted", map_id=w.map_id)

    def resolve_waypoint(self, ref: str, map_id: str | None = None) -> Waypoint:
        """Resolve an id, name or alias ("Loading Zone", "loading_zone") to a waypoint."""
        if not isinstance(ref, str) or not ref.strip():
            raise Invalid("Waypoint reference must be a non-empty string")
        ref = ref.strip()
        w = self.waypoints.get(ref)
        if w and (map_id is None or w.map_id == map_id):
            return w
        key = norm_name(ref)
        pool = self.list_waypoints(map_id)
        matches = [
            w
            for w in pool
            if key in {norm_name(w.name), norm_name(w.id)} | {norm_name(a) for a in w.aliases}
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise Conflict(
                f"Waypoint {ref!r} is ambiguous across maps; specify map_id",
                {"candidates": [f"{w.map_id}:{w.id}" for w in matches]},
            )
        names = sorted({w.name for w in pool})
        close = difflib.get_close_matches(ref, names, n=3, cutoff=0.5)
        raise NotFound(
            f"Unknown waypoint {ref!r}"
            + (f" on map {map_id!r}" if map_id else "")
            + (f". Did you mean: {', '.join(close)}?" if close else ""),
            {"valid_waypoints": names},
        )

    # ------------------------------------------------------------------
    # Lanes
    # ------------------------------------------------------------------

    def list_lanes(self, map_id: str | None = None) -> list[Lane]:
        return [ln for ln in self.lanes.values() if map_id is None or ln.map_id == map_id]

    def get_lane(self, lane_id: str) -> Lane:
        ln = self.lanes.get(lane_id)
        if ln is None:
            raise NotFound(f"Unknown lane {lane_id!r}")
        return ln

    def create_lane(self, map_id: str, req: LaneCreate) -> Lane:
        self.get_map(map_id)
        for end in (req.from_id, req.to_id):
            w = self.waypoints.get(end)
            if w is None or w.map_id != map_id:
                raise Invalid(f"Lane endpoint {end!r} is not a waypoint on map {map_id!r}")
        for ln in self.list_lanes(map_id):
            same = (ln.from_id, ln.to_id) == (req.from_id, req.to_id)
            rev = (ln.from_id, ln.to_id) == (req.to_id, req.from_id)
            if same or (rev and (ln.bidirectional or req.bidirectional)):
                raise Conflict(
                    f"A lane between {req.from_id!r} and {req.to_id!r} already exists ({ln.id})"
                )
        lane_id = req.id or self._unique_id(
            self.lanes, f"L_{req.from_id}_{req.to_id}", keep_case=True
        )
        if lane_id in self.lanes:
            raise Conflict(f"Lane id {lane_id!r} already exists")
        ln = Lane(id=lane_id, map_id=map_id, **req.model_dump(exclude={"id"}))
        self.lanes[lane_id] = ln
        self._commit("lane", "upsert", ln, "lanes", map_id)
        return ln

    def update_lane(self, lane_id: str, patch: LanePatch) -> Lane:
        ln = self.get_lane(lane_id)
        updated = Lane.model_validate(_merge(ln, patch))
        self.lanes[lane_id] = updated
        self._commit("lane", "upsert", updated, "lanes", ln.map_id)
        if patch.closed is not None and patch.closed != ln.closed:
            state = "closed" if updated.closed else "reopened"
            self._bus.emit(EventType.MAP_UPDATED, f"Lane {lane_id} {state}", map_id=ln.map_id)
        return updated

    def delete_lane(self, lane_id: str) -> None:
        ln = self.get_lane(lane_id)
        self.lanes.pop(lane_id)
        self._commit("lane", "delete", ln, "lanes", ln.map_id)

    # ------------------------------------------------------------------
    # Zones
    # ------------------------------------------------------------------

    def list_zones(self, map_id: str | None = None) -> list[Zone]:
        return [z for z in self.zones.values() if map_id is None or z.map_id == map_id]

    def get_zone(self, zone_id: str) -> Zone:
        z = self.zones.get(zone_id)
        if z is None:
            raise NotFound(f"Unknown zone {zone_id!r}")
        return z

    def create_zone(self, map_id: str, req: ZoneCreate) -> Zone:
        self.get_map(map_id)
        zone_id = req.id or self._unique_id(self.zones, req.name)
        if zone_id in self.zones:
            raise Conflict(f"Zone id {zone_id!r} already exists")
        z = Zone(id=zone_id, map_id=map_id, **req.model_dump(exclude={"id"}))
        self.zones[zone_id] = z
        self._commit("zone", "upsert", z, "zones", map_id)
        self._bus.emit(EventType.MAP_UPDATED, f"Zone '{z.name}' ({z.type}) added", map_id=map_id)
        return z

    def update_zone(self, zone_id: str, patch: ZonePatch) -> Zone:
        z = self.get_zone(zone_id)
        updated = Zone.model_validate(_merge(z, patch))
        self.zones[zone_id] = updated
        self._commit("zone", "upsert", updated, "zones", z.map_id)
        return updated

    def delete_zone(self, zone_id: str) -> None:
        z = self.get_zone(zone_id)
        self.zones.pop(zone_id)
        self._commit("zone", "delete", z, "zones", z.map_id)
        self._bus.emit(EventType.MAP_UPDATED, f"Zone '{z.name}' removed", map_id=z.map_id)

    # ------------------------------------------------------------------
    # Fleets
    # ------------------------------------------------------------------

    def get_fleet(self, fleet_id: str) -> Fleet:
        f = self.fleets.get(fleet_id)
        if f is None:
            raise NotFound(f"Unknown fleet {fleet_id!r}", {"known": sorted(self.fleets)})
        return f

    def fleet_robot_ids(self, fleet_id: str) -> list[str]:
        return [r.id for r in self.robots.values() if r.fleet_id == fleet_id]

    def _check_maps(self, map_ids: list[str]) -> None:
        unknown = [m for m in map_ids if m not in self.maps]
        if unknown:
            raise Invalid(f"Unknown map(s): {unknown}", {"known": sorted(self.maps)})

    def create_fleet(self, req: FleetCreate) -> Fleet:
        fleet_id = req.id or self._unique_id(self.fleets, req.name)
        if fleet_id in self.fleets:
            raise Conflict(f"Fleet {fleet_id!r} already exists")
        self._check_maps(req.map_ids)
        f = Fleet(id=fleet_id, **req.model_dump(exclude={"id"}))
        self.fleets[fleet_id] = f
        self._commit("fleet", "upsert", f, "fleets")
        self._bus.emit(
            EventType.FLEET_CREATED, f"Fleet '{f.name}' ({f.robot_type}) created", fleet_id=f.id
        )
        return f

    def update_fleet(self, fleet_id: str, patch: FleetPatch) -> Fleet:
        f = self.get_fleet(fleet_id)
        data = _merge(f, patch)
        data["updated_at"] = time.time()
        updated = Fleet.model_validate(data)
        self._check_maps(updated.map_ids)
        self.fleets[fleet_id] = updated
        self._commit("fleet", "upsert", updated, "fleets")
        self._bus.emit(
            EventType.FLEET_UPDATED, f"Fleet '{updated.name}' updated", fleet_id=fleet_id
        )
        return updated

    def delete_fleet(self, fleet_id: str) -> None:
        f = self.get_fleet(fleet_id)
        members = self.fleet_robot_ids(fleet_id)
        if members:
            raise Conflict(
                f"Fleet '{f.name}' still has robots; move or remove them first",
                {"robots": members},
            )
        self.fleets.pop(fleet_id)
        self._commit("fleet", "delete", f, "fleets")
        self._bus.emit(EventType.FLEET_REMOVED, f"Fleet '{f.name}' removed", fleet_id=fleet_id)

    # ------------------------------------------------------------------
    # Robots
    # ------------------------------------------------------------------

    def get_robot(self, robot_id: str) -> Robot:
        r = self.robots.get(robot_id)
        if r is None:
            raise NotFound(f"Unknown robot {robot_id!r}", {"known": sorted(self.robots)})
        return r

    def find_robot(self, ref: str) -> Robot:
        """Resolve an id or display name ("UGV-03", "ugv_03", "robot 3")."""
        if ref in self.robots:
            return self.robots[ref]
        key = norm_name(ref)
        matches = [r for r in self.robots.values() if key in (norm_name(r.id), norm_name(r.name))]
        if len(matches) == 1:
            return matches[0]
        names = sorted(r.name for r in self.robots.values())
        close = difflib.get_close_matches(ref, names, n=3, cutoff=0.5)
        raise NotFound(
            f"Unknown robot {ref!r}" + (f". Did you mean: {', '.join(close)}?" if close else ""),
            {"robots": names},
        )

    def _validate_robot(self, robot: Robot) -> None:
        fleet = self.get_fleet(robot.fleet_id)
        map_id = robot.navigation.map_id
        if map_id:
            self.get_map(map_id)
            if fleet.map_ids and map_id not in fleet.map_ids:
                raise Invalid(
                    f"Map {map_id!r} is not compatible with fleet '{fleet.name}'",
                    {"fleet_maps": fleet.map_ids},
                )
        effective_map = map_id or (fleet.map_ids[0] if fleet.map_ids else None)
        if robot.spawn.waypoint_id:
            w = self.waypoints.get(robot.spawn.waypoint_id)
            if w is None:
                raise Invalid(f"Spawn waypoint {robot.spawn.waypoint_id!r} does not exist")
            if effective_map and w.map_id != effective_map:
                raise Invalid(
                    f"Spawn waypoint {w.id!r} is on map {w.map_id!r}, robot operates on "
                    f"{effective_map!r}"
                )
            for z in self.list_zones(w.map_id):
                if z.type not in ("restricted", "no_fly") or robot.fleet_id in z.allowed_fleets:
                    continue
                if z.layer is not None and z.layer != w.layer:
                    continue
                if point_in_polygon((w.x, w.y), z.polygon):
                    raise Invalid(
                        f"Spawn waypoint '{w.name}' lies inside {z.type.replace('_', '-')} zone "
                        f"'{z.name}', which fleet '{fleet.name}' may not enter"
                    )
        if (
            robot.robot_type
            and robot.robot_type != fleet.robot_type
            and fleet.robot_type != "other"
        ):
            raise Invalid(
                f"Robot type {robot.robot_type!r} does not match fleet type {fleet.robot_type!r}"
            )

    def create_robot(self, req: RobotCreate) -> Robot:
        robot_id = req.id or self._unique_id(self.robots, req.name)
        if robot_id in self.robots:
            raise Conflict(f"Robot {robot_id!r} already exists")
        robot = Robot(id=robot_id, **req.model_dump(exclude={"id"}))
        self._validate_robot(robot)
        self.robots[robot_id] = robot
        self._commit("robot", "upsert", robot, "robots")
        fleet = self.fleets[robot.fleet_id]
        self._bus.emit(
            EventType.ROBOT_REGISTERED,
            f"Robot '{robot.name}' registered in fleet '{fleet.name}'",
            robot_id=robot_id,
            fleet_id=robot.fleet_id,
        )
        return robot

    def update_robot(self, robot_id: str, patch: RobotPatch) -> Robot:
        r = self.get_robot(robot_id)
        data = _merge(r, patch)
        data["updated_at"] = time.time()
        updated = Robot.model_validate(data)
        self._validate_robot(updated)
        self.robots[robot_id] = updated
        self._commit("robot", "upsert", updated, "robots")
        self._bus.emit(
            EventType.ROBOT_UPDATED,
            f"Robot '{updated.name}' updated",
            robot_id=robot_id,
            fleet_id=updated.fleet_id,
            data={"fields": sorted(patch.model_dump(exclude_unset=True))},
        )
        return updated

    def delete_robot(self, robot_id: str) -> None:
        r = self.get_robot(robot_id)
        self.robots.pop(robot_id)
        self._commit("robot", "delete", r, "robots")
        self._bus.emit(
            EventType.ROBOT_REMOVED,
            f"Robot '{r.name}' removed from fleet {r.fleet_id}",
            robot_id=robot_id,
            fleet_id=r.fleet_id,
        )

    def effective_config(self, robot_id: str) -> EffectiveRobotConfig:
        r = self.get_robot(robot_id)
        f = self.get_fleet(r.fleet_id)
        phys = r.physical
        return EffectiveRobotConfig(
            robot_type=r.robot_type or f.robot_type,
            capabilities=r.capabilities if r.capabilities is not None else list(f.capabilities),
            map_id=r.navigation.map_id or (f.map_ids[0] if f.map_ids else None),
            layer=f.layer,
            footprint=phys.footprint or f.footprint,
            max_speed=min(phys.max_speed or f.limits.max_speed, f.limits.max_speed),
            max_accel=min(phys.max_accel or f.limits.max_accel, f.limits.max_accel),
            max_decel=f.limits.max_decel,
            max_yaw_rate=f.limits.max_yaw_rate,
            cruise_altitude=f.limits.cruise_altitude,
            max_vertical_speed=f.limits.max_vertical_speed,
            payload_kg=phys.payload_kg,
            communication=r.communication or f.communication,
            nav_stack=r.navigation.stack or f.navigation.stack or NavStack.KINEMATIC_SIM,
            localization=r.navigation.localization or f.navigation.localization,
            priority=f.priority,
            battery=f.battery,
        )

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def import_map_seed(self, seed: MapSeed, replace: bool = False) -> Map:
        if seed.id and seed.id in self.maps:
            if not replace:
                return self.maps[seed.id]
            self.delete_map(seed.id)
        m = self.create_map(MapCreate(**seed.model_dump(exclude={"waypoints", "lanes", "zones"})))
        for w in seed.waypoints:
            self.create_waypoint(m.id, w)
        for ln in seed.lanes:
            self.create_lane(m.id, ln)
        for z in seed.zones:
            self.create_zone(m.id, z)
        logger.info(
            f"Imported map {m.id}: {len(seed.waypoints)} waypoints, "
            f"{len(seed.lanes)} lanes, {len(seed.zones)} zones"
        )
        return m

    def load_scenario(self, path: Path) -> dict[str, int]:
        """Import a scenario file: {"maps": [...files], "fleets": [...], "robots": [...]}."""
        spec = json.loads(path.read_text(encoding="utf-8"))
        base = path.parent
        counts = {"maps": 0, "fleets": 0, "robots": 0}
        for ref in spec.get("maps", []):
            map_path = (base / ref).resolve()
            seed = MapSeed.model_validate(json.loads(map_path.read_text(encoding="utf-8")))
            if seed.id not in self.maps:
                self.import_map_seed(seed)
                counts["maps"] += 1
        for raw in spec.get("fleets", []):
            req = FleetCreate.model_validate(raw)
            if req.id not in self.fleets:
                self.create_fleet(req)
                counts["fleets"] += 1
        for raw in spec.get("robots", []):
            req = RobotCreate.model_validate(raw)
            if req.id not in self.robots:
                self.create_robot(req)
                counts["robots"] += 1
        return counts
