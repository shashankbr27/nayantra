"""
MCP tool registry + execution against a real in-process Nayantra Core.
"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio

from nayantra.core.runtime import NayantraCore
from nayantra.core.server import create_app
from nayantra.mcp.core_client import CoreClient, Trace
from nayantra.mcp.tools import (
    TOOL_REGISTRY,
    ToolContext,
    ToolParamError,
    execute_tool,
    get_all_tools,
)


@pytest_asyncio.fixture
async def ctx():
    core = NayantraCore(db_path=":memory:", scenario="warehouse_demo")
    await core.start()
    app = create_app(core, start_core=False)
    client = CoreClient(base_url="http://core", transport=httpx.ASGITransport(app=app))
    yield ToolContext(core=client, trace=Trace(mission_id="m-test", command="deliver two boxes"))
    await client.close()
    await core.stop()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_tools_registered():
    assert len(get_all_tools()) > 10


def test_tool_schema_shape():
    for tool in get_all_tools():
        assert {"name", "description", "parameters", "required", "risk"} <= set(tool)
        assert tool["risk"] in ("read", "act", "dangerous")


def test_expected_high_level_tools_present():
    names = {t["name"] for t in get_all_tools()}
    required = {
        "list_fleets",
        "list_robots",
        "get_robot_state",
        "get_fleet_state",
        "get_map",
        "list_waypoints",
        "create_task",
        "pause_task",
        "resume_task",
        "cancel_task",
        "navigate_robot",
        "get_traffic_state",
        "get_active_conflicts",
        "get_task_status",
        "register_robot",
        "create_fleet",
    }
    assert not required - names, f"missing: {required - names}"


def test_no_low_level_motion_tools():
    for t in get_all_tools(include_legacy=True):
        blob = (t["name"] + t["description"]).lower()
        assert "cmd_vel" not in blob and "velocity" not in t["name"]


def test_legacy_aliases_hidden_but_executable():
    visible = {t["name"] for t in get_all_tools()}
    everything = {t["name"] for t in get_all_tools(include_legacy=True)}
    for legacy in ("move_robot", "dispatch_task", "get_task_state", "get_building_map"):
        assert legacy not in visible and legacy in everything


def test_open_rmf_infra_tools_are_opt_in():
    names = {t["name"] for t in get_all_tools()}
    assert not {"list_doors", "control_door", "request_lift"} & names


def test_dangerous_tools_are_marked():
    risk = {t["name"]: t["risk"] for t in get_all_tools()}
    assert (
        risk["stop_all_robots"] == risk["emergency_stop_all"] == risk["remove_robot"] == "dangerous"
    )


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def test_list_robots_and_filter(ctx):
    robots = await execute_tool(ctx, "list_robots", {})
    assert len(robots) == 16
    ugvs = await execute_tool(ctx, "list_robots", {"fleet_id": "warehouse_ugv"})
    assert {r["id"] for r in ugvs} == {"ugv_01", "ugv_02", "ugv_03", "ugv_04"}


async def test_get_map_lists_places_and_areas(ctx):
    m = await execute_tool(ctx, "get_map", {})
    assert m["id"] == "warehouse_demo"
    assert any(p["name"] == "Receiving Bay 1" for p in m["places"])
    assert {"Receiving", "Storage"} <= {a["name"] for a in m["areas"]}


async def test_create_task_lets_nayantra_allocate_and_traces_origin(ctx):
    res = await execute_tool(
        ctx,
        "create_task",
        {"task_type": "delivery", "pickup": "receiving", "dropoff": "storage", "count": 2},
    )
    assert res["ok"] and len(res["tasks"]) == 2
    t = res["tasks"][0]
    assert t["robot"] and "Selected" in t["allocation"]
    status = await execute_tool(ctx, "get_task_status", {"task_id": t["id"]})
    assert status["origin"]["source"] == "llm"
    assert status["origin"]["command"] == "deliver two boxes"
    assert status["origin"]["mission_id"] == "m-test"


async def test_unknown_place_comes_back_as_readable_error(ctx):
    res = await execute_tool(
        ctx, "create_task", {"task_type": "navigate", "destination": "Recieving Bay 1"}
    )
    assert res["ok"] is False
    assert "Did you mean" in res["error"] and "Receiving Bay 1" in res["error"]


async def test_invalid_parameters_are_rejected_before_running(ctx):
    with pytest.raises(ToolParamError):
        await execute_tool(ctx, "create_task", {"task_type": "teleport"})
    with pytest.raises(ToolParamError):
        await execute_tool(
            ctx, "navigate_robot", {"robot": "ugv_01", "destination": "x", "speed": 9}
        )


async def test_unknown_tool_raises(ctx):
    with pytest.raises(KeyError):
        await execute_tool(ctx, "publish_cmd_vel", {})


async def test_stop_all_needs_operator_confirmation(ctx):
    res = await execute_tool(ctx, "stop_all_robots", {})
    assert res["confirmation_required"] is True
    assert "confirm" in res["message"]


async def test_restricted_destination_needs_confirmation(ctx):
    res = await execute_tool(
        ctx, "navigate_robot", {"robot": "UGV-01", "destination": "HV Maintenance Point"}
    )
    assert res["confirmation_required"] is True


async def test_legacy_move_robot_still_works(ctx):
    res = await execute_tool(
        ctx, "move_robot", {"waypoint": "Loading Dock 1", "robot_name": "ugv_02"}
    )
    assert res["ok"] and res["task"]["robot"] == "ugv_02"


async def test_traffic_state_shape(ctx):
    t = await execute_tool(ctx, "get_traffic_state", {})
    assert {"waiting", "reservations_held", "planned_routes", "recent_conflicts"} <= set(t)


# ---------------------------------------------------------------------------
# Isaac Demo tools (opt-in via ISAAC_DEMO_URL; the helper is tested directly)
# ---------------------------------------------------------------------------


async def test_isaac_demo_request_errors_when_url_unset(monkeypatch):
    from nayantra.mcp.tools import _isaac_demo_request

    monkeypatch.setattr("nayantra.mcp.tools.settings.ISAAC_DEMO_URL", "")
    with pytest.raises(RuntimeError, match="ISAAC_DEMO_URL is not configured"):
        await _isaac_demo_request("GET", "/state")


async def test_isaac_demo_request_calls_right_endpoint(monkeypatch):
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
            return_value=httpx.Response(200, json={"x": 1.2, "moving": False})
        )
        assert (await _isaac_demo_request("POST", "/goto", params={"waypoint": "charging_dock"}))[
            "ok"
        ] is True
        assert (await _isaac_demo_request("GET", "/state"))["x"] == pytest.approx(1.2)


async def test_isaac_demo_request_propagates_http_errors(monkeypatch):
    import respx

    from nayantra.mcp.tools import _isaac_demo_request

    monkeypatch.setattr("nayantra.mcp.tools.settings.ISAAC_DEMO_URL", "http://isaac:8900")
    with respx.mock:
        respx.post("http://isaac:8900/goto").mock(return_value=httpx.Response(404, text="bad"))
        with pytest.raises(httpx.HTTPStatusError):
            await _isaac_demo_request("POST", "/goto", params={"waypoint": "no_such_place"})


def test_isaac_demo_tools_registered_iff_url_is_set():
    from nayantra.config import settings

    names = {
        "isaac_list_waypoints",
        "isaac_goto_waypoint",
        "isaac_goto_xy",
        "isaac_get_robot_state",
    }
    registered = set(TOOL_REGISTRY) & names
    assert registered == (names if settings.ISAAC_DEMO_URL else set())
