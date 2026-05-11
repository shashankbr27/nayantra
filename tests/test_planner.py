"""
tests/test_planner.py

Tests for the multi-step task planner:
  - Dependency graph validation
  - Cycle detection
  - Execution group building (parallelism)
  - Parameter enrichment from context
  - Plan description
"""
from __future__ import annotations

import pytest

from nayantra.agent.models import AgentPlan, ToolCall
from nayantra.agent.planner import PlanValidationError, TaskPlanner


@pytest.fixture
def planner():
    return TaskPlanner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_plan(*tools: str, deps: dict = None) -> AgentPlan:
    """Build an AgentPlan from tool names and an optional deps dict {step_idx: depends_on}."""
    deps = deps or {}
    steps = [
        ToolCall(tool=t, parameters={}, depends_on=deps.get(i))
        for i, t in enumerate(tools)
    ]
    return AgentPlan(steps=steps)


# ---------------------------------------------------------------------------
# Validation — valid plans
# ---------------------------------------------------------------------------

def test_validate_empty_plan_ok(planner):
    planner.validate(AgentPlan(steps=[]))  # should not raise


def test_validate_linear_deps_ok(planner):
    plan = make_plan("a", "b", "c", deps={1: 0, 2: 1})
    planner.validate(plan)  # should not raise


def test_validate_diamond_deps_ok(planner):
    # 0 → 1, 0 → 2, 1 → 3, 2 → 3
    plan = make_plan("a", "b", "c", "d", deps={1: 0, 2: 0, 3: 2})
    planner.validate(plan)


def test_validate_no_deps_ok(planner):
    plan = make_plan("a", "b", "c")
    planner.validate(plan)


# ---------------------------------------------------------------------------
# Validation — invalid plans
# ---------------------------------------------------------------------------

def test_validate_out_of_range_dep_raises(planner):
    # step 0 claims to depend on step 5 (doesn't exist)
    steps = [ToolCall(tool="a", parameters={}, depends_on=5)]
    plan = AgentPlan(steps=steps)
    with pytest.raises(PlanValidationError, match="out-of-range"):
        planner.validate(plan)


def test_validate_forward_dep_raises(planner):
    # step 0 tries to depend on step 1 (forward — not allowed)
    steps = [
        ToolCall(tool="a", parameters={}, depends_on=1),
        ToolCall(tool="b", parameters={}),
    ]
    plan = AgentPlan(steps=steps)
    with pytest.raises(PlanValidationError, match="not a prior step"):
        planner.validate(plan)


def test_validate_self_dep_raises(planner):
    steps = [ToolCall(tool="a", parameters={}, depends_on=0)]
    plan = AgentPlan(steps=steps)
    with pytest.raises(PlanValidationError):
        planner.validate(plan)


# ---------------------------------------------------------------------------
# Execution groups (parallelism)
# ---------------------------------------------------------------------------

def test_groups_no_deps_all_in_one_group(planner):
    plan = make_plan("a", "b", "c")
    groups = planner.build_execution_groups(plan)
    # All independent → single group
    assert len(groups) == 1
    assert sorted(groups[0]) == [0, 1, 2]


def test_groups_linear_chain_sequential_groups(planner):
    # 0 → 1 → 2
    plan = make_plan("a", "b", "c", deps={1: 0, 2: 1})
    groups = planner.build_execution_groups(plan)
    assert len(groups) == 3
    assert groups[0] == [0]
    assert groups[1] == [1]
    assert groups[2] == [2]


def test_groups_fan_out(planner):
    # 0 is root; 1 and 2 both depend on 0; 3 depends on 2
    plan = make_plan("root", "b", "c", "d", deps={1: 0, 2: 0, 3: 2})
    groups = planner.build_execution_groups(plan)
    # depth: root=0, b=1, c=1, d=2
    assert groups[0] == [0]
    assert sorted(groups[1]) == [1, 2]
    assert groups[2] == [3]


def test_groups_two_independent_chains(planner):
    # Chain A: 0 → 1; Chain B: 2 → 3  (completely independent)
    plan = make_plan("a0", "a1", "b0", "b1", deps={1: 0, 3: 2})
    groups = planner.build_execution_groups(plan)
    assert len(groups) == 2
    assert sorted(groups[0]) == [0, 2]
    assert sorted(groups[1]) == [1, 3]


# ---------------------------------------------------------------------------
# Parameter enrichment
# ---------------------------------------------------------------------------

def test_enrich_injects_fleet_name_from_context(planner):
    step = ToolCall(tool="move_robot", parameters={"robot_name": "r1", "waypoint": "dock"})
    enriched = planner.enrich_step(step, {"fleet_name": "fleet_a"})
    assert enriched.parameters["fleet_name"] == "fleet_a"


def test_enrich_does_not_overwrite_existing_fleet(planner):
    step = ToolCall(tool="move_robot", parameters={"fleet_name": "my_fleet", "robot_name": "r1", "waypoint": "w"})
    enriched = planner.enrich_step(step, {"fleet_name": "other_fleet"})
    assert enriched.parameters["fleet_name"] == "my_fleet"


def test_enrich_injects_task_id_for_monitoring_tools(planner):
    for tool in ("get_task_state", "cancel_task", "resume_task", "interrupt_task", "get_task_log"):
        step = ToolCall(tool=tool, parameters={})
        enriched = planner.enrich_step(step, {"task_id": "task-xyz"})
        assert enriched.parameters["task_id"] == "task-xyz", f"Failed for {tool}"


def test_enrich_strips_waypoint_whitespace(planner):
    step = ToolCall(tool="move_robot", parameters={"waypoint": "  charging dock  "})
    enriched = planner.enrich_step(step, {})
    assert enriched.parameters["waypoint"] == "charging dock"


def test_enrich_does_not_mutate_original_step(planner):
    step = ToolCall(tool="move_robot", parameters={"waypoint": "dock"})
    planner.enrich_step(step, {"fleet_name": "f"})
    assert "fleet_name" not in step.parameters


# ---------------------------------------------------------------------------
# Describe
# ---------------------------------------------------------------------------

def test_describe_empty_plan(planner):
    desc = planner.describe(AgentPlan(steps=[]))
    assert "no tool steps" in desc


def test_describe_with_steps(planner):
    plan = make_plan("list_robots", "move_robot", deps={1: 0})
    desc = planner.describe(plan)
    assert "list_robots" in desc
    assert "move_robot" in desc
    assert "depends on step 0" in desc


def test_describe_includes_reason(planner):
    steps = [ToolCall(tool="list_robots", parameters={}, reason="Need robot list first")]
    plan = AgentPlan(steps=steps)
    desc = planner.describe(plan)
    assert "Need robot list first" in desc
