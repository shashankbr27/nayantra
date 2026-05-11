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
    required = {"list_robots", "dispatch_task", "get_task_state", "move_robot",
                "list_doors", "list_lifts", "list_alerts"}
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
