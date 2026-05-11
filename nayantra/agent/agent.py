"""
nayantra/agent/agent.py

Core AI Agent:
  - Supports Anthropic Claude and OpenAI GPT-4o as backends
  - Uses structured output / tool-use APIs for deterministic planning
  - Delegates step ordering and parallelism to TaskPlanner
  - Executes multi-step plans against the MCP server with retry
  - Propagates dynamic IDs (task_id, alert_id) between steps
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from nayantra.agent.models import (
    AgentPlan,
    MissionResult,
    StepResult,
    StepStatus,
    ToolCall,
)
from nayantra.agent.planner import PlanValidationError, TaskPlanner
from nayantra.config import settings

logger = logging.getLogger("nayantra.agent")

# IDs we surface into the shared step context. Searched recursively in tool results.
_ID_KEYS = frozenset({"task_id", "alert_id", "robot_id", "mission_id", "fleet_name", "robot_name"})

# ---------------------------------------------------------------------------
# System prompt injected for every planning call
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are an autonomous robot fleet operations assistant.
You have access to an Open-RMF fleet management system. Your job is to:
1. Parse the user's natural-language command.
2. Produce a structured multi-step plan using the available MCP tools.
3. Be precise about robot IDs, locations, and task types.

Available tool categories:
- Fleet & robot management: list_robots, get_robot_status, move_robot, stop_robot
- Task lifecycle: post_dispatch_task, get_task_state, post_cancel_task, post_resume_task
- Infrastructure: get_doors, get_door_state, get_lifts, get_lift_state
- Alerts & safety: get_alerts, post_reset_fire_alarm_trigger

Rules:
- Always verify a robot exists before commanding it.
- Use "navigate_to_waypoint" tasks for named locations.
- Use "delivery" tasks for point-A-to-point-B delivery.
- If a command is ambiguous, ask ONE clarifying question.
- Never make up robot IDs — always call list_robots first if unsure.
"""


# ---------------------------------------------------------------------------
# Tool-schema converters (shared between Claude and OpenAI code paths)
# ---------------------------------------------------------------------------

def to_anthropic_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an MCP tool schema to Anthropic tool-use format."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": {
            "type": "object",
            "properties": tool.get("parameters", {}),
        },
    }


def to_openai_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an MCP tool schema to OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": tool.get("parameters", {}),
            },
        },
    }


class RMFAgent:
    """LLM-powered agent that translates natural language into RMF fleet operations."""

    def __init__(self, mcp_url: Optional[str] = None) -> None:
        self.mcp_url = (mcp_url or settings.MCP_SERVER_URL).rstrip("/")
        self._tools_cache: List[Dict[str, Any]] = []
        self._tools_fetched_at: float = 0.0
        self._http = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
        self._planner = TaskPlanner()
        self._setup_llm_client()

    def _setup_llm_client(self) -> None:
        """Initialise the appropriate LLM client based on config."""
        if settings.LLM_PROVIDER == "anthropic":
            import anthropic  # type: ignore

            self._llm_provider = "anthropic"
            self._anthropic = anthropic.AsyncAnthropic(
                api_key=settings.ANTHROPIC_API_KEY
            )
            logger.info(f"LLM: Anthropic {settings.ANTHROPIC_MODEL}")
        else:
            from openai import AsyncOpenAI  # type: ignore

            self._llm_provider = "openai"
            self._openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            logger.info(f"LLM: OpenAI {settings.OPENAI_MODEL}")

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    async def _get_tools(self) -> List[Dict[str, Any]]:
        """Fetch available tools from MCP server (cached for 60 s)."""
        now = time.monotonic()
        if self._tools_cache and (now - self._tools_fetched_at) < 60:
            return self._tools_cache
        try:
            resp = await self._http.get(f"{self.mcp_url}/tools")
            resp.raise_for_status()
            self._tools_cache = resp.json()
            self._tools_fetched_at = now
            logger.debug(f"Fetched {len(self._tools_cache)} tools from MCP")
        except httpx.HTTPError as exc:
            logger.warning(f"MCP tool fetch failed: {exc}; using cache/fallback")
            if not self._tools_cache:
                self._tools_cache = self._load_fallback_tools()
        return self._tools_cache

    def _load_fallback_tools(self) -> List[Dict[str, Any]]:
        """Load tool definitions from the local fallback JSON."""
        try:
            with open(settings.FALLBACK_TOOLS_FILE) as fh:
                data = json.load(fh)
            logger.info(f"Loaded {len(data)} fallback tools from {settings.FALLBACK_TOOLS_FILE}")
            return data
        except Exception as exc:
            logger.error(f"Could not load fallback tools: {exc}")
            return []

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def _plan_with_anthropic(
        self, command: str, tools: List[Dict[str, Any]]
    ) -> AgentPlan:
        """Use Claude tool-use API to create a structured plan."""
        anthropic_tools = [to_anthropic_tool(t) for t in tools]

        messages = [{"role": "user", "content": command}]
        resp = await self._anthropic.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=anthropic_tools,
            messages=messages,
        )

        steps: List[ToolCall] = []
        direct_answer: Optional[str] = None

        for block in resp.content:
            if block.type == "tool_use":
                steps.append(
                    ToolCall(
                        tool=block.name,
                        parameters=block.input,
                        reason=f"Claude selected {block.name}",
                    )
                )
            elif block.type == "text" and block.text.strip():
                direct_answer = block.text.strip()

        return AgentPlan(steps=steps, direct_answer=direct_answer if not steps else None)

    async def _plan_with_openai(
        self, command: str, tools: List[Dict[str, Any]]
    ) -> AgentPlan:
        """Use GPT-4o function-calling to create a structured plan."""
        oai_tools = [to_openai_tool(t) for t in tools]

        resp = await self._openai.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": command},
            ],
            tools=oai_tools,
            tool_choice="auto",
            temperature=0,
        )

        msg = resp.choices[0].message
        steps: List[ToolCall] = []
        direct_answer: Optional[str] = None

        if msg.tool_calls:
            for tc in msg.tool_calls:
                steps.append(
                    ToolCall(
                        tool=tc.function.name,
                        parameters=json.loads(tc.function.arguments or "{}"),
                        reason=f"GPT-4o selected {tc.function.name}",
                    )
                )
        elif msg.content:
            direct_answer = msg.content

        return AgentPlan(steps=steps, direct_answer=direct_answer)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=8))
    async def plan(self, command: str) -> AgentPlan:
        """Generate an execution plan for the given command."""
        tools = await self._get_tools()
        if self._llm_provider == "anthropic":
            return await self._plan_with_anthropic(command, tools)
        else:
            return await self._plan_with_openai(command, tools)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    @retry(
        retry=retry_if_exception_type(httpx.TransportError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=1, max=8),
        reraise=True,
    )
    async def _call_mcp(self, tool: str, params: Dict[str, Any]) -> Any:
        """POST /run on the MCP server with transport-error retry."""
        resp = await self._http.post(
            f"{self.mcp_url}/run",
            json={"tool": tool, "parameters": params},
        )
        resp.raise_for_status()
        return resp.json()

    async def _execute_step(
        self,
        step: ToolCall,
        step_index: int,
        context: Dict[str, Any],
    ) -> StepResult:
        """Execute one tool call against the MCP server."""
        params = self._resolve_params(step.parameters, context)
        start = time.monotonic()
        try:
            result = await self._call_mcp(step.tool, params)
            duration = (time.monotonic() - start) * 1000
            self._extract_ids(result, context)
            return StepResult(
                step_index=step_index,
                tool=step.tool,
                parameters=params,
                status=StepStatus.SUCCESS,
                result=result,
                duration_ms=round(duration, 2),
            )
        except httpx.HTTPStatusError as exc:
            logger.error(f"Step {step_index} ({step.tool}) HTTP error: {exc}")
            return StepResult(
                step_index=step_index,
                tool=step.tool,
                parameters=params,
                status=StepStatus.FAILED,
                error=str(exc),
            )
        except Exception as exc:
            logger.error(f"Step {step_index} ({step.tool}) unexpected error: {exc}")
            return StepResult(
                step_index=step_index,
                tool=step.tool,
                parameters=params,
                status=StepStatus.FAILED,
                error=str(exc),
            )

    def _resolve_params(
        self, params: Dict[str, Any], context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Replace {{key}} placeholders with values from prior step outputs."""
        resolved = {}
        for k, v in params.items():
            if isinstance(v, str) and v.startswith("{{") and v.endswith("}}"):
                key = v[2:-2].strip()
                resolved[k] = context.get(key, v)
            elif isinstance(v, dict):
                resolved[k] = self._resolve_params(v, context)
            else:
                resolved[k] = v
        return resolved

    def _extract_ids(self, result: Any, context: Dict[str, Any]) -> None:
        """
        Walk a (possibly nested) result and copy any known ID keys into context.

        Recurses into dicts and lists so an ID buried under
        result["data"]["state"]["booking"]["id"] is still picked up.
        """
        if isinstance(result, dict):
            for k, v in result.items():
                if k in _ID_KEYS and isinstance(v, str) and v:
                    context[k] = v
                else:
                    self._extract_ids(v, context)
        elif isinstance(result, list):
            for item in result:
                self._extract_ids(item, context)

    async def _execute_group(
        self,
        plan: AgentPlan,
        indices: List[int],
        context: Dict[str, Any],
    ) -> List[StepResult]:
        """Run all steps in one parallel group concurrently."""
        return await asyncio.gather(*[
            self._execute_step(
                self._planner.enrich_step(plan.steps[i], context),
                i,
                context,
            )
            for i in indices
        ])

    async def execute_plan(self, plan: AgentPlan, command: str) -> MissionResult:
        """
        Execute a plan using the TaskPlanner's parallel execution groups.

        Steps with no dependencies run concurrently; dependent steps wait
        for their predecessors. Mission aborts on the first failed step.
        """
        mission = MissionResult(command=command)
        context: Dict[str, Any] = {}

        try:
            groups = self._planner.build_execution_groups(plan)
        except PlanValidationError as exc:
            logger.error(f"Plan validation failed: {exc}")
            mission.summary = f"Invalid plan: {exc}"
            return mission

        aborted = False
        for group_idx, group in enumerate(groups):
            if aborted:
                break
            logger.info(
                f"[Mission {mission.mission_id[:8]}] "
                f"Group {group_idx + 1}/{len(groups)}: {len(group)} step(s) in parallel"
            )
            results = await self._execute_group(plan, group, context)
            for result in results:
                mission.steps.append(result)
                if result.status == StepStatus.FAILED:
                    aborted = True
                    logger.warning(f"Step {result.step_index} failed — aborting mission")

        mission.steps.sort(key=lambda s: s.step_index)
        mission.success = (
            len(mission.steps) == len(plan.steps)
            and all(s.status == StepStatus.SUCCESS for s in mission.steps)
        )
        mission.summary = await self._summarise(command, mission)
        return mission

    async def _summarise(self, command: str, mission: MissionResult) -> str:
        """Ask the LLM to produce a human-readable mission summary."""
        results_json = json.dumps(
            [s.model_dump() for s in mission.steps], indent=2
        )
        prompt = (
            f"The user asked: {command!r}\n\n"
            f"Execution results:\n{results_json}\n\n"
            "Summarise what happened in 2-3 sentences, plain English, "
            "no bullet points."
        )
        try:
            if self._llm_provider == "anthropic":
                resp = await self._anthropic.messages.create(
                    model=settings.ANTHROPIC_MODEL,
                    max_tokens=256,
                    messages=[{"role": "user", "content": prompt}],
                )
                return resp.content[0].text.strip()
            else:
                resp = await self._openai.chat.completions.create(
                    model=settings.OPENAI_MODEL,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.5,
                    max_tokens=256,
                )
                return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error(f"Summary generation failed: {exc}")
            status = "successfully" if mission.success else "with errors"
            return f"Mission completed {status} in {len(mission.steps)} steps."

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, command: str) -> MissionResult:
        """Full pipeline: plan → execute → summarise."""
        logger.info(f"Command received: {command!r}")
        plan = await self.plan(command)

        if plan.direct_answer:
            return MissionResult(
                command=command,
                summary=plan.direct_answer,
                success=True,
            )

        if plan.clarification_needed:
            return MissionResult(
                command=command,
                summary=f"Clarification needed: {plan.clarification_needed}",
                success=False,
            )

        return await self.execute_plan(plan, command)

    async def stream_run(self, command: str) -> AsyncIterator[str]:
        """
        Streaming variant — yields SSE-formatted JSON strings so a web
        client can display live step-by-step progress.
        """
        yield _sse("status", {"message": "Planning mission…"})
        plan = await self.plan(command)

        if plan.direct_answer:
            yield _sse("done", {"summary": plan.direct_answer, "success": True})
            return

        try:
            groups = self._planner.build_execution_groups(plan)
        except PlanValidationError as exc:
            yield _sse("done", {"summary": f"Invalid plan: {exc}", "success": False})
            return

        mission = MissionResult(command=command)
        context: Dict[str, Any] = {}
        aborted = False

        for group in groups:
            if aborted:
                break
            for i in group:
                yield _sse("step_start", {"index": i, "tool": plan.steps[i].tool})
            results = await self._execute_group(plan, group, context)
            for result in results:
                mission.steps.append(result)
                yield _sse("step_done", result.model_dump())
                if result.status == StepStatus.FAILED:
                    aborted = True

        mission.steps.sort(key=lambda s: s.step_index)
        mission.success = (
            len(mission.steps) == len(plan.steps)
            and all(s.status == StepStatus.SUCCESS for s in mission.steps)
        )
        mission.summary = await self._summarise(command, mission)
        yield _sse("done", {"summary": mission.summary, "success": mission.success})

    async def close(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
