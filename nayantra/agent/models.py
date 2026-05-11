"""
nayantra/agent/models.py

Pydantic models for the AI agent pipeline.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class StepStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolCall(BaseModel):
    tool: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    reason: Optional[str] = None
    depends_on: Optional[int] = None


class AgentPlan(BaseModel):
    steps: List[ToolCall] = Field(default_factory=list)
    direct_answer: Optional[str] = None
    clarification_needed: Optional[str] = None


class StepResult(BaseModel):
    step_index: int
    tool: str
    parameters: Dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.SUCCESS
    result: Optional[Any] = None
    error: Optional[str] = None
    duration_ms: float = 0.0


class MissionResult(BaseModel):
    mission_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    command: str = ""
    summary: str = ""
    success: bool = False
    steps: List[StepResult] = Field(default_factory=list)
