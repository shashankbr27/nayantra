"""
scripts/isaac_boot.py — Isaac Sim as a ROS 2 publisher for Nav2, on a GPU workstation.

Isaac runs as the physics/sensor simulator: it publishes over ROS 2 (/clock, /tf,
/odom) and subscribes /cmd_vel, so Nav2 can drive the robot. Two view modes:

  STREAM=0 (default) — headless, no rendering. Visualise with RViz2 on a viewer
                       machine, which renders locally from /tf etc. Lightest weight.
  STREAM=1           — render + WebRTC-stream the sim (via the stock streaming
                       experience) so you can watch the photoreal warehouse in the
                       Isaac WebRTC client. Set PUBLIC_IP for remote/VPN clients.

What it does:
  1. SimulationApp (headless; STREAM=1 adds the WebRTC streaming experience).
  2. Enables the ROS 2 bridge extension.
  3. Loads the warehouse + spawns Carter (carter_v1 by default).
  4. Builds an OmniGraph that publishes /clock and /tf.
  5. Builds a drive OmniGraph: /cmd_vel -> DifferentialController -> wheels, and
     publishes /odom. This is what makes Nav2 -> robot motion work end-to-end.
     Disable with ENABLE_DIFF_DRIVE=0.
  6. Plays the timeline and steps forever.

Env (core):
  STREAM             0/1 — WebRTC-stream the viewport (default 0 = headless/RViz)
  PUBLIC_IP          public IP for the WebRTC client (needed over a VPN)
  CAM_EYE / CAM_TARGET  viewport camera pose in STREAM mode (comma-separated x,y,z)
  ROS_DOMAIN_ID      ROS 2 domain (default 0; must match your viewer)
  ROBOT_NAME         robot prim name (default carter)
  ROBOT_X / ROBOT_Y  spawn position
  SCENE_USD / ROBOT_USD / ISAAC_ASSETS_ROOT
  SELF_TEST=1        ground plane only (no external assets), still publishes TF

Env (lidar — obstacle avoidance; needs carter_v1_physx_lidar.usd or similar):
  LIDAR              0/1 (default 0) — publish the robot's PhysX lidar as /scan
  LIDAR_TOPIC        default /scan
  LIDAR_FRAME_ID     default carter_lidar (must match the base_link->lidar
                     static TF published by nav2_demo.launch.py)
  LIDAR_PRIM         explicit lidar prim path (default: auto-discover)

Env (drive graph — carter_v1 defaults; override per your USD):
  ENABLE_DIFF_DRIVE  0/1 (default 1; auto-skipped in SELF_TEST)
  CMD_VEL_TOPIC      default /cmd_vel
  ODOM_TOPIC         default /odom
  ODOM_FRAME_ID      default odom
  CHASSIS_FRAME_ID   default base_link
  WHEEL_RADIUS       default 0.14
  WHEEL_DISTANCE     default 0.413
  LEFT_WHEEL_JOINT   default left_wheel
  RIGHT_WHEEL_JOINT  default right_wheel
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# -----------------------------------------------------------------------------
# 1. SimulationApp — headless, no livestream.
# -----------------------------------------------------------------------------
try:
    from isaacsim import SimulationApp  # 4.5+
except ImportError:
    from omni.isaac.kit import SimulationApp  # <=4.2

# STREAM=1 -> render + WebRTC-stream the sim so you can SEE it in the Isaac
# WebRTC client. It launches the stock streaming experience
# (isaacsim.exp.full.streaming.kit), which correctly enables
# omni.kit.livestream.{core,webrtc,app} — the `.app` one is what binds :49100
# (our earlier hand-rolled livestream:2 missed it). Set PUBLIC_IP to the address
# your client reaches this box at (needed over a VPN). STREAM=0 (default) is
# headless/no-render for the RViz path.
_STREAM = os.getenv("STREAM", "0").strip().lower() in ("1", "true", "yes")
if _STREAM:
    import isaacsim as _isaacsim_pkg

    _apps_dir = Path(_isaacsim_pkg.__file__).parent / "apps"
    _experience = os.getenv("STREAM_EXPERIENCE", str(_apps_dir / "isaacsim.exp.full.streaming.kit"))
    simulation_app = SimulationApp(
        {
            "headless": True,
            "width": int(os.getenv("RENDER_WIDTH", "1280")),
            "height": int(os.getenv("RENDER_HEIGHT", "720")),
        },
        experience=_experience,
    )
else:
    simulation_app = SimulationApp({"headless": True, "renderer": "RayTracedLighting"})


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

# LIDAR=1 -> publish the robot's PhysX lidar as /scan so Nav2's costmap obstacle
# layer can see (and avoid) shelves/props. Requires a robot USD that carries a
# Lidar prim (carter_v1_physx_lidar.usd — run_demo.sh selects it automatically).
# Default 0: current no-lidar behaviour is untouched. The sensor extension must
# be enabled BEFORE the stage composes, or the Lidar prim type is unknown.
ENABLE_LIDAR = os.getenv("LIDAR", "0").strip().lower() in ("1", "true", "yes")
_LIDAR_EXT = None
if ENABLE_LIDAR:
    for ext in ("isaacsim.sensors.physx", "omni.isaac.range_sensor"):
        try:
            if enable_extension(ext):
                _LIDAR_EXT = ext
                break
        except Exception:  # noqa: BLE001
            continue
    simulation_app.update()
    say(f"lidar sensor extension: {_LIDAR_EXT or 'FAILED TO ENABLE (no /scan)'}")

# -----------------------------------------------------------------------------
# 3. Core imports
# -----------------------------------------------------------------------------
import importlib  # noqa: E402

import omni.graph.core as og  # noqa: E402
import omni.timeline  # noqa: E402
import omni.usd  # noqa: E402
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics  # noqa: E402

# World moved to isaacsim.core.api in 4.5+ (was omni.isaac.core in <=4.2).
try:
    from isaacsim.core.api import World  # type: ignore  # 4.5+
except ImportError:
    from omni.isaac.core import World  # type: ignore  # <=4.2

# get_assets_root_path moved across versions:
#   6.0 -> isaacsim.storage.native ; 4.5 -> isaacsim.core.utils.nucleus ;
#   <=4.2 -> omni.isaac.core.utils.nucleus. Try them in order.
get_assets_root_path = None
for _m in (
    "isaacsim.storage.native",
    "isaacsim.core.utils.nucleus",
    "omni.isaac.core.utils.nucleus",
):
    try:
        get_assets_root_path = importlib.import_module(_m).get_assets_root_path
        break
    except Exception:  # noqa: BLE001
        continue
if get_assets_root_path is None:

    def get_assets_root_path():  # type: ignore
        return None


def _add_ref(stage, usd_path: str, prim_path: str):
    """Reference a USD asset so its full subtree composes. Prefer Isaac's loader
    (resolves the asset's defaultPrim + forces payloads via the omni resolver);
    fall back to raw pxr + an explicit payload load. The isaacsim stage helper
    was renamed/moved across versions and its kwarg changed (prim_path -> path),
    so we probe all known module names and both signatures."""
    for modname in (
        "isaacsim.core.utils.stage",
        "isaacsim.core.experimental.utils.stage",
        "omni.isaac.core.utils.stage",
    ):
        try:
            fn = importlib.import_module(modname).add_reference_to_stage
        except Exception:  # noqa: BLE001
            continue
        for kwargs in (
            {"usd_path": usd_path, "prim_path": prim_path},
            {"usd_path": usd_path, "path": prim_path},
        ):
            try:
                fn(**kwargs)
                say(f"referenced {prim_path} via {modname}")
                return stage.GetPrimAtPath(prim_path)
            except TypeError:
                continue  # wrong kwarg name for this version; try the other
            except Exception as exc:  # noqa: BLE001
                say(f"{modname} add_reference failed: {exc}")
                break
    # Fallback: raw pxr reference, then force payloads to load so the subtree
    # actually populates (default load rules may leave them unloaded).
    prim = stage.DefinePrim(prim_path, "Xform")
    prim.GetReferences().AddReference(usd_path)
    try:
        stage.Load(prim.GetPath())
    except Exception:  # noqa: BLE001
        pass
    say(f"referenced {prim_path} via pxr fallback")
    return prim


def _first_resolvable(paths, label):
    """First USD path the resolver can open; else first usable candidate."""
    cands = [p for p in paths if p and "None/" not in p]
    for p in cands:
        try:
            if Sdf.Layer.FindOrOpen(p) is not None:
                say(f"{label} -> {p}")
                return p
        except Exception:  # noqa: BLE001
            continue
    if cands:
        say(f"WARNING: no {label} candidate resolved; trying {cands[0]!r}")
        return cands[0]
    return ""


# -----------------------------------------------------------------------------
# 4. Build the stage
# -----------------------------------------------------------------------------
assets_root = os.getenv("ISAAC_ASSETS_ROOT") or get_assets_root_path()
say(f"assets_root = {assets_root}")
SCENE_USD = _first_resolvable(
    [
        os.getenv("SCENE_USD", ""),
        f"{assets_root}/Isaac/Environments/Simple_Warehouse/warehouse.usd",
    ],
    "SCENE_USD",
)
# Nova Carter moved under a vendor folder in 6.0 (Isaac/Robots/NVIDIA/Carter/...).
ROBOT_USD = _first_resolvable(
    [
        os.getenv("ROBOT_USD", ""),
        f"{assets_root}/Isaac/Robots/NVIDIA/Carter/nova_carter/nova_carter.usd",
        f"{assets_root}/Isaac/Robots/NVIDIA/Carter/nova_carter.usd",
        f"{assets_root}/Isaac/Robots/Carter/nova_carter.usd",
    ],
    "ROBOT_USD",
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
    if not SCENE_USD or not ROBOT_USD:
        say(
            "ERROR: could not resolve scene/robot USD. Set ISAAC_ASSETS_ROOT, "
            "SCENE_USD, ROBOT_USD, or SELF_TEST=1."
        )
        simulation_app.close()
        sys.exit(1)
    say(f"Loading scene: {SCENE_USD}")
    _add_ref(_stage, SCENE_USD, "/World/Warehouse")
    say(f"Spawning {ROBOT_NAME} from {ROBOT_USD}")
    _add_ref(_stage, ROBOT_USD, ROBOT_PRIM)
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
                _add_ref(_stage, ROBOT_USD, p)
        except Exception as exc:
            say(f"EXTRA_ROBOTS_JSON: {exc}")

world.reset()


# -----------------------------------------------------------------------------
# 4b. Find the real articulation root inside the referenced robot. /World/carter
#     is just our Xform wrapper; Nova Carter's articulation/odometry root is a
#     nested prim (NVIDIA's own guide targets `chassis_link`). Pointing the
#     ArticulationController / ComputeOdometry at the wrapper fails with
#     "not a valid rigid body or articulation root", so discover it here.
# -----------------------------------------------------------------------------
def _find_drive_prims(stage, wrapper_path: str):
    """Return (articulation_root, chassis_rigid_body).

    The ArticulationController needs the prim carrying ArticulationRootAPI;
    ComputeOdometry needs a rigid body (the chassis). For carter_v1 these differ:
    root = /World/carter (ArticulationRoot), chassis = /World/carter/chassis_link.
    """
    wrapper = stage.GetPrimAtPath(wrapper_path)
    art_root = chassis = base_link = None
    if wrapper and wrapper.IsValid():
        for prim in Usd.PrimRange(wrapper):
            name = prim.GetName()
            if art_root is None and prim.HasAPI(UsdPhysics.ArticulationRootAPI):
                art_root = prim.GetPath().pathString
            if chassis is None and name == "chassis_link":
                chassis = prim.GetPath().pathString
            if base_link is None and name == "base_link":
                base_link = prim.GetPath().pathString
    art = art_root or chassis or base_link or wrapper_path
    chas = chassis or base_link or art_root or wrapper_path
    return art, chas


def _dump_robot_tree(stage, wrapper_path: str) -> None:
    """Log physics-relevant prims + joints so we can see the articulation root
    and the wheel joint names (which differ between Carter variants)."""
    wrapper = stage.GetPrimAtPath(wrapper_path)
    n = 0
    joints: list[str] = []
    say(f"--- prim scan under {wrapper_path} ---")
    if wrapper and wrapper.IsValid():
        for prim in Usd.PrimRange(wrapper):
            n += 1
            tn = str(prim.GetTypeName())
            apis = []
            if prim.HasAPI(UsdPhysics.ArticulationRootAPI):
                apis.append("ArticulationRoot")
            if prim.HasAPI(UsdPhysics.RigidBodyAPI):
                apis.append("RigidBody")
            if apis or prim.GetName() in ("chassis_link", "base_link", "carter", "nova_carter"):
                say(f"    {prim.GetPath()}  <{tn}>  {','.join(apis) or '-'}")
            if "Joint" in tn or "wheel" in prim.GetName().lower():
                joints.append(f"{prim.GetName()}<{tn}>")
    say(f"--- joints ({len(joints)}): {joints[:24]} ---")
    say(f"--- scanned {n} prims ---")


if not self_test:
    # A bare add-reference can need a few app updates before the articulation
    # is fully parsed and traversable.
    for _ in range(20):
        simulation_app.update()
    _dump_robot_tree(_stage, ROBOT_PRIM)

if self_test:
    ARTICULATION_ROOT = CHASSIS_PRIM = ROBOT_PRIM
else:
    ARTICULATION_ROOT, CHASSIS_PRIM = _find_drive_prims(_stage, ROBOT_PRIM)
say(f"articulation root (ArtCtl) : {ARTICULATION_ROOT}")
say(f"chassis prim   (Odom)     : {CHASSIS_PRIM}")


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
    # carter_v1 drive joints are 'left_wheel'/'right_wheel' (Nova Carter used
    # 'joint_wheel_left/right'); override per robot via env if needed.
    LEFT_WHEEL_JOINT = os.getenv("LEFT_WHEEL_JOINT", "left_wheel")
    RIGHT_WHEEL_JOINT = os.getenv("RIGHT_WHEEL_JOINT", "right_wheel")

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
                    ("ArtCtl.inputs:targetPrim", [ARTICULATION_ROOT]),
                    ("ArtCtl.inputs:jointNames", [LEFT_WHEEL_JOINT, RIGHT_WHEEL_JOINT]),
                    ("ComputeOdom.inputs:chassisPrim", [CHASSIS_PRIM]),
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
# 5c. Lidar OmniGraph (LIDAR=1): PhysX lidar -> ROS2PublishLaserScan on /scan.
#     Nav2's costmap obstacle layer consumes this to mark/clear obstacles, which
#     is what makes "route around the shelf / replan when blocked" work. Fully
#     optional: any failure here degrades to the current no-lidar behaviour.
# -----------------------------------------------------------------------------
def _find_lidar_prim(stage, wrapper_path: str) -> str | None:
    """First Lidar-typed prim under the robot (carter_v1_physx_lidar.usd has one
    under chassis_link). Type name is 'Lidar' for the PhysX range sensor."""
    wrapper = stage.GetPrimAtPath(wrapper_path)
    if not (wrapper and wrapper.IsValid()):
        return None
    for prim in Usd.PrimRange(wrapper):
        if "Lidar" in str(prim.GetTypeName()):
            return prim.GetPath().pathString
    return None


if ENABLE_LIDAR and not self_test and _LIDAR_EXT:
    LIDAR_TOPIC = os.getenv("LIDAR_TOPIC", "/scan")
    LIDAR_FRAME_ID = os.getenv("LIDAR_FRAME_ID", "carter_lidar")
    _lidar_path = os.getenv("LIDAR_PRIM") or _find_lidar_prim(_stage, ROBOT_PRIM)
    if not _lidar_path:
        say(
            "WARNING: no Lidar prim found under the robot — /scan disabled. "
            "Use a lidar-equipped USD (carter_v1_physx_lidar.usd) or set LIDAR_PRIM."
        )
    else:
        # Log the lidar's offset from the chassis so the base_link->lidar static
        # TF in nav2_demo.launch.py (env LIDAR_XYZ) can be tuned to match.
        try:
            _cache = UsdGeom.XformCache()
            _rel = _cache.ComputeRelativeTransform(
                _stage.GetPrimAtPath(_lidar_path), _stage.GetPrimAtPath(CHASSIS_PRIM)
            )[0]
            _t = _rel.ExtractTranslation()
            say(
                f"lidar prim: {_lidar_path}  offset from chassis: "
                f"({_t[0]:.3f}, {_t[1]:.3f}, {_t[2]:.3f}) — export LIDAR_XYZ to match"
            )
        except Exception:  # noqa: BLE001
            say(f"lidar prim: {_lidar_path}")
        _new_lidar = {
            "read_lidar": "isaacsim.sensors.physx.IsaacReadLidarBeams",
            "publish_scan": "isaacsim.ros2.bridge.ROS2PublishLaserScan",
        }
        _old_lidar = {
            "read_lidar": "omni.isaac.range_sensor.IsaacReadLidarBeams",
            "publish_scan": "omni.isaac.ros2_bridge.ROS2PublishLaserScan",
        }
        lnt = _new_lidar if (_LIDAR_EXT == "isaacsim.sensors.physx") else _old_lidar
        try:
            og.Controller.edit(
                {"graph_path": "/LidarGraph", "evaluator_name": "execution"},
                {
                    og.Controller.Keys.CREATE_NODES: [
                        ("OnTick", "omni.graph.action.OnPlaybackTick"),
                        ("SimTime", nt["simtime"]),
                        ("ReadLidar", lnt["read_lidar"]),
                        ("PublishScan", lnt["publish_scan"]),
                    ],
                    og.Controller.Keys.CONNECT: [
                        ("OnTick.outputs:tick", "ReadLidar.inputs:execIn"),
                        ("ReadLidar.outputs:execOut", "PublishScan.inputs:execIn"),
                        ("SimTime.outputs:simulationTime", "PublishScan.inputs:timeStamp"),
                        ("ReadLidar.outputs:azimuthRange", "PublishScan.inputs:azimuthRange"),
                        ("ReadLidar.outputs:depthRange", "PublishScan.inputs:depthRange"),
                        ("ReadLidar.outputs:horizontalFov", "PublishScan.inputs:horizontalFov"),
                        (
                            "ReadLidar.outputs:horizontalResolution",
                            "PublishScan.inputs:horizontalResolution",
                        ),
                        ("ReadLidar.outputs:intensitiesData", "PublishScan.inputs:intensitiesData"),
                        ("ReadLidar.outputs:linearDepthData", "PublishScan.inputs:linearDepthData"),
                        ("ReadLidar.outputs:numCols", "PublishScan.inputs:numCols"),
                        ("ReadLidar.outputs:numRows", "PublishScan.inputs:numRows"),
                        ("ReadLidar.outputs:rotationRate", "PublishScan.inputs:rotationRate"),
                    ],
                    og.Controller.Keys.SET_VALUES: [
                        ("ReadLidar.inputs:lidarPrim", [_lidar_path]),
                        ("PublishScan.inputs:topicName", LIDAR_TOPIC),
                        ("PublishScan.inputs:frameId", LIDAR_FRAME_ID),
                    ],
                },
            )
            say(f"Lidar graph created: {_lidar_path} -> {LIDAR_TOPIC} (frame {LIDAR_FRAME_ID})")
        except Exception as exc:
            say(f"WARNING: lidar graph setup failed (continuing without /scan): {exc}")
elif ENABLE_LIDAR and not _LIDAR_EXT:
    say("LIDAR=1 but no sensor extension loaded — /scan disabled.")

# -----------------------------------------------------------------------------
# 6. Play and step forever — this drives the graph (OnPlaybackTick).
# -----------------------------------------------------------------------------
# Reset FIRST (initialise physics/articulation), then play LAST. Order matters:
# world.reset() stops the timeline, so if it runs *after* play() the timeline is
# left stopped and OnPlaybackTick never fires — every graph publisher (/tf,/odom)
# goes silent even though the publishers exist.
world.reset()
omni.timeline.get_timeline_interface().play()


def _set_camera_view(eye, target):
    """Aim the active viewport camera so the WebRTC stream shows the scene
    (not just the floor). STREAM mode only; harmless if unavailable."""
    for _mod in ("isaacsim.core.utils.viewports", "omni.isaac.core.utils.viewports"):
        try:
            importlib.import_module(_mod).set_camera_view(eye=eye, target=target)
            say(f"camera set via {_mod}: eye={eye} target={target}")
            return
        except Exception:  # noqa: BLE001
            continue
    say("set_camera_view unavailable; orbit/zoom in the streamed GUI instead")


if _STREAM:
    for _ in range(5):
        simulation_app.update()  # let the viewport come up before aiming it
    # Elevated 3/4 view of the warehouse centred on Carter's start. Override with
    # CAM_EYE / CAM_TARGET (comma-separated x,y,z) if you want a different angle.
    _set_camera_view(
        [float(v) for v in os.getenv("CAM_EYE", "6,6,5").split(",")],
        [float(v) for v in os.getenv("CAM_TARGET", "0,0,0.5").split(",")],
    )
say("ROS2_READY: Isaac is headless and publishing ROS 2. Connect RViz from your laptop.")
if ENABLE_DIFF_DRIVE and not self_test:
    _scan = f", {os.getenv('LIDAR_TOPIC', '/scan')}" if ENABLE_LIDAR else ""
    say(
        f"  Publishes: /clock, /tf, {os.getenv('ODOM_TOPIC', '/odom')}{_scan}   "
        f"Subscribes: {os.getenv('CMD_VEL_TOPIC', '/cmd_vel')}   (robot prim: {ROBOT_PRIM})"
    )
    say("  Drive it: ros2 topic pub /cmd_vel geometry_msgs/Twist '{linear: {x: 0.2}}' --once")
else:
    say(f"  Publishes: /clock, /tf   (robot prim: {ROBOT_PRIM})")

# Heartbeat is OFF by default (it spams the log). Set HEARTBEAT_STEPS=N to get a
# periodic "still alive" line every N physics steps.
_HEARTBEAT = int(os.getenv("HEARTBEAT_STEPS", "0"))
step = 0
try:
    while simulation_app.is_running():
        # Render only when streaming (STREAM=1) so you can see it in the WebRTC
        # client. Rendering also pumps the full app, which makes OnPlaybackTick
        # fire reliably (so /tf,/odom publish). STREAM=0 is headless for RViz.
        world.step(render=_STREAM)
        step += 1
        if _HEARTBEAT and step % _HEARTBEAT == 0:
            say(f"sim running, step {step}")
except KeyboardInterrupt:
    pass
finally:
    simulation_app.close()
