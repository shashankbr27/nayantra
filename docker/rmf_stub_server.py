"""
docker/rmf_stub_server.py

Lightweight stub for the Open-RMF REST API.
Used via docker-compose for local development without a live RMF installation.

Runs on port 8000 and returns realistic simulated data for all endpoints.
"""
from __future__ import annotations

import time
import uuid

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Nayantra RMF API Stub", version="stub")


def _ok(data):
    return JSONResponse({"status": "ok", "data": data, "timestamp": int(time.time())})


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/fleets")
def get_fleets():
    return _ok([{
        "name": "turtlebot_fleet",
        "robots": {
            "turtlebot3_1": {
                "name": "turtlebot3_1", "status": "idle",
                "battery": 0.95, "task_id": "",
                "location": {"x": 0.0, "y": 0.0, "yaw": 0.0, "level_name": "L1"},
            }
        },
    }])


@app.get("/tasks")
def get_tasks():
    return _ok([])


@app.post("/tasks/dispatch_task")
def dispatch_task():
    return _ok({"task_id": str(uuid.uuid4()), "state": "queued"})


@app.post("/tasks/cancel_task")
def cancel_task():
    return _ok({"action": "cancelled"})


@app.get("/tasks/{task_id}/state")
def get_task_state(task_id: str):
    return _ok({"task_id": task_id, "status": "underway"})


@app.get("/tasks/{task_id}/log")
def get_task_log(task_id: str):
    return _ok({"task_id": task_id, "log": []})


@app.get("/doors")
def get_doors():
    return _ok([{"name": "main_door", "current_mode": {"value": 0}}])


@app.get("/doors/{door_name}/state")
def get_door_state(door_name: str):
    return _ok({"name": door_name, "current_mode": {"value": 0}})


@app.get("/lifts")
def get_lifts():
    return _ok([{"name": "lift_1", "current_floor": "L1", "available_floors": ["L1", "L2"]}])


@app.get("/lifts/{lift_name}/state")
def get_lift_state(lift_name: str):
    return _ok({"name": lift_name, "current_floor": "L1"})


@app.get("/alerts")
def get_alerts():
    return _ok([])


@app.get("/building_map")
def get_building_map():
    return _ok({
        "name": "SimWarehouse",
        "levels": [{"name": "L1", "elevation": 0.0, "nav_graphs": []}],
    })


@app.get("/dispensers")
def get_dispensers():
    return _ok([])


@app.get("/ingestors")
def get_ingestors():
    return _ok([])


@app.get("/fire_alarm_trigger")
def get_fire_alarm():
    return _ok({"triggered": False})


@app.post("/fire_alarm_trigger/reset")
def reset_fire_alarm():
    return _ok({"action": "reset"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
