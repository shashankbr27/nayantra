"""
nayantra/agent/planner.py

Multi-step task planner with dependency-graph resolution.

The LLM produces an AgentPlan containing a flat list of ToolCall steps,
each with an optional `depends_on` index.  This planner:

  1. Validates the dependency graph (detects cycles)
  2. Builds a topological execution order
  3. Supports parallel execution of independent steps
  4. Enriches step parameters with domain-specific defaults

Example dependency chain:
  Step 0: list_robots            (no dependency)
  Step 1: dispatch_task          depends_on=0   (needs robot list result)
  Step 2: get_task_state         depends_on=1   (needs task_id from step 1)
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional, Set, Tuple

from nayantra.agent.models import AgentPlan, MissionResult, StepResult, StepStatus, ToolCall

logger = logging.getLogger("nayantra.planner")


class PlanValidationError(Exception):
    """Raised when a plan has a structural problem (cycles, bad indices)."""
    pass


class TaskPlanner:
    """
    Validates and optimises AgentPlan execution order.

    Usage:
        planner = TaskPlanner()
        ordered_groups = planner.build_execution_groups(plan)
        # ordered_groups is a list of lists — each inner list can run in parallel
    """

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate(self, plan: AgentPlan) -> None:
        """
        Raise PlanValidationError if the plan has:
          - Invalid depends_on indices
          - Circular dependencies
        """
        n = len(plan.steps)
        for i, step in enumerate(plan.steps):
            if step.depends_on is not None:
                if not (0 <= step.depends_on < n):
                    raise PlanValidationError(
                        f"Step {i} ({step.tool}) references out-of-range "
                        f"depends_on={step.depends_on} (plan has {n} steps)"
                    )
                if step.depends_on >= i:
                    raise PlanValidationError(
                        f"Step {i} ({step.tool}) depends_on={step.depends_on} "
                        f"which is not a prior step"
                    )

        # Detect cycles via DFS
        if self._has_cycle(plan):
            raise PlanValidationError("Plan contains a dependency cycle")

    def _has_cycle(self, plan: AgentPlan) -> bool:
        """Kahn's algorithm — returns True if the dependency graph has a cycle."""
        n = len(plan.steps)
        in_degree = [0] * n
        adj: Dict[int, List[int]] = defaultdict(list)

        for i, step in enumerate(plan.steps):
            if step.depends_on is not None:
                adj[step.depends_on].append(i)
                in_degree[i] += 1

        queue = deque(i for i in range(n) if in_degree[i] == 0)
        processed = 0
        while queue:
            node = queue.popleft()
            processed += 1
            for neighbour in adj[node]:
                in_degree[neighbour] -= 1
                if in_degree[neighbour] == 0:
                    queue.append(neighbour)

        return processed != n

    # ------------------------------------------------------------------
    # Execution group builder (topological sort → parallel groups)
    # ------------------------------------------------------------------

    def build_execution_groups(self, plan: AgentPlan) -> List[List[int]]:
        """
        Return a list of execution groups.

        Each group is a list of step indices that can run in parallel
        (they share no dependencies within the same group).

        Example for a 4-step plan where step 2 depends on step 0:
            Group 0: [0, 1]     ← run in parallel
            Group 1: [2, 3]     ← run after group 0
        """
        self.validate(plan)
        n = len(plan.steps)

        # Compute depth of each node
        depth: List[int] = [0] * n
        for i, step in enumerate(plan.steps):
            if step.depends_on is not None:
                depth[i] = depth[step.depends_on] + 1

        # Group by depth
        groups: Dict[int, List[int]] = defaultdict(list)
        for i in range(n):
            groups[depth[i]].append(i)

        return [groups[d] for d in sorted(groups.keys())]

    # ------------------------------------------------------------------
    # Enrichment: inject sensible defaults / context-aware params
    # ------------------------------------------------------------------

    def enrich_step(
        self,
        step: ToolCall,
        context: Dict[str, Any],
    ) -> ToolCall:
        """
        Fill in missing parameters from context produced by prior steps.

        Rules applied:
          - If a navigate/move tool is missing fleet_name and context has one, inject it
          - If a task-monitoring tool is missing task_id and context has one, inject it
          - Normalise waypoint strings (strip whitespace, lower-case)
        """
        params = dict(step.parameters)

        # Inject fleet_name from context when not supplied
        if "fleet_name" not in params and "fleet_name" in context:
            params["fleet_name"] = context["fleet_name"]

        # Inject robot_name from context when not supplied
        if "robot_name" not in params and "robot_name" in context:
            params["robot_name"] = context["robot_name"]

        # Inject task_id from context for task-monitoring tools
        if step.tool in ("get_task_state", "get_task_log", "cancel_task", "resume_task", "interrupt_task"):
            if "task_id" not in params and "task_id" in context:
                params["task_id"] = context["task_id"]

        # Normalise waypoint casing
        if "waypoint" in params and isinstance(params["waypoint"], str):
            params["waypoint"] = params["waypoint"].strip()

        return ToolCall(
            tool=step.tool,
            parameters=params,
            reason=step.reason,
            depends_on=step.depends_on,
        )

    # ------------------------------------------------------------------
    # Describe plan (for logging / debug)
    # ------------------------------------------------------------------

    def describe(self, plan: AgentPlan) -> str:
        """Return a human-readable plan description."""
        if not plan.steps:
            return "(no tool steps — direct answer)"
        lines = ["Execution plan:"]
        for i, step in enumerate(plan.steps):
            dep = f" [depends on step {step.depends_on}]" if step.depends_on is not None else ""
            lines.append(f"  {i}. {step.tool}{dep}")
            if step.reason:
                lines.append(f"     reason: {step.reason}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parallel execution helper used by the agent
# ---------------------------------------------------------------------------

async def execute_group_parallel(
    steps: List[ToolCall],
    indices: List[int],
    execute_fn,          # async fn(step, index, context) -> StepResult
    context: Dict[str, Any],
) -> List[StepResult]:
    """
    Execute a group of independent steps concurrently.

    Args:
        steps:       All plan steps (needed to look up by index).
        indices:     Indices in this parallel group.
        execute_fn:  Coroutine function to run each step.
        context:     Shared mutable context (updated after each step).
    """
    tasks = [
        asyncio.create_task(execute_fn(steps[i], i, context))
        for i in indices
    ]
    results = await asyncio.gather(*tasks, return_exceptions=False)
    return list(results)
