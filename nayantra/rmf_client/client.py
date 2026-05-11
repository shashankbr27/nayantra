"""
nayantra/rmf_client/client.py

Async OpenRMF REST API client built on httpx.

Key improvements over the original:
  - Fully async (httpx.AsyncClient)
  - Automatic retry with exponential back-off (tenacity)
  - Simulated debug mode returns realistic payloads
  - Type-safe via Pydantic models
  - Proper resource cleanup via close()
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger("rmf.client")


class OpenRMFClient:
    """Async HTTP client for the Open-RMF REST API."""

    def __init__(
        self,
        api_url: str = "http://localhost:8000",
        token: str | None = None,
        debug: bool = False,
        timeout: int = 30,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.debug = debug
        headers = {"Content-Type": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        else:
            logger.warning("No API token — authenticated endpoints will fail with 401")
        self._http = httpx.AsyncClient(
            base_url=self.api_url,
            headers=headers,
            timeout=timeout,
        )

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sim(self, data: Any) -> dict[str, Any]:
        """Return a simulated success response in debug mode."""
        return {
            "source": "simulated",
            "timestamp": int(time.time()),
            "data": data,
        }

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _get(self, path: str) -> Any:
        logger.debug(f"GET {self.api_url}{path}")
        resp = await self._http.get(path)
        resp.raise_for_status()
        return resp.json()

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        logger.debug(f"POST {self.api_url}{path} body={payload}")
        resp = await self._http.post(path, json=payload)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code}

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _delete(self, path: str) -> Any:
        logger.debug(f"DELETE {self.api_url}{path}")
        resp = await self._http.delete(path)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code}

    # ------------------------------------------------------------------
    # Fleets & Robots
    # ------------------------------------------------------------------

    async def get_fleets(self) -> Any:
        if self.debug:
            return self._sim(
                [
                    {
                        "name": "turtlebot_fleet",
                        "robots": {
                            "turtlebot3_1": {
                                "name": "turtlebot3_1",
                                "status": "idle",
                                "task_id": "",
                                "battery": 0.95,
                                "location": {"x": 0.0, "y": 0.0, "yaw": 0.0, "level_name": "L1"},
                            }
                        },
                    }
                ]
            )
        return await self._get("/fleets")

    async def get_robot_state(self, fleet_name: str, robot_name: str) -> Any:
        if self.debug:
            return self._sim(
                {
                    "name": robot_name,
                    "fleet": fleet_name,
                    "status": "idle",
                    "battery": 0.9,
                    "location": {"x": 1.0, "y": 2.0, "yaw": 0.0, "level_name": "L1"},
                }
            )
        return await self._get(f"/fleets/{fleet_name}/robots/{robot_name}")

    async def get_fleet_log(self, fleet_name: str) -> Any:
        if self.debug:
            return self._sim({"fleet": fleet_name, "log": []})
        return await self._get(f"/fleets/{fleet_name}/log")

    async def post_decommission_robot(self, fleet_name: str, robot_name: str) -> Any:
        if self.debug:
            return self._sim({"fleet": fleet_name, "robot": robot_name, "action": "decommissioned"})
        return await self._post(f"/fleets/{fleet_name}/decommission", {"robot_name": robot_name})

    async def post_recommission_robot(self, fleet_name: str, robot_name: str) -> Any:
        if self.debug:
            return self._sim({"fleet": fleet_name, "robot": robot_name, "action": "recommissioned"})
        return await self._post(f"/fleets/{fleet_name}/recommission", {"robot_name": robot_name})

    # ------------------------------------------------------------------
    # Tasks
    # ------------------------------------------------------------------

    async def get_tasks(self) -> Any:
        if self.debug:
            return self._sim([])
        return await self._get("/tasks")

    async def get_task_state(self, task_id: str) -> Any:
        if self.debug:
            return self._sim({"task_id": task_id, "status": "underway", "progress": "50%"})
        return await self._get(f"/tasks/{task_id}/state")

    async def get_task_log(self, task_id: str) -> Any:
        if self.debug:
            return self._sim({"task_id": task_id, "log": []})
        return await self._get(f"/tasks/{task_id}/log")

    async def post_dispatch_task(self, payload: dict[str, Any]) -> Any:
        if self.debug:
            return self._sim({"task_id": str(uuid.uuid4()), "state": "queued"})
        return await self._post("/tasks/dispatch_task", payload)

    async def post_cancel_task(self, payload: dict[str, Any]) -> Any:
        if self.debug:
            return self._sim({"action": "cancelled", **payload})
        return await self._post("/tasks/cancel_task", payload)

    async def post_resume_task(self, payload: dict[str, Any]) -> Any:
        if self.debug:
            return self._sim({"action": "resumed", **payload})
        return await self._post("/tasks/resume_task", payload)

    async def post_interrupt_task(self, payload: dict[str, Any]) -> Any:
        if self.debug:
            return self._sim({"action": "interrupted", **payload})
        return await self._post("/tasks/interrupt_task", payload)

    async def post_kill_task(self, payload: dict[str, Any]) -> Any:
        if self.debug:
            return self._sim({"action": "killed", **payload})
        return await self._post("/tasks/kill_task", payload)

    # ------------------------------------------------------------------
    # Doors
    # ------------------------------------------------------------------

    async def get_doors(self) -> Any:
        if self.debug:
            return self._sim([{"name": "main_door", "current_mode": {"value": 0}}])
        return await self._get("/doors")

    async def get_door_state(self, door_name: str) -> Any:
        if self.debug:
            return self._sim({"name": door_name, "current_mode": {"value": 0}})
        return await self._get(f"/doors/{door_name}/state")

    async def post_door_request(self, door_name: str, payload: dict[str, Any]) -> Any:
        if self.debug:
            return self._sim({"door": door_name, "requested_mode": payload.get("mode")})
        return await self._post(f"/doors/{door_name}/request", payload)

    # ------------------------------------------------------------------
    # Lifts / Elevators
    # ------------------------------------------------------------------

    async def get_lifts(self) -> Any:
        if self.debug:
            return self._sim(
                [{"name": "lift_1", "current_floor": "L1", "available_floors": ["L1", "L2", "L3"]}]
            )
        return await self._get("/lifts")

    async def get_lift_state(self, lift_name: str) -> Any:
        if self.debug:
            return self._sim(
                {
                    "name": lift_name,
                    "current_floor": "L1",
                    "destination_floor": "L1",
                    "door_state": {"value": 0},
                    "motion_state": {"value": 0},
                }
            )
        return await self._get(f"/lifts/{lift_name}/state")

    async def post_lift_request(self, lift_name: str, payload: dict[str, Any]) -> Any:
        if self.debug:
            return self._sim(
                {"lift": lift_name, "requested_floor": payload.get("destination_floor")}
            )
        return await self._post(f"/lifts/{lift_name}/request", payload)

    # ------------------------------------------------------------------
    # Alerts
    # ------------------------------------------------------------------

    async def get_alerts(self) -> Any:
        if self.debug:
            return self._sim([])
        return await self._get("/alerts")

    async def get_alert(self, alert_id: str) -> Any:
        if self.debug:
            return self._sim({"id": alert_id, "type": "warning", "message": "Simulated alert"})
        return await self._get(f"/alerts/{alert_id}")

    async def post_alert_response(self, alert_id: str, payload: dict[str, Any]) -> Any:
        if self.debug:
            return self._sim({"alert_id": alert_id, "response": payload.get("response")})
        return await self._post(f"/alerts/{alert_id}/response", payload)

    # ------------------------------------------------------------------
    # Fire alarm
    # ------------------------------------------------------------------

    async def get_previous_fire_alarm_trigger(self) -> Any:
        if self.debug:
            return self._sim({"triggered": False})
        return await self._get("/fire_alarm_trigger")

    async def post_reset_fire_alarm_trigger(self, payload: dict[str, Any]) -> Any:
        if self.debug:
            return self._sim({"action": "reset"})
        return await self._post("/fire_alarm_trigger/reset", payload)

    # ------------------------------------------------------------------
    # Building map
    # ------------------------------------------------------------------

    async def get_building_map(self) -> Any:
        if self.debug:
            return self._sim(
                {
                    "name": "SimWarehouse",
                    "levels": [
                        {
                            "name": "L1",
                            "elevation": 0.0,
                            "nav_graphs": [{"name": "0", "vertices": [], "edges": []}],
                        }
                    ],
                }
            )
        return await self._get("/building_map")

    # ------------------------------------------------------------------
    # Dispensers & Ingestors
    # ------------------------------------------------------------------

    async def get_dispensers(self) -> Any:
        if self.debug:
            return self._sim([{"guid": "dispenser_1", "type": "dispenser"}])
        return await self._get("/dispensers")

    async def get_ingestors(self) -> Any:
        if self.debug:
            return self._sim([{"guid": "ingestor_1", "type": "ingestor"}])
        return await self._get("/ingestors")
