# Isaac Sim Setup

## Prerequisites

- NVIDIA GPU with RTX capability
- NVIDIA Isaac Sim 4.x installed via Omniverse Launcher
- `ISAAC_SIM_ENABLED=true` in `config/.env`

## Configuration

```env
ISAAC_SIM_ENABLED=true
ISAAC_SIM_URL=http://localhost:8211
ISAAC_SIM_SCENE_PATH=/Isaac/Environments/Simple_Warehouse/warehouse.usd
```

## Quick Start

1. Launch Isaac Sim from the Omniverse Launcher.
2. Enable the **ROS 2 Bridge** extension inside Isaac Sim.
3. Open or load your warehouse USD scene.
4. Start the Nayantra stack:

```bash
docker compose up
```

The `IsaacSimBridge` in `nayantra/isaac_sim/sim_bridge.py` will connect automatically on startup.

## Stub Mode

If `ISAAC_SIM_ENABLED=false` (default), the bridge runs in stub mode — all
spawn and navigation calls are logged but no GPU resources are used.
This is the default for development machines without Isaac Sim.

## Robot Spawning

Use `RobotSpawner` from `nayantra/isaac_sim/robot_spawner.py`:

```python
from nayantra.isaac_sim.robot_spawner import RobotSpawner, RobotConfig

spawner = RobotSpawner()
await spawner.connect()
await spawner.spawn_fleet([
    RobotConfig(name="r1", x=0.0, y=0.0),
    RobotConfig(name="r2", x=3.0, y=1.5),
])
```
