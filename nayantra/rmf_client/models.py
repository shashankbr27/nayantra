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
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Common
# ---------------------------------------------------------------------------

class Location2D(BaseModel):
    x: float
    y: float
    yaw: float
    level_name: str = ""
    index: Optional[int] = None


# ---------------------------------------------------------------------------
# Fleet & Robot
# ---------------------------------------------------------------------------

class RobotStatus(BaseModel):
    name: str
    status: str = ""                     # idle | charging | working | error
    task_id: str = ""
    battery: float = Field(0.0, ge=0.0, le=1.0)
    location: Optional[Location2D] = None
    commission: Optional[Dict[str, Any]] = None


class FleetState(BaseModel):
    name: str
    robots: Dict[str, RobotStatus] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TaskPriority(BaseModel):
    type: str = "binary"
    value: int = 0


class TaskRequest(BaseModel):
    unix_millis_earliest_start_time: int = 0
    priority: TaskPriority = Field(default_factory=TaskPriority)
    category: str                        # navigate_to_waypoint | delivery | patrol | loop
    description: Dict[str, Any] = Field(default_factory=dict)
    fleet_name: Optional[str] = None
    robot_name: Optional[str] = None
    labels: List[str] = Field(default_factory=list)


class DispatchTaskRequest(BaseModel):
    type: str = "dispatch_task_request"
    request: TaskRequest


class TaskPhase(BaseModel):
    id: int
    category: Optional[str] = None
    detail: Optional[str] = None


class TaskBooking(BaseModel):
    id: str
    unix_millis_earliest_start_time: Optional[int] = None
    priority: Optional[TaskPriority] = None
    labels: List[str] = Field(default_factory=list)
    requester: Optional[str] = None


class TaskStatus(BaseModel):
    value: str = "unknown"   # uninitialized | blocked | error | failed | queued | standby | underway | completed | killed | canceled | interruped


class TaskState(BaseModel):
    booking: TaskBooking
    category: Optional[str] = None
    detail: Optional[str] = None
    unix_millis_start_time: Optional[int] = None
    unix_millis_finish_time: Optional[int] = None
    original_estimate_millis: Optional[int] = None
    estimate_millis: Optional[int] = None
    assigned_to: Optional[Dict[str, str]] = None
    status: Optional[TaskStatus] = None
    phases: Optional[Dict[str, TaskPhase]] = None
    active: Optional[int] = None
    completed: List[int] = Field(default_factory=list)
    cancelled: Optional[int] = None
    killed: Optional[int] = None
    interrupted_summary: Optional[Dict[str, Any]] = None


class DispatchTaskResponse(BaseModel):
    success: bool
    task_id: Optional[str] = None
    state: Optional[TaskState] = None
    errors: Optional[List[Dict[str, Any]]] = None


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
    request_time: Optional[int] = None


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
    available_floors: List[str] = Field(default_factory=list)
    lift_time: Optional[int] = None
    session_id: str = ""


class LiftRequest(BaseModel):
    destination_floor: str
    door_state: int = LiftDoorState.OPEN
    request_type: int = 0
    session_id: str = "nayantra"
    request_time: Optional[int] = None


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

class AlertType(BaseModel):
    value: str = "task"   # task | fleet | default


class Alert(BaseModel):
    id: str
    original_id: str = ""
    category: Optional[AlertType] = None
    unix_millis_alert_time: int = 0
    title: str = ""
    subtitle: str = ""
    message: str = ""
    display: Optional[Dict[str, Any]] = None
    tier: str = "info"    # info | warning | error
    responses_available: List[str] = Field(default_factory=list)
    alert_parameters: List[Dict[str, Any]] = Field(default_factory=list)


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
    params: List[Dict[str, Any]] = Field(default_factory=list)


class GraphEdge(BaseModel):
    v1_idx: int
    v2_idx: int
    params: List[Dict[str, Any]] = Field(default_factory=list)
    edge_type: int = 0


class NavGraph(BaseModel):
    name: str
    vertices: List[GraphVertex] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)
    params: List[Dict[str, Any]] = Field(default_factory=list)


class Level(BaseModel):
    name: str
    elevation: float = 0.0
    nav_graphs: List[NavGraph] = Field(default_factory=list)
    images: List[Any] = Field(default_factory=list)


class BuildingMap(BaseModel):
    name: str
    levels: List[Level] = Field(default_factory=list)
    lifts: List[Any] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Dispensers / Ingestors
# ---------------------------------------------------------------------------

class Dispenser(BaseModel):
    guid: str
    type: str = "dispenser"


class Ingestor(BaseModel):
    guid: str
    type: str = "ingestor"
