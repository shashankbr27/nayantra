# 🤖 Nayantra — Multi-Fleet Robot Orchestration

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-green.svg)](https://www.python.org/)
[![ROS 2 Humble](https://img.shields.io/badge/ROS2-Humble-orange.svg)](https://docs.ros.org/en/humble/)
[![Isaac Sim 4.x](https://img.shields.io/badge/Isaac%20Sim-4.x-brightgreen.svg)](https://developer.nvidia.com/isaac-sim)

> **Natural language → LLM agent → MCP tools → Nayantra Core (fleets · tasks · traffic · safety) → robot adapters → simulated / Nav2 / Isaac robots**

Nayantra is a web-based platform for running several heterogeneous robot
fleets (ground vehicles, drones, quadrupeds, humanoids) on one shared map.
Operators work in a mission-control UI or give plain-English commands. The
**Nayantra Core** owns the world model and decides which robot does what,
which route it takes, and when it may move:

- **Task allocation** is done by the core's task manager, not by the LLM.
  Every choice comes with an explanation of why that robot was picked.
- **Traffic coordination** is predictive. Routes are planned in space and
  time over a nav graph of waypoints and lanes, with reservations,
  conflict detection and negotiation.
- **Safety** does not depend on the LLM. Zones and emergency stops are
  enforced in the core, and the LLM never sends velocity commands.

> **Honest naming.** The core is *inspired by* Open-RMF: nav graphs,
> lanes, reservations and negotiation. It is **not** Open-RMF and does not
> run `rmf_traffic`. A real Open-RMF server is still supported as optional
> infrastructure (`OPENRMF_INFRA_TOOLS=true`).
>
> The MCP server is a REST transport built around MCP tool semantics
> (`/tools`, `/run`, `/sse`). It is not the MCP JSON-RPC wire protocol.

---

## 📐 Architecture

```
 Operator UI (React, :8000/)          Natural language (CLI · UI command bar)
        │  REST /api/v1 + WebSocket             │
        │                                        ▼
        │                         Agent API (:8080) — Claude / GPT / Gemini
        │                         think → act → observe loop
        │                                        │ tool calls
        │                                        ▼
        │                         MCP server (:7000) — schema-validated,
        │                         risk-tagged tools; can never confirm
        │                         dangerous actions
        ▼                                        │ REST
┌────────────────────────────────────────────────┴──────────────────────┐
│ Nayantra Core (:8000)                                                  │
│  World registry — maps, waypoints, lanes, zones, fleets, robots        │
│  Task manager   — explicit state machine, allocation + explanations    │
│  Traffic        — space-time planning, reservations, conflict          │
│                   negotiation, deadlock detection                      │
│  Fleet manager  — per-robot executors, charging, zone monitor          │
│  Safety guard · confirmations · event bus · SQLite store               │
└───────────────┬───────────────────────┬───────────────────────┬───────┘
                │                       │                       │
         SimAdapter              Nav2Adapter (ROS 2)     IsaacDemoAdapter
     (built-in kinematic      (rclpy, see status below)  (HTTP → Isaac Sim)
         simulator)
```

Deep dives:

- [docs/platform_architecture.md](docs/platform_architecture.md): the
  multi-fleet design, its decisions, traffic coordination and the migration
  plan.
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
| 🖥 **Operator UI** | Mission-control dashboard: an interactive map (zoom, pan, layers, selection, live robot poses), fleet overview, robot command center, task composer and drawer, traffic and conflict view, alerts and events, map/graph editor, and simulation Start/Pause/Stop. Dark and light themes. |
| 🧠 **Natural language** | Provider-agnostic agent (Anthropic / OpenAI / Gemini) that uses about 25 high-level tools. It creates tasks and never drives robots directly. Commands are traced end to end (mission → command → tool → task). |
| 🔌 **Adapters** | One adapter interface. Implementations: the built-in simulator, Nav2 over ROS 2, and the Isaac demo over HTTP. Protocols that aren't implemented are registered but fail their connection test with an explicit reason. |
| 🔁 **Compatibility** | The old rmf-web-shaped routes (`/fleets`, `/tasks/dispatch_task`, `/building_map` …) are still served, backed by the core. `python -m nayantra.rmf_bridge.server` still starts a single-robot setup. |

**Implementation status**

- The **built-in simulator** path is exercised end to end by the test suite.
  The chain is register robot → fleet → map → task → allocation → traffic →
  navigation → completion, driven both through the REST API and through the
  NL agent with a scripted LLM.
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
| Docker + Compose | latest | *optional* container stack |
| NVIDIA Isaac Sim | 4.x | *optional* Isaac demo |
| ROS 2 | Humble | *optional* Nav2 robots |

No GPU, ROS 2 or LLM key is needed to run the core, the UI and the
simulated warehouse demo.

### 1. Install

```bash
git clone https://github.com/shashankbr27/nayantra.git
cd nayantra
pip install -e ".[test]"
cp config/.env.example config/.env   # add ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY for NL
```

### 2. Build the UI and start the core

```bash
(cd web && npm ci && npm run build)   # once; the core serves web/dist
nayantra-core                        # → http://localhost:8000/
```

On first start the core loads the **warehouse demo** scenario
(`config/scenarios/warehouse_demo.json`): 4 fleets and 16 simulated robots
on a warehouse map with a narrow corridor, a restricted high-voltage zone
and UAV air lanes.

Useful flags:

- `--scenario isaac_carter` loads another scenario.
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

1. Pick a waypoint on the map and create a delivery.
2. Watch the allocation explanation.
3. Watch the robots negotiate the corridor.

From the CLI:

```bash
nayantra "send two ground robots to deliver from Receiving Bay 1 to Storage Rack B"
nayantra "why is ugv_03 waiting?"
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
│   │   ├── adapters/       # sim · nav2 (ROS 2) · isaac_demo · unsupported
│   │   ├── api.py · ws.py  # /api/v1 REST + WebSocket
│   │   ├── legacy_rmf.py   # rmf-web-shaped compatibility routes
│   │   └── server.py       # `nayantra-core`
│   ├── mcp/                # MCP tool server + core client
│   ├── agent/              # NL agent (CLI, API v1/v2)
│   ├── rmf_client/         # Optional real Open-RMF client
│   ├── rmf_bridge/         # Compat launcher → single-robot core
│   ├── isaac_sim/ · ros2_adapter/ · zenoh_bridge/ · api/
├── web/                    # Operator UI (React + TypeScript + Vite)
├── config/
│   ├── maps/               # Map seeds (warehouse_demo, isaac_warehouse)
│   ├── scenarios/          # Fleet + robot seeds
│   ├── .env.example
│   └── tools.json          # Generated tool definitions
├── docker/                 # Dockerfile.core · .mcp · .agent · compose
├── tests/                  # Pytest suite (229 tests; tests/core = platform)
├── docs/
├── scripts/
└── pyproject.toml
```

---

## 🎮 Isaac Sim Setup

See [docs/getting_started.md](docs/getting_started.md) and [docs/isaac_sim_setup.md](docs/isaac_sim_setup.md) for the full guide.

**Quick summary:**
1. Install Isaac Sim 4.x from NGC or Omniverse Launcher
2. Enable the ROS 2 Bridge extension in Isaac Sim
3. Run `scripts/isaac_sim_server.py` inside Isaac Sim's Script Editor
4. Set `ISAAC_SIM_ENABLED=true` in `config/.env`

---

## 🛰 Visualization with RViz 2

For ROS 2 robots (Nav2 / the Isaac Sim ROS 2 Bridge), RViz 2 is still the
right tool for sensor-level views: it subscribes to the standard `/tf`,
`/map` and `nav_msgs/Path` topics. The operator UI shows the
fleet-level picture.

```bash
source /opt/ros/humble/setup.bash
rviz2
```

Full setup, troubleshooting, and a recommended display list:
[docs/rviz_setup.md](docs/rviz_setup.md).

> With the built-in simulator there is no ROS 2 graph for RViz to
> subscribe to. Use the operator UI's live map instead.

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
pytest tests/ -v --cov=nayantra          # backend (no GPU / ROS 2 / LLM needed)
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
