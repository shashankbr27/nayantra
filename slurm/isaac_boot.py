"""
slurm/isaac_boot.py

Standalone Isaac Sim startup script.

What it does, in order:
  1. Bootstraps SimulationApp with WebRTC livestream enabled (livestream=2).
  2. Resolves the Isaac assets root (Nucleus or local mirror).
  3. Adds the Simple_Warehouse USD to the stage.
  4. Spawns one or more robots at configurable poses.
  5. Steps the simulation forever so the WebRTC stream stays alive.

Override anything via environment variables before launching:
    SCENE_USD          : full USD path to load instead of Simple_Warehouse
    ROBOT_USD          : full USD path for the spawned robot
    ROBOT_NAME         : prim name (defaults to "turtlebot3_1")
    ROBOT_X, ROBOT_Y   : initial XY position in metres
    EXTRA_ROBOTS_JSON  : JSON list of {name, x, y} for additional robots
    RENDER_WIDTH/HEIGHT, RENDER_DT

Run from slurm/isaac_warehouse.slurm.  To run by hand inside an interactive
allocation:
    cd /isaac-sim
    ./python.sh /workspace/slurm/isaac_boot.py
"""

from __future__ import annotations

import json
import logging
import os
import sys

# -----------------------------------------------------------------------------
# 1. SimulationApp MUST be created before any other omni.* import.
#    livestream=2 enables WebRTC (the same path runheadless.webrtc.sh uses).
# -----------------------------------------------------------------------------
try:
    from isaacsim import SimulationApp  # Isaac Sim 4.5+
except ImportError:
    from omni.isaac.kit import SimulationApp  # 4.2 and earlier

CONFIG = {
    "headless": True,
    "livestream": 2,  # 0=off, 1=native streaming, 2=WebRTC
    "renderer": "RayTracedLighting",
    "width": int(os.getenv("RENDER_WIDTH", "1280")),
    "height": int(os.getenv("RENDER_HEIGHT", "720")),
    "anti_aliasing": 3,
}

simulation_app = SimulationApp(CONFIG)

# -----------------------------------------------------------------------------
# 2. Omni / Isaac imports (only valid AFTER SimulationApp is up)
# -----------------------------------------------------------------------------
import omni.usd  # noqa: E402

try:
    from omni.isaac.core import World  # type: ignore  # 4.2+
    from omni.isaac.core.utils.nucleus import get_assets_root_path  # type: ignore
    from omni.isaac.core.utils.stage import add_reference_to_stage  # type: ignore
except ImportError:
    # Some 4.5 builds renamed under isaacsim.* — try both.
    from isaacsim.core.api import World  # type: ignore
    from isaacsim.core.utils.nucleus import get_assets_root_path  # type: ignore
    from isaacsim.core.utils.stage import add_reference_to_stage  # type: ignore

logging.basicConfig(level=logging.INFO, format="[isaac_boot] %(message)s")
log = logging.getLogger("isaac_boot")

# -----------------------------------------------------------------------------
# 3. Resolve assets root (Nucleus URL, local mirror, or override)
# -----------------------------------------------------------------------------
assets_root = os.getenv("ISAAC_ASSETS_ROOT") or get_assets_root_path()
if not assets_root:
    log.error(
        "Could not resolve Isaac asset root. "
        "Set ISAAC_ASSETS_ROOT (e.g. omniverse://server/NVIDIA/Assets/Isaac/4.5) "
        "or configure Nucleus."
    )
    simulation_app.close()
    sys.exit(1)
log.info(f"Asset root: {assets_root}")

SCENE_USD = os.getenv(
    "SCENE_USD",
    f"{assets_root}/Isaac/Environments/Simple_Warehouse/warehouse.usd",
)
ROBOT_USD = os.getenv(
    "ROBOT_USD",
    f"{assets_root}/Isaac/Robots/Turtlebot/turtlebot3_burger.usd",
)

# -----------------------------------------------------------------------------
# 4. Build the stage: warehouse + robot(s)
# -----------------------------------------------------------------------------
world = World(stage_units_in_meters=1.0)

log.info(f"Loading scene: {SCENE_USD}")
add_reference_to_stage(usd_path=SCENE_USD, prim_path="/World/Warehouse")


def _spawn(prim_name: str, x: float = 0.0, y: float = 0.0, z: float = 0.0) -> None:
    """Spawn one robot as a USD reference at the given pose."""
    prim_path = f"/World/{prim_name}"
    log.info(f"Spawning robot {prim_name} at ({x:.2f}, {y:.2f}) from {ROBOT_USD}")
    add_reference_to_stage(usd_path=ROBOT_USD, prim_path=prim_path)

    # Translate the reference so it doesn't sit on top of the warehouse origin.
    stage = omni.usd.get_context().get_stage()
    prim = stage.GetPrimAtPath(prim_path)
    if prim and prim.IsValid():
        from pxr import Gf, UsdGeom  # type: ignore

        xform = UsdGeom.Xformable(prim)
        # Clear any default ops, then set a single translate op.
        xform.ClearXformOpOrder()
        xform.AddTranslateOp().Set(Gf.Vec3d(float(x), float(y), float(z)))


# Default robot
_spawn(
    os.getenv("ROBOT_NAME", "turtlebot3_1"),
    x=float(os.getenv("ROBOT_X", "0.0")),
    y=float(os.getenv("ROBOT_Y", "0.0")),
)

# Optional additional robots via JSON env
extras = os.getenv("EXTRA_ROBOTS_JSON", "").strip()
if extras:
    try:
        for r in json.loads(extras):
            _spawn(r["name"], x=float(r.get("x", 0)), y=float(r.get("y", 0)))
    except Exception as exc:
        log.error(f"Could not parse EXTRA_ROBOTS_JSON: {exc}")

# Reset to initialise physics handles, etc.
world.reset()
log.info("Stage ready. WebRTC stream is live; connect with the Isaac Sim WebRTC Client.")

# -----------------------------------------------------------------------------
# 5. Run forever (until the WebRTC client kills us, the job's walltime ends,
#    or someone Ctrl+Cs the script).
# -----------------------------------------------------------------------------
RENDER_DT = float(os.getenv("RENDER_DT", "0.0"))  # 0 = render as fast as possible
try:
    while simulation_app.is_running():
        world.step(render=True)
except KeyboardInterrupt:
    log.info("Interrupted; shutting down cleanly.")
finally:
    simulation_app.close()
