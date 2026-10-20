"""
nayantra/core/adapters/base.py

The contract between the fleet layer and a robot.

The fleet layer (not the robot) decides *when* a robot may move: it hands the
adapter a path of graph poses that the traffic coordinator has cleared, and
extends it as more lanes are cleared, like Open-RMF full-control fleet
adapters. The adapter drives there with whatever stack the robot has (Nav2,
PX4, vendor SDK, the built-in kinematic simulator) and reports progress.

Adapters report only what they can measure. Fields they cannot observe stay
None and the UI shows them as "not reported".
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from nayantra.core.models import EffectiveRobotConfig, Pose, Robot, RobotHealth, Velocity

NavState = Literal["idle", "moving", "arrived", "failed", "paused", "estop", "offline"]


@dataclass
class CheckResult:
    """One line of the connection test ("✓ Odometry available")."""

    name: str
    ok: bool
    detail: str = ""
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "required": self.required}


@dataclass
class AdapterSnapshot:
    online: bool = False
    pose: Pose = field(default_factory=Pose)
    velocity: Velocity = field(default_factory=Velocity)
    battery_pct: float | None = None
    charging: bool = False
    nav_state: NavState = "offline"
    nav_detail: str = ""
    path_index: int = -1  # index of the last reached pose of the commanded path
    path_len: int = 0
    action: str | None = None
    action_done: bool = False
    health: RobotHealth = field(default_factory=RobotHealth)


class RobotAdapter(ABC):
    kind: str = "base"
    #: actions the robot performs natively; anything else is a timed dwell
    native_actions: frozenset[str] = frozenset()

    def __init__(self, robot: Robot, config: EffectiveRobotConfig, spawn: Pose | None) -> None:
        self.robot = robot
        self.config = config
        self.spawn = spawn or Pose()

    @property
    def robot_id(self) -> str:
        return self.robot.id

    def reconfigure(self, robot: Robot, config: EffectiveRobotConfig) -> None:
        self.robot = robot
        self.config = config

    # -- lifecycle ---------------------------------------------------------
    @abstractmethod
    async def connect(self) -> list[CheckResult]: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def snapshot(self) -> AdapterSnapshot: ...

    # -- motion ------------------------------------------------------------
    @abstractmethod
    async def follow_path(self, path: list[Pose], speed_limits: list[float] | None = None) -> None:
        """Drive through `path` in order, replacing any previous path."""

    @abstractmethod
    async def stop(self) -> None:
        """Controlled stop; forget the path."""

    @abstractmethod
    async def pause(self) -> None: ...

    @abstractmethod
    async def resume(self) -> None: ...

    @abstractmethod
    async def emergency_stop(self) -> None: ...

    @abstractmethod
    async def release_estop(self) -> None: ...

    # -- actions -----------------------------------------------------------
    async def start_action(self, action: str, params: dict[str, Any] | None = None) -> None:
        """Begin a native action (dock, charge, takeoff …). Default: unsupported."""
        raise NotImplementedError(f"{self.kind} adapter has no native '{action}' action")

    async def cancel_action(self) -> None:
        return None

    async def restart_navigation(self) -> list[CheckResult]:
        return [
            CheckResult("Restart navigation", False, f"not supported by the {self.kind} adapter")
        ]
