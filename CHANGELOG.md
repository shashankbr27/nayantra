# Changelog

All notable changes to Nayantra are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). While the major version is 0, minor
releases may change interfaces.

## [0.1.0] - 2026-10-20

First tagged release. It brings `main` up to date with `dev`: the control plane,
the operator UI, the Isaac Sim and Gazebo backends, and a one-command launcher.

Earlier builds reported `2.0.0` from `/api/v1/health` and in `pyproject.toml`.
No tag or GitHub release ever carried that number, and 0.1.0 replaces it, so a
checkout that was installed as `2.0.0` will see this as a downgrade. Reinstall
with `pip install -e .`.

### Added

- **Control plane** (`nayantra/core`, port 8000). One world model for maps,
  waypoints, lanes, zones, fleets and robots, backed by SQLite. A task manager
  with an explicit state machine and cost-based allocation that explains its
  choice. A traffic coordinator with space-time reservations, conflict
  negotiation (delay, reroute, priority, make way, joint planning) and deadlock
  detection. A safety guard with operator confirmation for dangerous actions,
  which the MCP client can never confirm. REST API (`/api/v1`), a WebSocket
  world stream, an event bus, alerts and a per-task trace.
- **Robot adapters.** A kinematic simulator (UGV, UAV, quadruped, humanoid), a
  Nav2 adapter for ROS 2 Humble and Jazzy (cmd_vel type auto-detected), and an
  Isaac Sim Carter adapter.
- **Operator UI** (`web`). Operations, fleets, robots and tasks pages and a map
  editor, a dark and a light theme, and an assistant panel that keeps the
  conversation history.
- **LLM and MCP interface.** An MCP tool server, an agent API whose follow-up
  commands carry the conversation so far, `isaac_demo` tools for driving Carter
  through the WebRTC API, and `scripts/drive_cli.py`, a terminal driver for the
  Isaac demo.
- **Isaac Sim 6.0+ with Nav2.** Natural language to Nav2 goals for `carter_v1`,
  lidar obstacle avoidance, A* planning, an optional collision monitor, and
  `scripts/run_demo.sh`.
- **Gazebo Harmonic backend** (`simulation/gazebo`). The world is generated from
  the Nayantra map so planning and physics cannot drift apart, with one Nav2
  stack per robot and three robots: GZ UGV-1, UGV-2 and UGV-3.
- **Launcher.** `nayantra.sh` (and `nayantra.cmd` on Windows) runs `setup`,
  `start`, `stop`, `restart`, `status`, `logs`, `gui` and `doctor`. `setup` offers Gazebo,
  Isaac Sim or Auto; `detect` reports what a machine can run, and why, without
  changing anything. Auto picks Isaac Sim only when the machine meets NVIDIA's
  published minimum. Windows always gets Gazebo, because Isaac Sim does not run
  in WSL2.
- A `CHANGELOG.md`, and `tests/test_version.py`, which fails if the version
  strings disagree.

### Fixed

- **Swap gridlock.** Two parked robots each wanting the other's spot no longer
  freeze: the charge task routes to a parked-on candidate so the traffic
  coordinator can make way or plan the swap.
- **Nav2 goal sequencing.** A task no longer counts as arrived before Nav2 has
  finished, so the next goal is not rejected with "another navigator is
  processing".
- The API no longer reports a task completed while the robot's state still holds
  an older pose.
- `web/src/lib` was missing from the repository because a `.gitignore` rule for
  Python's `lib/` matched it; the rule is now anchored to the repo root.

### Changed

- The README leads with the platform and its three planes (intent,
  orchestration, embodiment) and shows both simulator badges.
- Isaac Sim 6.0+ is the supported version throughout.

### Removed

- The H200 and cluster deployment (`slurm/` and its wrappers). The H200 has no
  NVENC encoder or RT cores, so Isaac Sim's viewport cannot run there; the
  workstation path replaces it.
- The Open-RMF comparison table from the README.

### Known limitations

- **Isaac Sim through the launcher has not run on a real Isaac Sim install.**
  The start, ready-wait, Nav2 retry and stop sequence is tested against a
  stand-in. The `run_demo.sh` demo has run on an RTX 6000 Ada workstation, and
  the core's Nav2 adapter has run live against Gazebo only.
- ROS 2 Humble is supported in code and unit tests but has not run live.
- Nav2 occasionally hangs during startup (about one start in ten on the
  Gazebo backend). The launcher retries it.
- The Gazebo window can be flaky on the NVIDIA WSL2 driver.
- A database created before GZ UGV-3 existed does not gain it: the core seeds a
  scenario only into an empty registry. Use `start --reset`, or POST the robot
  to `/api/v1/robots`.
- The interactive simulator menu has not been tried in a real Windows console;
  if it does not appear, setup falls back to Gazebo, the only valid choice there.

[0.1.0]: https://github.com/shashankbr27/nayantra/releases/tag/v0.1.0
