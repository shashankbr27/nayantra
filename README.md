# 🤖 Nayantra — Agentic Operations Platform for Heterogeneous Robot Fleets

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-green.svg)](https://www.python.org/)
[![ROS 2 Humble | Jazzy](https://img.shields.io/badge/ROS%202-Humble%20%7C%20Jazzy-orange.svg)](https://docs.ros.org/)
[![Isaac Sim 6.0+](https://img.shields.io/badge/Isaac%20Sim-6.0%2B-brightgreen.svg)](https://developer.nvidia.com/isaac-sim)

> **Say what you want done. Nayantra decides who does it, which way they go, and whether it is safe, then explains why.**

Nayantra is a control plane for running several robot fleets (ground
vehicles, drones, quadrupeds, humanoids) on one shared map. It sits **above**
the robots' own navigation stacks. Operators work in a mission-control UI or
give plain-English commands. The **Nayantra Core** owns the world model and
makes every allocation, routing, traffic and safety decision:

- **Agent-native.** The LLM talks to typed, risk-tagged tools that create
  *tasks*. It never sends velocities, and it can never confirm a dangerous
  action. That is a property of the tool layer, not of the prompt.
- **Explainable allocation.** The core's task manager, not the LLM, picks the
  robot. Every choice carries a trace of the candidates it weighed and why
  the winner won.
- **Safety independent of intelligence.** A safety kernel enforces zones,
  speed caps and emergency stops in the core. It does not consult the LLM,
  the allocator or the traffic planner.
- **Stack-agnostic.** A robot is anything that can follow a cleared path and
  report progress. Nav2 on ROS 2 (Humble or Jazzy), Isaac Sim 6.0+, and
  vendor or custom stacks all plug in through one adapter contract.

> **Relationship to Open-RMF.** Nayantra borrows vocabulary (nav graph,
> lane, reservation) and is **not** Open-RMF: it does not run `rmf_traffic`
> and does not need Open-RMF anywhere in the path. A real Open-RMF server
> remains available as optional infrastructure (`OPENRMF_INFRA_TOOLS=true`);
> see [How this differs from Open-RMF](#-how-this-differs-from-open-rmf).
>
> The MCP server is a REST transport built around MCP tool semantics
> (`/tools`, `/run`, `/sse`). It is not the MCP JSON-RPC wire protocol.

---

## 📐 Architecture

Three planes with one rule: **intent flows down, state flows up, and only the
core decides.**

```
 INTENT PLANE — who asks, and how
 ┌───────────────────────────────────────────────────────────────────────┐
 │  Operator UI (React, :8000/)        Natural language (CLI · command bar)│
 │  REST /api/v1 + WebSocket                   │                          │
 │        │                         Agent API (:8080)  Claude·GPT·Gemini  │
 │        │                         think → act → observe                 │
 │        │                                    │ tool calls               │
 │        │                         MCP server (:7000)                    │
 │        │                         schema-validated, risk-tagged tools;  │
 │        │                         cannot confirm dangerous actions      │
 └────────┼────────────────────────────────────┼─────────────────────────┘
          │  tasks, not velocities             │
 ORCHESTRATION PLANE — Nayantra Core (:8000), the only place decisions are made
 ┌────────▼────────────────────────────────────▼─────────────────────────┐
 │  World model     maps · waypoints · lanes · zones · fleets · robots    │
 │        │            (single source of truth, SQLite)                   │
 │  Task manager ──► Allocator (capability · reach · battery · cost,      │
 │        │            returns an explanation)                            │
 │        └───────► Traffic coordinator (space-time reservations,         │
 │                     conflict negotiation, deadlock detection)          │
 │  Safety kernel    zones · speed caps · e-stop · confirmation gate      │
 │  Event bus        audit trail, alerts, WebSocket fan-out               │
 └───────────────────────────────┬───────────────────────────────────────┘
                                 │  cleared path in  ▼   ▲  pose, battery, progress out
 EMBODIMENT PLANE — Robot adapter contract (nayantra/core/adapters/)
 ┌───────────────────────────────▼───────────────────────────────────────┐
 │  Nav2Adapter (ROS 2 Humble | Jazzy)     IsaacDemoAdapter (HTTP)        │
 │  vendor / custom adapters (same contract)                              │
 └──────────┬───────────────────────────────────────┬────────────────────┘
            │ LAN: ROS 2 DDS                        │ multi-site: Zenoh bridge
   ┌────────▼─────────┐                    ┌────────▼─────────┐
   │ Isaac Sim 6.0+   │                    │ Physical robots  │
   │ (Nav2 + ROS 2    │                    │ (Nav2 + hardware)│
   │  Bridge)         │                    └──────────────────┘
   └──────────────────┘
```

Why this shape:

- **Decisions live in one place.** The UI, the LLM and the CLI all produce
  the same object, a task. There is no second path to a robot.
- **The adapter contract is narrow.** The core hands an adapter a path it has
  already cleared with the traffic coordinator and extends it lane by lane.
  The adapter drives it with whatever the robot has and reports progress.
  Fields an adapter cannot measure stay empty and the UI shows them as "not
  reported".
- **Robots keep their own local autonomy.** Obstacle avoidance and control
  stay in Nav2 (or the vendor stack). The core decides *where and when*, not
  *how*.

### 🔀 How this differs from Open-RMF

| | Open-RMF | Nayantra |
|---|---|---|
| **Layer** | ROS 2 middleware; fleet adapters live inside its graph | Control plane *above* the robots; ROS 2 is one adapter family |
| **Entry point** | Task requests from the RMF API / dashboard | Operator clicks **or** natural language, both compiled to tasks by typed tools |
| **Allocation** | Bidding between fleet adapters | Central allocator that returns a readable why-this-robot trace |
| **Map & world** | Building maps and traffic-editor YAML | One editable world model (maps, lanes, zones, fleets) with integrity checks |
| **Safety** | Traffic negotiation | Independent safety kernel; the LLM path can never confirm dangerous actions |
| **Airspace** | Ground-centric | Altitude layers for UAVs in the same planner |
| **Requires ROS 2** | Yes | No (only for ROS 2 adapters) |
| **Together** | | Open-RMF can be attached as optional infrastructure |

Deep dives:

- [docs/platform_architecture.md](docs/platform_architecture.md): the design,
  its decisions, traffic coordination and the migration plan.
- [docs/architecture.md](docs/architecture.md): components, security model
  and extension guide.

---

## ✨ Features

| Area | What exists today |
|---|---|
| 🗺 **World model** | One authoritative store for maps, waypoints (nav-graph vertices), lanes (one-way, speed limit, width, layer, altitude, closed) and zones (restricted, no-fly, slow, fleet boundary …). Full CRUD with integrity checks. Maps and scenarios are seeded from `config/`. |
| 🚚 **Multi-fleet** | Fleets of UGVs, UAVs, quadrupeds and humanoids with their own capabilities, payloads, battery models and routing rules. A 7-step registration wizard ends in a live connection test. |
| 📋 **Tasks** | Navigate, delivery, patrol, inspection, charge and more. Statuses run from `QUEUED` to `COMPLETED`, `FAILED` or `CANCELLED`, and every task carries a "why is it waiting" reason. Allocation weighs capability, map, layer, payload, reachability, battery, queue and cost, and returns a readable trace. |
| 🚦 **Traffic** | Prioritized space-time (SIPP) planning over a reservation table. An execution order graph keeps robots deadlock-free when they are delayed. Conflicts are classified (head-on, intersection, bottleneck, …) and resolved by delay, reroute, priority, make-way or joint re-planning. Airspace is split into altitude layers for UAVs. |
| 🛡 **Safety** | Forbidden-zone enforcement, speed caps and a forward stop zone in the simulator. Emergency stop works per robot, per fleet or for everything. Dangerous commands need operator confirmation, which the MCP/LLM path can never give. |
| 🖥 **Operator UI** | Mission-control dashboard: an interactive map (zoom, pan, layers, selection, live robot poses), fleet overview, robot command center, task composer and drawer, traffic and conflict view, alerts and events, map/graph editor, and Start/Pause/Stop for simulated fleets. Dark and light themes. |
| 🧠 **Natural language** | Provider-agnostic agent (Anthropic / OpenAI / Gemini) that uses about 25 high-level tools. It creates tasks and never drives robots directly. Commands are traced end to end (mission → command → tool → task). |
| 🔌 **Adapters** | One adapter interface. Implementations: Nav2 over ROS 2 (Humble and Jazzy) and the Isaac demo over HTTP. Protocols that aren't implemented are registered but fail their connection test with an explicit reason. |
| 🔁 **Compatibility** | The old rmf-web-shaped routes (`/fleets`, `/tasks/dispatch_task`, `/building_map` …) are still served, backed by the core. `python -m nayantra.rmf_bridge.server` still starts a single-robot setup. |

**Implementation status**

- The orchestration chain (register robot → fleet → map → task → allocation →
  traffic → navigation → completion) is exercised end to end by the test
  suite, through the REST API and through the NL agent with a scripted LLM.
- The **Nav2 adapter** has not yet been run against a live Nav2 stack.
- **Multi-robot Isaac Sim** is not wired up. The Isaac demo adapter drives
  the single carter_v1 demo robot.

---

## 🚀 Quick Start

### Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.11+ | everything |
| Node.js | 20.19+ / 22.12+ | building the operator UI (once) |
| NVIDIA Isaac Sim | 6.0+ | the Isaac demo (RTX GPU) |
| ROS 2 | Humble (Ubuntu 22.04) or Jazzy (Ubuntu 24.04) | Nav2 robots |
| Nav2 | `ros-<distro>-navigation2`, `ros-<distro>-nav2-bringup` | Nav2 robots |
| Docker + Compose | latest | *optional* container stack |

The core, the UI and the test suite need no GPU, ROS 2 or LLM key. ROS 2
adapters need the core started from a shell where ROS 2 is sourced
(`source /opt/ros/<distro>/setup.bash`) on the same `ROS_DOMAIN_ID` as the
robots. The connection test reports which distro it found.

### 1. Install

```bash
git clone https://github.com/shashankbr27/nayantra.git
cd nayantra
pip install -e ".[test]"
cp config/.env.example config/.env   # add ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY for NL
```

### 2. Build the UI and start the core

```bash
(cd web && npm ci && npm run build)          # once; the core serves web/dist
nayantra-core --scenario isaac_carter        # → http://localhost:8000/
```

The `isaac_carter` scenario (`config/scenarios/isaac_carter.json`) registers
the carter_v1 robot of the Isaac Sim warehouse, driven through Nav2. Bring
the robot side up as in [Isaac Sim Setup](#-isaac-sim-setup) below; the
robot shows as online in the UI once its ROS 2 topics are discovered.

Useful flags:

- `--scenario NAME` loads another scenario from `config/scenarios/`.
- `--db data/other.db` uses a different database.
- `--reset` wipes the state and re-seeds.
- `--port 8765` changes the port.

State is persisted in SQLite (`data/nayantra.db`).

### 3. Full stack (core + MCP + NL agent)

```bash
bash scripts/start.sh        # Windows: scripts\start.ps1
```

| Service | URL |
|---|---|
| Operator UI | http://localhost:8000/ |
| Core API (OpenAPI) | http://localhost:8000/docs |
| MCP server | http://localhost:7000/tools |
| Agent API | http://localhost:8080/docs |

Or use Docker: `docker compose -f docker/docker-compose.yml up --build`.

### 4. Try it

In the UI:

1. Pick a waypoint on the map and create a navigate or delivery task.
2. Read the allocation explanation.
3. Watch the robot execute it on the live map (and in the Isaac Sim stream).

From the CLI:

```bash
nayantra "take carter to the loading dock"
nayantra "why is carter waiting?"
nayantra "stop all robots"          # parked for operator confirmation in the UI
```

Or through the REST API directly:

```bash
curl -X POST http://localhost:8000/api/v1/tasks \
  -H "Content-Type: application/json" \
  -d '{"type": "navigate", "params": {"destination": "Charger 1"}}'
```

---

## 📁 Project Structure

```
nayantra/
├── nayantra/
│   ├── core/               # Nayantra Core — the control plane
│   │   ├── models.py       # Pydantic schemas for everything
│   │   ├── world.py        # World registry (maps, waypoints, lanes, zones, fleets, robots)
│   │   ├── routing.py      # Nav graph, lane rules, A*
│   │   ├── traffic.py      # Space-time planning, reservations, negotiation
│   │   ├── tasks.py        # Task state machine + allocation retry
│   │   ├── allocation.py   # Robot selection with explanations
│   │   ├── fleet.py        # Per-robot executors, charging, zone monitor
│   │   ├── safety.py       # Zone / speed guard
│   │   ├── confirmations.py
│   │   ├── events.py       # Event bus + alerts
│   │   ├── adapters/       # nav2 (ROS 2 Humble/Jazzy) · isaac_demo · unsupported · sim (test fixture)
│   │   ├── api.py · ws.py  # /api/v1 REST + WebSocket
│   │   ├── legacy_rmf.py   # rmf-web-shaped compatibility routes
│   │   └── server.py       # `nayantra-core`
│   ├── mcp/                # MCP tool server + core client
│   ├── agent/              # NL agent (CLI, API v1/v2)
│   ├── rmf_client/         # Optional real Open-RMF client
│   ├── rmf_bridge/         # Compat launcher → single-robot core
│   ├── isaac_sim/ · ros2_adapter/ · zenoh_bridge/ · api/
├── simulation/gazebo/      # Gazebo world generator, robot model, launch, Nav2 params
├── web/                    # Operator UI (React + TypeScript + Vite)
├── config/
│   ├── maps/               # Map seeds
│   ├── scenarios/          # Fleet + robot seeds
│   ├── .env.example
│   └── tools.json          # Generated tool definitions
├── docker/                 # Dockerfile.core · .mcp · .agent · compose
├── tests/                  # Pytest suite (245 tests; tests/core = platform)
├── docs/
├── scripts/
└── pyproject.toml
```

---

## 🎮 Isaac Sim Setup

Requires **Isaac Sim 6.0 or newer**. See [docs/getting_started.md](docs/getting_started.md)
and [docs/isaac_sim_setup.md](docs/isaac_sim_setup.md) for the full guide.

**Quick summary** (three terminals on the GPU machine, same `ROS_DOMAIN_ID`):

```bash
bash scripts/run_demo.sh isaac    # Isaac Sim 6.0+: warehouse + carter_v1, /odom /scan /cmd_vel
bash scripts/run_demo.sh nav2     # Nav2 + TF tree (Humble or Jazzy, auto-detected)
nayantra-core --scenario isaac_carter
```

`bash scripts/run_demo.sh check` verifies ROS 2, Nav2 and the topics.
`scripts/install_isaac_pip.sh` installs Isaac Sim 6.0 from pip without Docker.

---

## 🧪 Simulation Backends: Gazebo and Stub Mode

This branch (`feat/stub-gazebo`) adds two backends that `dev` deliberately does
not carry.

| Backend | Needs | What it is for |
|---|---|---|
| **Gazebo** (Harmonic) | ROS 2 Humble or Jazzy, Nav2, `ros_gz` | Open-source multi-robot simulation. Each robot is a Nav2 robot under its own namespace, driven by the same `Nav2Adapter` as Isaac Sim and hardware. |
| **Stub mode** | nothing (Python only) | Development, tests and UI work. Robots come from the core's built-in kinematic simulator. |

```bash
# Gazebo: two terminals, same ROS_DOMAIN_ID
bash scripts/run_gazebo.sh sim        # Gazebo + bridge + Nav2 per robot
bash scripts/run_gazebo.sh core       # nayantra-core --scenario gazebo_warehouse

# Stub mode
nayantra-core                         # warehouse_demo: 4 fleets, 16 simulated robots
```

The Gazebo world is generated from the Nayantra map, so the planning world and
the physical world cannot drift apart. Architecture, frames and limits:
[docs/gazebo_architecture.md](docs/gazebo_architecture.md). Stub mode:
[docs/stub_mode.md](docs/stub_mode.md).

> The Gazebo assets are consistency-tested in CI but **not yet run against a
> live Gazebo + Nav2**. `bash scripts/run_gazebo.sh check` reports each piece.

---

## 🛰 Visualization with RViz 2

For ROS 2 robots (Nav2 / the Isaac Sim ROS 2 Bridge), RViz 2 is still the
right tool for sensor-level views: it subscribes to the standard `/tf`,
`/map` and `nav_msgs/Path` topics. The operator UI shows the
fleet-level picture.

```bash
source /opt/ros/<humble|jazzy>/setup.bash
rviz2
```

Full setup, troubleshooting, and a recommended display list:
[docs/rviz_setup.md](docs/rviz_setup.md).

---

## 📡 Zenoh Bridge (Multi-site / WAN)

If your robot is on a different network segment, enable Zenoh:

```bash
# On the server/cloud side
ZENOH_ENABLED=true python -m nayantra.zenoh_bridge.bridge --mode server

# On the robot side
ZENOH_ENABLED=true python -m nayantra.zenoh_bridge.bridge --mode client --router <server_ip>
```

For LAN deployments, set `ZENOH_ENABLED=false` and ROS 2 DDS handles discovery natively.

---

## 🔐 Authentication

Tokens are signed HS256 JWTs. To enable:

```bash
# 1. Generate a strong secret in config/.env
JWT_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
echo "JWT_SECRET=$JWT_SECRET" >> config/.env
echo "USE_AUTH=true"          >> config/.env

# 2. Issue a token
nayantra-token --subject admin --hours 240
```

> The agent **refuses to start** with `USE_AUTH=true` and an empty `JWT_SECRET`.
> By default servers bind to `127.0.0.1`; override `MCP_SERVER_HOST` / `AGENT_API_HOST` to expose them.

---

## 🧪 Running Tests

```bash
pip install -e ".[test]"
pytest tests/ -v --cov=nayantra          # backend; needs no GPU, ROS 2 or LLM
                                         # (robots are an in-process test fixture)
(cd web && npm ci && npm run build)      # UI typecheck + build
```

---

## 🤝 Contributing

We welcome contributions! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for the branch policy and PR workflow.

---

## 🛡 Security

Found a vulnerability? Please **do not** open a public issue. See [SECURITY.md](SECURITY.md) for the responsible-disclosure process.

---

## 📄 License

Apache License 2.0 — see [LICENSE](LICENSE)
