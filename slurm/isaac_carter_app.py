"""
slurm/isaac_carter_app.py — Isaac Sim standalone app for the Nayantra demo.

Runs INSIDE the isaac-sim 4.5.0 container via /isaac-sim/python.sh:
  - points the asset root at the LOCAL asset packs (gpu02 has no internet)
  - enables WebRTC livestream (advertising the jump host's IP)
  - enables the ROS 2 bridge (bundled humble libs)
  - loads the Carter warehouse navigation scene
  - presses play and runs the sim loop forever

Launched by slurm/run_isaac_carter.sh — don't run directly.
Config via env: PUBLIC_IP, ASSETS_ROOT, SCENE_USD.
"""

import os
import sys
from pathlib import Path

PUBLIC_IP = os.environ.get("PUBLIC_IP", "172.25.60.80")
ASSETS_ROOT = os.environ.get("ASSETS_ROOT", "/home/shashank/isaac-assets/Assets/Isaac/4.5")
SCENE_USD = os.environ.get(
    "SCENE_USD", ASSETS_ROOT + "/Isaac/Samples/ROS2/Scenario/carter_warehouse_navigation.usd"
)

print(f"[carter_app] assets_root={ASSETS_ROOT}", flush=True)
print(f"[carter_app] scene={SCENE_USD}", flush=True)
print(f"[carter_app] public_ip={PUBLIC_IP}", flush=True)

if not Path(SCENE_USD).is_file():
    print(f"[carter_app] FATAL: scene USD not found: {SCENE_USD}", flush=True)
    sys.exit(2)

from isaacsim import SimulationApp  # noqa: E402

simulation_app = SimulationApp({"headless": True, "width": 1280, "height": 720})

import carb  # noqa: E402

settings = carb.settings.get_settings()
# Local assets BEFORE anything resolves paths against the cloud.
settings.set("/persistent/isaac/asset_root/default", ASSETS_ROOT)
# WebRTC livestream: ICE candidates must advertise the jump host.
settings.set("/app/livestream/publicEndpointAddress", PUBLIC_IP)
settings.set("/app/livestream/allowResize", True)

from isaacsim.core.utils.extensions import enable_extension  # noqa: E402

enable_extension("omni.kit.livestream.webrtc")
print("[carter_app] livestream extension enabled", flush=True)

# ROS 2 bridge — extension was renamed in 4.5; try new id then old.
for ext_id in ("isaacsim.ros2.bridge", "omni.isaac.ros2_bridge"):
    try:
        enable_extension(ext_id)
        print(f"[carter_app] ros2 bridge enabled: {ext_id}", flush=True)
        break
    except Exception as exc:
        print(f"[carter_app] {ext_id} not available: {exc}", flush=True)

simulation_app.update()

from isaacsim.core.utils.stage import open_stage  # noqa: E402

print("[carter_app] opening stage (first load takes a few minutes)...", flush=True)
open_stage(SCENE_USD)
for _ in range(20):
    simulation_app.update()
print("[carter_app] stage opened", flush=True)

import omni.timeline  # noqa: E402

timeline = omni.timeline.get_timeline_interface()
timeline.play()
print("[carter_app] timeline playing — ROS 2 graphs active. APP_READY", flush=True)

frame = 0
while simulation_app.is_running():
    simulation_app.update()
    frame += 1
    if frame % 3600 == 0:
        print(f"[carter_app] alive, frame={frame}", flush=True)

print("[carter_app] shutting down", flush=True)
simulation_app.close()
