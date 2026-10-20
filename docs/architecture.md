# Architecture Deep-Dive

> For the multi-fleet platform design, see
> [platform_architecture.md](platform_architecture.md). It covers the
> decisions, the traffic algorithm, the migration plan and backward
> compatibility. This page is the component-level reference.

## Overview

Nayantra separates **intent** (natural language, operator clicks) from
**execution** (robots). The **Nayantra Core** sits between the two. It owns
the world model and makes every allocation, routing, traffic and safety
decision. Neither the LLM nor the UI talks to robots directly.

```
┌──────────────────────────────────────────────────────────────────────┐
│                         INTENT LAYER                                 │
│  Operator UI (web/, served at :8000/)     NL: CLI · UI command bar   │
│  REST /api/v1 + WebSocket /api/v1/ws      AI Agent (:8080)           │
│                                           think → act → observe loop │
└───────────────┬───────────────────────────────────┬──────────────────┘
                │                                   │  tool calls
                │                    ┌──────────────▼──────────────────┐
                │                    │ MCP server (:7000)              │
                │                    │ Pydantic-validated, risk-tagged │
                │                    │ high-level tools (REST /run)    │
                │                    └──────────────┬──────────────────┘
                │                                   │  REST /api/v1
┌───────────────▼───────────────────────────────────▼──────────────────┐
│                   NAYANTRA CORE (:8000) — control plane              │
│  World registry · Task manager + allocation · Traffic coordinator    │
│  Fleet manager (executors) · Safety guard · Confirmations            │
│  Event bus + alerts · SQLite document store                          │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  RobotAdapter interface
┌────────────────────────────▼─────────────────────────────────────────┐
│                       ADAPTER LAYER                                  │
│  SimAdapter (built-in)  │  Nav2Adapter (rclpy)  │  IsaacDemoAdapter  │
│                         │  LAN: DDS / WAN: Zenoh│  (HTTP)            │
└────────────────────────────┬─────────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────────┐
│                       ROBOT LAYER                                    │
│   Kinematic simulator │ Nav2 → hardware / Isaac Sim │ Isaac demo     │
└──────────────────────────────────────────────────────────────────────┘
```

Open-RMF is **optional infrastructure**. With `OPENRMF_INFRA_TOOLS=true`
the MCP server also exposes door, lift and dispenser tools through
`nayantra/rmf_client`. The core's traffic coordination is inspired by
Open-RMF, but it is Nayantra's own implementation, not `rmf_traffic`.

---

## Component Details

### 1. Nayantra Core (`nayantra/core/`)

| Module | Responsibility |
|---|---|
| `models.py` | Pydantic schemas for every entity and request (`extra="forbid"`), plus the event types |
| `store.py` | SQLite document store (WAL) for documents, events and meta |
| `world.py` | `WorldRegistry` for maps, waypoints, lanes, zones, fleets and robots. Full CRUD with integrity checks (bounds, unique names, references, spawn not in a forbidden zone) and fuzzy name resolution ("Did you mean …") |
| `routing.py` | `MapGraph`: lane rules per robot profile, speeds, conflict sets for close or crossing lanes, A*, and explanations for unreachable goals |
| `traffic.py` | `TrafficCoordinator`: prioritized space-time planning (SIPP) over a reservation table, execution ordering per resource, conflict classification, negotiation (delay, reroute, priority, make-way, joint re-planning), deadlock detection and stale-wait re-planning |
| `tasks.py` | `TaskManager`: an explicit transition table (`queued → assigned → planning → executing ⇄ waiting_for_traffic → completed / failed / cancelled`, plus `waiting_for_robot` and `paused`), place resolution (waypoint or zone), step building and allocation retry |
| `allocation.py` | Filters robots by capability, map, layer, payload, reachability and battery, then scores them by cost and queue. Returns an `Allocation` that explains the choice and every rejection |
| `fleet.py` | `FleetManager`: a per-robot executor with pause/cancel/e-stop checkpoints, charging and auto-charge, a zone monitor, make-way moves and operator commands |
| `safety.py` | `SafetyGuard`: forbidden zones, restricted targets and speed caps |
| `confirmations.py` | Parks dangerous actions until an operator confirms them (TTL 180 s). Requests from the MCP client can never confirm |
| `events.py` | `EventBus`: persisted events, raising and resolving alerts, and WebSocket fan-out with resync on lag |
| `adapters/` | `RobotAdapter` interface: `connect`, `snapshot`, `follow_path`, `stop`, `pause`, `resume`, `emergency_stop`, `release_estop` |
| `api.py`, `ws.py` | `/api/v1` REST (robots, fleets, maps, waypoints, lanes, zones, tasks, traffic, events, alerts, confirmations, sim, agent relay) and the `/api/v1/ws` live world model |
| `legacy_rmf.py` | The old rmf-web-shaped routes, backed by the core. They exist only for compatibility |
| `server.py` | `nayantra-core`: the app factory; it also serves `web/dist` |

**Design rules**

- The LLM never allocates robots or sends velocities. It creates tasks and
  commands, and the core decides.
- Every state change is an explicit transition that emits an event.
- Every wait or failure carries a human-readable reason. Examples: "waiting
  for ugv_02 to clear the narrow corridor", "no robot with payload ≥ 20 kg
  can reach Storage Rack C".

### 2. AI Agent (`nayantra/agent/`)

**agent.py: a provider-agnostic tool-use loop**

```
Command
  │
  ▼
_get_tools()          ← MCP GET /tools (cached 60 s; config/tools.json fallback)
  │
  ▼
provider.start(command, tools)
loop (≤ 12 iterations):
    turn = provider.next_turn()        ← LLM picks tool calls or answers
    for call in turn.calls:
        POST MCP /run  {tool, params, context: {mission_id, command}}
    provider.add_results(...)          ← tool results, errors included
  │
  ▼
MissionResult (summary + every step)
```

Providers are Anthropic, OpenAI and Gemini (`LLM_PROVIDER`). Each one
converts the same tool schemas into its own format. Tool errors, such as an
unknown waypoint with "Did you mean" suggestions or a missing confirmation,
go back to the model, so it can correct itself or explain the problem.

`agent.run()` blocks until the mission is done. `agent.stream_run()` yields
`status`, `step_start`, `step_done` and `done` events. The operator UI
command bar uses the streaming form through the core's
`POST /api/v1/agent/command` relay.

---

### 3. MCP Server (`nayantra/mcp/`)

It is a REST transport built around MCP tool semantics: `GET /tools`,
`POST /run` and `GET /sse`. It is not the MCP JSON-RPC wire protocol.

**Tool registry pattern**

```python
class RobotParams(Params):          # Pydantic, extra="forbid"
    robot: str = Field(..., description="Robot id or name")

@_tool("stop_robot", "Stop one robot now and cancel its current task.",
       RobotParams, risk="act", endpoint="POST /robots/{id}/stop")
async def _stop_robot(ctx: ToolContext, p: RobotParams) -> Any:
    return await ctx.core.post(f"/robots/{p.robot}/stop", {...}, trace=ctx.trace)
```

- Parameters are validated before the handler runs. `/run` answers 422 for
  bad parameters and 404 for an unknown tool.
- Every tool has a risk level: `read`, `act` or `dangerous`. Dangerous tools
  (stop all, e-stop all, remove robot …) come back as *pending
  confirmation*. The core refuses to let the MCP client confirm
  (`X-Nayantra-Client: mcp`).
- Trace headers (`X-Nayantra-Mission`, `X-Nayantra-Command`,
  `X-Nayantra-Tool`) are stored on tasks. This lets the UI show which NL
  command created which task.
- Legacy tool names (`move_robot`, `dispatch_task` …) still run but are
  hidden from `/tools`.

---

### 4. Operator UI (`web/`)

React + TypeScript + Vite, with zustand stores. The world store is filled
by the `/api/v1/ws` snapshot and then kept current by incremental events.

- **Operations:**
  - Live SVG map with pan, zoom, layers, selection and interpolated
    robot poses.
  - Fleet sidebar with simulation controls.
  - Inspector: a robot command center, plus waypoint and multi-select panels.
  - Bottom dock: events, alerts, tasks, traffic and system.
  - NL command bar.
- **Fleets / Robots & Tasks:** overviews, the task composer, and the task
  drawer (allocation trace, "why waiting").
- **Robot registration:** a 7-step wizard ending in a live connection test.
- **Map editor:** waypoints, lanes and zones; background image upload
  (PNG/PGM + YAML); rmf nav-graph export.

---

### 5. Optional Open-RMF client (`nayantra/rmf_client/`)

Used only with `OPENRMF_INFRA_TOOLS=true`, for doors, lifts, dispensers
and ingestors on a real Open-RMF server. Transport errors are retried 3×
with exponential back-off. HTTP 4xx/5xx errors are not retried. In
`DEBUG_MODE` it returns empty infrastructure lists rather than invented
devices.

---

### 6. Isaac Sim Bridge (`nayantra/isaac_sim/`)

**Integration points**

| Channel | Use |
|---|---|
| Kit HTTP REST API (`localhost:8211`) | Load scene, spawn/delete robot prims, set poses |
| ROS 2 Bridge extension (via `ros2_bridge` topic) | Nav2 goals, odometry, TF |

**Stub mode**

When `ISAAC_SIM_ENABLED=false`, `IsaacSimBridge` returns stub dicts for all
methods. This means CI pipelines, unit tests, and developer laptops all work
without an NVIDIA GPU.

**RobotSpawner**

`RobotSpawner` sits above the bridge and manages a fleet:
- Spawns multiple robots concurrently via `asyncio.gather`
- Maintains an in-memory pose registry updated by `poll_states()`
- Runs a background `asyncio.Task` that publishes state at 1 Hz (configurable)

---

### 7. Zenoh Bridge (`nayantra/zenoh_bridge/`)

**When to use Zenoh vs direct DDS**

| Scenario | Recommendation |
|---|---|
| Same LAN / subnet | `ZENOH_ENABLED=false` — ROS 2 DDS multicast works natively |
| Different subnets | `ZENOH_ENABLED=true` — Zenoh unicast peers across NAT/WAN |
| Cloud ↔ Robot | `ZENOH_ENABLED=true` + `ZENOH_MODE=router` on the cloud side |

**Topic mapping**

The `TOPIC_MAP` list in `bridge.py` is the single configuration point for
which ROS 2 topics are bridged and in which direction. No code changes needed
to add new topics — just extend the list.

---

## Data Flow: "Send a ground robot to Charger 1"

```
1. The operator types the command in the UI command bar
   → POST /api/v1/agent/command (core) → relayed to the agent API /stream
   │
2. The agent loop: the LLM receives the system prompt, the MCP tool list
   and the command
   → calls create_task {task_type: "navigate", destination: "Charger 1",
                        fleet_id: "warehouse_ugv"}
   │
3. MCP /run validates the params (Pydantic)
   → POST core /api/v1/tasks with the trace headers
   │
4. Core TaskManager
   → resolves "Charger 1" to waypoint CH1
   → task QUEUED
   → allocate(): picks a robot and explains the choice, e.g. "ugv_02:
     idle, reachable in 14 s, 86 % battery; ugv_01 busy with a delivery"
   → ASSIGNED
   │
5. FleetManager executor
   → TrafficCoordinator.request_route(): a space-time plan against current
     reservations
   → PLANNING → EXECUTING
   → the robot waits for traffic at the corridor (WAITING_FOR_TRAFFIC,
     with the reason) → EXECUTING
   → SimAdapter / Nav2Adapter follow_path
   │
6. Every transition emits an event
   → WebSocket → the UI shows the robot moving, the task timeline and any
     conflict banner
   │
7. The tool result returns to the LLM, which may call get_task_status
   → summary: "ugv_02 is on its way to Charger 1 (task …)"
   │
8. COMPLETED: the reservation is released and the robot parks on CH1
```

---

## Security Model

| Layer | Mechanism |
|---|---|
| Core API / UI | Binds to `127.0.0.1` by default (`CORE_HOST`). Dangerous operations need operator confirmation. The MCP client can never confirm |
| Agent API | Binds to `127.0.0.1` by default. Add API-key or OAuth auth via a FastAPI dependency |
| MCP Server | HS256 JWT (`USE_AUTH=true`), validated on every request |
| Safety | Forbidden zones, speed caps and e-stop are enforced in the core and adapters, independent of the LLM |
| Open-RMF API (optional) | Bearer JWT in the `Authorization` header |
| Zenoh | TLS mutual auth (production deployment) |
| Isaac Sim | Local-only REST API (bind to loopback in production) |

The JWT secret is configured via `JWT_SECRET` in `.env`. Rotate tokens with
`python scripts/generate_token.py`.

---

## Extending the System

### Add a robot type or protocol

1. Implement `RobotAdapter` in `nayantra/core/adapters/<name>.py`. Look at
   `sim.py` for the full interface and `isaac_demo.py` for a minimal HTTP
   adapter.
2. Map the protocol to it in `create_adapter()` in
   `nayantra/core/adapters/__init__.py`. Add the protocol to `Protocol` in
   `models.py` if it is new. `mqtt`, `rest`, `websocket` and `custom`
   already exist and use `UnsupportedAdapter` today.
3. The registration wizard picks it up from `/api/v1/meta`.

### Add an MCP tool

1. Add a `Params` model and an `@_tool(...)` handler to
   `nayantra/mcp/tools.py`. Give it a risk level, and have it call the core
   API through `ctx.core`.
2. Regenerate `config/tools.json` if you rely on the offline fallback.
3. Add a test in `tests/test_mcp_server.py`.

Tools should express intent (create or cancel a task, stop a robot). They
should not micro-manage motion.

### Add an LLM provider

1. Subclass `_Provider` in `nayantra/agent/agent.py`. Implement `start`,
   `next_turn`, `add_results` and `summarise`, and write a schema
   converter.
2. Register it in `_PROVIDERS` and in the `LLM_PROVIDER` literal in
   `nayantra/config.py`.
3. Test it with a scripted provider (see `tests/test_agent_loop.py`).

### Add a new transport (e.g. MQTT)

Create `nayantra/mqtt_bridge/bridge.py` following the same pattern as the
Zenoh bridge. The core, MCP and agent layers are transport-agnostic.
