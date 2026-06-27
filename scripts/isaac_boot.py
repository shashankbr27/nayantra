"""
scripts/isaac_boot.py

Headless Isaac Sim as a ROS 2 publisher (no viewport streaming).

Isaac runs as the physics/sensor simulator and publishes over ROS 2 (/clock, /tf,
/odom, etc.). You visualise on a viewer machine with RViz2, which renders locally
from those topics — full interactive 3D, no video encoding, no capture. This is
the right path when:
  - the host GPU lacks NVENC or RT cores (WebRTC streaming won't work), or
  - you want Nav2 / SLAM to drive the robot from ROS 2 commands, or
  - you want a thin, deterministic ROS 2 surface for an upstream agent stack.

If you instead want photoreal WebRTC streaming (RTX 6000 Ada / A6000-class GPU
with NVENC), use scripts/run_isaac_workstation.sh + scripts/isaac_demo.py.

What it does:
  1. SimulationApp headless (livestream off).
  2. Enables the ROS 2 bridge extension.
  3. Loads the warehouse + spawns Carter (assets from /isaac-assets, mounted).
  4. Builds an OmniGraph that publishes /clock and /tf for the robot.
  5. Builds a drive OmniGraph: subscribes /cmd_vel, drives Carter's wheels via
     DifferentialController, publishes /odom. This is what makes Nav2 -> robot
     motion actually work end-to-end. Disable with ENABLE_DIFF_DRIVE=0.
  6. Plays the timeline and steps forever.

Env (core):
  ROS_DOMAIN_ID      ROS 2 domain (default 0; must match your laptop)
  ROBOT_NAME         robot prim name (default carter)
  ROBOT_X / ROBOT_Y  spawn position
  SCENE_USD / ROBOT_USD / ISAAC_ASSETS_ROOT
  SELF_TEST=1        ground plane only (no external assets), still publishes TF

Env (drive graph — Carter v2 defaults shown; override per your USD):
  ENABLE_DIFF_DRIVE  0/1 (default 1; auto-skipped in SELF_TEST)
  CMD_VEL_TOPIC      default /cmd_vel
  ODOM_TOPIC         default /odom
  ODOM_FRAME_ID      default odom
  CHASSIS_FRAME_ID   default base_link
  WHEEL_RADIUS       default 0.14   (m, Nova Carter)
  WHEEL_DISTANCE     default 0.413  (m, Nova Carter wheel base)
  LEFT_WHEEL_JOINT   default joint_wheel_left
  RIGHT_WHEEL_JOINT  default joint_wheel_right
"""

from __future__ import annotations

import json
import os
import sys

# -----------------------------------------------------------------------------
# 1. SimulationApp — headless, no livestream.
# -----------------------------------------------------------------------------
try:
    from isaacsim import SimulationApp  # 4.5+
except ImportError:
    from omni.isaac.kit import SimulationApp  # <=4.2

simulation_app = SimulationApp(
    {
        "headless": True,
        "renderer": "RayTracedLighting",
    }
)


def say(msg: str) -> None:
    # Kit hijacks the logging module; raw print survives in `docker logs`.
    print(f"[isaac_boot] {msg}", flush=True)


# -----------------------------------------------------------------------------
# 2. Enable the ROS 2 bridge BEFORE other core imports that need it.
# -----------------------------------------------------------------------------
try:
    from isaacsim.core.utils.extensions import enable_extension  # 4.5
except ImportError:
    from omni.isaac.core.utils.extensions import enable_extension  # <=4.2

_ROS2_EXT = None
for ext in ("isaacsim.ros2.bridge", "omni.isaac.ros2_bridge"):
    try:
        if enable_extension(ext):
            _ROS2_EXT = ext
            break
    except Exception:
        continue
simulation_app.update()
say(f"ROS 2 bridge extension: {_ROS2_EXT or 'FAILED TO ENABLE'}")
say(
    f"ROS_DOMAIN_ID={os.getenv('ROS_DOMAIN_ID', '0')}  "
    f"RMW={os.getenv('RMW_IMPLEMENTATION', 'default')}"
)

# -----------------------------------------------------------------------------
# 3. Core imports
# -----------------------------------------------------------------------------
import omni.graph.core as og  # noqa: E402
import omni.timeline  # noqa: E402
import omni.usd  # noqa: E402

try:
    from omni.isaac.core import World  # type: ignore
    from omni.isaac.core.utils.nucleus import get_assets_root_path  # type: ignore
    from omni.isaac.core.utils.stage import add_reference_to_stage  # type: ignore
except ImportError:
    from isaacsim.core.api import World  # type: ignore
    from isaacsim.core.utils.nucleus import get_assets_root_path  # type: ignore
    from isaacsim.core.utils.stage import add_reference_to_stage  # type: ignore

# -----------------------------------------------------------------------------
# 4. Build the stage
# -----------------------------------------------------------------------------
assets_root = os.getenv("ISAAC_ASSETS_ROOT") or get_assets_root_path()
SCENE_USD = os.getenv(
    "SCENE_USD",
    f"{assets_root}/Isaac/Environments/Simple_Warehouse/warehouse.usd" if assets_root else "",
)
ROBOT_USD = os.getenv(
    "ROBOT_USD", f"{assets_root}/Isaac/Robots/Carter/nova_carter.usd" if assets_root else ""
)
ROBOT_NAME = os.getenv("ROBOT_NAME", "carter")
ROBOT_PRIM = f"/World/{ROBOT_NAME}"

world = World(stage_units_in_meters=1.0)
_stage = omni.usd.get_context().get_stage()
self_test = os.getenv("SELF_TEST", "").strip() in ("1", "true", "yes")

if self_test:
    say("SELF_TEST: ground plane only")
    world.scene.add_default_ground_plane()
    # A simple cube as a stand-in robot body so /tf has something to move.
    from pxr import Gf, UsdGeom  # type: ignore

    cube = UsdGeom.Cube.Define(_stage, ROBOT_PRIM)
    cube.GetSizeAttr().Set(1.0)
    UsdGeom.Xformable(cube).AddTranslateOp().Set(Gf.Vec3d(0, 0, 0.5))
else:
    if not assets_root:
        say("ERROR: no asset root. Set ISAAC_ASSETS_ROOT or SELF_TEST=1.")
        simulation_app.close()
        sys.exit(1)
    say(f"Loading scene: {SCENE_USD}")
    add_reference_to_stage(usd_path=SCENE_USD, prim_path="/World/Warehouse")
    say(f"Spawning {ROBOT_NAME} from {ROBOT_USD}")
    add_reference_to_stage(usd_path=ROBOT_USD, prim_path=ROBOT_PRIM)
    from pxr import Gf, UsdGeom  # type: ignore

    prim = _stage.GetPrimAtPath(ROBOT_PRIM)
    if prim and prim.IsValid():
        xf = UsdGeom.Xformable(prim)
        xf.ClearXformOpOrder()
        xf.AddTranslateOp().Set(
            Gf.Vec3d(float(os.getenv("ROBOT_X", "0")), float(os.getenv("ROBOT_Y", "0")), 0.0)
        )
    extras = os.getenv("EXTRA_ROBOTS_JSON", "").strip()
    if extras:
        try:
            for r in json.loads(extras):
                p = f"/World/{r['name']}"
                add_reference_to_stage(usd_path=ROBOT_USD, prim_path=p)
        except Exception as exc:
            say(f"EXTRA_ROBOTS_JSON: {exc}")

world.reset()


# -----------------------------------------------------------------------------
# 5. ROS 2 OmniGraph: publish /clock + /tf for the robot subtree.
#    Node type names differ slightly across versions; try new then old.
# -----------------------------------------------------------------------------
def _node_types():
    new = {
        "clock": "isaacsim.ros2.bridge.ROS2PublishClock",
        "tf": "isaacsim.ros2.bridge.ROS2PublishTransformTree",
        "simtime": "isaacsim.core.nodes.IsaacReadSimulationTime",
    }
    old = {
        "clock": "omni.isaac.ros2_bridge.ROS2PublishClock",
        "tf": "omni.isaac.ros2_bridge.ROS2PublishTransformTree",
        "simtime": "omni.isaac.core_nodes.IsaacReadSimulationTime",
    }
    return new if (_ROS2_EXT == "isaacsim.ros2.bridge") else old


nt = _node_types()
try:
    og.Controller.edit(
        {"graph_path": "/ROS2Graph", "evaluator_name": "execution"},
        {
            og.Controller.Keys.CREATE_NODES: [
                ("OnTick", "omni.graph.action.OnPlaybackTick"),
                ("SimTime", nt["simtime"]),
                ("PublishClock", nt["clock"]),
                ("PublishTF", nt["tf"]),
            ],
            og.Controller.Keys.CONNECT: [
                ("OnTick.outputs:tick", "PublishClock.inputs:execIn"),
                ("OnTick.outputs:tick", "PublishTF.inputs:execIn"),
                ("SimTime.outputs:simulationTime", "PublishClock.inputs:timeStamp"),
                ("SimTime.outputs:simulationTime", "PublishTF.inputs:timeStamp"),
            ],
            og.Controller.Keys.SET_VALUES: [
                ("PublishClock.inputs:topicName", "/clock"),
                ("PublishTF.inputs:targetPrims", [ROBOT_PRIM]),
            ],
        },
    )
    say("ROS 2 graph created: publishing /clock and /tf")
except Exception as exc:
    say(f"WARNING: ROS 2 graph setup failed: {exc}")
    say("The bridge is enabled but TF/clock publishing may be incomplete.")

# -----------------------------------------------------------------------------
# 5b. Drive OmniGraph: /cmd_vel -> wheels (DifferentialController) and publish
#     /odom. Without this, Nav2's NavigateToPose has no actuator on the
#     robot side -- the goal is accepted by Nav2 but the robot stays still.
#
#     The node types and the wheel parameters here are Nova Carter v2 defaults.
#     If you're using a different robot, override LEFT_WHEEL_JOINT /
#     RIGHT_WHEEL_JOINT / WHEEL_RADIUS / WHEEL_DISTANCE via env. Inspect joint
#     names with:  ros2 param get /robot_state_publisher robot_description
#     or in Isaac's Stage panel under {ROBOT_PRIM}/joints.
# -----------------------------------------------------------------------------
ENABLE_DIFF_DRIVE = os.getenv("ENABLE_DIFF_DRIVE", "1").strip() in ("1", "true", "yes")
if ENABLE_DIFF_DRIVE and not self_test:
    CMD_VEL_TOPIC = os.getenv("CMD_VEL_TOPIC", "/cmd_vel")
    ODOM_TOPIC = os.getenv("ODOM_TOPIC", "/odom")
    ODOM_FRAME_ID = os.getenv("ODOM_FRAME_ID", "odom")
    CHASSIS_FRAME_ID = os.getenv("CHASSIS_FRAME_ID", "base_link")
    WHEEL_RADIUS = float(os.getenv("WHEEL_RADIUS", "0.14"))
    WHEEL_DISTANCE = float(os.getenv("WHEEL_DISTANCE", "0.413"))
    LEFT_WHEEL_JOINT = os.getenv("LEFT_WHEEL_JOINT", "joint_wheel_left")
    RIGHT_WHEEL_JOINT = os.getenv("RIGHT_WHEEL_JOINT", "joint_wheel_right")

    # Pick node types by extension version (same dance as section 5).
    _new_drive = {
        "twist_sub": "isaacsim.ros2.bridge.ROS2SubscribeTwist",
        "diff_ctl": "isaacsim.robot.wheeled_robots.DifferentialController",
        "art_ctl": "isaacsim.core.nodes.IsaacArticulationController",
        "compute_odom": "isaacsim.core.nodes.IsaacComputeOdometry",
        "publish_odom": "isaacsim.ros2.bridge.ROS2PublishOdometry",
    }
    _old_drive = {
        "twist_sub": "omni.isaac.ros2_bridge.ROS2SubscribeTwist",
        "diff_ctl": "omni.isaac.wheeled_robots.DifferentialController",
        "art_ctl": "omni.isaac.core_nodes.IsaacArticulationController",
        "compute_odom": "omni.isaac.core_nodes.IsaacComputeOdometry",
        "publish_odom": "omni.isaac.ros2_bridge.ROS2PublishOdometry",
    }
    dnt = _new_drive if (_ROS2_EXT == "isaacsim.ros2.bridge") else _old_drive

    try:
        og.Controller.edit(
            {"graph_path": "/CarterDriveGraph", "evaluator_name": "execution"},
            {
                og.Controller.Keys.CREATE_NODES: [
                    ("OnTick", "omni.graph.action.OnPlaybackTick"),
                    ("SimTime", nt["simtime"]),
                    ("TwistSub", dnt["twist_sub"]),
                    ("BreakLin", "omni.graph.nodes.BreakVector3"),
                    ("BreakAng", "omni.graph.nodes.BreakVector3"),
                    ("DiffCtl", dnt["diff_ctl"]),
                    ("ArtCtl", dnt["art_ctl"]),
                    ("ComputeOdom", dnt["compute_odom"]),
                    ("PublishOdom", dnt["publish_odom"]),
                ],
                og.Controller.Keys.CONNECT: [
                    # Tick fan-out — every physics tick evaluates the whole chain
                    ("OnTick.outputs:tick", "TwistSub.inputs:execIn"),
                    ("OnTick.outputs:tick", "DiffCtl.inputs:execIn"),
                    ("OnTick.outputs:tick", "ArtCtl.inputs:execIn"),
                    ("OnTick.outputs:tick", "ComputeOdom.inputs:execIn"),
                    ("OnTick.outputs:tick", "PublishOdom.inputs:execIn"),
                    # Twist (Vec3) -> scalars -> DifferentialController
                    ("TwistSub.outputs:linearVelocity", "BreakLin.inputs:tuple"),
                    ("TwistSub.outputs:angularVelocity", "BreakAng.inputs:tuple"),
                    ("BreakLin.outputs:x", "DiffCtl.inputs:linearVelocity"),
                    ("BreakAng.outputs:z", "DiffCtl.inputs:angularVelocity"),
                    # DiffController -> ArticulationController -> wheels
                    ("DiffCtl.outputs:velocityCommand", "ArtCtl.inputs:velocityCommand"),
                    # ComputeOdometry -> PublishOdometry (with simulation timestamp)
                    ("SimTime.outputs:simulationTime", "PublishOdom.inputs:timeStamp"),
                    ("ComputeOdom.outputs:linearVelocity", "PublishOdom.inputs:linearVelocity"),
                    ("ComputeOdom.outputs:angularVelocity", "PublishOdom.inputs:angularVelocity"),
                    ("ComputeOdom.outputs:position", "PublishOdom.inputs:position"),
                    ("ComputeOdom.outputs:orientation", "PublishOdom.inputs:orientation"),
                ],
                og.Controller.Keys.SET_VALUES: [
                    ("TwistSub.inputs:topicName", CMD_VEL_TOPIC),
                    ("DiffCtl.inputs:wheelDistance", WHEEL_DISTANCE),
                    ("DiffCtl.inputs:wheelRadius", WHEEL_RADIUS),
                    ("ArtCtl.inputs:targetPrim", [ROBOT_PRIM]),
                    ("ArtCtl.inputs:jointNames", [LEFT_WHEEL_JOINT, RIGHT_WHEEL_JOINT]),
                    ("ComputeOdom.inputs:chassisPrim", [ROBOT_PRIM]),
                    ("PublishOdom.inputs:topicName", ODOM_TOPIC),
                    ("PublishOdom.inputs:odomFrameId", ODOM_FRAME_ID),
                    ("PublishOdom.inputs:chassisFrameId", CHASSIS_FRAME_ID),
                ],
            },
        )
        say(f"Drive graph created: {CMD_VEL_TOPIC} -> wheels, {ODOM_TOPIC} published")
        say(f"  wheels: radius={WHEEL_RADIUS} m, base={WHEEL_DISTANCE} m")
        say(f"  joints: [{LEFT_WHEEL_JOINT}, {RIGHT_WHEEL_JOINT}]")
    except Exception as exc:
        say(f"WARNING: drive graph setup failed: {exc}")
        say("The robot's TF frame will still publish (visible in RViz), but Nav2")
        say("goals will not move the robot. Common causes:")
        say("  - joint names don't match this USD (check Stage panel / robot_description)")
        say("  - node types missing (the wheeled_robots ext didn't load)")
        say("  - run with ENABLE_DIFF_DRIVE=0 to disable cleanly")
elif self_test:
    say("ENABLE_DIFF_DRIVE skipped in SELF_TEST mode (no articulated robot).")
else:
    say("ENABLE_DIFF_DRIVE=0 — robot's TF will publish but won't drive on /cmd_vel.")

# -----------------------------------------------------------------------------
# 6. Play and step forever — this drives the graph (OnPlaybackTick).
# -----------------------------------------------------------------------------
omni.timeline.get_timeline_interface().play()
world.reset()
say("ROS2_READY: Isaac is headless and publishing ROS 2. Connect RViz from your laptop.")
if ENABLE_DIFF_DRIVE and not self_test:
    say(
        f"  Publishes: /clock, /tf, {os.getenv('ODOM_TOPIC', '/odom')}   "
        f"Subscribes: {os.getenv('CMD_VEL_TOPIC', '/cmd_vel')}   (robot prim: {ROBOT_PRIM})"
    )
    say("  Drive it: ros2 topic pub /cmd_vel geometry_msgs/Twist '{linear: {x: 0.2}}' --once")
else:
    say(f"  Publishes: /clock, /tf   (robot prim: {ROBOT_PRIM})")

step = 0
try:
    while simulation_app.is_running():
        # render=False: we publish ROS 2 (TF/odom/scan via physics) and visualise
        # in RViz on the laptop, so no GPU rendering is needed here. This also
        # sidesteps the H200's missing RT cores (only the RTX renderer needs them)
        # and runs far faster.
        world.step(render=False)
        step += 1
        if step % 600 == 0:
            say(f"sim running, step {step}")
except KeyboardInterrupt:
    pass
finally:
    simulation_app.close()
