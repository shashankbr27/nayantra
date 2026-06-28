"""
nayantra/mcp/tools.py

Declarative MCP tool registry.

Each tool is defined as a dict (the schema exposed to the LLM) paired with
an async handler that calls the OpenRMF client.  Adding a new tool is a
single-block addition — no if/elif chains required.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from nayantra.config import settings
from nayantra.rmf_client.client import OpenRMFClient

logger = logging.getLogger("nayantra.tools")

# ---------------------------------------------------------------------------
# Type alias
# ---------------------------------------------------------------------------
Handler = Callable[
    [OpenRMFClient, dict[str, Any]],
    Coroutine[Any, Any, Any],
]

# ---------------------------------------------------------------------------
# Internal registry: (schema, handler) tuples
# ---------------------------------------------------------------------------
TOOL_REGISTRY: dict[str, tuple[dict[str, Any], Handler]] = {}


def _tool(schema: dict[str, Any]) -> Callable[[Handler], Handler]:
    """Decorator that registers a tool handler alongside its schema."""

    def decorator(fn: Handler) -> Handler:
        name = schema["name"]
        TOOL_REGISTRY[name] = (schema, fn)
        return fn

    return decorator


def get_all_tools() -> list[dict[str, Any]]:
    """Return all tool schemas (used by /tools endpoint and LLM context)."""
    return [schema for schema, _ in TOOL_REGISTRY.values()]


async def execute_tool(client: OpenRMFClient, tool_name: str, params: dict[str, Any]) -> Any:
    """Dispatch a tool call to the registered handler."""
    if tool_name not in TOOL_REGISTRY:
        raise KeyError(f"Unknown tool: {tool_name}")
    _, handler = TOOL_REGISTRY[tool_name]
    logger.info(f"Executing tool: {tool_name} params={params}")
    return await handler(client, params)


# ===========================================================================
# TOOL DEFINITIONS
# ===========================================================================

# ---------------------------------------------------------------------------
# Fleet & Robot
# ---------------------------------------------------------------------------


@_tool(
    {
        "name": "list_robots",
        "description": "List all robots registered across all fleets.",
        "parameters": {},
        "api_endpoint": "GET /fleets",
    }
)
async def _list_robots(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_fleets()


@_tool(
    {
        "name": "get_robot_status",
        "description": "Get the current status and pose of a specific robot.",
        "parameters": {
            "fleet_name": {"type": "string", "description": "Name of the fleet"},
            "robot_name": {"type": "string", "description": "Name of the robot"},
        },
        "api_endpoint": "GET /fleets/{fleet_name}/robots/{robot_name}",
    }
)
async def _get_robot_status(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_robot_state(params["fleet_name"], params["robot_name"])


@_tool(
    {
        "name": "get_fleet_log",
        "description": "Retrieve the event log for a named fleet.",
        "parameters": {
            "fleet_name": {"type": "string", "description": "Fleet name"},
        },
        "api_endpoint": "GET /fleets/{fleet_name}/log",
    }
)
async def _get_fleet_log(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_fleet_log(params["fleet_name"])


@_tool(
    {
        "name": "decommission_robot",
        "description": "Remove a robot from active duty in its fleet.",
        "parameters": {
            "fleet_name": {"type": "string", "description": "Fleet name"},
            "robot_name": {"type": "string", "description": "Robot name"},
        },
        "api_endpoint": "POST /fleets/{fleet_name}/decommission",
    }
)
async def _decommission_robot(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_decommission_robot(params["fleet_name"], params["robot_name"])


@_tool(
    {
        "name": "recommission_robot",
        "description": "Return a decommissioned robot to active duty.",
        "parameters": {
            "fleet_name": {"type": "string", "description": "Fleet name"},
            "robot_name": {"type": "string", "description": "Robot name"},
        },
        "api_endpoint": "POST /fleets/{fleet_name}/recommission",
    }
)
async def _recommission_robot(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_recommission_robot(params["fleet_name"], params["robot_name"])


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@_tool(
    {
        "name": "dispatch_task",
        "description": (
            "Dispatch a new task to the RMF fleet. "
            "Supports: navigate_to_waypoint, delivery, patrol, loop."
        ),
        "parameters": {
            "category": {
                "type": "string",
                "description": "Task category: navigate_to_waypoint | delivery | patrol | loop",
            },
            "description": {
                "type": "object",
                "description": "Category-specific task payload (see RMF task API docs)",
            },
            "fleet_name": {
                "type": "string",
                "description": "Target fleet (optional — RMF will assign if omitted)",
            },
            "robot_name": {
                "type": "string",
                "description": "Target robot (optional — RMF will assign if omitted)",
            },
            "priority": {
                "type": "integer",
                "description": "Task priority 0 (lowest) – 9 (highest). Default 0.",
            },
        },
        "api_endpoint": "POST /tasks/dispatch_task",
    }
)
async def _dispatch_task(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    payload = {
        "type": "dispatch_task_request",
        "request": {
            "unix_millis_earliest_start_time": 0,
            "priority": {"type": "binary", "value": params.get("priority", 0)},
            "category": params["category"],
            "description": params.get("description", {}),
        },
    }
    if params.get("fleet_name"):
        payload["request"]["fleet_name"] = params["fleet_name"]
    if params.get("robot_name"):
        payload["request"]["robot_name"] = params["robot_name"]
    return await client.post_dispatch_task(payload)


@_tool(
    {
        "name": "move_robot",
        "description": (
            "Command a robot to navigate to a named waypoint. "
            "This is a convenience wrapper around dispatch_task."
        ),
        "parameters": {
            "fleet_name": {"type": "string", "description": "Fleet name"},
            "robot_name": {"type": "string", "description": "Robot name"},
            "waypoint": {"type": "string", "description": "Named waypoint / place on the map"},
        },
        "api_endpoint": "POST /tasks/dispatch_task",
    }
)
async def _move_robot(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    payload = {
        "type": "dispatch_task_request",
        "request": {
            "unix_millis_earliest_start_time": 0,
            "priority": {"type": "binary", "value": 0},
            "category": "navigate_to_waypoint",
            "description": {"waypoint": params["waypoint"]},
            "fleet_name": params.get("fleet_name"),
            "robot_name": params.get("robot_name"),
        },
    }
    return await client.post_dispatch_task(payload)


@_tool(
    {
        "name": "stop_robot",
        "description": "Immediately cancel all active tasks for a robot.",
        "parameters": {
            "task_id": {"type": "string", "description": "ID of the task to cancel"},
        },
        "api_endpoint": "POST /tasks/cancel_task",
    }
)
async def _stop_robot(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_cancel_task({"task_id": params["task_id"]})


@_tool(
    {
        "name": "get_task_state",
        "description": "Get the current state of a task by its ID.",
        "parameters": {
            "task_id": {"type": "string", "description": "Task ID"},
        },
        "api_endpoint": "GET /tasks/{task_id}/state",
    }
)
async def _get_task_state(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_task_state(params["task_id"])


@_tool(
    {
        "name": "list_tasks",
        "description": "Query all active and completed tasks.",
        "parameters": {},
        "api_endpoint": "GET /tasks",
    }
)
async def _list_tasks(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_tasks()


@_tool(
    {
        "name": "get_task_log",
        "description": "Retrieve the detailed event log for a task.",
        "parameters": {
            "task_id": {"type": "string", "description": "Task ID"},
        },
        "api_endpoint": "GET /tasks/{task_id}/log",
    }
)
async def _get_task_log(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_task_log(params["task_id"])


@_tool(
    {
        "name": "cancel_task",
        "description": "Cancel a pending or active task.",
        "parameters": {
            "task_id": {"type": "string", "description": "Task ID to cancel"},
        },
        "api_endpoint": "POST /tasks/cancel_task",
    }
)
async def _cancel_task(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_cancel_task({"task_id": params["task_id"]})


@_tool(
    {
        "name": "resume_task",
        "description": "Resume a paused task.",
        "parameters": {
            "task_id": {"type": "string", "description": "Task ID to resume"},
        },
        "api_endpoint": "POST /tasks/resume_task",
    }
)
async def _resume_task(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_resume_task({"task_id": params["task_id"]})


@_tool(
    {
        "name": "interrupt_task",
        "description": "Temporarily interrupt a running task.",
        "parameters": {
            "task_id": {"type": "string", "description": "Task ID to interrupt"},
        },
        "api_endpoint": "POST /tasks/interrupt_task",
    }
)
async def _interrupt_task(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_interrupt_task({"task_id": params["task_id"]})


# ---------------------------------------------------------------------------
# Doors
# ---------------------------------------------------------------------------


@_tool(
    {
        "name": "list_doors",
        "description": "List all doors in the building.",
        "parameters": {},
        "api_endpoint": "GET /doors",
    }
)
async def _list_doors(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_doors()


@_tool(
    {
        "name": "get_door_state",
        "description": "Get the current open/closed state of a named door.",
        "parameters": {
            "door_name": {"type": "string", "description": "Door name"},
        },
        "api_endpoint": "GET /doors/{door_name}/state",
    }
)
async def _get_door_state(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_door_state(params["door_name"])


@_tool(
    {
        "name": "control_door",
        "description": "Open or close a named door.",
        "parameters": {
            "door_name": {"type": "string", "description": "Door name"},
            "mode": {
                "type": "integer",
                "description": "0 = closed, 2 = open",
            },
        },
        "api_endpoint": "POST /doors/{door_name}/request",
    }
)
async def _control_door(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_door_request(params["door_name"], {"mode": params["mode"]})


# ---------------------------------------------------------------------------
# Lifts / Elevators
# ---------------------------------------------------------------------------


@_tool(
    {
        "name": "list_lifts",
        "description": "List all lifts (elevators) in the building.",
        "parameters": {},
        "api_endpoint": "GET /lifts",
    }
)
async def _list_lifts(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_lifts()


@_tool(
    {
        "name": "get_lift_state",
        "description": "Get current floor and mode of a named lift.",
        "parameters": {
            "lift_name": {"type": "string", "description": "Lift name"},
        },
        "api_endpoint": "GET /lifts/{lift_name}/state",
    }
)
async def _get_lift_state(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_lift_state(params["lift_name"])


@_tool(
    {
        "name": "request_lift",
        "description": "Call a lift to a destination floor.",
        "parameters": {
            "lift_name": {"type": "string", "description": "Lift name"},
            "destination_floor": {"type": "string", "description": "Target floor label"},
            "door_state": {
                "type": "integer",
                "description": "0 = closed, 2 = open",
            },
        },
        "api_endpoint": "POST /lifts/{lift_name}/request",
    }
)
async def _request_lift(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_lift_request(
        params["lift_name"],
        {
            "destination_floor": params["destination_floor"],
            "door_state": params.get("door_state", 2),
        },
    )


# ---------------------------------------------------------------------------
# Alerts & Safety
# ---------------------------------------------------------------------------


@_tool(
    {
        "name": "list_alerts",
        "description": "List all active alerts in the system.",
        "parameters": {},
        "api_endpoint": "GET /alerts",
    }
)
async def _list_alerts(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_alerts()


@_tool(
    {
        "name": "get_alert",
        "description": "Get details of a specific alert.",
        "parameters": {
            "alert_id": {"type": "string", "description": "Alert ID"},
        },
        "api_endpoint": "GET /alerts/{alert_id}",
    }
)
async def _get_alert(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_alert(params["alert_id"])


@_tool(
    {
        "name": "respond_to_alert",
        "description": "Acknowledge or respond to an alert.",
        "parameters": {
            "alert_id": {"type": "string", "description": "Alert ID"},
            "response": {
                "type": "string",
                "description": "Response action (e.g., acknowledge, resume, cancel)",
            },
        },
        "api_endpoint": "POST /alerts/{alert_id}/response",
    }
)
async def _respond_to_alert(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_alert_response(params["alert_id"], {"response": params["response"]})


@_tool(
    {
        "name": "reset_fire_alarm",
        "description": "Reset the fire alarm trigger after it has been addressed.",
        "parameters": {},
        "api_endpoint": "POST /fire_alarm_trigger/reset",
    }
)
async def _reset_fire_alarm(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.post_reset_fire_alarm_trigger({})


@_tool(
    {
        "name": "get_fire_alarm_state",
        "description": "Check whether the fire alarm has been triggered.",
        "parameters": {},
        "api_endpoint": "GET /fire_alarm_trigger",
    }
)
async def _get_fire_alarm_state(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_previous_fire_alarm_trigger()


# ---------------------------------------------------------------------------
# Building map & infrastructure
# ---------------------------------------------------------------------------


@_tool(
    {
        "name": "get_building_map",
        "description": "Retrieve the building map including levels, waypoints, and lanes.",
        "parameters": {},
        "api_endpoint": "GET /building_map",
    }
)
async def _get_building_map(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_building_map()


@_tool(
    {
        "name": "list_dispensers",
        "description": "List all dispenser (payload drop-off) stations.",
        "parameters": {},
        "api_endpoint": "GET /dispensers",
    }
)
async def _list_dispensers(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_dispensers()


@_tool(
    {
        "name": "list_ingestors",
        "description": "List all ingestor (payload pick-up) stations.",
        "parameters": {},
        "api_endpoint": "GET /ingestors",
    }
)
async def _list_ingestors(client: OpenRMFClient, params: dict[str, Any]) -> Any:
    return await client.get_ingestors()


# ---------------------------------------------------------------------------
# Isaac Demo (scripts/isaac_demo.py) tools
#
# Only registered when ISAAC_DEMO_URL is set. These let the LLM drive Carter
# in the live Isaac Sim WebRTC demo directly, bypassing the RMF / fleet adapter
# pipeline. Useful for visually impressive demos where the agent's command
# produces a robot movement in the photoreal Isaac viewport.
# ---------------------------------------------------------------------------


async def _isaac_demo_request(method: str, path: str, **kw: Any) -> Any:
    """Short-lived httpx call to the isaac_demo control API."""
    if not settings.ISAAC_DEMO_URL:
        raise RuntimeError(
            "ISAAC_DEMO_URL is not configured. Set it in .env "
            "(e.g. http://172.25.60.165:8900) to enable the Isaac demo tools."
        )
    url = f"{settings.ISAAC_DEMO_URL.rstrip('/')}{path}"
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.request(method, url, **kw)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "text": resp.text}


if settings.ISAAC_DEMO_URL:

    @_tool(
        {
            "name": "isaac_list_waypoints",
            "description": (
                "List the named waypoints available in the Isaac Sim demo "
                "warehouse (e.g. 'charging_dock', 'loading_dock', 'canteen'). "
                "Use this before calling isaac_goto_waypoint to discover valid names."
            ),
            "parameters": {},
            "api_endpoint": "GET /waypoints",
        }
    )
    async def _isaac_list_waypoints(client: OpenRMFClient, params: dict[str, Any]) -> Any:
        return await _isaac_demo_request("GET", "/waypoints")

    @_tool(
        {
            "name": "isaac_goto_waypoint",
            "description": (
                "Drive the robot to a named waypoint in the Isaac Sim demo. "
                "PREFER THIS over move_robot for warehouse-demo commands: this "
                "produces a visible motion in the live WebRTC stream."
            ),
            "parameters": {
                "waypoint": {
                    "type": "string",
                    "description": "Named waypoint (call isaac_list_waypoints to discover).",
                },
            },
            "api_endpoint": "POST /goto?waypoint=...",
        }
    )
    async def _isaac_goto_waypoint(client: OpenRMFClient, params: dict[str, Any]) -> Any:
        return await _isaac_demo_request("POST", "/goto", params={"waypoint": params["waypoint"]})

    @_tool(
        {
            "name": "isaac_goto_xy",
            "description": (
                "Drive the robot to absolute (x, y) coordinates in the Isaac Sim demo. "
                "Use when the destination isn't a named waypoint."
            ),
            "parameters": {
                "x": {"type": "number", "description": "Target x in metres."},
                "y": {"type": "number", "description": "Target y in metres."},
            },
            "api_endpoint": "POST /goto?x=..&y=..",
        }
    )
    async def _isaac_goto_xy(client: OpenRMFClient, params: dict[str, Any]) -> Any:
        return await _isaac_demo_request(
            "POST", "/goto", params={"x": params["x"], "y": params["y"]}
        )

    @_tool(
        {
            "name": "isaac_get_robot_state",
            "description": (
                "Get the live state of the robot in the Isaac Sim demo: position, "
                "yaw, whether it's currently moving, and the current target."
            ),
            "parameters": {},
            "api_endpoint": "GET /state",
        }
    )
    async def _isaac_get_robot_state(client: OpenRMFClient, params: dict[str, Any]) -> Any:
        return await _isaac_demo_request("GET", "/state")

    logger.info(f"Isaac Demo tools registered (target: {settings.ISAAC_DEMO_URL})")
