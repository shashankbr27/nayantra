"""
tests/test_rmf_models.py

Tests for the Pydantic RMF data models.
"""
from __future__ import annotations

import pytest

from nayantra.rmf_client.models import (
    Alert,
    BuildingMap,
    DispatchTaskRequest,
    DoorMode,
    DoorState,
    FleetState,
    Level,
    LiftRequest,
    LiftState,
    Location2D,
    RobotStatus,
    TaskBooking,
    TaskPriority,
    TaskRequest,
    TaskState,
    TaskStatus,
)


# ---------------------------------------------------------------------------
# Location2D
# ---------------------------------------------------------------------------

def test_location_defaults():
    loc = Location2D(x=1.0, y=2.0, yaw=0.5)
    assert loc.level_name == ""
    assert loc.index is None


def test_location_full():
    loc = Location2D(x=1.0, y=2.0, yaw=0.5, level_name="L1", index=3)
    assert loc.level_name == "L1"
    assert loc.index == 3


# ---------------------------------------------------------------------------
# RobotStatus
# ---------------------------------------------------------------------------

def test_robot_status_defaults():
    r = RobotStatus(name="r1")
    assert r.status == ""
    assert r.battery == 0.0
    assert r.task_id == ""


def test_robot_status_battery_range():
    r = RobotStatus(name="r1", battery=0.85)
    assert r.battery == 0.85


def test_robot_status_with_location():
    loc = Location2D(x=1.0, y=2.0, yaw=0.0)
    r = RobotStatus(name="r1", location=loc)
    assert r.location.x == 1.0


# ---------------------------------------------------------------------------
# FleetState
# ---------------------------------------------------------------------------

def test_fleet_state_empty_robots():
    f = FleetState(name="fleet_a")
    assert f.robots == {}


def test_fleet_state_with_robots():
    robots = {"r1": RobotStatus(name="r1", status="idle")}
    f = FleetState(name="fleet_a", robots=robots)
    assert "r1" in f.robots
    assert f.robots["r1"].status == "idle"


# ---------------------------------------------------------------------------
# Task models
# ---------------------------------------------------------------------------

def test_task_priority_defaults():
    p = TaskPriority()
    assert p.type == "binary"
    assert p.value == 0


def test_task_request_minimal():
    req = TaskRequest(category="navigate_to_waypoint")
    assert req.category == "navigate_to_waypoint"
    assert req.description == {}
    assert req.fleet_name is None
    assert req.priority.value == 0


def test_dispatch_task_request_type():
    req = DispatchTaskRequest(
        request=TaskRequest(
            category="navigate_to_waypoint",
            description={"waypoint": "dock"},
        )
    )
    assert req.type == "dispatch_task_request"
    assert req.request.description["waypoint"] == "dock"


def test_task_state_minimal():
    ts = TaskState(booking=TaskBooking(id="task-001"))
    assert ts.booking.id == "task-001"
    assert ts.completed == []


def test_task_status_values():
    for v in ("queued", "underway", "completed", "failed", "cancelled", "killed"):
        s = TaskStatus(value=v)
        assert s.value == v


# ---------------------------------------------------------------------------
# Door models
# ---------------------------------------------------------------------------

def test_door_state_defaults():
    d = DoorState(name="main_door")
    assert d.current_mode.value == DoorMode.CLOSED


def test_door_mode_enum():
    assert DoorMode.CLOSED == 0
    assert DoorMode.MOVING == 1
    assert DoorMode.OPEN == 2


# ---------------------------------------------------------------------------
# Lift models
# ---------------------------------------------------------------------------

def test_lift_state_defaults():
    ls = LiftState(name="lift_1")
    assert ls.current_floor == ""
    assert ls.available_floors == []


def test_lift_request_defaults():
    lr = LiftRequest(destination_floor="L2")
    assert lr.door_state == 2
    assert lr.session_id == "nayantra"


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------

def test_alert_minimal():
    a = Alert(id="alert-001")
    assert a.tier == "info"
    assert a.responses_available == []


def test_alert_full():
    a = Alert(
        id="alert-001",
        title="Obstacle detected",
        tier="warning",
        responses_available=["acknowledge", "cancel"],
    )
    assert a.title == "Obstacle detected"
    assert len(a.responses_available) == 2


# ---------------------------------------------------------------------------
# BuildingMap
# ---------------------------------------------------------------------------

def test_building_map_defaults():
    bm = BuildingMap(name="Warehouse")
    assert bm.levels == []
    assert bm.lifts == []


def test_building_map_with_levels():
    bm = BuildingMap(
        name="Warehouse",
        levels=[Level(name="L1", elevation=0.0), Level(name="L2", elevation=4.0)],
    )
    assert len(bm.levels) == 2
    assert bm.levels[1].elevation == 4.0
