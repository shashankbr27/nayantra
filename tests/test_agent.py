"""
tests/test_agent.py

Unit tests for agent pipeline models and plan resolution.
"""
from __future__ import annotations

import pytest

from nayantra.agent.models import AgentPlan, MissionResult, StepResult, StepStatus, ToolCall


# ---------------------------------------------------------------------------
# ToolCall
# ---------------------------------------------------------------------------

def test_tool_call_minimal():
    tc = ToolCall(tool="list_robots")
    assert tc.tool == "list_robots"
    assert tc.parameters == {}
    assert tc.depends_on is None
    assert tc.reason is None


def test_tool_call_with_params():
    tc = ToolCall(tool="get_task_state", parameters={"task_id": "abc-123"}, depends_on=0)
    assert tc.parameters["task_id"] == "abc-123"
    assert tc.depends_on == 0


# ---------------------------------------------------------------------------
# AgentPlan
# ---------------------------------------------------------------------------

def test_agent_plan_empty():
    plan = AgentPlan()
    assert plan.steps == []
    assert plan.direct_answer is None
    assert plan.clarification_needed is None


def test_agent_plan_direct_answer():
    plan = AgentPlan(direct_answer="No robots available.")
    assert plan.direct_answer == "No robots available."
    assert plan.steps == []


def test_agent_plan_with_steps():
    plan = AgentPlan(steps=[
        ToolCall(tool="list_robots"),
        ToolCall(tool="get_task_state", parameters={"task_id": "t1"}, depends_on=0),
    ])
    assert len(plan.steps) == 2
    assert plan.steps[1].depends_on == 0


# ---------------------------------------------------------------------------
# StepResult
# ---------------------------------------------------------------------------

def test_step_result_success():
    sr = StepResult(step_index=0, tool="list_robots", status=StepStatus.SUCCESS)
    assert sr.status == StepStatus.SUCCESS
    assert sr.error is None


def test_step_result_failure():
    sr = StepResult(
        step_index=1,
        tool="dispatch_task",
        status=StepStatus.FAILED,
        error="Connection refused",
    )
    assert sr.status == StepStatus.FAILED
    assert "refused" in sr.error


def test_step_status_enum_values():
    assert StepStatus.SUCCESS == "success"
    assert StepStatus.FAILED == "failed"
    assert StepStatus.SKIPPED == "skipped"


# ---------------------------------------------------------------------------
# MissionResult
# ---------------------------------------------------------------------------

def test_mission_result_defaults():
    mr = MissionResult(command="list all robots")
    assert mr.command == "list all robots"
    assert mr.success is False
    assert mr.steps == []
    assert mr.summary == ""
    assert len(mr.mission_id) > 10


def test_mission_result_unique_ids():
    a = MissionResult()
    b = MissionResult()
    assert a.mission_id != b.mission_id


def test_mission_result_with_steps():
    mr = MissionResult(
        command="move robot",
        success=True,
        steps=[StepResult(step_index=0, tool="move_robot", status=StepStatus.SUCCESS)],
    )
    assert len(mr.steps) == 1
    assert mr.success is True
