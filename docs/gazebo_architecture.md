# Gazebo Simulation — Architecture

Gazebo is the open-source, GPU-optional simulation backend for Nayantra. It sits
next to Isaac Sim, not inside the core: to Nayantra a Gazebo robot is a Nav2
robot on ROS 2, driven by the same `Nav2Adapter` as an Isaac or a physical one.
No Gazebo-specific code exists in the core.

**Status.** The world, robot model, launch file and scenario are consistency-tested
in CI without Gazebo (`tests/test_gazebo_assets.py`). They have **not yet been run
against a live Gazebo + Nav2** (the development machine has neither). Bring-up
should go through `bash scripts/run_gazebo.sh check`, which reports each piece
separately.

## Where it fits

```
                       Nayantra Core (:8000)
         world model · allocator · traffic · safety kernel
                              │  cleared path in ▼   ▲ pose · battery · progress
                     Nav2Adapter  (ROS 2 Humble | Jazzy)
                              │  /<ns>/odom  ▲    ▼ /<ns>/navigate_through_poses
        ┌─────────────────────┴───────────────────────────────────────────┐
        │ per robot, namespace /ugv_01, /ugv_02 …                          │
        │  Nav2: controller · smoother · planner · behaviors · bt_navigator│
        │  static map→odom (identity) · base_link→lidar_link               │
        └─────────────────────┬───────────────────────────────────────────┘
                              │  /<ns>/cmd_vel ▼   ▲ /<ns>/{odom,tf,scan}   /clock
                     ros_gz_bridge  (parameter_bridge)
                              │  Gazebo transport
        ┌─────────────────────┴───────────────────────────────────────────┐
        │ Gazebo Harmonic (gz sim)                                         │
        │  world  nayantra_warehouse.sdf  ← generated from the Nayantra map│
        │  robots nayantra_ugv (diff-drive + gpu_lidar), one per namespace │
        └─────────────────────────────────────────────────────────────────┘
```

Nothing in the lower box knows about tasks, fleets or safety zones. That is by
design: the core decides *where and when*; Nav2 and Gazebo do *how*.

## One map, two worlds

`config/maps/gazebo_warehouse.json` is the source of truth. The core plans on its
waypoints, lanes and zones. `simulation/gazebo/generate_world.py` turns its
`walls` and `obstacles` into the SDF boxes the robots physically collide with, so
the planning world and the physical world cannot drift apart. The Gazebo world
frame **is** the Nayantra `map` frame.

```
config/maps/gazebo_warehouse.json ──generate_world.py──► worlds/nayantra_warehouse.sdf
        │                                                     (tests fail if stale)
        └──► Nayantra world model (nav graph, zones)
```

## Frames and odometry

The robot model publishes odometry in the **world** frame (`OdometryPublisher`
reports the model's world pose). Because the world frame equals `map`, the
`map → odom` transform is a static identity wherever a robot spawns, and no
localization (AMCL/SLAM) is needed. Gazebo's own diff-drive odometry starts at 0
at the spawn point, so it is routed to unused topics.

Each robot has its own TF tree under its namespace (`/ugv_01/tf`), like the
standard Nav2 multi-robot setup. The core's `Nav2Adapter` therefore reads the
robot pose from odometry, not from a shared `/tf`.

## Multi-robot

Robots are namespaced end to end. `GZ_ROBOTS="ugv_01:-10.5:0:0,ugv_02:10.5:0:3.14159"`
(the default) spawns two; names must equal the robot ids in
`config/scenarios/gazebo_warehouse.json`, whose `navigation.namespace` is the same
name. `simulation/gazebo/nav2_params.py` derives each robot's Nav2 parameters from
`config/nav2/carter_nav2.yaml`: topics become relative, parameters move under the
namespace, and `use_sim_time` is set.

Traffic between the robots is the core's job. Both robots belong to one fleet on
one nav graph, so reservations and negotiation apply as they do for any fleet.

## Humble and Jazzy

| | Humble (Ubuntu 22.04) | Jazzy (Ubuntu 24.04) |
|---|---|---|
| Gazebo | Harmonic from the OSRF apt repo | Harmonic (default for Jazzy) |
| ROS bridge | `ros-humble-ros-gzharmonic` | `ros-jazzy-ros-gz` |
| Nav2 | `ros-humble-navigation2`, `ros-humble-nav2-bringup` | `ros-jazzy-navigation2`, `ros-jazzy-nav2-bringup` |

`scripts/run_gazebo.sh` picks Jazzy, else Humble. The e-stop `cmd_vel` type
(Twist or TwistStamped) is detected by the Nav2 adapter; the model here uses
`Twist`, which `config/nav2/carter_nav2.yaml` also selects.

## Running it

```bash
# terminal 1: Gazebo + bridge + one Nav2 stack per robot
bash scripts/run_gazebo.sh sim            # GZ_HEADLESS=1 for no GUI
# terminal 2: the core, from a shell with the same ROS 2 environment
bash scripts/run_gazebo.sh core           # → http://localhost:8000/
bash scripts/run_gazebo.sh check          # gz, Nav2, ros_gz, per-robot odom
```

If you edit the map, regenerate the world: `bash scripts/run_gazebo.sh world`.

## Layout

| Path | Role |
|---|---|
| `simulation/gazebo/generate_world.py` | map JSON → world SDF |
| `simulation/gazebo/worlds/nayantra_warehouse.sdf` | generated world (do not edit) |
| `simulation/gazebo/models/nayantra_ugv.sdf.in` | robot template (`@NAME@` = namespace) |
| `simulation/gazebo/robots.py` | robot list, model rendering, bridge arguments |
| `simulation/gazebo/nav2_params.py` | per-robot Nav2 parameters |
| `simulation/gazebo/launch/gazebo_warehouse.launch.py` | Gazebo + bridge + Nav2 |
| `config/maps/gazebo_warehouse.json`, `config/scenarios/gazebo_warehouse.json` | world model seeds |
| `scripts/run_gazebo.sh` | `sim` · `core` · `check` · `world` |

## Known limits

- Not yet validated on a live Gazebo + Nav2 stack (see Status).
- No localization: navigation is in the odom frame, as in the Isaac demo.
- The lidar is a `gpu_lidar`, which needs a rendering backend. Headless machines
  need EGL (`GZ_HEADLESS=1` uses `--headless-rendering`).
- Only ground robots. UAVs would need a separate adapter and simulator (planned).
