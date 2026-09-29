"""
REST + WebSocket tests against the real core (in-memory DB, warehouse demo
scenario, built-in kinematic simulator running at high speed).

The vertical-slice test walks the operator flow end to end:
register robot → assign fleet → appears on dashboard/map → pick waypoint →
task created → allocated → robot navigates → live pose → task completes.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from nayantra.core import api as core_api
from nayantra.core.runtime import NayantraCore
from nayantra.core.server import create_app

UI = {"X-Nayantra-Client": "ui"}
MCP = {"X-Nayantra-Client": "mcp"}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(core_api, "IMAGE_DIR", tmp_path / "maps")
    core = NayantraCore(db_path=":memory:", scenario="warehouse_demo")
    app = create_app(core)
    with TestClient(app) as c:
        c.patch("/api/v1/sim", json={"speed": 8.0})
        wait_for(lambda: all(rt.state.online for rt in core.fleet.runtimes.values()), 5)
        c.core = core
        yield c


def wait_for(fn, timeout: float = 20.0, interval: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


def task_status(c, tid):
    return c.get(f"/api/v1/tasks/{tid}").json()["status"]


# ---------------------------------------------------------------------------


def test_health_meta_world(client):
    assert client.get("/api/v1/health").json()["status"] == "ok"
    meta = client.get("/api/v1/meta").json()
    assert "ugv" in meta["robot_types"] and "delivery" in meta["task_types"]
    world = client.get("/api/v1/world").json()
    assert len(world["fleets"]) == 4
    assert len(world["robots"]) == 16
    assert {m["id"] for m in world["maps"]} == {"warehouse_demo", "isaac_warehouse"}
    ugv = next(f for f in world["fleets"] if f["id"] == "warehouse_ugv")
    assert ugv["stats"]["total"] == 4 and ugv["stats"]["online"] == 4


def test_waypoint_crud_and_validation(client):
    base = "/api/v1/maps/warehouse_demo/waypoints"
    r = client.post(base, json={"name": "Quality Check", "x": 12.0, "y": 15.0, "type": "location"})
    assert r.status_code == 201, r.text
    wp = r.json()
    assert wp["id"] == "quality_check" and wp["map_id"] == "warehouse_demo"
    # rename + move
    r = client.patch(f"/api/v1/waypoints/{wp['id']}", json={"name": "QC Station", "x": 12.5})
    assert r.json()["name"] == "QC Station" and r.json()["x"] == 12.5
    # workspace hard limit
    r = client.post(base, json={"name": "Outside", "x": 99, "y": 0})
    assert r.status_code == 422 and "outside the workspace" in r.json()["message"]
    # names are unique per map
    assert client.post(base, json={"name": "qc station", "x": 1, "y": 1}).status_code == 409
    # unknown fields are rejected
    assert client.post(base, json={"name": "X", "x": 1, "y": 1, "colour": "red"}).status_code == 422
    # lanes need real endpoints
    r = client.post("/api/v1/maps/warehouse_demo/lanes", json={"from_id": wp["id"], "to_id": "NA5"})
    assert r.status_code == 201
    r = client.post(
        "/api/v1/maps/warehouse_demo/lanes", json={"from_id": wp["id"], "to_id": "nope"}
    )
    assert r.status_code == 422
    assert client.delete(f"/api/v1/waypoints/{wp['id']}").status_code == 200
    assert client.get(f"/api/v1/waypoints/{wp['id']}").status_code == 404


def test_fleet_crud(client):
    r = client.post(
        "/api/v1/fleets", json={"name": "Night Shift", "robot_type": "ugv", "map_ids": ["nowhere"]}
    )
    assert r.status_code == 422
    r = client.post(
        "/api/v1/fleets",
        json={"name": "Night Shift", "robot_type": "ugv", "map_ids": ["warehouse_demo"]},
    )
    assert r.status_code == 201 and r.json()["id"] == "night_shift"
    assert client.delete("/api/v1/fleets/warehouse_ugv").status_code == 409  # has robots
    assert client.delete("/api/v1/fleets/night_shift").status_code == 200


def test_vertical_slice_register_to_completion(client):
    c = client
    # 1. new fleet
    r = c.post(
        "/api/v1/fleets",
        json={
            "id": "test_ugv",
            "name": "Test UGV Fleet",
            "robot_type": "ugv",
            "capabilities": ["navigate", "carry_payload"],
            "map_ids": ["warehouse_demo"],
            "limits": {"max_speed": 1.5},
        },
    )
    assert r.status_code == 201, r.text
    # 2. register a robot through the same payload the wizard sends
    r = c.post(
        "/api/v1/robots",
        headers=UI,
        json={
            "id": "tester_1",
            "name": "Tester-1",
            "fleet_id": "test_ugv",
            "manufacturer": "Acme",
            "model": "T-100",
            "robot_type": "ugv",
            "navigation": {"map_id": "warehouse_demo", "namespace": "/tester_1"},
            "physical": {"payload_kg": 50, "max_speed": 1.2},
            "communication": {"protocol": "simulation"},
            "spawn": {"waypoint_id": "LD2", "battery_pct": 90},
        },
    )
    assert r.status_code == 201, r.text
    view = r.json()
    assert view["checks"], "connection test results are returned with the registration"
    assert all(ch["ok"] for ch in view["checks"] if ch["required"])
    # 3. robot appears on the dashboard / map, online, at its spawn waypoint
    state = wait_for(
        lambda: (s := c.get("/api/v1/robots/tester_1").json()["state"]) and s["online"] and s
    )
    assert abs(state["pose"]["x"] - 35.0) < 0.1 and abs(state["pose"]["y"] - 1.8) < 0.1, state
    assert "tester_1" in c.get("/api/v1/fleets/test_ugv").json()["robots"]
    # 4. operator picks a waypoint → task
    r = c.post("/api/v1/robots/tester_1/navigate", headers=UI, json={"waypoint": "Storage Rack C"})
    assert r.status_code == 201, r.text
    task = r.json()
    assert task["assigned_robot"] == "tester_1", task["allocation"]
    assert "Selected Tester-1" in task["allocation"]["explanation"], task["allocation"]
    # 5. robot navigates; live position changes; task completes
    seen = set()

    def progressed():
        st = c.get("/api/v1/robots/tester_1").json()["state"]
        seen.add((round(st["pose"]["x"], 1), round(st["pose"]["y"], 1)))
        return task_status(c, task["id"]) == "completed"

    wait_for(progressed, 40)
    assert len(seen) >= 3, f"expected live position updates, saw {seen}"
    final = c.get("/api/v1/robots/tester_1").json()["state"]
    assert abs(final["pose"]["x"] - 35.0) < 0.2 and abs(final["pose"]["y"] - 19.5) < 0.2, final
    trace = c.get(f"/api/v1/tasks/{task['id']}/trace").json()
    types = {e["type"] for e in trace["events"]}
    assert {"TASK_CREATED", "ROBOT_TASK_ASSIGNED", "ROBOT_TASK_COMPLETED"} <= types, types
    assert trace["origin"]["source"] == "operator"


def test_delivery_is_allocated_by_capability(client):
    r = client.post(
        "/api/v1/tasks",
        json={
            "type": "delivery",
            "params": {"pickup": "receiving", "dropoff": "storage", "payload": "box"},
        },
    )
    assert r.status_code == 201, r.text
    task = r.json()["tasks"][0]
    robot = client.get(f"/api/v1/robots/{task['assigned_robot']}").json()
    assert "carry_payload" in robot["effective"]["capabilities"]
    uav = next(c for c in task["allocation"]["candidates"] if c["robot_id"] == "uav_01")
    assert not uav["eligible"] and "carry_payload" in uav["reasons"][0]
    # zone places resolve to concrete waypoints once the robot gets there
    wait_for(lambda: task_status(client, task["id"]) == "completed", 60)
    done = client.get(f"/api/v1/tasks/{task['id']}").json()
    targets = [s["target"] for s in done["steps"] if s["kind"] == "go_to"]
    assert targets[0].startswith("RC") and targets[1].startswith("ST")


def test_dangerous_actions_need_operator_confirmation(client):
    c = client
    r = c.post("/api/v1/robots/stop-all", headers=MCP, params={"confirm": "true"})
    assert r.status_code == 202 and r.json()["confirmation_required"]
    cid = r.json()["confirmation"]["id"]
    assert r.json()["confirmation"]["requested_by"] == "mcp"
    # the agent cannot approve its own request
    assert c.post(f"/api/v1/confirmations/{cid}/confirm", headers=MCP).status_code == 409
    pending = c.get("/api/v1/confirmations").json()
    assert any(p["id"] == cid for p in pending)
    r = c.post(f"/api/v1/confirmations/{cid}/confirm", headers=UI)
    assert r.json()["status"] == "executed"
    # UI with confirm=true executes directly
    r = c.delete("/api/v1/robots/quad_04", headers=UI, params={"confirm": "true"})
    assert r.status_code == 200
    wait_for(lambda: "quad_04" not in c.core.fleet.runtimes, 5)


def test_restricted_destination_requires_confirmation(client):
    r = client.post(
        "/api/v1/robots/ugv_02/navigate", headers=UI, json={"waypoint": "HV Maintenance Point"}
    )
    assert r.status_code == 202
    body = r.json()
    assert "restricted" in body["confirmation"]["summary"].lower()
    r = client.post(f"/api/v1/confirmations/{body['confirmation']['id']}/confirm", headers=UI)
    assert r.json()["status"] == "executed"
    tid = r.json()["result"]["tasks"][0]
    task = client.get(f"/api/v1/tasks/{tid}").json()
    assert task["allow_restricted"] is True


def test_single_robot_estop_is_immediate_and_blocks_work(client):
    c = client
    r = c.post("/api/v1/robots/ugv_03/estop", headers=UI, json={"reason": "test"})
    assert r.status_code == 200
    wait_for(lambda: c.get("/api/v1/robots/ugv_03").json()["state"]["mode"] == "emergency_stop", 5)
    r = c.post("/api/v1/robots/ugv_03/navigate", headers=UI, json={"waypoint": "Receiving Bay 1"})
    task = r.json()
    assert task["status"] == "waiting_for_robot"
    assert "emergency stop" in task["status_reason"]
    # the agent may not release an e-stop
    assert c.post("/api/v1/robots/ugv_03/release-estop", headers=MCP).status_code == 409
    assert c.post("/api/v1/robots/ugv_03/release-estop", headers=UI).status_code == 200
    wait_for(
        lambda: task_status(c, task["id"]) in ("assigned", "planning", "executing", "completed"), 10
    )


def test_pause_resume_cancel(client):
    c = client
    task = c.post(
        "/api/v1/robots/ugv_04/navigate", headers=UI, json={"waypoint": "Storage Rack A"}
    ).json()
    wait_for(lambda: task_status(c, task["id"]) == "executing", 10)
    assert c.post(f"/api/v1/tasks/{task['id']}/pause").json()["status"] == "paused"
    wait_for(lambda: c.get("/api/v1/robots/ugv_04").json()["state"]["mode"] == "paused", 5)
    c.post(f"/api/v1/tasks/{task['id']}/resume")
    wait_for(
        lambda: task_status(c, task["id"]) in ("planning", "executing", "waiting_for_traffic"), 5
    )
    assert c.post(f"/api/v1/tasks/{task['id']}/cancel").json()["status"] == "cancelled"


def test_unimplemented_protocol_fails_connection_test_honestly(client):
    r = client.post(
        "/api/v1/robots",
        json={
            "name": "MQTT Bot",
            "fleet_id": "warehouse_ugv",
            "communication": {"protocol": "mqtt", "endpoint": "mqtt://broker"},
            "spawn": {"waypoint_id": "SA5"},
        },
    )
    assert r.status_code == 201
    checks = r.json()["checks"]
    assert checks and not checks[0]["ok"] and "not implemented" in checks[0]["detail"]
    assert r.json()["state"]["online"] is False


def test_legacy_rmf_routes_still_work(client):
    fleets = client.get("/fleets").json()["data"]
    assert any(f["name"] == "warehouse_ugv" and "ugv_01" in f["robots"] for f in fleets)
    r = client.post(
        "/tasks/dispatch_task",
        json={
            "type": "dispatch_task_request",
            "request": {
                "category": "navigate_to_waypoint",
                "description": {"waypoint": "Loading Dock 1"},
            },
        },
    )
    assert r.status_code == 200, r.text
    tid = r.json()["data"]["task_id"]
    assert client.get(f"/tasks/{tid}/state").json()["data"]["task_id"] == tid
    assert client.get("/doors").json()["data"] == []  # never fabricated
    bm = client.get("/building_map").json()["data"]
    assert any(len(level["nav_graphs"][0]["edges"]) > 0 for level in bm["levels"])


def test_map_image_upload_and_rmf_export(client):
    png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    r = client.post(
        "/api/v1/maps/warehouse_demo/image",
        json={
            "data_url": png,
            "resolution": 0.05,
            "origin": [0, 0, 0],
            "width_px": 1,
            "height_px": 1,
        },
    )
    assert r.status_code == 200 and r.json()["image"]["url"].startswith(
        "/api/v1/maps/warehouse_demo/image"
    )
    assert client.get("/api/v1/maps/warehouse_demo/image").status_code == 200
    export = client.get("/api/v1/maps/warehouse_demo/export/rmf-nav-graph")
    assert export.status_code == 200 and '"vertices"' in export.text


def test_websocket_streams_snapshot_then_updates(client):
    with client.websocket_connect("/api/v1/ws") as ws:
        first = ws.receive_json()
        assert first["type"] == "snapshot" and len(first["data"]["robots"]) == 16
        client.post(
            "/api/v1/maps/warehouse_demo/waypoints", json={"name": "WS Probe", "x": 5, "y": 5}
        )
        seen, probe = set(), False
        for _ in range(400):
            msg = ws.receive_json()
            seen.add(msg["type"])
            probe = probe or (msg["type"] == "registry" and msg["data"]["id"] == "ws_probe")
            if probe and "robots" in seen:
                break
        assert probe and "robots" in seen


def test_spawn_inside_restricted_zone_is_rejected(client):
    r = client.post(
        "/api/v1/robots",
        json={"name": "Trespasser", "fleet_id": "warehouse_ugv", "spawn": {"waypoint_id": "RX1"}},
    )
    assert r.status_code == 422 and "restricted" in r.json()["message"]


def test_robot_trapped_in_new_restricted_zone_can_drive_out(client):
    c = client
    # ugv_04 sits on Charger 4 (9.5, 1.8); an operator draws a restricted zone around it.
    r = c.post(
        "/api/v1/maps/warehouse_demo/zones",
        json={
            "name": "Spill",
            "type": "restricted",
            "polygon": [[8.5, 0.8], [10.5, 0.8], [10.5, 5.2], [8.5, 5.2]],
        },
    )
    assert r.status_code == 201
    task = c.post(
        "/api/v1/robots/ugv_04/navigate", headers=UI, json={"waypoint": "Intersection 1"}
    ).json()
    wait_for(lambda: task_status(c, task["id"]) == "completed", 40)
    notes = " ".join(h["message"] for h in c.get(f"/api/v1/tasks/{task['id']}").json()["history"])
    assert "Leaving restricted zone 'Spill'" in notes


def test_zone_destination_is_estimated_to_a_free_spot(client):
    c = client
    # uav_02..04 sit on Drone Pads 2-4; uav_01 hovers at Storage Overwatch, nearest Pad 4.
    task = c.post(
        "/api/v1/robots/uav_01/navigate", headers=UI, json={"waypoint": "Storage Overwatch (air)"}
    ).json()
    wait_for(lambda: task_status(c, task["id"]) == "completed", 40)

    def land_task():
        return next(
            (
                t
                for t in c.get("/api/v1/tasks").json()
                if t["type"] == "land" and t["assigned_robot"] == "uav_01"
            ),
            None,
        )

    land = wait_for(land_task, 20)
    explanation = land["allocation"]["explanation"]
    assert "Drone Pad 1" in explanation and "Drone Pad 4" not in explanation, explanation
    wait_for(lambda: task_status(c, land["id"]) == "completed", 40)
    assert c.get("/api/v1/robots/uav_01").json()["state"]["current_waypoint"] == "PAD1"
