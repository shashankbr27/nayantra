# Stub Mode

Stub mode runs the whole Nayantra stack with **no GPU, no ROS 2, no Isaac Sim and
no LLM key**. It is for development, tests, CI and quick UI work. It is not a
demo path and it never talks to a real robot or simulator.

Everything here lives on the `feat/stub-gazebo` branch so that `dev` stays focused
on the Isaac Sim 6.0+ and ROS 2 (Humble/Jazzy) paths.

## What "stub" means here

| Piece | Stubbed by | Real counterpart |
|---|---|---|
| Robots | the core's built-in kinematic simulator (`protocol: "simulation"`, `SimAdapter`) | `Nav2Adapter` (Isaac Sim, Gazebo, hardware) |
| Isaac Sim bridge | `ISAAC_SIM_ENABLED=false` (`nayantra/isaac_sim/`) | Isaac Sim 6.0+ over ROS 2 |
| Zenoh | `ZENOH_ENABLED=false` (logs what it would relay) | Zenoh bridge between sites |
| Open-RMF client | `DEBUG_MODE=true` (canned responses) | a real Open-RMF server |

Stubbed responses are marked as such (for example `"source": "isaac_stub"`), and
the built-in simulator's robots are labelled as simulated in the UI.

## Running it

```bash
pip install -e ".[test]"
(cd web && npm ci && npm run build)
nayantra-core                      # default scenario: warehouse_demo
```

The `warehouse_demo` scenario (`config/scenarios/warehouse_demo.json`) has 4 fleets
and 16 simulated robots on a warehouse map with a narrow corridor, a restricted
high-voltage zone and UAV air lanes. Open http://localhost:8000/, use the
simulation Start/Pause/Stop controls, create a delivery, and watch the allocation
explanation and the traffic negotiation.

Full stack with the agent and MCP server: `bash scripts/start.sh`.

## What it does and does not prove

It exercises the core end to end: world model, allocation, traffic, safety,
executors, REST/WebSocket and the NL agent (with a scripted LLM in tests). It does
**not** prove anything about ROS 2, Nav2, DDS, sensors or physics; use
[Gazebo](gazebo_architecture.md) or Isaac Sim for that.
