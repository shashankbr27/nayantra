# 🤖 Nayantra — LLM-Powered Autonomous Robot Navigation

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-green.svg)](https://www.python.org/)
[![ROS 2 Humble](https://img.shields.io/badge/ROS2-Humble-orange.svg)](https://docs.ros.org/en/humble/)
[![Isaac Sim 4.x](https://img.shields.io/badge/Isaac%20Sim-4.x-brightgreen.svg)](https://developer.nvidia.com/isaac-sim)
[![MCP](https://img.shields.io/badge/Protocol-MCP-purple.svg)](https://modelcontextprotocol.io/)

> **Natural language → LLM → MCP → OpenRMF → Fleet Adapter → Nav2 → Real/Simulated Robot**

Nayantra is an open-source framework that lets you control autonomous robots using **plain English commands**. It connects a large language model (LLM) to the Open-RMF fleet management system through the Model Context Protocol (MCP), supporting both real robot hardware and NVIDIA Isaac Sim simulation.

---

## 📐 Architecture

```
┌─────────────┐     Natural Language     ┌──────────────────────────────────────┐
│    User /   │ ───────────────────────► │           AI Agent (main.py)         │
│   Web API   │                          │  • Claude / GPT-4o intent parsing    │
└─────────────┘                          │  • Multi-step task planning          │
                                         │  • Tool selection & orchestration    │
                                         └──────────────┬───────────────────────┘
                                                        │  MCP Tool Calls
                                                        ▼
                                         ┌──────────────────────────────────────┐
                                         │         MCP Server (FastAPI)         │
                                         │  • Tool registry & validation        │
                                         │  • SSE streaming / REST endpoint     │
                                         │  • Auth middleware (JWT)             │
                                         └──────────────┬───────────────────────┘
                                                        │  HTTP / WebSocket
                                                        ▼
                                         ┌──────────────────────────────────────┐
                                         │      OpenRMF Client (rmf_client)     │
                                         │  • Fleet / task / door / lift API   │
                                         │  • Retry logic + error handling      │
                                         └──────────────┬───────────────────────┘
                                                        │  REST API
                                                        ▼
                                         ┌──────────────────────────────────────┐
                                         │         Open-RMF Server              │
                                         │  (rmf-web / rmf_traffic_editor)      │
                                         └──────────────┬───────────────────────┘
                                                        │  RMF Fleet Adapter
                                                        ▼
                          ┌─────────────────────────────────────────────────────┐
                          │                  Transport Layer                    │
                          │                                                     │
                          │  LAN (same network)     │  WAN / Multi-site        │
                          │  ─────────────────      │  ───────────────────     │
                          │  ROS 2 DDS directly     │  Zenoh bridge (ros2dds)  │
                          │                         │  ↕ Zenoh network ↕       │
                          │                         │  Zenoh bridge (ros2dds)  │
                          └──────────┬──────────────┴──────────────────────────┘
                                     │  ROS 2 Topics / Services
                                     ▼
                          ┌─────────────────────────┐
                          │     Robot Adapter        │
                          │  • Nav2 integration      │
                          │  • State publishing      │
                          └──────────┬──────────────┘
                                     │
                    ┌────────────────┴────────────────┐
                    │                                 │
                    ▼                                 ▼
         ┌──────────────────┐             ┌──────────────────────┐
         │  Isaac Sim 4.x   │             │   Physical Robot     │
         │  (Simulation)    │             │  (Nav2 + Hardware)   │
         └──────────────────┘             └──────────────────────┘
```

Full deep-dive: [docs/architecture.md](docs/architecture.md).

---

## ✨ Features

| Feature | Description |
|---|---|
| 🧠 **LLM Intent Parsing** | Claude / GPT-4o understands natural language commands |
| 🔧 **MCP Protocol** | Full Model Context Protocol server with SSE + REST transport |
| 🚗 **OpenRMF Integration** | Fleet management, task dispatch, doors, lifts, alerts |
| 🎮 **Isaac Sim Support** | Full NVIDIA Isaac Sim 4.x simulation with USD stage control |
| 📡 **Zenoh Bridge** | Transparent LAN / WAN robot connectivity via Zenoh |
| 🔄 **Multi-step Planning** | Agent plans and executes complex multi-robot missions |
| 📊 **Real-time Streaming** | SSE + WebSocket live task status updates to the UI |
| 🛡️ **JWT Auth** | Secure API access with configurable token signing |
| 🐳 **Docker Compose** | One-command spin-up for the entire stack |
| 🧪 **Stub-everything mode** | Runs on a laptop with no GPU, no RMF, no robot |
| 🧪 **Full Test Suite** | Pytest unit + integration tests with mocked RMF |

---

## 🚀 Quick Start

### Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | 3.11+ | [python.org](https://www.python.org/) |
| Docker + Compose | Latest | [docker.com](https://www.docker.com/) |
| NVIDIA Isaac Sim | 4.x *(optional)* | [NGC](https://catalog.ngc.nvidia.com/orgs/nvidia/containers/isaac-sim) |
| ROS 2 | Humble *(optional)* | [docs.ros.org](https://docs.ros.org/en/humble/) |
| Open-RMF | Latest *(optional)* | [github.com/open-rmf](https://github.com/open-rmf) |

> Items marked *optional* are only needed for live robots / sim. The bundled stub mode runs everything on a laptop.

### 1. Clone the Repository

```bash
git clone https://github.com/shashankbr27/nayantra.git
cd nayantra
```

### 2. Configure Environment

```bash
cp config/.env.example config/.env
# Edit config/.env — add your ANTHROPIC_API_KEY (or OPENAI_API_KEY)
```

### 3. Start with Docker Compose (recommended)

```bash
docker compose -f docker/docker-compose.yml up --build
```

This starts: MCP Server · AI Agent API · OpenRMF stub (for dev) · Prometheus / Grafana.

### 4. Or Run Locally

```bash
# Install Nayantra in editable mode
pip install -e .

# Terminal 1: MCP Server
nayantra-mcp-server        # or: python -m nayantra.mcp.server

# Terminal 2: AI Agent API
nayantra-api               # or: python -m nayantra.agent.api

# Terminal 3: CLI Agent
nayantra                   # or: python -m nayantra.agent.main
```

### 5. Send Your First Command

```bash
# Via CLI
nayantra "list all robots"

# Via HTTP API
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"command": "Send robot turtlebot3 to the charging dock"}'
```

---

## 📁 Project Structure

```
nayantra/
├── nayantra/
│   ├── agent/              # LLM AI Agent
│   │   ├── main.py         # CLI entry point
│   │   ├── api.py          # FastAPI HTTP interface (v1)
│   │   ├── api_v2.py       # v2 — adds WebSocket + dashboard
│   │   ├── agent.py        # Core agent logic
│   │   ├── planner.py      # Multi-step task planner
│   │   └── models.py       # Pydantic data models
│   ├── mcp/                # MCP Server
│   │   ├── server.py       # FastAPI MCP server (SSE + REST)
│   │   ├── tools.py        # Tool registry
│   │   └── auth.py         # JWT middleware
│   ├── rmf_client/         # OpenRMF HTTP client
│   ├── isaac_sim/          # NVIDIA Isaac Sim integration
│   ├── zenoh_bridge/       # Zenoh network bridge
│   ├── ros2_adapter/       # Open-RMF fleet adapter (Nav2)
│   └── api/                # Dashboard + WS monitor
├── config/
│   ├── .env.example        # Template environment file
│   └── tools.json          # Fallback tool definitions
├── docker/
│   ├── Dockerfile.agent
│   ├── Dockerfile.mcp
│   ├── Dockerfile.stub
│   ├── docker-compose.yml
│   └── rmf_stub_server.py
├── tests/                  # Pytest suite (~170 tests)
├── docs/                   # Architecture + setup deep-dives
├── scripts/                # setup / start / stop / token helpers
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
pytest tests/ -v --cov=nayantra
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
