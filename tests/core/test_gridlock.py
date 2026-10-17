"""Gridlock between parked robots: two robots each sitting on the other's destination.

The Gazebo warehouse (three robots: UGV-1 on the receiving dock, UGV-3 on the charger, UGV-2 on the
shipping dock) driven by the built-in simulator. "Send UGV-1 to charge" while "send UGV-3 to the receiving
dock" used to freeze both: UGV-1's charge task only slept while "all destinations are occupied", so it
never asked the traffic coordinator for a route, and the coordinator's swap planner (which needs both
robots to have a pending request) never engaged.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nayantra.core.runtime import NayantraCore
from nayantra.core.server import create_app

UI = {"X-Nayantra-Client": "ui"}
REPO = Path(__file__).resolve().parents[2]
CHARGER = (-10.5, -5.5)
RECV_DOCK = (-10.5, 0.0)


def wait_for(fn, timeout: float = 40.0, interval: float = 0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


@pytest.fixture
def warehouse(tmp_path):
    """The real Gazebo warehouse map and robots, but driven by the kinematic simulator."""
    scenario = json.loads(
        (REPO / "config/scenarios/gazebo_warehouse.json").read_text(encoding="utf-8")
    )
    scenario["maps"] = [str(REPO / "config/maps/gazebo_warehouse.json")]
    for fleet in scenario["fleets"]:
        fleet["communication"] = {"protocol": "simulation", "namespace": ""}
        fleet["limits"]["max_speed"] = 1.0
    path = tmp_path / "gridlock.json"
    path.write_text(json.dumps(scenario), encoding="utf-8")
    core = NayantraCore(db_path=":memory:", scenario=str(path))
    with TestClient(create_app(core)) as c:
        c.patch("/api/v1/sim", json={"speed": 8.0})
        wait_for(
            lambda: (
                len(core.fleet.runtimes) == 3
                and all(rt.state.online for rt in core.fleet.runtimes.values())
            ),
            10,
        )
        c.core = core
        yield c


def pos(c, rid):
    p = c.get(f"/api/v1/robots/{rid}").json()["state"]["pose"]
    return p["x"], p["y"]


def near(c, rid, target, tol=0.5):
    x, y = pos(c, rid)
    return math.hypot(x - target[0], y - target[1]) < tol


def task_of(c, rid):
    tasks = c.get("/api/v1/tasks").json()
    tasks = tasks if isinstance(tasks, list) else tasks.get("items", [])
    return [t for t in tasks if t.get("assigned_robot") == rid]


def test_charge_and_navigate_that_swap_places_resolve(warehouse):
    """UGV-1 (receiving dock) -> charger while UGV-3 (charger) -> receiving dock."""
    c = warehouse
    assert near(c, "ugv_01", RECV_DOCK) and near(c, "ugv_03", CHARGER)
    assert c.post("/api/v1/robots/ugv_01/charge", headers=UI).status_code == 201
    assert (
        c.post(
            "/api/v1/robots/ugv_03/navigate", headers=UI, json={"waypoint": "recv_dock"}
        ).status_code
        == 201
    )
    closest = [99.0]

    def swapped():
        (x1, y1), (x3, y3) = pos(c, "ugv_01"), pos(c, "ugv_03")
        closest[0] = min(closest[0], math.hypot(x1 - x3, y1 - y3))
        return near(c, "ugv_01", CHARGER) and near(c, "ugv_03", RECV_DOCK)

    wait_for(swapped, 60)
    assert closest[0] > 0.9, "the robots must never overlap while swapping"
    for rid in ("ugv_01", "ugv_03"):
        assert all(t["status"] in ("completed", "executing") for t in task_of(c, rid)), task_of(
            c, rid
        )


def test_navigate_swap_between_the_same_two_places_resolves(warehouse):
    """Same swap with two explicit destinations: the path that already worked must keep working."""
    c = warehouse
    assert (
        c.post(
            "/api/v1/robots/ugv_01/navigate", headers=UI, json={"waypoint": "charger"}
        ).status_code
        == 201
    )
    assert (
        c.post(
            "/api/v1/robots/ugv_03/navigate", headers=UI, json={"waypoint": "recv_dock"}
        ).status_code
        == 201
    )
    wait_for(lambda: near(c, "ugv_01", CHARGER) and near(c, "ugv_03", RECV_DOCK), 60)


def test_swap_when_both_robots_are_already_working_is_planned_jointly(warehouse):
    """Neither robot can be asked to make way (both have a task), so the swap planner has to do it.

    Both robots are held while their tasks are queued, then released together: UGV-1's charge task and
    UGV-3's navigate task start in the same moment, each blocked by the other.
    """
    c = warehouse
    for rid in ("ugv_01", "ugv_03"):
        assert c.post(f"/api/v1/robots/{rid}/pause", headers=UI).status_code == 200
    assert c.post("/api/v1/robots/ugv_01/charge", headers=UI).status_code == 201
    assert (
        c.post(
            "/api/v1/robots/ugv_03/navigate", headers=UI, json={"waypoint": "recv_dock"}
        ).status_code
        == 201
    )
    for rid in ("ugv_01", "ugv_03"):
        c.post(f"/api/v1/robots/{rid}/resume", headers=UI)
    closest = [99.0]

    def swapped():
        (x1, y1), (x3, y3) = pos(c, "ugv_01"), pos(c, "ugv_03")
        closest[0] = min(closest[0], math.hypot(x1 - x3, y1 - y3))
        return near(c, "ugv_01", CHARGER) and near(c, "ugv_03", RECV_DOCK)

    wait_for(swapped, 60)
    assert closest[0] > 0.9
    kinds = {t["type"] for rid in ("ugv_01", "ugv_03") for t in task_of(c, rid)}
    assert "make_way" not in kinds, (
        "both robots were busy: nobody should have been asked to make way"
    )


def test_charge_task_asks_an_idle_robot_to_leave_the_only_charger(warehouse):
    """UGV-3 sits idle on the only charger; UGV-1 needs it. UGV-3 must make way, not block forever."""
    c = warehouse
    assert near(c, "ugv_03", CHARGER)
    assert c.post("/api/v1/robots/ugv_01/charge", headers=UI).status_code == 201
    wait_for(lambda: near(c, "ugv_01", CHARGER), 60)
    assert not near(c, "ugv_03", CHARGER, 1.0), "the idle robot should have moved off the charger"


def test_charge_task_keeps_waiting_while_another_robot_is_heading_to_the_charger(warehouse):
    """The fallback must not steal a charger another robot is already driving to."""
    c = warehouse
    # UGV-3 leaves the charger for a far node; UGV-2 is sent to the charger first.
    assert (
        c.post(
            "/api/v1/robots/ugv_03/navigate", headers=UI, json={"waypoint": "north_mid"}
        ).status_code
        == 201
    )
    wait_for(lambda: not near(c, "ugv_03", CHARGER, 1.5), 20)
    assert c.post("/api/v1/robots/ugv_02/charge", headers=UI).status_code == 201
    wait_for(lambda: c.core.fleet.runtimes["ugv_02"].goal_node == "charger", 10)
    assert c.post("/api/v1/robots/ugv_01/charge", headers=UI).status_code == 201
    time.sleep(1.5)
    assert c.core.fleet.runtimes["ugv_01"].goal_node != "charger", (
        "UGV-01 must not claim the charger UGV-02 is heading to"
    )
    assert c.core.fleet.runtimes["ugv_02"].goal_node == "charger"
