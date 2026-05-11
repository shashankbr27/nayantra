"""
nayantra/agent/models.py

Pydantic models for the AI agent pipeline.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class StepStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class ToolCall(BaseModel):
    tool: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    depends_on: int | None = None


class AgentPlan(BaseModel):
    steps: list[ToolCall] = Field(default_factory=list)
    direct_answer: str | None = None
    clarification_needed: str | None = None


class StepResult(BaseModel):
    step_index: int
    tool: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    status: StepStatus = StepStatus.SUCCESS
    result: Any | None = None
    error: str | None = None
    duration_ms: float = 0.0


class MissionResult(BaseModel):
    mission_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    command: str = ""
    summary: str = ""
    success: bool = False
    steps: list[StepResult] = Field(default_factory=list)
