"""
nayantra/rmf_bridge/server.py

Compatibility launcher for the old "RMF bridge" (a Nayantra-native,
rmf-web-shaped REST server for ONE Nav2 robot; never Open-RMF itself).

It now starts the Nayantra Core with a single robot described by the legacy
settings, so everything that pointed at the bridge keeps working (:8000,
/fleets, /tasks/dispatch_task, /building_map …). The robot also gets traffic
coordination, the operator UI at /, and the /api/v1 API:

    FLEET_NAME    fleet id           (default warehouse_fleet)
    ROBOT_NAME    robot id           (default carter1)
    ROS2_ENABLED  true  → Nav2 over ROS 2 (Isaac Sim / hardware)
                  false → the built-in kinematic simulator

The map is config/maps/isaac_warehouse.json, the consolidated waypoint table
that used to be split across the bridge, isaac_demo.py and nav_cli.py. State is
in-memory, as before; use `nayantra-core` for the persistent multi-fleet setup.

Run:
    python -m nayantra.rmf_bridge.server
"""

from __future__ import annotations

import json
import logging

from nayantra.config import settings
from nayantra.core.models import FleetCreate, MapSeed, RobotCreate
from nayantra.core.runtime import CONFIG_DIR, NayantraCore
from nayantra.core.server import create_app

logger = logging.getLogger("nayantra.rmf_bridge")


def build_core() -> NayantraCore:
    core = NayantraCore(db_path=":memory:", scenario="")
    seed = MapSeed.model_validate(
        json.loads((CONFIG_DIR / "maps" / "isaac_warehouse.json").read_text(encoding="utf-8"))
    )
    core.registry.import_map_seed(seed)
    spec = json.loads((CONFIG_DIR / "scenarios" / "isaac_carter.json").read_text(encoding="utf-8"))
    fleet = spec["fleets"][0] | {"id": settings.FLEET_NAME}
    robot = spec["robots"][0] | {
        "id": settings.ROBOT_NAME,
        "name": settings.ROBOT_NAME,
        "fleet_id": settings.FLEET_NAME,
    }
    if not settings.ROS2_ENABLED:
        fleet["communication"] = {"protocol": "simulation"}
        fleet["navigation"] = {"stack": "kinematic_sim", "localization": "ground_truth"}
    core.registry.create_fleet(FleetCreate.model_validate(fleet))
    core.registry.create_robot(RobotCreate.model_validate(robot))
    return core


def main() -> None:
    import uvicorn

    logging.basicConfig(
        level=getattr(logging, settings.LOGGING_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger.info(
        f"RMF bridge (compat) → Nayantra Core on {settings.RMF_BRIDGE_HOST}:{settings.RMF_BRIDGE_PORT} "
        f"(ros2={settings.ROS2_ENABLED}, fleet={settings.FLEET_NAME}, robot={settings.ROBOT_NAME})"
    )
    uvicorn.run(
        create_app(build_core()), host=settings.RMF_BRIDGE_HOST, port=settings.RMF_BRIDGE_PORT
    )


if __name__ == "__main__":
    main()
