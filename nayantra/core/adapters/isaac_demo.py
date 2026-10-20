"""
nayantra/core/adapters/isaac_demo.py

Adapter for scripts/isaac_demo.py — Carter in Isaac Sim with kinematic glide
and a tiny HTTP control API (GET /state, POST /goto?x=&y=). It keeps the
existing WebRTC demo usable from the new control plane.

The demo API has no stop command, so stop/pause/e-stop send the robot to its
current position, which halts the glide. There is no battery telemetry.
"""

from __future__ import annotations

import asyncio
import math
import time

import httpx

from nayantra.core.adapters.base import AdapterSnapshot, CheckResult, RobotAdapter
from nayantra.core.models import Pose, RobotHealth, Velocity

POLL_S = 0.2
PASS_TOL_M = 0.3


class IsaacDemoAdapter(RobotAdapter):
    kind = "isaac_demo"

    def __init__(self, robot, config, spawn) -> None:
        super().__init__(robot, config, spawn)
        self.base = (config.communication.endpoint or "http://127.0.0.1:8900").rstrip("/")
        self._http: httpx.AsyncClient | None = None
        self.pose = Pose(**self.spawn.model_dump())
        self.moving = False
        self.online = False
        self.latency_ms: float | None = None
        self.path: list[Pose] = []
        self.path_index = -1
        self.paused = False
        self.estop = False
        self.failed = ""
        self._poll: asyncio.Task | None = None
        self._sent_index = -1

    async def connect(self) -> list[CheckResult]:
        self._http = httpx.AsyncClient(timeout=5.0)
        checks = []
        try:
            t0 = time.monotonic()
            state = (await self._http.get(f"{self.base}/state")).json()
            self.latency_ms = (time.monotonic() - t0) * 1000
            self._apply(state)
            checks.append(
                CheckResult("Robot discovered", True, f"isaac_demo control API at {self.base}")
            )
            checks.append(
                CheckResult(
                    "Pose available", True, f"({self.pose.x:.2f}, {self.pose.y:.2f}) from /state"
                )
            )
        except Exception as exc:  # noqa: BLE001
            self.online = False
            return [
                CheckResult(
                    "Robot discovered",
                    False,
                    f"cannot reach {self.base}/state ({exc}). Is scripts/isaac_demo.py running and "
                    "is the endpoint reachable from this machine?",
                )
            ]
        checks.append(
            CheckResult(
                "Navigation interface available",
                True,
                "POST /goto (kinematic glide, no obstacle avoidance)",
            )
        )
        checks.append(
            CheckResult(
                "Battery telemetry available",
                False,
                "isaac_demo does not report battery",
                required=False,
            )
        )
        self.online = True
        if self._poll is None:
            self._poll = asyncio.create_task(self._poll_loop())
        return checks

    def _apply(self, state: dict) -> None:
        x, y = float(state.get("x", 0.0)), float(state.get("y", 0.0))
        self.pose = Pose(x=x, y=y, yaw=float(state.get("yaw", 0.0)))
        self.moving = bool(state.get("moving"))

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            try:
                t0 = time.monotonic()
                self._apply((await self._http.get(f"{self.base}/state")).json())
                self.latency_ms = (time.monotonic() - t0) * 1000
                self.online = True
                await self._advance()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                self.online = False

    async def _advance(self) -> None:
        if self.paused or self.estop or not self.path:
            return
        nxt = self.path_index + 1
        if nxt >= len(self.path):
            return
        target = self.path[nxt]
        if (
            math.hypot(target.x - self.pose.x, target.y - self.pose.y) < PASS_TOL_M
            and not self.moving
        ):
            self.path_index = nxt
            nxt += 1
        if nxt < len(self.path) and self._sent_index != nxt:
            await self._goto(self.path[nxt])
            self._sent_index = nxt

    async def _goto(self, p: Pose) -> None:
        await self._http.post(f"{self.base}/goto", params={"x": round(p.x, 3), "y": round(p.y, 3)})

    def snapshot(self) -> AdapterSnapshot:
        if not self.online:
            state = "offline"
        elif self.estop:
            state = "estop"
        elif self.paused:
            state = "paused"
        elif self.path and self.path_index < len(self.path) - 1:
            state = "moving"
        elif self.path:
            state = "arrived"
        else:
            state = "idle"
        return AdapterSnapshot(
            online=self.online,
            pose=self.pose,
            velocity=Velocity(),
            battery_pct=None,
            nav_state=state,
            nav_detail=self.failed,
            path_index=self.path_index,
            path_len=len(self.path),
            health=RobotHealth(
                localization="ground_truth (Isaac)",
                navigation="ok" if self.online else None,
                network="HTTP",
                latency_ms=round(self.latency_ms, 1) if self.latency_ms else None,
            ),
        )

    async def follow_path(self, path, speed_limits=None) -> None:
        if self.estop or not self.online:
            return
        self.path = list(path)
        self.path_index = -1
        self._sent_index = -1
        self.paused = False
        await self._advance()

    async def _hold(self) -> None:
        try:
            await self._goto(self.pose)
        except Exception:  # noqa: BLE001
            pass

    async def stop(self) -> None:
        self.path, self.path_index = [], -1
        await self._hold()

    async def pause(self) -> None:
        self.paused = True
        await self._hold()

    async def resume(self) -> None:
        self.paused = False
        self._sent_index = -1
        await self._advance()

    async def emergency_stop(self) -> None:
        self.estop = True
        self.path, self.path_index = [], -1
        await self._hold()

    async def release_estop(self) -> None:
        self.estop = False

    async def disconnect(self) -> None:
        if self._poll:
            self._poll.cancel()
            self._poll = None
        if self._http:
            await self._http.aclose()
        self.online = False
