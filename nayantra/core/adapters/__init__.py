"""Robot adapters: one common interface, per-protocol implementations."""

from __future__ import annotations

from nayantra.core.adapters.base import AdapterSnapshot, CheckResult, RobotAdapter
from nayantra.core.adapters.sim import SimAdapter, SimEngine
from nayantra.core.adapters.unsupported import UnsupportedAdapter
from nayantra.core.models import EffectiveRobotConfig, Pose, Robot

__all__ = [
    "AdapterSnapshot",
    "CheckResult",
    "RobotAdapter",
    "SimAdapter",
    "SimEngine",
    "UnsupportedAdapter",
    "create_adapter",
]

_PLANNED = {
    "mqtt": "MQTT",
    "rest": "generic REST",
    "websocket": "WebSocket",
    "custom": "custom",
}


def create_adapter(
    robot: Robot,
    config: EffectiveRobotConfig,
    spawn: Pose | None,
    sim: SimEngine,
    spawn_label: str = "",
) -> RobotAdapter:
    protocol = config.communication.protocol
    if protocol == "simulation":
        return SimAdapter(robot, config, spawn, sim, spawn_label=spawn_label)
    if protocol == "ros2":
        if config.nav_stack in ("nav2", "kinematic_sim", "custom", "vendor"):
            from nayantra.core.adapters.nav2 import Nav2Adapter

            return Nav2Adapter(robot, config, spawn)
        return UnsupportedAdapter(
            robot,
            config,
            spawn,
            f"ROS 2 robots using the '{config.nav_stack}' stack need a dedicated adapter "
            "(PX4/ArduPilot adapters are planned; see docs/platform_architecture.md).",
        )
    if protocol == "isaac_demo":
        from nayantra.core.adapters.isaac_demo import IsaacDemoAdapter

        return IsaacDemoAdapter(robot, config, spawn)
    label = _PLANNED.get(protocol, protocol)
    return UnsupportedAdapter(
        robot,
        config,
        spawn,
        f"The {label} adapter is not implemented yet. The robot is registered but cannot be "
        "controlled; use the ROS 2 or simulation protocol, or add an adapter under "
        "nayantra/core/adapters/.",
    )
