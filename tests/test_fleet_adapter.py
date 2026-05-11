"""
tests/test_fleet_adapter.py

Tests for the ROS 2 fleet adapter simulation mode.
No ROS 2 required — all tests run against the stub/simulation path.
"""

from __future__ import annotations

import math

import pytest

from nayantra.ros2_adapter.fleet_adapter import (
    WAREHOUSE_WAYPOINTS,
    RMFFleetAdapter,
    RobotMode,
    RobotState,
)


@pytest.fixture
def adapter():
    return RMFFleetAdapter(
        fleet_name="test_fleet",
        robot_name="test_bot",
        ros2_enabled=False,
    )


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


def test_adapter_initial_mode_idle(adapter):
    assert adapter.state.mode == RobotMode.IDLE


def test_adapter_initial_position_origin(adapter):
    assert adapter.state.location.x == 0.0
    assert adapter.state.location.y == 0.0


def test_adapter_state_dict_shape(adapter):
    d = adapter.state_dict()
    assert "name" in d
    assert "fleet" in d
    assert "mode" in d
    assert "battery_percent" in d
    assert "location" in d
    assert "task_id" in d


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_navigate_to_sets_goal(adapter):
    result = await adapter.navigate_to(3.0, 2.0, 0.5)
    assert result is True
    assert adapter._nav_goal is not None
    assert adapter._nav_goal.x == 3.0
    assert adapter._nav_goal.y == 2.0


@pytest.mark.asyncio
async def test_navigate_sets_moving_mode(adapter):
    await adapter.navigate_to(1.0, 1.0)
    assert adapter.state.mode == RobotMode.MOVING


@pytest.mark.asyncio
async def test_navigate_to_waypoint_known(adapter):
    result = await adapter.navigate_to_waypoint("charging_dock", WAREHOUSE_WAYPOINTS)
    assert result is True
    assert adapter._nav_goal is not None
    goal = adapter._nav_goal
    cx, cy, _ = WAREHOUSE_WAYPOINTS["charging_dock"]
    assert goal.x == cx
    assert goal.y == cy


@pytest.mark.asyncio
async def test_navigate_to_waypoint_unknown_returns_false(adapter):
    result = await adapter.navigate_to_waypoint("nonexistent_place", WAREHOUSE_WAYPOINTS)
    assert result is False


# ---------------------------------------------------------------------------
# Simulation stepping
# ---------------------------------------------------------------------------


def test_sim_step_moves_toward_goal(adapter):
    adapter._nav_goal = type("G", (), {"x": 2.0, "y": 0.0, "yaw": 0.0})()
    adapter._step_simulation(dt=1.0)
    # At 0.5 m/s for 1 second, should have moved 0.5m toward (2,0)
    assert adapter.state.location.x > 0.0
    assert adapter.state.mode == RobotMode.MOVING


def test_sim_step_arrives_and_clears_goal(adapter):
    # Place goal very close to origin
    adapter._nav_goal = type("G", (), {"x": 0.01, "y": 0.0, "yaw": 0.0})()
    adapter._step_simulation(dt=1.0)
    assert adapter._nav_goal is None
    assert adapter.state.mode == RobotMode.IDLE


def test_sim_step_no_goal_stays_idle(adapter):
    adapter._nav_goal = None
    adapter._step_simulation(dt=1.0)
    assert adapter.state.mode == RobotMode.IDLE
    assert adapter.state.location.x == 0.0


def test_yaw_computed_correctly(adapter):
    """Robot heading toward (1, 1) should be ~45°."""
    adapter._nav_goal = type("G", (), {"x": 1.0, "y": 1.0, "yaw": 0.0})()
    adapter._step_simulation(dt=1.0)
    expected_yaw = math.atan2(1.0, 1.0)  # ~0.785 rad
    assert abs(adapter.state.location.yaw - expected_yaw) < 0.01


# ---------------------------------------------------------------------------
# Pause / resume / emergency stop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_sets_paused_mode(adapter):
    await adapter.navigate_to(5.0, 0.0)
    await adapter.pause()
    assert adapter.state.mode == RobotMode.PAUSED
    assert adapter._nav_goal is None


@pytest.mark.asyncio
async def test_resume_from_paused(adapter):
    adapter.state.mode = RobotMode.PAUSED
    await adapter.resume()
    assert adapter.state.mode == RobotMode.IDLE


@pytest.mark.asyncio
async def test_resume_when_not_paused_is_noop(adapter):
    adapter.state.mode = RobotMode.MOVING
    await adapter.resume()
    assert adapter.state.mode == RobotMode.MOVING


@pytest.mark.asyncio
async def test_emergency_stop(adapter):
    await adapter.navigate_to(5.0, 5.0)
    await adapter.emergency_stop()
    assert adapter.state.mode == RobotMode.EMERGENCY
    assert adapter._nav_goal is None
    assert adapter.state.task_id == ""


# ---------------------------------------------------------------------------
# State callbacks
# ---------------------------------------------------------------------------


def test_state_callback_called_on_publish(adapter):
    received = []
    adapter.on_state_update(received.append)
    adapter._publish_state()
    assert len(received) == 1
    assert isinstance(received[0], RobotState)


def test_multiple_callbacks(adapter):
    results_a, results_b = [], []
    adapter.on_state_update(results_a.append)
    adapter.on_state_update(results_b.append)
    adapter._publish_state()
    assert len(results_a) == 1
    assert len(results_b) == 1


# ---------------------------------------------------------------------------
# Warehouse waypoints
# ---------------------------------------------------------------------------


def test_warehouse_waypoints_have_required_locations():
    for name in ("charging_dock", "zone_a", "zone_b", "zone_c"):
        assert name in WAREHOUSE_WAYPOINTS
        coords = WAREHOUSE_WAYPOINTS[name]
        assert len(coords) == 3


def test_warehouse_waypoints_are_numeric():
    for name, coords in WAREHOUSE_WAYPOINTS.items():
        for v in coords:
            assert isinstance(v, (int, float)), f"{name} has non-numeric coord {v}"
