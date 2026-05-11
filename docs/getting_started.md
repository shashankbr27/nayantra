# Getting Started — Nayantra + Isaac Sim

This guide walks you from a fresh clone to a working agent driving robots in
NVIDIA Isaac Sim. Total time on a properly-spec'd machine: **~30 minutes**
(plus Isaac Sim download time).

---

## 1. Prerequisites

### Hardware
- **GPU:** NVIDIA RTX (RTX 3070 or better recommended for Isaac Sim)
- **RAM:** 32 GB minimum
- **Disk:** ~60 GB free (~50 GB for Isaac Sim assets)

### Software
| Component        | Version  | Notes                                                       |
|------------------|----------|-------------------------------------------------------------|
| OS               | Ubuntu 22.04 / Windows 11 + WSL2 | Isaac Sim runs best on Linux       |
| NVIDIA driver    | 535.x +  | Required for RTX acceleration                                |
| Python           | 3.11+    | For the agent stack                                          |
| Git              | any      | To clone the repo                                            |
| Isaac Sim        | 4.0 +    | Install via [Omniverse Launcher](https://www.nvidia.com/omniverse/) |
| Docker (optional)| 24+      | If you want the full compose stack                           |

### LLM access
You need an API key for **one** of:
- **Anthropic** (Claude) — recommended, get a key at https://console.anthropic.com
- **OpenAI** (GPT-4o) — get a key at https://platform.openai.com

---

## 2. One-time setup

### 2.1 Install the project

```bash
git clone <your-fork-url> navigation
cd navigation
bash scripts/setup.sh
```

`setup.sh` will:
1. Verify Python 3.11+ is on `PATH`
2. Create a virtualenv at `.venv/`
3. Install all project dependencies (`pip install -e ".[test]"`)
4. Copy `config/.env.example` → `.env` if it's missing
5. Create `runtime/logs/`, `runtime/pids/`, and `data/`

> **Windows users:** run `scripts/setup.sh` from a WSL2 shell or Git Bash. The
> native Python path (`python -m venv`, `pip install -e .`) also works in
> PowerShell if you prefer — see [§7 Windows-native steps](#7-windows-native-steps).

### 2.2 Fill in `.env`

Open `.env` and set **at minimum** the LLM key:

```bash
# Pick one
ANTHROPIC_API_KEY=sk-ant-…       # OR
OPENAI_API_KEY=sk-…
```

For Isaac Sim integration, also confirm:

```bash
ISAAC_SIM_ENABLED=true
ISAAC_SIM_URL=http://localhost:8211
```

---

## 3. Smoke test (no Isaac Sim yet)

Before plugging in Isaac Sim, verify the stack works against the simulated
RMF stub.

### 3.1 Temporarily disable Isaac Sim

Edit `.env`:
```bash
ISAAC_SIM_ENABLED=false
```

### 3.2 Start the stack

```bash
bash scripts/start.sh
```

You should see:
```
[start] Starting rmf-stub on port 8000 ...
[start] rmf-stub is healthy
[start] Starting mcp-server on port 7000 ...
[start] mcp-server is healthy
[start] Starting agent-api on port 8080 ...
[start] agent-api is healthy
[start] All services launched.
```

### 3.3 Run a test command

```bash
# In a new terminal
source .venv/bin/activate
python -m nayantra.agent.main "list all robots"
```

You should get a summary describing the simulated turtlebot fleet.

Or open the dashboard in a browser: **http://localhost:8080/**

### 3.4 Stop the stack

```bash
bash scripts/stop.sh
```

---

## 4. Isaac Sim integration

### 4.1 Launch Isaac Sim

Open Isaac Sim from the Omniverse Launcher.

### 4.2 Enable the ROS 2 Bridge extension

Inside Isaac Sim:
1. **Window → Extensions**
2. Search for **"ROS 2 Bridge"**
3. Toggle it on and check **AUTOLOAD**

### 4.3 Load a warehouse scene

Either:
- **File → Open** the default warehouse:
  `omniverse://localhost/Isaac/Environments/Simple_Warehouse/warehouse.usd`
- Or paste your own USD path into `ISAAC_SIM_SCENE_PATH` in `.env`.

### 4.4 Start the REST bridge inside Isaac Sim

Open the Script Editor (**Window → Script Editor**) and paste:

```python
import sys
sys.path.insert(0, '/path/to/navigation')   # ← edit to your repo path

from scripts.isaac_sim_server import start
start(host="0.0.0.0", port=8211)
```

> The Script Editor will block while uvicorn runs. To check it's alive:
> ```bash
> curl http://localhost:8211/health
> # {"status":"ok","isaac_available":true,"tracked_robots":[]}
> ```

> **Fallback (no Isaac Sim available):** the same `scripts/isaac_sim_server.py`
> works as a standalone Python process. Just run:
> ```bash
> source .venv/bin/activate
> python scripts/isaac_sim_server.py
> ```
> It returns realistic stub responses so you can exercise the full stack.

### 4.5 Re-enable Isaac Sim in `.env`

```bash
ISAAC_SIM_ENABLED=true
```

### 4.6 Restart the stack

```bash
bash scripts/stop.sh
bash scripts/start.sh
```

You should see `isaac_sim` show up as **healthy** in the agent's startup
readiness check.

---

## 5. End-to-end test

With Isaac Sim running and the stack up:

```bash
source .venv/bin/activate

# Spawn robots in Isaac Sim from a Python REPL
python <<'EOF'
import asyncio
from nayantra.isaac_sim.robot_spawner import RobotSpawner, RobotConfig

async def main():
    spawner = RobotSpawner()
    await spawner.connect()
    await spawner.spawn_fleet([
        RobotConfig(name="tb3_1", x=0.0, y=0.0),
        RobotConfig(name="tb3_2", x=3.0, y=1.5),
    ])
    print(spawner.fleet_summary())
    await spawner.close()

asyncio.run(main())
EOF
```

Then drive them with natural language:

```bash
python -m nayantra.agent.main "send tb3_1 to charging dock"
python -m nayantra.agent.main "what robots are available?"
python -m nayantra.agent.main --stream "dispatch a delivery from main door to store room"
```

Or use the dashboard at **http://localhost:8080/** — it has a command box
and a real-time fleet view fed by the WebSocket at `/ws/fleet`.

---

## 6. Verifying everything

| Check                | Command                                       | Expected                        |
|----------------------|-----------------------------------------------|---------------------------------|
| Stub RMF             | `curl http://localhost:8000/health`           | `{"status":"ok"}`               |
| MCP server           | `curl http://localhost:7000/health`           | `{"status":"ok","tools":N}`     |
| MCP tool list        | `curl http://localhost:7000/tools`            | JSON array of ~25 tools         |
| Agent API            | `curl http://localhost:8080/health`           | `{"status":"ok"}`                |
| Agent readiness      | `curl http://localhost:8080/v1/readiness`     | All checks green                 |
| Isaac Sim bridge     | `curl http://localhost:8211/health`           | `{"isaac_available":true,...}` |
| Unit tests           | `pytest`                                      | 172 passed                       |
| Linter               | `ruff check nayantra tests scripts`           | no errors                        |

---

## 7. Windows-native steps

If you're not using WSL, run these from a PowerShell prompt in the project
root (the bash scripts assume a Unix shell):

```powershell
# One-time setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[test]"
Copy-Item config\.env.example .env
mkdir runtime\logs, runtime\pids, data -Force

# Edit .env, then start each service in its own terminal:
python docker\rmf_stub_server.py                                # Terminal 1
python -m nayantra.mcp.server                                        # Terminal 2
python -m uvicorn nayantra.agent.api_v2:app --host 0.0.0.0 --port 8080  # Terminal 3
```

---

## 8. Troubleshooting

### `ModuleNotFoundError: No module named 'nayantra'`
You're not running from the project root, or the virtualenv isn't activated.
Always `cd` into the repo root and `source .venv/bin/activate` first.

### `MCP_SERVER_URL` connection refused
The MCP server isn't running. Check `runtime/logs/mcp-server.log` for the
real error. Most common cause: missing API key in `.env`.

### Isaac Sim shows up as unhealthy
1. Confirm Isaac Sim's Script Editor is still running the
   `scripts/isaac_sim_server.py` script (it must not be cancelled).
2. Verify `curl http://localhost:8211/health` from your host shell.
3. If you're running Isaac Sim on a different machine, set
   `ISAAC_SIM_URL=http://<host-ip>:8211` in `.env`.

### `429 Too Many Requests` from Anthropic / OpenAI
The LLM provider is rate-limiting. Either upgrade your API tier or set
`DEBUG_MODE=true` to skip live LLM calls (uses cached tool fallbacks).

### Port already in use
Another process is bound to 7000, 8000, 8080, or 8211. Find and kill it:
```bash
lsof -i :8080            # Linux/macOS
netstat -ano | findstr 8080   # Windows
```

### Tests fail with `pydantic_settings` not found
Dependencies aren't installed in the active virtualenv. Re-run
`bash scripts/setup.sh` (or `pip install -e ".[test]"` manually).

---

## 9. Docker alternative

If you prefer Docker over a local virtualenv:

```bash
cp config/.env.example .env       # then edit .env
docker compose -f docker/docker-compose.yml up --build
```

That brings up: `mcp-server`, `agent-api`, `rmf-stub`, `prometheus`,
`grafana`. Isaac Sim is **not** containerised — it still runs on the host
GPU. Set `ISAAC_SIM_URL=http://host.docker.internal:8211` (Mac/Windows) or
the host bridge IP (Linux) so containers can reach it.

---

## 10. Next steps

- **Add new MCP tools:** drop a `@_tool(...)` decorator in `nayantra/mcp/tools.py`
  — no routing changes needed.
- **Plug in real robots:** set `ROS2_ENABLED=true` and source your ROS 2
  workspace before launching `nayantra/ros2_adapter/fleet_adapter.py`.
- **Multi-site deployments:** set `ZENOH_ENABLED=true` and start the bridge
  via `python -m nayantra.zenoh_bridge.bridge --mode router` on your server.
- **Production hardening:** set `USE_AUTH=true`, generate a fresh
  `JWT_SECRET`, and issue tokens with `python -m scripts.generate_token`.
