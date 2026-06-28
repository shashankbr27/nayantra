# Nav2 Stack — Implementation Plan

**Status:** Design doc. Not implemented yet. Tracking branch: `shashankbr27/nav2-stack`.

## Why this exists

Today the agent → robot path has two working modes:

1. **Stub / kinematic** — `nayantra/rmf_bridge/server.py` translates MCP tool
   calls into linear-interpolation movement in the in-process fleet adapter.
   Position state is correct; nothing in the real world (or Isaac) moves.
2. **Isaac Demo** — `scripts/isaac_demo.py` exposes an HTTP API on port 8900
   that does kinematic glide via `set_world_pose`. Carter visibly moves in
   the WebRTC viewport. No physics, no obstacles, no real planning.

Neither uses Nav2. That's fine for the SIG demo, but a Nav2 path gives us:

- Real planning around real obstacles
- Local costmap dynamic obstacle avoidance
- Drop-in compatibility with any production Nav2-equipped robot
- A genuine "RMF dispatch → real ROS 2 action → wheels turn" loop that
  validates the architecture end-to-end

This branch is the design for that.

## What's already there to build on

After [shashankbr27/rviz-handoff-trace-ui](https://github.com/shashankbr27/nayantra/tree/shashankbr27/rviz-handoff-trace-ui)
lands, [scripts/isaac_boot.py](../scripts/isaac_boot.py) already:

- Loads warehouse USD + spawns Carter
- Publishes `/clock`, `/tf` (whole robot subtree)
- Subscribes `/cmd_vel` → `DifferentialController` → wheel velocities
- Publishes `/odom` from `IsaacComputeOdometry`

And [nayantra/ros2_adapter/fleet_adapter.py](../nayantra/ros2_adapter/fleet_adapter.py)
already, in live mode, publishes `NavigateToPose` goals on `/navigate_to_pose`
and consumes the resulting feedback.

So the missing pieces are:

1. **A `/scan` publisher in Isaac** so Nav2 can do obstacle layer + AMCL
2. **A Nav2 launcher** running `nav2_bringup` against a known warehouse map
3. **A warehouse OccupancyGrid** (`map_server` input)
4. **A first-pose seed** so AMCL knows where Carter starts (or use a static
   `map → odom` transform and skip AMCL entirely)

## End-to-end flow once implemented

```
LLM ──"send carter to charging_dock"──► MCP agent
        │
        ▼
   move_robot tool ──► rmf_bridge.dispatch_task ──► fleet_adapter.navigate_to_waypoint
                                                          │
                                                          ▼
                                          /navigate_to_pose (Nav2 action)
                                                          │
                                            ┌─────────────┴───────────────┐
                                            ▼                              ▼
                                     Nav2 planner_server          Nav2 controller_server
                                     (global plan)                (local plan → /cmd_vel)
                                            │
                                            ▼
                                  /cmd_vel ──► isaac_boot's DriveGraph ──► Carter's wheels
                                            │
                                            ▼  (Carter moves in Isaac)
                                  /odom + /scan ──► back into Nav2 costmaps
```

RViz on the laptop subscribes to `/map`, `/tf`, `/plan`, `/global_costmap`,
`/local_costmap`, `/scan`. Operator sees the entire planning chain live.

## File-by-file plan

| File | Purpose | Effort |
|---|---|---|
| `scripts/isaac_boot.py` (extend) | Add section 5c: `/scan` publisher from Carter's lidar sensor prim | Medium — needs the right OmniGraph nodes; lidar prim path varies per USD |
| `config/nav2_warehouse_params.yaml` | Nav2 stack params: planner, controller, costmaps, bt_navigator | Medium — start from `nav2_bringup` defaults, tune robot radius/footprint |
| `config/warehouse_map.pgm` + `.yaml` | Pre-baked OccupancyGrid of Simple_Warehouse | Small if we generate it once via SLAM; needs a one-time mapping run |
| `scripts/generate_warehouse_map.py` | One-shot offline tool: drive Carter with teleop + `slam_toolbox` → save map | Small — wrapper around standard SLAM toolbox commands |
| `scripts/run_nav2_docker.sh` | Docker launcher (Humble + Nav2 + Cyclone DDS), mounts the params YAML + map, `--network host` | Small |
| `docs/nav2_setup.md` | Operator runbook: prereqs → start Isaac → start Nav2 → start agent → drive | Small |
| `docs/rviz_setup.md` (extend) | Add Path D: "Nav2 stack with warehouse map" | Small |
| `tests/test_nav2_integration.py` *(optional)* | Smoke test that walks the dispatch payload through `rmf_bridge` and asserts the right `NavigateToPose` goal would be sent. No real Nav2 needed. | Small |

## Risks / open questions

### R1 — Carter's lidar prim path varies per USD version

The Nova Carter USD ships with either an RTX lidar (`/World/carter/chassis_link/front_3d_lidar`)
or no lidar at all in some builds. The first piece of integration work has to
be confirming the prim path in **your** USD via the Isaac Stage panel, then
plumbing a `ROS2RtxLidarHelper` node into the existing OmniGraph.

If Carter has no lidar in the USD you're using, options are:
- Add one (omni.kit.physx → PhysX lidar prim) — cheap, scriptable
- Switch to a TurtleBot3 sample USD that already has wheels + lidar wired

### R2 — AMCL vs static `map → odom`

AMCL needs a few seconds + a lidar to converge. For a demo, the user doesn't
want to wait. Two options:

- **AMCL** — proper localization, drift-resistant. Slower start.
- **Static transform `map → odom = identity`** — works only if `/odom` itself
  doesn't drift much and Carter starts at `(0, 0)` in the map frame. For a
  short demo, this is enough. For multi-hour ops, no.

Recommend: ship the static-transform path first for the SIG demo. AMCL is
a stretch goal.

### R3 — `nav2_bringup` is heavy

The full Nav2 stack is ~6 lifecycle nodes. Cold start takes 5–10 seconds.
For a demo where the operator types a command and expects the robot to
*move within 2 seconds*, we need Nav2 already running with all nodes
`activated` before the first dispatch. The launcher's healthcheck should
gate the agent API on Nav2 being ready.

### R4 — Costmap radius / inflation tuning

Default `nav2_bringup` params expect a Turtlebot-class footprint (radius
0.22 m). Nova Carter v2 is 0.4 × 0.55 m. Inflation, robot_radius, and
controller `min_x_velocity_threshold` all need adjusting or the planner
will refuse to plan through narrow warehouse aisles. Plan to spend ~1 day
on this tuning specifically.

## Milestones

**M1 — Carter has a lidar publishing `/scan`** (1–2 days)
- Identify lidar prim path in your Nova Carter USD
- Extend `isaac_boot.py` with the lidar OmniGraph chunk
- Verify with `ros2 topic hz /scan` from the laptop

**M2 — Static map + map_server + RViz shows the warehouse floor** (1 day)
- Either: drive Carter with teleop + `slam_toolbox` → save map
- Or: convert the USD geometry offline (use NVIDIA's `occupancy_map`
  extension if available in your Isaac build)
- Commit the resulting `.pgm` + `.yaml` to `config/`

**M3 — Nav2 launched + RViz shows costmaps** (1–2 days)
- `scripts/run_nav2_docker.sh` with Humble + Nav2 image
- `config/nav2_warehouse_params.yaml` tuned for Carter
- Manual test: `ros2 action send_goal /navigate_to_pose ...` from a terminal
  drives Carter

**M4 — Agent → Nav2 round-trip** (1 day)
- `ROS2_ENABLED=true` in the rmf_bridge config
- LLM command "send Carter to charging dock" produces visible motion
- The dashboard's tool trace and RViz both reflect the same reality

**M5 — Polish** (1 day)
- AMCL with initial-pose seed (optional, stretch)
- Recovery behaviours for stuck robot
- Costmap obstacle layer driven by `/scan`

**Total: 5–7 working days** of integration, mostly because OmniGraph
configuration and Nav2 param tuning are both "I'll only know it works
when I run it" tasks.

## What to do *now* if you want to get unblocked

Before any of this Nav2 work is needed, you have two faster paths:

- For the **SIG demo on Isaac**: use the [isaac_demo MCP tools](../nayantra/mcp/tools.py)
  that already landed on `shashankbr27/rviz-handoff-trace-ui`. Set
  `ISAAC_DEMO_URL` in `.env`, run `scripts/isaac_demo.py` on the workstation,
  the LLM picks `isaac_goto_waypoint` and Carter moves in the photoreal
  viewport. **Demo-complete in an afternoon.**

- For a **real robot demo**: skip Isaac entirely, set `ROS2_ENABLED=true`,
  point the fleet adapter at a robot whose owner already has Nav2 running.
  The fleet adapter publishes `NavigateToPose` goals; the existing Nav2 on
  the robot consumes them.

Nav2-from-scratch + Isaac is the right architecture for a v1.0 release that
showcases the full stack — not for next week.

## When this branch is ready to merge

Definition-of-done for `shashankbr27/nav2-stack`:

- [ ] `scripts/isaac_boot.py` publishes `/scan` (validated with `ros2 topic hz`)
- [ ] `scripts/run_nav2_docker.sh` brings up Nav2 with all lifecycle nodes
      reaching state `active`
- [ ] `config/warehouse_map.{pgm,yaml}` committed
- [ ] `config/nav2_warehouse_params.yaml` tuned for Carter (no planner aborts
      on the canonical waypoint set in `nayantra/ros2_adapter/fleet_adapter.py`)
- [ ] Manual end-to-end test: LLM command produces Carter motion + RViz costmaps update
- [ ] `docs/nav2_setup.md` + Path D in `docs/rviz_setup.md` updated
- [ ] Existing test suite still green (no regression — Nav2 is opt-in via
      `ROS2_ENABLED`, so stub mode tests are unaffected)
