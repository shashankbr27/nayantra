# RViz 2 Setup for Nayantra

Nayantra's web dashboard is intentionally **trace-only**. It shows which MCP
tools the agent selected, the parameters it called them with, and a
human-readable result for each step. It does **not** render the map or robots.

For live visualization, use **RViz 2** — the ROS 2 ecosystem's standard 3D
viewer. RViz consumes the same `/tf`, `/map`, and `nav_msgs/Path` topics that
Nav2 and the Isaac Sim ROS 2 Bridge already publish, so there's no Nayantra-
specific publisher to install.

## When RViz works (and when it doesn't)

| Backend | RViz works? | What you'll see |
|---|---|---|
| **Real robot (Nav2)** | ✅ Yes | Map, robot model, planned/current path, goal pose, costmaps |
| **Isaac Sim (with ROS 2 Bridge)** | ✅ Yes | Same as above, sourced from the simulation |
| **Pure stub mode** (`DEBUG_MODE=true`, no rclpy) | ❌ No | RViz has nothing to subscribe to. Use the dashboard's tool trace only. |

If you want to demo Nayantra on a laptop with no robot and no ROS 2 stack,
stay in stub mode and use the dashboard. The moment Isaac Sim or a real
robot enters the picture, switch the visualization to RViz.

## Prerequisites

- **ROS 2 Humble** (or newer) sourced in the terminal you launch RViz from
  - Ubuntu 22.04: `sudo apt install ros-humble-rviz2`
  - Windows: run RViz inside a WSL2 Ubuntu environment (native ROS 2 on
    Windows works but is fragile; WSL2 is the path of least pain)
- For real robots: a working **Nav2** stack publishing the standard topics
- For Isaac Sim: the **ROS 2 Bridge** extension enabled, scene loaded

## Path A — Isaac Sim

1. Open Isaac Sim, load your warehouse / building scene.
2. Inside Isaac Sim: **Window → Extensions** → enable **ROS 2 Bridge** and
   tick **AUTOLOAD**.
3. Make sure the scene contains:
   - At least one robot prim with a TF action graph (the Isaac Sim
     "TurtleBot3 Sample" already has this wired up).
   - A `nav_msgs/OccupancyGrid` publisher (Isaac Sim's `Differential
     Controller` + `ROS2 Bridge` template handles this).
4. In a sourced ROS 2 terminal on the same machine (or any reachable
   machine on the same `ROS_DOMAIN_ID`):

   ```bash
   source /opt/ros/humble/setup.bash
   ros2 topic list      # confirm /tf, /map, /scan, /odom are visible
   rviz2 -d $(pwd)/docs/nayantra.rviz   # optional preset, see below
   ```

5. Launch Nayantra as usual:

   ```bash
   ISAAC_SIM_ENABLED=true nayantra-mcp-server &
   ISAAC_SIM_ENABLED=true nayantra-api &
   ```

6. Issue commands via the dashboard or CLI. The robot moves in Isaac Sim,
   you watch it in RViz, and you read the agent's reasoning in the
   dashboard's trace.

## Path B — Real robot (Nav2)

1. Boot the robot. Confirm Nav2 is running and publishing the usual topics:

   ```bash
   ros2 topic list | grep -E "/(tf|map|amcl_pose|plan|odom)"
   ros2 topic hz /tf
   ```

2. Launch RViz with the Nav2 standard config, or the included preset:

   ```bash
   rviz2 -d $(pwd)/docs/nayantra.rviz
   ```

3. Launch Nayantra's fleet adapter pointed at the live ROS 2 graph:

   ```bash
   python -m nayantra.ros2_adapter.fleet_adapter \
       --fleet turtlebot_fleet --robot tb3_1 --ros2
   ```

4. Start the MCP server and agent API. From here it's identical to the
   Isaac Sim path.

## Sample RViz config (`docs/nayantra.rviz`)

A starting `.rviz` preset is on the roadmap. Until it lands, build your own
by enabling these Displays in RViz 2:

| Display | Topic | Why |
|---|---|---|
| **TF** | (auto) | Robot pose, sensor frames |
| **Map** | `/map` | OccupancyGrid floor plan |
| **RobotModel** | `/robot_description` | 3D mesh of the robot |
| **LaserScan** | `/scan` | Live lidar return |
| **Path** | `/plan` | Nav2's planned path |
| **PoseWithCovariance** | `/amcl_pose` | Localization estimate |
| **MarkerArray** | `/rmf_visualization/static` *(if you run rmf-web)* | Building waypoints, lanes |

Set **Fixed Frame** to `map`.

If you want Nayantra to publish its own status overlays (active task per
robot, the agent's current step, etc.) as `visualization_msgs/MarkerArray`,
that's a roadmap item — see [#TBD] on the issue tracker.

## Troubleshooting

**RViz starts but shows nothing.**
You're on a different `ROS_DOMAIN_ID` from the robot / sim. Match them:

```bash
export ROS_DOMAIN_ID=30   # whatever the robot uses
ros2 topic list
```

**`/map` is empty.**
- Isaac Sim: the scene doesn't have a map publisher graph. Add the "ROS 2
  Publish Map" template via Action Graph.
- Real robot: `nav2_map_server` isn't running. Check the Nav2 launch file.

**RViz crashes on Windows.**
Native ROS 2 RViz on Windows uses Qt5 + DirectX and is brittle. Switch to
WSL2 + an X server (VcXsrv or Wayland-WSLg on Windows 11):

```bash
# inside WSL2 Ubuntu 22.04
sudo apt install ros-humble-rviz2
export DISPLAY=$(ip route list default | awk '{print $3}'):0
rviz2
```

**Dashboard and RViz disagree.**
The dashboard reflects what Nayantra's agent + MCP layer *think* is
happening (task IDs, statuses from RMF). RViz reflects what the *robot's
actual TF tree* says. A divergence usually means the fleet adapter has
drifted from Nav2 — check `/rmf_fleet/<fleet>/robot_state` against
`/amcl_pose` for the same robot.

## Why this split (and not an in-browser 3D view)

Three reasons:

1. **Avoid reinventing RViz.** Every robotics team that uses ROS 2 already
   has RViz workflows. Forcing them onto a web canvas adds friction with
   no upside.
2. **The agent's contribution is reasoning, not rendering.** Showing the
   tool selection + result is the unique Nayantra view. The 3D map is
   commodity.
3. **Cheap to operate.** RViz over DDS is rock-solid. A WebGL fleet
   visualizer needs a constant WebSocket telemetry feed, which adds
   latency and a failure mode without changing what the operator can
   decide.
