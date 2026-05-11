"""
nayantra/isaac_sim/sim_bridge.py

NVIDIA Isaac Sim integration bridge.

Responsibilities:
  1. Load / reset USD scene via Isaac Sim REST API (Nucleus / Kit HTTP)
  2. Spawn / despawn robot prims
  3. Forward RMF task commands → Isaac Sim action graph
  4. Publish simulated robot state back to RMF via ROS 2 topics

Isaac Sim REST API reference:
  https://docs.omniverse.nvidia.com/isaacsim/latest/reference_python/isaac_core.html

ROS 2 bridge topics (via isaac_ros_bridge / ros2_bridge extension):
  /odom                        → nav_msgs/Odometry
  /cmd_vel                     → geometry_msgs/Twist
  /tf                          → tf2_msgs/TFMessage
  /rmf_fleet/robot_state       → rmf_fleet_msgs/RobotState  (custom)

Usage:
    bridge = IsaacSimBridge()
    await bridge.connect()
    await bridge.spawn_robot("turtlebot3_1", x=0.0, y=0.0)
    await bridge.send_nav_goal("turtlebot3_1", waypoint="charging_dock")
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, Optional

import httpx

from nayantra.config import settings

logger = logging.getLogger("rmf.isaac")

# ---------------------------------------------------------------------------
# Isaac Sim Kit REST endpoints
# ---------------------------------------------------------------------------
_STAGE_LOAD   = "/stage/load"
_STAGE_RESET  = "/stage/reset"
_PRIM_CREATE  = "/prim/create"
_PRIM_DELETE  = "/prim/delete"
_PRIM_SET_ATTR = "/prim/set_attribute"
_ACTION_GRAPH  = "/action_graph/execute"
_ROBOT_STATE   = "/robot_state"        # custom endpoint exposed by our Isaac extension


class IsaacSimBridge:
    """
    Async bridge to NVIDIA Isaac Sim.

    When ISAAC_SIM_ENABLED=false (default for local dev without a GPU),
    all methods silently return simulated responses so the rest of the
    stack continues to function.
    """

    def __init__(self) -> None:
        self._enabled = settings.ISAAC_SIM_ENABLED
        self._base_url = settings.ISAAC_SIM_URL.rstrip("/")
        self._http: Optional[httpx.AsyncClient] = None
        self._connected = False

    async def connect(self) -> bool:
        """Open connection to Isaac Sim and load the default scene."""
        if not self._enabled:
            logger.info("Isaac Sim disabled — running in stub mode")
            return True

        self._http = httpx.AsyncClient(
            base_url=self._base_url, timeout=30
        )
        try:
            resp = await self._http.get("/health")
            resp.raise_for_status()
            self._connected = True
            logger.info(f"Connected to Isaac Sim at {self._base_url}")
            await self._load_scene()
            return True
        except Exception as exc:
            logger.error(f"Could not connect to Isaac Sim: {exc}")
            self._connected = False
            return False

    async def _load_scene(self) -> None:
        """Load the configured USD scene."""
        payload = {"url": settings.ISAAC_SIM_SCENE_PATH}
        await self._post(_STAGE_LOAD, payload)
        logger.info(f"Scene loaded: {settings.ISAAC_SIM_SCENE_PATH}")

    async def reset_scene(self) -> Dict[str, Any]:
        """Reset simulation to initial state."""
        if not self._enabled or not self._connected:
            return self._stub("scene_reset")
        return await self._post(_STAGE_RESET, {})

    # ------------------------------------------------------------------
    # Robot lifecycle
    # ------------------------------------------------------------------

    async def spawn_robot(
        self,
        robot_name: str,
        usd_path: str = "/Isaac/Robots/Turtlebot/turtlebot3_burger.usd",
        x: float = 0.0,
        y: float = 0.0,
        yaw: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Spawn a robot prim at the given position.

        Args:
            robot_name: Unique name for the robot prim.
            usd_path:   Path to the robot USD asset in Nucleus.
            x, y, yaw:  Initial pose in metres and radians.
        """
        if not self._enabled or not self._connected:
            logger.info(f"[STUB] Spawning robot {robot_name} at ({x}, {y})")
            return self._stub(f"spawn:{robot_name}")

        payload = {
            "prim_path": f"/World/Robots/{robot_name}",
            "usd_path": usd_path,
            "attributes": {
                "xformOp:translate": [x, y, 0.0],
                "xformOp:rotateXYZ": [0.0, 0.0, yaw],
            },
        }
        result = await self._post(_PRIM_CREATE, payload)
        logger.info(f"Spawned {robot_name} at ({x}, {y})")
        return result

    async def despawn_robot(self, robot_name: str) -> Dict[str, Any]:
        """Remove a robot prim from the scene."""
        if not self._enabled or not self._connected:
            return self._stub(f"despawn:{robot_name}")
        return await self._post(
            _PRIM_DELETE,
            {"prim_path": f"/World/Robots/{robot_name}"},
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def send_nav_goal(
        self,
        robot_name: str,
        waypoint: Optional[str] = None,
        x: Optional[float] = None,
        y: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        Command a simulated robot to navigate to a waypoint or coordinate.

        The action graph in Isaac Sim translates this into a Nav2 goal
        via the ROS 2 bridge.
        """
        if not self._enabled or not self._connected:
            dest = waypoint or f"({x}, {y})"
            logger.info(f"[STUB] {robot_name} → navigate to {dest}")
            return self._stub(f"nav_goal:{robot_name}→{dest}")

        payload: Dict[str, Any] = {
            "robot_prim": f"/World/Robots/{robot_name}",
            "action": "navigate",
        }
        if waypoint:
            payload["waypoint"] = waypoint
        else:
            payload["goal"] = {"x": x, "y": y}

        return await self._post(_ACTION_GRAPH, payload)

    async def get_robot_pose(self, robot_name: str) -> Dict[str, Any]:
        """Query the current pose of a simulated robot."""
        if not self._enabled or not self._connected:
            return self._stub({"x": 0.0, "y": 0.0, "yaw": 0.0, "level_name": "L1"})
        try:
            resp = await self._http.get(
                _ROBOT_STATE,
                params={"prim": f"/World/Robots/{robot_name}"},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.error(f"get_robot_pose failed: {exc}")
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._http:
            raise RuntimeError("Not connected to Isaac Sim")
        resp = await self._http.post(path, json=payload)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code}

    @staticmethod
    def _stub(tag: Any) -> Dict[str, Any]:
        import time
        return {"source": "isaac_stub", "tag": str(tag), "timestamp": int(time.time())}

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()
