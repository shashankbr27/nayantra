"""Quick local smoke test of the RMF bridge in kinematic-sim mode."""

import time

from fastapi.testclient import TestClient

from nayantra.rmf_bridge.server import app


def main() -> None:
    with TestClient(app) as client:
        r = client.get("/health").json()
        print("health:", r["mode"], "robots:", r["robots"])
        assert r["status"] == "ok"

        # building map exposes waypoint names for the LLM
        bm = client.get("/building_map").json()["data"]
        names = [v["name"] for v in bm["levels"][0]["nav_graphs"][0]["vertices"]]
        print("waypoints:", names)

        # dispatch exactly what the MCP move_robot tool sends
        payload = {
            "type": "dispatch_task_request",
            "request": {
                "unix_millis_earliest_start_time": 0,
                "priority": {"type": "binary", "value": 0},
                "category": "navigate_to_waypoint",
                "description": {"waypoint": "zone_c"},
                "fleet_name": None,
                "robot_name": None,
            },
        }
        r = client.post("/tasks/dispatch_task", json=payload).json()
        task_id = r["data"]["task_id"]
        print("dispatched:", r["data"])

        # bad waypoint must fail fast with the valid list
        bad = client.post(
            "/tasks/dispatch_task",
            json={
                "request": {
                    "category": "navigate_to_waypoint",
                    "description": {"waypoint": "narnia"},
                }
            },
        )
        print("bad waypoint ->", bad.status_code, bad.json()["detail"][:70], "...")
        assert bad.status_code == 400

        # poll to completion (zone_c is 2 m away, sim speed 0.5 m/s => ~4-5 s)
        deadline = time.time() + 30
        status = "queued"
        while time.time() < deadline:
            status = client.get(f"/tasks/{task_id}/state").json()["data"]["status"]
            if status in ("completed", "failed", "canceled"):
                break
            time.sleep(0.5)
        print("final task status:", status)

        fleets = client.get("/fleets").json()["data"]
        robot = list(fleets[0]["robots"].values())[0]
        print("robot location:", robot["location"], "status:", robot["status"])

        log = client.get(f"/tasks/{task_id}/log").json()["data"]["log"]
        for e in log:
            print("  log:", e["text"])

        assert status == "completed", f"task did not complete: {status}"
        assert abs(robot["location"]["x"] - 0.0) < 0.2
        assert abs(robot["location"]["y"] - (-2.0)) < 0.2

        # delivery task: pickup -> dropoff
        r = client.post(
            "/tasks/dispatch_task",
            json={
                "request": {
                    "category": "delivery",
                    "description": {"pickup": "zone_c", "dropoff": "elevator_lobby"},
                }
            },
        ).json()
        d_id = r["data"]["task_id"]
        deadline = time.time() + 60
        while time.time() < deadline:
            status = client.get(f"/tasks/{d_id}/state").json()["data"]["status"]
            if status in ("completed", "failed", "canceled"):
                break
            time.sleep(0.5)
        print("delivery status:", status)
        assert status == "completed"

        print("\nSMOKE TEST PASSED")


if __name__ == "__main__":
    main()
