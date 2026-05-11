"""
tests/test_rmf_client.py

Tests for OpenRMFClient in debug (simulated) mode.
All tests run without a live RMF server.
"""
from __future__ import annotations

import pytest

from nayantra.rmf_client.client import OpenRMFClient


@pytest.fixture
async def client():
    c = OpenRMFClient(debug=True)
    yield c
    await c.close()


# ---------------------------------------------------------------------------
# Fleets
# ---------------------------------------------------------------------------

async def test_get_fleets_returns_list(client):
    result = await client.get_fleets()
    data = result.get("data", result)
    assert isinstance(data, list)


async def test_get_robot_state_debug(client):
    result = await client.get_robot_state("turtlebot_fleet", "tb3_1")
    data = result.get("data", result)
    assert data is not None


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

async def test_get_tasks_debug(client):
    result = await client.get_tasks()
    assert result is not None


async def test_dispatch_task_debug(client):
    payload = {
        "type": "dispatch_task_request",
        "request": {
            "category": "navigate_to_waypoint",
            "description": {"waypoint": "charging_dock"},
        },
    }
    result = await client.post_dispatch_task(payload)
    data = result.get("data", {})
    assert "task_id" in data


async def test_cancel_task_debug(client):
    result = await client.post_cancel_task({"task_id": "test-id"})
    assert result is not None


async def test_get_task_state_debug(client):
    result = await client.get_task_state("some-task-id")
    assert result is not None


# ---------------------------------------------------------------------------
# Doors & Lifts
# ---------------------------------------------------------------------------

async def test_get_doors_debug(client):
    result = await client.get_doors()
    data = result.get("data", result)
    assert isinstance(data, list)


async def test_get_lifts_debug(client):
    result = await client.get_lifts()
    data = result.get("data", result)
    assert isinstance(data, list)


async def test_get_door_state_debug(client):
    result = await client.get_door_state("main_door")
    assert result is not None


async def test_get_lift_state_debug(client):
    result = await client.get_lift_state("lift_1")
    assert result is not None


# ---------------------------------------------------------------------------
# Alerts & Safety
# ---------------------------------------------------------------------------

async def test_get_alerts_debug(client):
    result = await client.get_alerts()
    assert result is not None


async def test_get_building_map_debug(client):
    result = await client.get_building_map()
    data = result.get("data", result)
    assert "name" in data


async def test_get_dispensers_debug(client):
    result = await client.get_dispensers()
    assert result is not None


async def test_get_ingestors_debug(client):
    result = await client.get_ingestors()
    assert result is not None
