"""
tests/test_ws_monitor.py

Unit tests for the WebSocket connection manager and event routing.
"""
from __future__ import annotations

import pytest

from nayantra.api.ws_monitor import ConnectionManager, _event_to_topic


# ---------------------------------------------------------------------------
# _event_to_topic mapping
# ---------------------------------------------------------------------------

def test_fleet_state_maps_to_fleet():
    assert _event_to_topic("fleet_state") == "fleet"


def test_task_update_maps_to_tasks():
    assert _event_to_topic("task_update") == "tasks"


def test_alert_maps_to_alerts():
    assert _event_to_topic("alert") == "alerts"


def test_mission_start_maps_to_missions():
    assert _event_to_topic("mission_start") == "missions"


def test_mission_end_maps_to_missions():
    assert _event_to_topic("mission_end") == "missions"


def test_mission_step_maps_to_missions():
    assert _event_to_topic("mission_step") == "missions"


def test_unknown_event_maps_to_wildcard():
    assert _event_to_topic("unknown_event_xyz") == "*"
    assert _event_to_topic("") == "*"


# ---------------------------------------------------------------------------
# ConnectionManager
# ---------------------------------------------------------------------------

def test_manager_starts_empty():
    mgr = ConnectionManager()
    assert mgr.count == 0


def test_manager_count_type():
    mgr = ConnectionManager()
    assert isinstance(mgr.count, int)


def test_router_is_created():
    from nayantra.api.ws_monitor import router
    assert router is not None


def test_manager_is_module_level():
    from nayantra.api.ws_monitor import manager
    assert isinstance(manager, ConnectionManager)
