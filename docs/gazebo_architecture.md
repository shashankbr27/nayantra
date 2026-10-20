# Gazebo Simulation — Architecture

Gazebo is the open-source, GPU-optional simulation backend for Nayantra. It sits
next to Isaac Sim, not inside the core: to Nayantra a Gazebo robot is a Nav2
robot on ROS 2, driven by the same `Nav2Adapter` as an Isaac or a physical one.
No Gazebo-specific code exists in the core.

**Status.** The assets are consistency-tested in CI without Gazebo
(`tests/test_gazebo_assets.py`) and have been run live on Gazebo Harmonic 8.15 +
Nav2 under ROS 2 **Jazzy** (Ubuntu 24.04 in WSL2, RTX 3050 laptop GPU). Verified,
with Gazebo's own physics state as the reference:

- both Nav2 stacks activate and the core connects both robots;
- navigate tasks from the dashboard API drive the robots to the right waypoints,
  including back-to-back tasks;
- pause/resume and e-stop hold the robot still (0.000 m drift) and resume it;
- two robots sent head-on down the same aisle pass without colliding.

Live testing found two bugs in `Nav2Adapter`, now fixed: the connection check ran
before DDS discovery finished and reported a healthy robot as an error, and a new
task's goal was rejected when Nav2 was still finishing the previous one. **Not yet
tested live:** ROS 2 Humble.

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

Robots are namespaced end to end. `GZ_ROBOTS="ugv_01:-10.5:0:0,ugv_02:10.5:0:3.14159,ugv_03:-10.5:-5.5:0"`
(the default) spawns three: the first two on the docks, the third on the charger. Names must equal the robot ids in
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

### One command: `nayantra.sh`

```bash
./nayantra.sh setup      # once: choose the simulator (a menu, or --sim auto|gazebo|isaac), then install it
./nayantra.sh detect     # what this machine can run, and why; changes nothing
./nayantra.sh start      # [--sim gazebo|isaac] [--headless] [--no-open] [--reset]
./nayantra.sh stop
./nayantra.sh gui        # (re)open the Gazebo window
./nayantra.sh status | logs [sim|isaac|nav2|core|mcp|agent|gui] | restart | doctor
```

This page describes the Gazebo path. `setup` can instead install Isaac Sim (see the
README's "Choosing a Simulator"); the choice is recorded in the state directory and
`start --sim` overrides it for one run.

`start` brings the stack up in dependency order: the Gazebo server and one Nav2
stack per robot, then (once every `bt_navigator` is active) the core with the
`gazebo_warehouse` scenario, the MCP server and the agent API, then the Gazebo
window, and opens the dashboard. The script is `nayantra.sh` because the repo root
already has a `nayantra/` package directory. In PowerShell `.\nayantra start` works
through `nayantra.cmd`.

With Isaac Sim, `start` runs the recipes in `scripts/run_demo.sh` in order: Isaac Sim
(streamed; ready when it prints `ROS2_READY`), then Nav2 (ready when `/bt_navigator` is
active; retried like the Gazebo stacks), then the core on the `isaac_carter` scenario,
the MCP server and the agent API. Isaac Sim does not run in WSL2, so on Windows `setup`
always chooses Gazebo and `--sim isaac` is refused with the reason.

**Windows.** ROS 2 Nav2 and Gazebo Harmonic need Linux, so on Windows the script
forwards into an Ubuntu 24.04 **WSL2** distro and `setup` creates it
(`NAYANTRA_WSL_DIR` picks where its disk goes). The repo stays on the Windows
drive. Details that matter:

- **State** (venv, database, logs, dashboard build) lives in `~/.nayantra` inside
  the distro: SQLite and Python imports are unreliable and slow over `/mnt/*`.
- **Keep-alive.** WSL stops a distro, and everything in it, when its last session
  closes. While the stack runs, the launcher holds one hidden session; `stop`
  releases it.
- **Port.** WSL forwards the dashboard to Windows `localhost`, but not if a Windows
  program (often VS Code's port forwarding) already listens on that port. The
  launcher then takes the next free port and prints it.
- **GPU.** Inside WSL, Mesa defaults to CPU rendering. The launcher selects the
  D3D12 driver and the NVIDIA adapter, so the lidar renders on the GPU.
- **Nav2 startup retry.** Roughly one start in ten, a lost DDS service reply leaves
  one robot's Nav2 lifecycle manager waiting forever, and it cannot be nudged from
  outside. If Nav2 is not active on every robot within 90 s, `start` restarts the
  simulation (up to three tries). The root cause is not yet found; switching Fast DDS
  to UDP-only did not help.
- **Ask box.** The MCP server and agent find the core through `NAYANTRA_CORE_URL`,
  which the launcher sets to the dashboard port it actually chose. The agent retries
  Gemini's transient 503 / 429 errors with a short backoff.
- **The window is a separate process** from the simulation server. The NVIDIA WSL
  driver was seen to crash while a window was created, and once to leave the
  simulation frozen, so after opening the window `start` checks that simulation
  time advances; if not, it unpauses the world and reopens the window with
  software rendering. `NAYANTRA_GUI_RENDER=gpu|software|auto` overrides this.
  A window crash never stops Nav2 or the dashboard.

### By hand

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
| `nayantra.sh`, `nayantra.cmd` | one-command setup / start / stop (Linux, and Windows via WSL2) |

## Known limits

- Validated live on ROS 2 Jazzy only; Humble is untested (see Status).
- The dashboard's **Simulation** panel (pause / stop / speed) drives the core's
  built-in simulator, not Gazebo, and is hidden when that simulator has no robots.
  Pause Gazebo from its own window.
- No localization: navigation is in the odom frame, as in the Isaac demo.
- The lidar is a `gpu_lidar`, which needs a rendering backend. Headless machines
  need EGL (`GZ_HEADLESS=1` uses `--headless-rendering`).
- Only ground robots. UAVs would need a separate adapter and simulator (planned).
