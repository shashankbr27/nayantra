# Architecture Deep-Dive

## Overview

Nayantra is a **layered architecture** that decouples intent (natural language)
from execution (robot hardware) through well-defined interfaces at every layer.

```
┌──────────────────────────────────────────────────────────────────────┐
│                         INTENT LAYER                                 │
│  User / Web API  ──→  AI Agent  ──→  Multi-step Planner              │
│                       (LLM tool-use)   (dependency graph)            │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  Structured tool calls
┌────────────────────────────▼─────────────────────────────────────────┐
│                       PROTOCOL LAYER                                 │
│              MCP Server (FastAPI + SSE + JWT auth)                   │
│              Declarative tool registry (25+ tools)                   │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  HTTP REST
┌────────────────────────────▼─────────────────────────────────────────┐
│                     FLEET MANAGEMENT LAYER                           │
│              Open-RMF REST API (rmf-web)                             │
│              Fleet adapter / traffic negotiation                     │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  RMF Fleet Adapter protocol
┌────────────────────────────▼─────────────────────────────────────────┐
│                      TRANSPORT LAYER                                 │
│   LAN: ROS 2 DDS (direct)   │   WAN: Zenoh bridge ↔ Zenoh network   │
└────────────────────────────┬─────────────────────────────────────────┘
                             │  ROS 2 topics / services
┌────────────────────────────▼─────────────────────────────────────────┐
│                       ROBOT LAYER                                    │
│   Robot Adapter ──→ Nav2 (navigation) ──→ Hardware / Isaac Sim       │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. AI Agent (`nayantra/agent/`)

**agent.py — Core reasoning loop**

```
Command
  │
  ▼
_get_tools()           ← cache 60s, fallback to tools.json
  │  tool schemas
  ▼
_plan_with_anthropic() or _plan_with_openai()
  │  AgentPlan (list of ToolCall steps)
  ▼
TaskPlanner.validate() ← cycle detection, index bounds check
  │
  ▼
execute_plan()
  ├── For each step group (parallel group from topo-sort):
  │     execute_group_parallel() ← asyncio.gather
  │         _execute_step()
  │             _resolve_params()  ← {{placeholder}} substitution
  │             POST /run to MCP
  │             _extract_ids()     ← task_id, robot_id propagation
  │
  ▼
_summarise()           ← LLM natural language summary
  │
  ▼
MissionResult
```

**Key design decision: why two LLM providers?**

Different operators have different compliance/cost requirements. Claude is
superior for complex multi-step reasoning; GPT-4o has a larger context window.
`LLM_PROVIDER` in `.env` switches between them at startup with no code change.

**Streaming vs blocking**

`agent.run()` blocks until the full mission completes — good for CLI and simple
integrations. `agent.stream_run()` yields SSE events after each step — essential
for live UI feedback with long multi-robot missions.

---

### 2. MCP Server (`nayantra/mcp/`)

**Why MCP?**

The [Model Context Protocol](https://modelcontextprotocol.io) is a standardised
interface between LLMs and tools. Using MCP means any MCP-compatible LLM or
client can drive our robot fleet — not just our agent.

**Tool registry pattern**

```python
@_tool({
    "name": "move_robot",
    "description": "...",
    "parameters": { ... },
})
async def _move_robot(client: OpenRMFClient, params: dict) -> Any:
    ...
```

The `@_tool` decorator registers both the JSON schema (sent to the LLM) and the
async handler (called at runtime) in a single block. The dispatcher in
`execute_tool()` is a simple dict lookup — O(1), no if/elif chains.

**SSE fan-out**

Every `POST /run` call publishes the result to a list of asyncio Queues. Each
`GET /sse` subscriber holds one Queue. This allows multiple UI clients to watch
the same operation without any message broker.

---

### 3. OpenRMF Client (`nayantra/rmf_client/`)

**Debug mode**

When `DEBUG_MODE=true`, every method returns a realistic simulated response
without any network I/O. This makes the entire stack testable on a laptop with
no robots, no GPU, and no RMF server — just `pytest`.

**Retry policy**

Transport errors (network blips) are retried 3× with exponential back-off
(1s, 2s, 4s) via tenacity. HTTP 4xx/5xx errors are **not** retried because
they represent application-level errors that won't self-heal.

---

### 4. Isaac Sim Bridge (`nayantra/isaac_sim/`)

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

### 5. Zenoh Bridge (`nayantra/zenoh_bridge/`)

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

## Data Flow: "Send robot R1 to charging dock"

```
1. User types command
   │
2. agent.run("Send robot R1 to charging dock")
   │
3. LLM (Claude/GPT) receives:
   - system prompt with tool descriptions
   - user command
   → Produces tool calls: [list_robots, move_robot]
   │
4. AgentPlan validated by TaskPlanner
   │
5. Step 0: POST /run  { tool: "list_robots" }
   → MCP → OpenRMF GET /fleets
   → Returns fleet + robot list
   → context["fleet_name"] = "turtlebot_fleet"
   │
6. Step 1: POST /run  { tool: "move_robot",
                        fleet_name: "turtlebot_fleet" (enriched),
                        robot_name: "R1",
                        waypoint: "charging_dock" }
   → MCP → OpenRMF POST /tasks/dispatch_task
   → Returns task_id: "task-abc-123"
   → context["task_id"] = "task-abc-123"
   │
7. (If real hardware) OpenRMF → Fleet Adapter → Nav2 → Robot moves
   (If Isaac Sim)     OpenRMF → Fleet Adapter → Isaac Sim Bridge → Nav2 sim
   │
8. LLM summarises: "Robot R1 has been successfully dispatched to the
   charging dock. Task ID: task-abc-123."
   │
9. Result returned to user / streamed via SSE
```

---

## Security Model

| Layer | Mechanism |
|---|---|
| Agent API | (Optional) API key or OAuth — add via FastAPI dependency |
| MCP Server | HS256 JWT (`USE_AUTH=true`) — validated on every request |
| OpenRMF API | Bearer JWT in `Authorization` header |
| Zenoh | TLS mutual auth (production deployment) |
| Isaac Sim | Local-only REST API (bind to loopback in production) |

The JWT secret is configured via `JWT_SECRET` in `.env` and must match the
secret configured on the RMF server side. Rotate tokens with
`python scripts/generate_token.py`.

---

## Extending the System

### Add a new RMF tool

1. Add handler in `nayantra/mcp/tools.py`:
```python
@_tool({
    "name": "my_tool",
    "description": "Does X.",
    "parameters": { "param": { "type": "string" } },
})
async def _my_tool(client, params):
    return await client.my_method(params["param"])
```

2. Add method + debug stub in `nayantra/rmf_client/client.py`
3. Add test in `tests/test_mcp_server.py`

### Add a new LLM provider

1. Add an `elif` branch in `RMFAgent._setup_llm_client()`
2. Add corresponding `_plan_with_<provider>()` method
3. Add to `LLM_PROVIDER` literal in `nayantra/config.py`

### Add a new transport (e.g. MQTT)

Create `nayantra/mqtt_bridge/bridge.py` following the same pattern as the Zenoh
bridge. The MCP server and agent layers are transport-agnostic.
