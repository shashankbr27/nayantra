"""
nayantra/core/ws.py

Realtime stream for the dashboard: ws://<core>/api/v1/ws

On connect the client receives {"type": "snapshot", "data": <world>} and then
incremental messages from the event bus:

  robots        list[RobotState]           (5 Hz)
  traffic       TrafficState               (≈1.7 Hz)
  task          Task                       (on every change)
  task_progress {id, progress, current_step}
  event         Event                      (durable log entries)
  alert         Alert
  conflict      Conflict
  registry      {kind, op, id, item}       (maps/waypoints/lanes/zones/fleets/robots)
  confirmation  PendingAction
  sim           SimStatus
  robot_removed {robot_id}

A client that falls behind gets a fresh snapshot instead of a gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger("nayantra.core.ws")

router = APIRouter()


def _dumps(msg) -> str:
    return json.dumps(msg, separators=(",", ":"), default=str)


@router.websocket("/api/v1/ws")
async def world_stream(ws: WebSocket) -> None:
    core = ws.app.state.core
    await ws.accept()
    sub = core.bus.subscribe()
    try:
        await ws.send_text(_dumps({"type": "snapshot", "data": core.snapshot()}))

        async def reader() -> None:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if msg.get("type") == "ping":
                    await ws.send_text(_dumps({"type": "pong", "data": {}}))
                elif msg.get("type") == "resync":
                    sub.needs_resync = True
                    sub.push({"type": "noop", "data": {}})

        read_task = asyncio.create_task(reader())
        try:
            while True:
                get = asyncio.create_task(sub.queue.get())
                done, _ = await asyncio.wait({get, read_task}, return_when=asyncio.FIRST_COMPLETED)
                if read_task in done:
                    get.cancel()
                    read_task.result()  # re-raise disconnects
                    break
                msg = get.result()
                if sub.needs_resync or msg.get("type") == "snapshot_required":
                    sub.needs_resync = False
                    while not sub.queue.empty():
                        sub.queue.get_nowait()
                    await ws.send_text(_dumps({"type": "snapshot", "data": core.snapshot()}))
                    continue
                if msg.get("type") == "noop":
                    continue
                await ws.send_text(_dumps(msg))
        finally:
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await read_task
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"ws closed: {exc}")
    finally:
        core.bus.unsubscribe(sub)
