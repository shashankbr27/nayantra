"""
nayantra/isaac_sim/robot_spawner.py

High-level robot fleet management for Isaac Sim.

Handles:
  - Spawning multiple robots from a fleet config
  - Maintaining pose registry for all simulated robots
  - Publishing periodic state updates to OpenRMF via ROS 2 bridge
  - Graceful cleanup on shutdown

Designed to be used alongside IsaacSimBridge in a single async context.

Usage:
    spawner = RobotSpawner()
    await spawner.connect()
    await spawner.spawn_fleet([
        RobotConfig(name="r1", x=0.0, y=0.0),
        RobotConfig(name="r2", x=3.0, y=1.5),
    ])
    await spawner.start_state_publisher()   # non-blocking
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from nayantra.isaac_sim.sim_bridge import IsaacSimBridge
from nayantra.config import settings

logger = logging.getLogger("rmf.spawner")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RobotConfig:
    """Configuration for a single simulated robot."""
    name: str
    fleet: str = "turtlebot_fleet"
    usd_path: str = "/Isaac/Robots/Turtlebot/turtlebot3_burger.usd"
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0


@dataclass
class RobotState:
    """Live state of a simulated robot."""
    name: str
    fleet: str
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    level: str = "L1"
    battery: float = 1.0
    status: str = "idle"    # idle | charging | working | error
    task_id: str = ""
    last_updated: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Spawner
# ---------------------------------------------------------------------------

class RobotSpawner:
    """
    Manages a fleet of simulated robots in Isaac Sim.

    In stub mode (ISAAC_SIM_ENABLED=false), all operations are logged
    as simulated so the rest of the stack can continue without a GPU.
    """

    def __init__(self) -> None:
        self._bridge = IsaacSimBridge()
        self._robots: Dict[str, RobotState] = {}
        self._publisher_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect the underlying Isaac Sim bridge."""
        return await self._bridge.connect()

    async def close(self) -> None:
        """Stop background tasks and close the bridge."""
        if self._publisher_task and not self._publisher_task.done():
            self._publisher_task.cancel()
            try:
                await self._publisher_task
            except asyncio.CancelledError:
                pass
        await self._bridge.close()

    # ------------------------------------------------------------------
    # Fleet spawn / despawn
    # ------------------------------------------------------------------

    async def spawn_fleet(self, configs: List[RobotConfig]) -> Dict[str, bool]:
        """
        Spawn multiple robots concurrently.

        Returns:
            Dict mapping robot_name → True/False (spawn success)
        """
        tasks = [asyncio.create_task(self._spawn_one(cfg)) for cfg in configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return {
            cfg.name: not isinstance(result, Exception)
            for cfg, result in zip(configs, results)
        }

    async def _spawn_one(self, cfg: RobotConfig) -> None:
        result = await self._bridge.spawn_robot(
            robot_name=cfg.name,
            usd_path=cfg.usd_path,
            x=cfg.x,
            y=cfg.y,
            yaw=cfg.yaw,
        )
        self._robots[cfg.name] = RobotState(
            name=cfg.name,
            fleet=cfg.fleet,
            x=cfg.x,
            y=cfg.y,
            yaw=cfg.yaw,
        )
        logger.info(f"Spawned {cfg.name} at ({cfg.x}, {cfg.y}) — {result}")

    async def despawn_all(self) -> None:
        """Remove all tracked robots from the simulation."""
        tasks = [
            asyncio.create_task(self._bridge.despawn_robot(name))
            for name in list(self._robots.keys())
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._robots.clear()
        logger.info("All simulated robots despawned")

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(
        self,
        robot_name: str,
        waypoint: Optional[str] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> Dict:
        """Send a navigation goal to a named robot."""
        if robot_name not in self._robots:
            raise KeyError(f"Robot {robot_name!r} is not spawned")

        result = await self._bridge.send_nav_goal(
            robot_name=robot_name,
            waypoint=waypoint,
            x=x,
            y=y,
        )

        # Optimistically update state
        state = self._robots[robot_name]
        state.status = "working"
        state.task_id = f"sim-task-{int(time.time())}"
        state.last_updated = time.time()

        logger.info(f"{robot_name} → navigating to {waypoint or (x, y)}")
        return result

    # ------------------------------------------------------------------
    # State polling & publishing
    # ------------------------------------------------------------------

    async def poll_states(self) -> Dict[str, RobotState]:
        """
        Query Isaac Sim for the current pose of every spawned robot
        and update the internal state registry.
        """
        for name, state in self._robots.items():
            try:
                pose = await self._bridge.get_robot_pose(name)
                data = pose.get("data", pose)
                state.x = data.get("x", state.x)
                state.y = data.get("y", state.y)
                state.yaw = data.get("yaw", state.yaw)
                state.level = data.get("level_name", state.level)
                state.last_updated = time.time()
            except Exception as exc:
                logger.debug(f"Poll {name}: {exc}")
        return dict(self._robots)

    async def start_state_publisher(self, interval_s: float = 1.0) -> None:
        """
        Start a background loop that polls Isaac Sim for robot poses
        and could publish them to the ROS 2 /rmf/fleet_state topic.

        Non-blocking — runs as an asyncio Task.
        """
        if self._publisher_task and not self._publisher_task.done():
            logger.warning("State publisher already running")
            return

        self._publisher_task = asyncio.create_task(
            self._publish_loop(interval_s)
        )
        logger.info(f"State publisher started (interval={interval_s}s)")

    async def _publish_loop(self, interval_s: float) -> None:
        while True:
            try:
                states = await self.poll_states()
                # In a full ROS 2 environment this would publish to
                # /rmf/fleet_state via rclpy.  In stub/sim mode we just log.
                logger.debug(
                    f"Fleet state: {[f'{n}@({s.x:.1f},{s.y:.1f})' for n, s in states.items()]}"
                )
            except Exception as exc:
                logger.error(f"State publisher error: {exc}")
            await asyncio.sleep(interval_s)

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_state(self, robot_name: str) -> Optional[RobotState]:
        return self._robots.get(robot_name)

    def list_robots(self) -> List[str]:
        return list(self._robots.keys())

    def fleet_summary(self) -> Dict:
        """Return a dict suitable for JSON serialisation."""
        return {
            name: {
                "fleet": s.fleet,
                "x": round(s.x, 3),
                "y": round(s.y, 3),
                "yaw": round(s.yaw, 3),
                "level": s.level,
                "battery": s.battery,
                "status": s.status,
                "task_id": s.task_id,
            }
            for name, s in self._robots.items()
        }
