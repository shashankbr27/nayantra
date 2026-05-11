# Zenoh Bridge Setup

## Overview

The Zenoh bridge (`nayantra/zenoh_bridge/bridge.py`) enables multi-site robot
deployments over a WAN by relaying ROS 2 DDS messages through the
[Eclipse Zenoh](https://zenoh.io/) pub/sub protocol.

For single-site LAN deployments, the bridge is not needed — direct ROS 2 DDS
discovery handles everything. Set `ZENOH_ENABLED=false` (the default).

## Prerequisites

```bash
pip install eclipse-zenoh
```

## Configuration

```env
ZENOH_ENABLED=true
ZENOH_ROUTER_URL=tcp/your-server-ip:7447
ZENOH_MODE=peer          # peer | client | router
```

## Running the Bridge

**Server side (router mode):**
```bash
python -m nayantra.zenoh_bridge.bridge --mode router
```

**Robot/edge side (client mode):**
```bash
python -m nayantra.zenoh_bridge.bridge --mode client --router tcp/server-ip:7447
```

## Topic Mappings

| ROS 2 Topic            | Zenoh Key Expression  | Direction       |
|------------------------|-----------------------|-----------------|
| `/rmf/fleet_state`     | `rmf/fleet_state`     | ROS2 → Zenoh    |
| `/rmf/task_summary`    | `rmf/task_summary`    | ROS2 → Zenoh    |
| `/rmf/door_states`     | `rmf/door_states`     | Bidirectional   |
| `/rmf/lift_states`     | `rmf/lift_states`     | Bidirectional   |
| `/rmf/cmd_vel`         | `rmf/cmd_vel`         | Zenoh → ROS2    |
