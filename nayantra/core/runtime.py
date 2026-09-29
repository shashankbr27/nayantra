"""
nayantra/core/runtime.py

NayantraCore — wires the control plane together and owns its lifecycle.

    DocumentStore ─ EventBus ─ WorldRegistry ─ GraphCache
                                   │               │
            TaskManager ── FleetManager ── TrafficCoordinator
                                   │
                        adapters (SimEngine, Nav2, …)
    Confirmations · SafetyGuard
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from nayantra.config import settings
from nayantra.core.adapters import SimEngine
from nayantra.core.adapters.ros2_runtime import Ros2Runtime
from nayantra.core.confirmations import Confirmations
from nayantra.core.events import EventBus
from nayantra.core.fleet import FleetManager
from nayantra.core.models import (
    TERMINAL_TASK_STATUSES,
    EventType,
    TaskCreate,
)
from nayantra.core.routing import GraphCache
from nayantra.core.safety import SafetyGuard
from nayantra.core.store import DocumentStore
from nayantra.core.tasks import TaskManager
from nayantra.core.traffic import TrafficCoordinator
from nayantra.core.world import WorldRegistry

logger = logging.getLogger("nayantra.core")

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"
VERSION = "2.0.0"


def scenario_path(name: str) -> Path:
    p = Path(name)
    if p.suffix == ".json" and p.exists():
        return p
    return CONFIG_DIR / "scenarios" / f"{name}.json"


class NayantraCore:
    def __init__(self, db_path: str | None = None, scenario: str | None = None) -> None:
        self.store = DocumentStore(db_path if db_path is not None else settings.NAYANTRA_DB_PATH)
        self.bus = EventBus(self.store)
        self.registry = WorldRegistry(self.store, self.bus)
        self.graphs = GraphCache(self.registry)
        self.traffic = TrafficCoordinator(self.graphs, self.bus, names=self.robot_name)
        self.sim = SimEngine(self.bus)
        self.safety = SafetyGuard(self.registry)
        self.tasks = TaskManager(self.registry, self.bus, self.store)
        self.fleet = FleetManager(
            self.registry, self.bus, self.graphs, self.traffic, self.sim, self.safety, self.tasks
        )
        self.tasks.attach(self.fleet)
        self.confirmations = Confirmations(self.bus)
        self.scenario = settings.NAYANTRA_SCENARIO if scenario is None else scenario
        self.started_at = time.time()
        self._register_confirmations()

    def robot_name(self, robot_id: str) -> str:
        r = self.registry.robots.get(robot_id)
        return r.name if r else robot_id

    # ------------------------------------------------------------------
    def seed(self) -> dict[str, int] | None:
        """Load the configured scenario into an empty registry (first run only)."""
        if not self.scenario or self.registry.maps or self.registry.fleets:
            return None
        path = scenario_path(self.scenario)
        if not path.exists():
            logger.warning(f"Scenario {self.scenario!r} not found at {path}; starting empty")
            return None
        counts = self.registry.load_scenario(path)
        self.store.set_meta("scenario", self.scenario)
        self.bus.emit(
            EventType.SYSTEM,
            f"Seeded scenario '{self.scenario}': {counts['maps']} maps, {counts['fleets']} fleets, "
            f"{counts['robots']} robots",
        )
        return counts

    async def start(self) -> None:
        self.seed()
        await self.sim.start_loop()
        await self.fleet.start()
        await self.tasks.start()
        self.bus.emit(
            EventType.SYSTEM,
            f"Nayantra core {VERSION} started: {len(self.registry.fleets)} fleets, "
            f"{len(self.registry.robots)} robots, {len(self.registry.maps)} maps",
        )

    async def stop(self) -> None:
        await self.tasks.stop()
        await self.fleet.stop()
        await self.sim.stop_loop()
        rt = Ros2Runtime.peek()
        if rt:
            rt.shutdown()
        self.store.close()

    async def reset(self, scenario: str | None = None) -> dict[str, int] | None:
        """Wipe the registry and re-seed (used by the demo reset button)."""
        await self.fleet.stop()
        for rid in list(self.traffic.plans) + list(self.traffic.parked):
            self.traffic.remove_robot(rid)
        self.fleet.runtimes.clear()
        self.store.clear()
        self.tasks.tasks.clear()
        self.registry.__init__(self.store, self.bus)  # reload (empty)
        self.graphs.invalidate()
        if scenario is not None:
            self.scenario = scenario
        counts = self.seed()
        self.fleet._started = False
        await self.fleet.start()
        self.bus.publish("snapshot_required", {})
        return counts

    # ------------------------------------------------------------------
    # Dangerous actions (executed only after operator confirmation)
    # ------------------------------------------------------------------

    def _register_confirmations(self) -> None:
        async def stop_all(p: dict) -> Any:
            ids = self._scope(p.get("fleet_id"))
            for rid in ids:
                await self.fleet.stop_robot(rid, p.get("reason") or "Stop all (confirmed)")
            return {"stopped": ids}

        async def estop_all(p: dict) -> Any:
            ids = self._scope(p.get("fleet_id"))
            for rid in ids:
                await self.fleet.emergency_stop(
                    rid, p.get("reason") or "Emergency stop all (confirmed)"
                )
            return {"emergency_stopped": ids}

        async def remove_robot(p: dict) -> Any:
            self.registry.delete_robot(p["robot_id"])
            return {"removed": p["robot_id"]}

        async def delete_map(p: dict) -> Any:
            self.registry.delete_map(p["map_id"])
            return {"deleted": p["map_id"]}

        async def restricted_task(p: dict) -> Any:
            req = TaskCreate.model_validate({**p["request"], "allow_restricted": True})
            created = self.tasks.create(req, count=int(p.get("count", 1)))
            return {"tasks": [t.id for t in created]}

        async def cancel_all(p: dict) -> Any:
            ids = [
                t.id for t in self.tasks.tasks.values() if t.status not in TERMINAL_TASK_STATUSES
            ]
            for tid in ids:
                await self.tasks.cancel(tid, "Cancel all tasks (confirmed)")
            return {"cancelled": ids}

        self.confirmations.register("stop_all", stop_all)
        self.confirmations.register("estop_all", estop_all)
        self.confirmations.register("remove_robot", remove_robot)
        self.confirmations.register("delete_map", delete_map)
        self.confirmations.register("restricted_task", restricted_task)
        self.confirmations.register("cancel_all_tasks", cancel_all)

    def _scope(self, fleet_id: str | None) -> list[str]:
        if fleet_id:
            self.registry.get_fleet(fleet_id)
            return self.registry.fleet_robot_ids(fleet_id)
        return list(self.registry.robots)

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def robot_view(self, robot_id: str) -> dict[str, Any]:
        robot = self.registry.get_robot(robot_id)
        rt = self.fleet.runtimes.get(robot_id)
        return {
            **robot.model_dump(mode="json"),
            "effective": self.registry.effective_config(robot_id).model_dump(mode="json"),
            "state": rt.state.model_dump(mode="json") if rt else None,
            "checks": rt.checks if rt else [],
        }

    def fleet_stats(self, fleet_id: str) -> dict[str, Any]:
        ids = self.registry.fleet_robot_ids(fleet_id)
        stats = {
            "total": len(ids),
            "online": 0,
            "busy": 0,
            "idle": 0,
            "charging": 0,
            "error": 0,
            "offline": 0,
        }
        for rid in ids:
            rt = self.fleet.runtimes.get(rid)
            st = rt.state if rt else None
            if st is None or not st.online:
                stats["offline"] += 1
                continue
            stats["online"] += 1
            if st.mode in ("error", "emergency_stop"):
                stats["error"] += 1
            elif st.mode == "charging" and not st.current_task_id:
                stats["charging"] += 1
            elif st.current_task_id:
                stats["busy"] += 1
            else:
                stats["idle"] += 1
        stats["utilization"] = round(stats["busy"] / stats["online"], 3) if stats["online"] else 0.0
        return stats

    def fleet_view(self, fleet_id: str) -> dict[str, Any]:
        f = self.registry.get_fleet(fleet_id)
        return {
            **f.model_dump(mode="json"),
            "robots": self.registry.fleet_robot_ids(fleet_id),
            "stats": self.fleet_stats(fleet_id),
        }

    def snapshot(self) -> dict[str, Any]:
        tasks = sorted(self.tasks.tasks.values(), key=lambda t: t.created_at, reverse=True)
        open_tasks = [t for t in tasks if t.status not in TERMINAL_TASK_STATUSES]
        recent = [t for t in tasks if t.status in TERMINAL_TASK_STATUSES][:40]
        return {
            "version": VERSION,
            "maps": [
                self.registry.map_detail(m).model_dump(mode="json") for m in self.registry.maps
            ],
            "fleets": [self.fleet_view(f) for f in self.registry.fleets],
            "robots": [self.robot_view(r) for r in self.registry.robots],
            "tasks": [t.model_dump(mode="json") for t in open_tasks + recent],
            "traffic": self.traffic.snapshot().model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in self.bus.recent(150)],
            "alerts": [a.model_dump(mode="json") for a in self.bus.alerts()],
            "confirmations": [c.model_dump(mode="json") for c in self.confirmations.list()],
            "sim": self.sim.status(),
        }
