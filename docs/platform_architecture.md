# Nayantra Platform Architecture — Analysis and Migration Plan

**Status:** living design doc for the `feat/multi-fleet-platform` work.
**Scope:** how the single-robot prototype becomes a multi-fleet control plane,
what was wrong with the prototype, and what changes where.

Honesty rule used throughout: a component is called *Open-RMF* only if it is an
actual Open-RMF package. Everything in `nayantra/core` is **Nayantra-native** and
*inspired by* Open-RMF concepts (fleets, nav graphs, traffic schedule,
negotiation, task bidding). Nothing here links against `rmf_traffic`,
`rmf_fleet_adapter` or `rmf-web`.

---

## 1. Current architecture (as found)

```
 CLI / dashboard.html (trace-only UI, :8080)
          │  POST /run, GET /stream
          ▼
 nayantra.agent  (:8080)  — LLM planner. Gemini: real agentic loop.
          │                 Anthropic/OpenAI: single-shot plan, results never
          │                 fed back to the model.
          │  POST /run {tool, parameters}
          ▼
 nayantra.mcp.server (:7000) — custom REST "MCP" (GET /tools, POST /run, SSE).
          │                    NOT the Model Context Protocol wire format.
          │  httpx (OpenRMFClient; DEBUG_MODE returns fabricated data)
          ▼
 :8000 — one of:
   • docker/rmf_stub_server.py   canned responses
   • nayantra.rmf_bridge.server   "RMF-compatible" REST, ONE robot, in-memory
          │
          ▼
 nayantra.ros2_adapter.fleet_adapter.RMFFleetAdapter
     • Nav2 NavigateToPose client + /odom subscriber (ROS2_ENABLED)
     • else linear-interpolation "kinematic sim"
          │
          ▼  ROS 2 DDS
 scripts/isaac_boot.py (Isaac Sim 6.0 + carter_v1, /odom /scan /cmd_vel)
 scripts/nav2_demo.launch.py (Nav2, static map→odom, no localization)

 Side paths that bypass the stack:
   scripts/nav_cli.py    NL (Gemini) → Nav2 directly
   scripts/isaac_demo.py kinematic Isaac + HTTP :8900 ← scripts/drive_cli.py,
                         isaac_* MCP tools
```

### Component inventory

| Component | State | Notes |
|---|---|---|
| `agent/agent.py` | working | Prompt hard-codes door/lift choreography and waypoint names that exist nowhere (`main_gate`, `board_room`, `workshop`). Only Gemini gets tool results back. |
| `agent/planner.py` | working | Dependency-graph execution for single-shot plans. |
| `mcp/server.py`, `mcp/tools.py` | working | Tools map 1:1 to Open-RMF REST calls. Params are unvalidated `properties` dicts (no `required`). |
| `rmf_client/client.py` | working | DEBUG_MODE fabricates doors (`main_door`), lifts (`lift_1`), a building map. |
| `rmf_bridge/server.py` | working, 1 robot | Nayantra-native imitation of the rmf-web REST shape. In-memory tasks, first-idle-robot "allocation", no traffic. |
| `ros2_adapter/fleet_adapter.py` | working, 1 robot | Named "RMF Fleet Adapter" but does not use `rmf_fleet_adapter`. Calls `rclpy.init()` per instance → a second robot in the same process fails. |
| `isaac_sim/sim_bridge.py`, `robot_spawner.py` | stub | Target a REST API (`/stage/load`, `/prim/create`) that only `scripts/isaac_sim_server.py` provides, whose live mode uses Isaac 4.x APIs. The real Isaac path is `isaac_boot.py` over ROS 2. |
| `zenoh_bridge/bridge.py` | stub | Logs what it would relay; never publishes into ROS 2. |
| `api/ws_monitor.py` | half | WS endpoint works; the `publish_*` helpers are never called. |
| `api/dashboard.html` | working | Agent tool trace only. No map, fleet or robot state. |
| `scripts/isaac_boot.py` + `nav2_demo.launch.py` | working (Carter + Nav2) | `EXTRA_ROBOTS_JSON` spawns extra prims, but only the first robot gets a drive graph. |
| Localization / SLAM | missing | Static `map→odom`; no map_server, no AMCL, no slam_toolbox. |
| Multi-robot, fleets, traffic, allocation | missing | |

### Waypoint tables that disagree

| Location | `zone_a` | `zone_b` | Other names |
|---|---|---|---|
| `ros2_adapter/fleet_adapter.py` `WAREHOUSE_WAYPOINTS` | (-3, 2) | (3, 2) | charging_dock, zone_c, pick/drop stations, elevator_lobby, entrance |
| `rmf_client/client.py` debug building map | copy of above | | |
| `docker/rmf_stub_server.py` | copy of above | | |
| `scripts/isaac_demo.py` `WAYPOINTS` | (-3, 0) | (3, 0) | main_gate, loading_dock, board_room, canteen, store_room … |
| `scripts/nav_cli.py` `WAYPOINTS` | (3, 0) | (-3, 0) | loading_dock (5,-2), shelf_1/2 … |
| agent `SYSTEM_PROMPT` examples | — | — | main_gate, board_room, workshop, canteen (not in the bridge) |
| `config.WAYPOINTS_FILE` → `config/waypoints.json` | — | — | file never existed |

The same spoken name drives the robot to three different places depending on
which entry point you use.

## 2. Current limitations

1. **Single robot, single fleet, single adapter.** `RMFBridge.__init__` builds one adapter from `FLEET_NAME`/`ROBOT_NAME`.
2. **No authoritative map.** Waypoints are hard-coded Python dicts; the "nav graph" has zero edges.
3. **No traffic coordination.** Robots meet only through Nav2's lidar costmap.
4. **The LLM effectively picks robots** (prompt: "list_robots … the system fills in the robot") instead of a task allocator.
5. **Fabricated state.** Debug mode invents doors, lifts and task progress (`"progress": "50%"`).
6. **No persistence** of fleets, robots or tasks. A restart loses everything.
7. **No confirmation flow, no hard safety limits** beyond Nav2's collision monitor.
8. **No explainability.** A task has a flat text log; there is no allocation rationale and no wait reason.
9. **The UI shows no spatial state.**

## 3. Proposed architecture

```
                     ┌───────────────────────────────┐
                     │  web/  (React + TS + Vite)    │
                     │  Operations · Fleets · Robots │
                     │  Tasks · Map editor · Sim     │
                     └──────────────┬────────────────┘
                   REST /api/v1  +  WebSocket /api/v1/ws
                     ┌──────────────▼────────────────┐
                     │  Nayantra Core  (:8000)       │  nayantra/core
                     │  WorldModel (SQLite registry) │
                     │  EventBus · Alerts            │
                     │  TaskManager · Allocator      │
                     │  FleetManager · RobotExecutor │
                     │  TrafficCoordinator           │
                     │  SafetyGuard · Confirmations  │
                     │  SimEngine (built-in kinematic│
                     │  simulator)                   │
                     │  legacy rmf-web-shaped routes │
                     └───┬───────────────────────┬───┘
          CoreClient     │                       │  RobotAdapter interface
      ┌──────────────────▼─────┐   ┌─────────────▼───────────────────────┐
      │ MCP server (:7000)     │   │ SimAdapter   (ugv/uav/legged/human) │
      │ validated, high-level  │   │ Nav2Adapter  (shared rclpy runtime, │
      │ tools; dangerous ops → │   │               namespaced)           │
      │ pending confirmation   │   │ IsaacDemoAdapter (HTTP :8900)       │
      └──────────▲─────────────┘   │ Unsupported (mqtt/rest/ws: explicit │
                 │                 │               "not implemented")    │
      ┌──────────┴─────────────┐   └─────────────┬───────────────────────┘
      │ Agent (:8080)  LLM     │                 │ ROS 2 DDS / Zenoh / HTTP
      │ intent → structured    │        Isaac Sim · Gazebo · real robots
      │ task; never motion     │
      └────────────────────────┘
```

### Key decisions

| # | Decision | Why |
|---|---|---|
| D1 | New package `nayantra/core` is the single control plane. It replaces `rmf_bridge` + `rmf_stub_server` on port 8000. | One owner of world state. It's a drop-in on the port every existing config already points at. |
| D2 | **Waypoints are nav-graph vertices.** Lanes reference waypoint ids. Zones are polygons. There is one map model, stored in SQLite and seeded from JSON in `config/maps/`. | "One authoritative representation." Operators edit through the UI and API, never Python. |
| D3 | The legacy rmf-web-shaped routes (`/fleets`, `/tasks/dispatch_task`, `/building_map`, …) are kept at the root of the core server and backed by the core. The new API lives under `/api/v1`. | Existing MCP deployments, `rmf_client` and scripts keep working. |
| D4 | Traffic = **predictive schedule + deterministic runtime reservations.** At plan time, per-robot itineraries with ETAs are checked pairwise and turned into precedence constraints (delay), reroutes or make-way requests. At run time a robot must atomically hold its next lane, next node and any corridor before moving. | Prediction prevents conflicts. Reservations *guarantee* mutual exclusion even when ETAs drift. Lidar stays the last layer. |
| D5 | The LLM talks only to high-level MCP tools with Pydantic-validated params. Robot selection is done by the allocator. Dangerous operations return `confirmation_required` and are held in the core until an operator confirms in the UI. The LLM has no confirm tool. | Safety must not depend on the LLM. |
| D6 | Adapters expose `navigate(pose)` per graph hop, like Open-RMF full-control fleet adapters. The fleet layer, not the robot, decides when the next hop is allowed. | Gives the traffic coordinator control while Nav2/PX4 still does local planning. |
| D7 | The built-in simulator is labelled as kinematic everywhere. It reports only what it knows. Health fields such as CPU and temperature stay `null` and the UI shows "not reported". | No fake telemetry. |
| D8 | Open-RMF compatibility path: the nav graph exports to the `rmf_fleet_adapter` nav-graph YAML shape, and adapters map onto EasyFullControl callbacks (`navigate`, `stop`, `action`). | Leaves room to swap in real Open-RMF later. |
| D9 | Open-RMF infrastructure tools (doors, lifts, dispensers, fire alarm) register only when `OPENRMF_INFRA_TOOLS=true`, i.e. a real rmf-web api-server is configured. | Stops the LLM being told to open doors that don't exist. |

### Traffic coordination detail

- **Resources:** `node:<waypoint>` and `lane:<lane>` (a bidirectional lane is one
  resource, which prevents head-on meetings). Geometrically crossing lanes and
  nodes closer than the clearance distance are precomputed into conflict sets.
- **Corridors:** a maximal chain of lanes through degree-2 vertices is reserved
  as a unit, so two robots never enter a single-width corridor from opposite ends.
- **Plan time:** A* over open lanes. It honours one-way lanes, restricted zones,
  fleet layer (ground/air), lane width against footprint, and speed limits.
  The resulting itinerary (`resource`, `t_enter`, `t_exit`) is compared with
  every other robot's itinerary and parked robot. Each overlap becomes a
  `Conflict` classified as same_lane / head_on / intersection / crossing /
  bottleneck / clearance. The lower-priority robot (task priority, then
  first-come) yields in one of three ways:
  - *delay*: a precedence constraint "enter R only after X has released R", explained as "UGV-03 delayed ≈4.2 s at I2 for UGV-01"
  - *reroute*: when the alternative arrives sooner than waiting
  - *make-way*: an idle robot on the only path is sent to a free parking node
- **Run time:** atomic all-or-nothing acquisition, release on progress, and a
  wait-for graph for deadlock detection (the lowest priority robot in the cycle
  replans; otherwise an operator alert). Every wait carries a human-readable
  reason: "waiting for lane L12 held by UGV-01".
- **Final safety layer (sim):** a forward stop-zone check per robot, analogous
  to Nav2's collision_monitor. It triggers `SAFETY_STOP` if coordination ever
  fails.
- **UAVs:** lanes and waypoints carry `layer` and `altitude`. Air lanes conflict
  only within the same altitude band. Landing pads are shared node resources;
  `no_fly` zones restrict the air layer. This is an abstraction for
  corridor-based airspace, not a 3D planner. PX4/ArduPilot and a dedicated
  airspace planner plug in behind `Uav*` adapters later.

### Localization / SLAM

Localization is a per-robot configuration field (`amcl`, `slam_toolbox`,
`cuvslam`, `rtabmap`, `gps`, `ground_truth`). Nav2 adapters report
`localization: ok | degraded | lost` from TF freshness. The UGV pipeline
(slam_toolbox → map → map_server → AMCL → Nav2) is a launch-level concern and
is tracked in `docs/nav2_stack_plan.md`. Nothing in the core assumes one method.

## 4. Files to modify

| File | Change |
|---|---|
| `nayantra/config.py` | Core settings (`CORE_*`, `NAYANTRA_DB_PATH`, `NAYANTRA_SCENARIO`, `OPENRMF_INFRA_TOOLS`, `NAYANTRA_CORE_URL`). `WAYPOINTS_FILE` deprecated. |
| `nayantra/mcp/tools.py`, `server.py` | Tools run against `CoreClient` with validated params and risk levels. Infra tools are gated. |
| `nayantra/agent/agent.py` | New system prompt. Agentic loop for all providers. Mission trace propagated to MCP → core. |
| `nayantra/rmf_bridge/server.py` | Becomes a compatibility launcher for the core (honours `FLEET_NAME`/`ROBOT_NAME`/`ROS2_ENABLED`). |
| `nayantra/ros2_adapter/fleet_adapter.py` | `WAREHOUSE_WAYPOINTS` becomes a read-only view of `config/maps/isaac_warehouse.json`. |
| `nayantra/rmf_client/client.py` | Debug mode no longer fabricates doors, lifts or a building map. |
| `scripts/nav_cli.py`, `scripts/isaac_demo.py` | Load waypoints from the core API, falling back to the seed map JSON. Built-in tables removed. |
| `scripts/start.sh`, `start.ps1`, `stop.*`, `docker/docker-compose.yml` | Start `nayantra-core` instead of the stub. Build the web UI. |
| `pyproject.toml`, `.github/workflows/ci.yml` | `nayantra-core` entry point. Web build job. |
| `README.md`, `docs/architecture.md` | Point to the new architecture. |

## 5. Files to create

```
nayantra/core/
  models.py          Pydantic schemas (maps, waypoints, lanes, zones, fleets,
                     robots, state, tasks, traffic, events, confirmations)
  store.py           SQLite document store
  events.py          EventBus, event types, alerts
  world.py           WorldModel: registries, validation, seeding, snapshots
  geometry.py        polygons, segments, distances
  routing.py         graph index, A*, corridor detection
  traffic.py         TrafficCoordinator (schedule, reservations, conflicts,
                     negotiation, deadlock detection)
  safety.py          SafetyGuard hard limits
  allocation.py      capability/cost-based task allocation with explanations
  tasks.py           TaskManager + explicit state machine
  fleet.py           FleetManager + per-robot RobotExecutor
  confirmations.py   pending dangerous actions
  sim.py             SimEngine (clock, collision stop, battery)
  adapters/{base,sim,nav2,ros2_runtime,isaac_demo,unsupported}.py
  api.py  ws.py  legacy_rmf.py  server.py
nayantra/mcp/core_client.py
config/maps/{isaac_warehouse,warehouse_demo}.json
config/scenarios/{warehouse_demo,isaac_carter}.json
web/                 React + TypeScript + Vite operator UI
tests/core/          unit + scenario tests (routing, traffic, allocation,
                     tasks, API, end-to-end vertical slice)
```

Removed: `docker/rmf_stub_server.py`, `docker/Dockerfile.stub` (duplicate
waypoints and canned data; the core replaces them).

## 6. Migration plan

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | This analysis; waypoint unification; branch | — |
| 1 Foundation | models, store, world, events, seed maps, REST API, legacy routes | `tests/core` green; old suite green |
| 2 Dashboard | web UI: live map, fleets, robot panel, tasks, events, WS | UI builds; renders live state from a running core |
| — Vertical slice | register robot → fleet → map → pick waypoint → task → allocate → navigate → live pose → complete | automated e2e test plus a manual UI run |
| 3 Multi-robot UGV | SimAdapter ×N, Nav2Adapter with shared rclpy and namespaces, allocator | allocation tests |
| 4 Traffic | reservations, conflict detection and resolution, traffic layer in UI | scenario tests: head-on, intersection, corridor, swap deadlock, idle blocker |
| 5 NL | MCP tools over core, agent loop, confirmations | tool tests; manual LLM run |
| 6 Simulation | Isaac multi-robot (namespaced drive graphs), synchronized dashboard | on the RTX workstation |
| 7 Heterogeneous | UAV/quadruped/humanoid sim adapters, air layer, capability allocation | scenario tests |
| 8 Real robots | Nav2 on hardware, localization, telemetry, safety integration | on hardware |

Backward compatibility kept throughout:
- `python -m nayantra.rmf_bridge.server` still starts a Nav2-backed control plane on :8000.
- The old MCP tool names `list_robots`, `move_robot`, `dispatch_task` and `get_task_state` still work.
- `scripts/run_demo.sh` is unchanged.
