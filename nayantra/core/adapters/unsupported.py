"""
Placeholder for communication protocols Nayantra does not implement yet.

A robot registered with such a protocol is stored and shown, but its
connection test fails with an explicit explanation, and it is never allocated
work. Nothing is faked.
"""

from __future__ import annotations

from nayantra.core.adapters.base import AdapterSnapshot, CheckResult, RobotAdapter
from nayantra.core.models import RobotHealth


class UnsupportedAdapter(RobotAdapter):
    kind = "unsupported"

    def __init__(self, robot, config, spawn, reason: str) -> None:
        super().__init__(robot, config, spawn)
        self.reason = reason

    async def connect(self) -> list[CheckResult]:
        return [CheckResult("Adapter available", False, self.reason)]

    async def disconnect(self) -> None:
        return None

    def snapshot(self) -> AdapterSnapshot:
        return AdapterSnapshot(
            online=False, nav_state="offline", nav_detail=self.reason, health=RobotHealth()
        )

    async def follow_path(self, path, speed_limits=None) -> None:
        raise NotImplementedError(self.reason)

    async def stop(self) -> None:
        return None

    async def pause(self) -> None:
        return None

    async def resume(self) -> None:
        return None

    async def emergency_stop(self) -> None:
        return None

    async def release_estop(self) -> None:
        return None
