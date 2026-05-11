"""
nayantra/rmf_client/models.py

Pydantic models for Open-RMF REST API payloads.

These models serve two purposes:
  1. Validate and parse responses from the RMF server
  2. Serialise request bodies sent to the RMF server

Reference: https://github.com/open-rmf/rmf-web/tree/main/packages/api-server
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------


class Location2D(BaseModel):
    x: float
    y: float
    yaw: float
    level_name: str = ""
    index: int | None = None


# ---------------------------------------------------------------------------
# Fleet & Robot
# ---------------------------------------------------------------------------


class RobotStatus(BaseModel):
    name: str
    status: str = ""  # idle | charging | working | error
    task_id: str = ""
    battery: float = Field(0.0, ge=0.0, le=1.0)
    location: Location2D | None = None
    commission: dict[str, Any] | None = None


class FleetState(BaseModel):
    name: str
    robots: dict[str, RobotStatus] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskPriority(BaseModel):
    type: str = "binary"
    value: int = 0


class TaskRequest(BaseModel):
    unix_millis_earliest_start_time: int = 0
    priority: TaskPriority = Field(default_factory=TaskPriority)
    category: str  # navigate_to_waypoint | delivery | patrol | loop
    description: dict[str, Any] = Field(default_factory=dict)
    fleet_name: str | None = None
    robot_name: str | None = None
    labels: list[str] = Field(default_factory=list)


class DispatchTaskRequest(BaseModel):
    type: str = "dispatch_task_request"
    request: TaskRequest


class TaskPhase(BaseModel):
    id: int
    category: str | None = None
    detail: str | None = None


class TaskBooking(BaseModel):
    id: str
    unix_millis_earliest_start_time: int | None = None
    priority: TaskPriority | None = None
    labels: list[str] = Field(default_factory=list)
    requester: str | None = None


class TaskStatus(BaseModel):
    value: str = "unknown"  # uninitialized | blocked | error | failed | queued | standby | underway | completed | killed | canceled | interruped


class TaskState(BaseModel):
    booking: TaskBooking
    category: str | None = None
    detail: str | None = None
    unix_millis_start_time: int | None = None
    unix_millis_finish_time: int | None = None
    original_estimate_millis: int | None = None
    estimate_millis: int | None = None
    assigned_to: dict[str, str] | None = None
    status: TaskStatus | None = None
    phases: dict[str, TaskPhase] | None = None
    active: int | None = None
    completed: list[int] = Field(default_factory=list)
    cancelled: int | None = None
    killed: int | None = None
    interrupted_summary: dict[str, Any] | None = None


class DispatchTaskResponse(BaseModel):
    success: bool
    task_id: str | None = None
    state: TaskState | None = None
    errors: list[dict[str, Any]] | None = None


# ---------------------------------------------------------------------------
# Doors
# ---------------------------------------------------------------------------


class DoorMode(IntEnum):
    CLOSED = 0
    MOVING = 1
    OPEN = 2


class DoorModeMsg(BaseModel):
    value: int = DoorMode.CLOSED


class DoorState(BaseModel):
    name: str
    current_mode: DoorModeMsg = Field(default_factory=DoorModeMsg)


class DoorRequest(BaseModel):
    mode: int = DoorMode.OPEN
    requester_id: str = "nayantra"
    request_time: int | None = None


# ---------------------------------------------------------------------------
# Lifts / Elevators
# ---------------------------------------------------------------------------


class LiftMotionState(IntEnum):
    STOPPED = 0
    UP = 1
    DOWN = 2
    UNKNOWN = 3


class LiftDoorState(IntEnum):
    CLOSED = 0
    MOVING = 1
    OPEN = 2


class LiftState(BaseModel):
    name: str
    current_floor: str = ""
    destination_floor: str = ""
    door_state: DoorModeMsg = Field(default_factory=DoorModeMsg)
    motion_state: DoorModeMsg = Field(default_factory=DoorModeMsg)
    available_floors: list[str] = Field(default_factory=list)
    lift_time: int | None = None
    session_id: str = ""


class LiftRequest(BaseModel):
    destination_floor: str
    door_state: int = LiftDoorState.OPEN
    request_type: int = 0
    session_id: str = "nayantra"
    request_time: int | None = None


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------


class AlertType(BaseModel):
    value: str = "task"  # task | fleet | default


class Alert(BaseModel):
    id: str
    original_id: str = ""
    category: AlertType | None = None
    unix_millis_alert_time: int = 0
    title: str = ""
    subtitle: str = ""
    message: str = ""
    display: dict[str, Any] | None = None
    tier: str = "info"  # info | warning | error
    responses_available: list[str] = Field(default_factory=list)
    alert_parameters: list[dict[str, Any]] = Field(default_factory=list)


class AlertResponse(BaseModel):
    id: str
    response: str


# ---------------------------------------------------------------------------
# Building map
# ---------------------------------------------------------------------------


class GraphVertex(BaseModel):
    x: float
    y: float
    name: str = ""
    params: list[dict[str, Any]] = Field(default_factory=list)


class GraphEdge(BaseModel):
    v1_idx: int
    v2_idx: int
    params: list[dict[str, Any]] = Field(default_factory=list)
    edge_type: int = 0


class NavGraph(BaseModel):
    name: str
    vertices: list[GraphVertex] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    params: list[dict[str, Any]] = Field(default_factory=list)


class Level(BaseModel):
    name: str
    elevation: float = 0.0
    nav_graphs: list[NavGraph] = Field(default_factory=list)
    images: list[Any] = Field(default_factory=list)


class BuildingMap(BaseModel):
    name: str
    levels: list[Level] = Field(default_factory=list)
    lifts: list[Any] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Dispensers / Ingestors
# ---------------------------------------------------------------------------


class Dispenser(BaseModel):
    guid: str
    type: str = "dispenser"


class Ingestor(BaseModel):
    guid: str
    type: str = "ingestor"
