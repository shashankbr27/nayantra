"""
tests/test_mcp_server.py

Tests for the MCP tool registry and tool execution.
"""

from __future__ import annotations

import pytest

from nayantra.mcp.tools import TOOL_REGISTRY, execute_tool, get_all_tools
from nayantra.rmf_client.client import OpenRMFClient

# ---------------------------------------------------------------------------
# Registry inspection
# ---------------------------------------------------------------------------


def test_tools_registered():
    assert len(get_all_tools()) > 0


def test_tool_schema_has_name_and_description():
    for tool in get_all_tools():
        assert "name" in tool, f"Tool missing 'name': {tool}"
        assert "description" in tool, f"Tool missing 'description': {tool}"


def test_expected_tools_present():
    names = {t["name"] for t in get_all_tools()}
    required = {
        "list_robots",
        "dispatch_task",
        "get_task_state",
        "move_robot",
        "list_doors",
        "list_lifts",
        "list_alerts",
    }
    missing = required - names
    assert not missing, f"Missing tools: {missing}"


def test_tool_registry_matches_get_all_tools():
    registry_names = set(TOOL_REGISTRY.keys())
    schema_names = {t["name"] for t in get_all_tools()}
    assert registry_names == schema_names


# ---------------------------------------------------------------------------
# Tool execution (debug mode)
# ---------------------------------------------------------------------------


@pytest.fixture
async def debug_client():
    c = OpenRMFClient(debug=True)
    yield c
    await c.close()


async def test_execute_list_robots(debug_client):
    result = await execute_tool(debug_client, "list_robots", {})
    assert result is not None


async def test_execute_list_tasks(debug_client):
    result = await execute_tool(debug_client, "list_tasks", {})
    assert result is not None


async def test_execute_list_doors(debug_client):
    result = await execute_tool(debug_client, "list_doors", {})
    assert result is not None


async def test_execute_list_lifts(debug_client):
    result = await execute_tool(debug_client, "list_lifts", {})
    assert result is not None


async def test_execute_list_alerts(debug_client):
    result = await execute_tool(debug_client, "list_alerts", {})
    assert result is not None


async def test_execute_unknown_tool_raises(debug_client):
    with pytest.raises(KeyError):
        await execute_tool(debug_client, "nonexistent_tool_xyz", {})


# ---------------------------------------------------------------------------
# Isaac Demo tools
#
# These are registered at import time only when ISAAC_DEMO_URL is set, so we
# test the underlying _isaac_demo_request helper directly (which works either
# way) and check the gating + the wire format.
# ---------------------------------------------------------------------------


async def test_isaac_demo_request_errors_when_url_unset(monkeypatch):
    """If ISAAC_DEMO_URL is empty, calling the helper must raise — never
    silently no-op, otherwise the LLM gets a misleading 'success' response."""
    from nayantra.mcp.tools import _isaac_demo_request

    monkeypatch.setattr("nayantra.mcp.tools.settings.ISAAC_DEMO_URL", "")
    with pytest.raises(RuntimeError, match="ISAAC_DEMO_URL is not configured"):
        await _isaac_demo_request("GET", "/state")


async def test_isaac_demo_request_calls_right_endpoint(monkeypatch):
    """When ISAAC_DEMO_URL is set, the helper hits the right URL with the
    right method and query params, and returns the parsed JSON body."""
    import httpx
    import respx

    from nayantra.mcp.tools import _isaac_demo_request

    monkeypatch.setattr("nayantra.mcp.tools.settings.ISAAC_DEMO_URL", "http://isaac:8900")

    with respx.mock:
        respx.post("http://isaac:8900/goto", params={"waypoint": "charging_dock"}).mock(
            return_value=httpx.Response(
                200, json={"ok": True, "target": [-5.0, -2.0], "name": "charging_dock"}
            )
        )
        respx.get("http://isaac:8900/state").mock(
            return_value=httpx.Response(200, json={"x": 1.2, "y": 0.5, "yaw": 0.0, "moving": False})
        )

        goto_result = await _isaac_demo_request(
            "POST", "/goto", params={"waypoint": "charging_dock"}
        )
        assert goto_result == {"ok": True, "target": [-5.0, -2.0], "name": "charging_dock"}

        state_result = await _isaac_demo_request("GET", "/state")
        assert state_result["moving"] is False
        assert state_result["x"] == pytest.approx(1.2)


async def test_isaac_demo_request_propagates_http_errors(monkeypatch):
    """A 4xx/5xx from isaac_demo bubbles up so the agent sees a failed step
    rather than treating it as success."""
    import httpx
    import respx

    from nayantra.mcp.tools import _isaac_demo_request

    monkeypatch.setattr("nayantra.mcp.tools.settings.ISAAC_DEMO_URL", "http://isaac:8900")

    with respx.mock:
        respx.post("http://isaac:8900/goto").mock(return_value=httpx.Response(404, text="bad"))
        with pytest.raises(httpx.HTTPStatusError):
            await _isaac_demo_request("POST", "/goto", params={"waypoint": "no_such_place"})


def test_isaac_demo_tools_registered_iff_url_is_set():
    """The four isaac_* tools should appear in the registry exactly when
    ISAAC_DEMO_URL was non-empty at module import time."""
    from nayantra.config import settings

    isaac_tool_names = {
        "isaac_list_waypoints",
        "isaac_goto_waypoint",
        "isaac_goto_xy",
        "isaac_get_robot_state",
    }
    registered = set(TOOL_REGISTRY.keys()) & isaac_tool_names

    if settings.ISAAC_DEMO_URL:
        assert registered == isaac_tool_names, (
            f"With ISAAC_DEMO_URL set, expected all isaac_* tools registered; "
            f"missing: {isaac_tool_names - registered}"
        )
    else:
        assert registered == set(), (
            f"With ISAAC_DEMO_URL empty, no isaac_* tools should register; got: {registered}"
        )
