"""
nayantra/api/ws_monitor.py

WebSocket-based real-time fleet monitor.

Clients connect to  ws://host:8080/ws/fleet  and receive a continuous
stream of JSON events:

  { "type": "fleet_state",   "data": { robots: [...] } }
  { "type": "task_update",   "data": { task_id, status, ... } }
  { "type": "alert",         "data": { id, title, tier, ... } }
  { "type": "mission_start", "data": { mission_id, command } }
  { "type": "mission_end",   "data": { mission_id, success, summary } }
  { "type": "pong",          "data": {} }

Clients can send:
  { "type": "ping" }
  { "type": "subscribe", "topics": ["fleet", "tasks", "alerts"] }

This module is mounted on the main FastAPI app:
    from nayantra.api.ws_monitor import router as ws_router
    app.include_router(ws_router)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("nayantra.ws_monitor")

router = APIRouter()

# ---------------------------------------------------------------------------
# Connection manager
# ---------------------------------------------------------------------------


class ConnectionManager:
    """Manages a set of active WebSocket connections with topic subscriptions."""

    def __init__(self) -> None:
        # ws → set of subscribed topics
        self._connections: dict[WebSocket, set[str]] = {}

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[ws] = {"fleet", "tasks", "alerts", "missions"}
        logger.info(f"WS client connected. Total: {len(self._connections)}")

    def disconnect(self, ws: WebSocket) -> None:
        self._connections.pop(ws, None)
        logger.info(f"WS client disconnected. Total: {len(self._connections)}")

    def subscribe(self, ws: WebSocket, topics: list[str]) -> None:
        if ws in self._connections:
            self._connections[ws] = set(topics)

    async def broadcast(self, event_type: str, data: Any) -> None:
        """Send an event to all clients subscribed to the relevant topic."""
        topic = _event_to_topic(event_type)
        payload = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        dead = []
        for ws, topics in self._connections.items():
            if topic in topics or topic == "*":
                try:
                    await ws.send_text(payload)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, event_type: str, data: Any) -> None:
        """Send directly to one client."""
        payload = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        try:
            await ws.send_text(payload)
        except Exception:
            self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


def _event_to_topic(event_type: str) -> str:
    mapping = {
        "fleet_state": "fleet",
        "task_update": "tasks",
        "alert": "alerts",
        "mission_start": "missions",
        "mission_end": "missions",
        "mission_step": "missions",
    }
    return mapping.get(event_type, "*")


# Module-level manager shared across the app
manager = ConnectionManager()


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/ws/fleet")
async def fleet_websocket(ws: WebSocket):
    """
    Real-time fleet state WebSocket endpoint.

    Connect with:
        wscat -c ws://localhost:8080/ws/fleet
    """
    await manager.connect(ws)
    try:
        # Send an immediate snapshot on connect
        await manager.send_to(
            ws,
            "connected",
            {
                "message": "Connected to Nayantra fleet monitor",
                "subscribed_topics": list(manager._connections.get(ws, set())),
            },
        )

        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                msg = json.loads(raw)
                await _handle_client_message(ws, msg)
            except TimeoutError:
                # Send heartbeat
                await manager.send_to(ws, "heartbeat", {"ts": time.time()})
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error(f"WS error: {exc}")
    finally:
        manager.disconnect(ws)


async def _handle_client_message(ws: WebSocket, msg: dict[str, Any]) -> None:
    msg_type = msg.get("type", "")

    if msg_type == "ping":
        await manager.send_to(ws, "pong", {})

    elif msg_type == "subscribe":
        topics = msg.get("topics", ["fleet", "tasks", "alerts", "missions"])
        manager.subscribe(ws, topics)
        await manager.send_to(ws, "subscribed", {"topics": topics})

    elif msg_type == "get_stats":
        # Return connection count and uptime
        await manager.send_to(
            ws,
            "stats",
            {
                "connected_clients": manager.count,
                "uptime_s": time.time(),
            },
        )

    else:
        logger.debug(f"Unknown WS message type: {msg_type!r}")


# ---------------------------------------------------------------------------
# Event publisher helpers (called from agent/api.py)
# ---------------------------------------------------------------------------


async def publish_fleet_state(robots: list[dict[str, Any]]) -> None:
    """Broadcast current fleet state to all subscribers."""
    await manager.broadcast("fleet_state", {"robots": robots})


async def publish_task_update(task_id: str, status: str, **kwargs: Any) -> None:
    """Broadcast a task state change."""
    await manager.broadcast("task_update", {"task_id": task_id, "status": status, **kwargs})


async def publish_alert(alert_id: str, title: str, tier: str = "info", **kwargs: Any) -> None:
    """Broadcast an RMF alert."""
    await manager.broadcast("alert", {"id": alert_id, "title": title, "tier": tier, **kwargs})


async def publish_mission_start(mission_id: str, command: str) -> None:
    await manager.broadcast("mission_start", {"mission_id": mission_id, "command": command})


async def publish_mission_end(mission_id: str, success: bool, summary: str) -> None:
    await manager.broadcast(
        "mission_end", {"mission_id": mission_id, "success": success, "summary": summary}
    )


async def publish_mission_step(mission_id: str, step_index: int, tool: str, status: str) -> None:
    await manager.broadcast(
        "mission_step",
        {
            "mission_id": mission_id,
            "step_index": step_index,
            "tool": tool,
            "status": status,
        },
    )
