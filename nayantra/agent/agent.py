"""
nayantra/agent/agent.py

Core AI Agent:
  - Supports Anthropic Claude, OpenAI GPT-4o, and Google Gemini as backends
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
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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

# Max think→act→observe iterations in the agentic loop (safety cap on long missions).
_MAX_AGENT_ITERS = 16

# ---------------------------------------------------------------------------
# System prompt injected for every planning call
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are Nayantra, an autonomous robot fleet operations planner for an
Open-RMF managed building. Given a natural-language command you select and call the
available MCP tools to carry it out. Think like a real fleet operator: emit a COMPLETE,
ordered sequence of tool calls for the whole mission — not just a single lookup.

Available tools (these are the ONLY tools that exist — use these EXACT names; do not
invent tools or guess endpoints):
- Fleet & robots:   list_robots, get_robot_status, get_fleet_log,
                    decommission_robot, recommission_robot
- Movement & tasks: move_robot (navigate a robot to a named waypoint), dispatch_task
                    (delivery / patrol / loop / navigate_to_waypoint), get_task_state,
                    list_tasks, get_task_log, cancel_task, resume_task, interrupt_task,
                    stop_robot
- Doors:            list_doors, get_door_state, control_door (mode 2 = open, 0 = closed)
- Lifts:            list_lifts, get_lift_state, request_lift
- Alerts & safety:  list_alerts, get_alert, respond_to_alert, reset_fire_alarm,
                    get_fire_alarm_state
- Building/infra:   get_building_map, list_dispensers, list_ingestors

General method for ANY command:
1. VERIFY: call list_robots (and get_robot_status when a specific robot matters) so you
   act on a robot that actually exists and is free.
2. CHECK INFRASTRUCTURE on the route: check relevant doors with get_door_state and open
   them with control_door (mode=2) if they may be closed; for floor changes use
   get_lift_state / request_lift.
3. ACT: move_robot to a named waypoint, or dispatch_task for a delivery/patrol/loop.
4. CONFIRM: get_task_state to verify the task was accepted.

PICKUP-AND-DELIVERY ("pick up X from A and drop it off at B") — emit this full
choreography, in order:
1. list_robots                         — find an available robot.
2. get_robot_status                    — confirm it is idle with battery.
3. get_door_state (door at pickup A)   — then control_door (mode=2) to open if closed.
4. move_robot to pickup location A.
5. dispatch_task (category "delivery") — the transport job: pickup at A, drop-off at B.
6. get_door_state (door at drop-off B) — then control_door (mode=2) to open if closed.
7. move_robot to drop-off location B.
8. get_task_state                      — confirm the delivery task.

WORKED EXAMPLES — study these and emit the SAME shape of plan. A multi-leg command
("do A, then B, then C") MUST produce all the steps for every leg, not a couple.

Example 1 — multi-leg: pick up, deliver, then relocate
  Command: "pick up the packages from main gate and drop it off to board room then go to canteen"
  Plan (12 tool calls):
     1. list_robots                                      GET  /fleets
     2. get_robot_status(fleet_name, robot_name)         GET  /fleets/{fleet_name}/robots/{robot_name}
     3. get_door_state(door_name="main_gate")            GET  /doors/main_gate/state
     4. control_door(door_name="main_gate", mode=2)      POST /doors/main_gate/request
     5. move_robot(waypoint="main_gate")                 POST /tasks/dispatch_task
     6. dispatch_task(category="delivery",
          description={"pickup":"main_gate","dropoff":"board_room"})   POST /tasks/dispatch_task
     7. get_door_state(door_name="board_room")           GET  /doors/board_room/state
     8. control_door(door_name="board_room", mode=2)     POST /doors/board_room/request
     9. move_robot(waypoint="board_room")                POST /tasks/dispatch_task
    10. get_task_state(task_id)                           GET  /tasks/{task_id}/state
    11. move_robot(waypoint="canteen")                   POST /tasks/dispatch_task
    12. get_task_state(task_id)                           GET  /tasks/{task_id}/state

Example 2 — simple A-to-B delivery
  Command: "deliver a part from the workshop to zone_a"
  Plan (8 tool calls):
     1. list_robots                                      GET  /fleets
     2. get_robot_status(fleet_name, robot_name)         GET  /fleets/{fleet_name}/robots/{robot_name}
     3. get_door_state(door_name="workshop")             GET  /doors/workshop/state
     4. control_door(door_name="workshop", mode=2)       POST /doors/workshop/request
     5. move_robot(waypoint="workshop")                  POST /tasks/dispatch_task
     6. dispatch_task(category="delivery",
          description={"pickup":"workshop","dropoff":"zone_a"})        POST /tasks/dispatch_task
     7. move_robot(waypoint="zone_a")                    POST /tasks/dispatch_task
     8. get_task_state(task_id)                           GET  /tasks/{task_id}/state

Example 3 — status query, no movement
  Command: "which robots are available and what are they doing?"
  Plan (3 tool calls):
     1. list_robots                                      GET  /fleets
     2. get_robot_status(fleet_name, robot_name)         GET  /fleets/{fleet_name}/robots/{robot_name}
     3. list_tasks                                       GET  /tasks

Example 4 — infrastructure only
  Command: "call the lift to floor 2 and open the lobby door"
  Plan (4 tool calls):
     1. get_lift_state(lift_name="lift_1")               GET  /lifts/lift_1/state
     2. request_lift(lift_name="lift_1", destination_floor="L2")       POST /lifts/lift_1/request
     3. get_door_state(door_name="lobby")                GET  /doors/lobby/state
     4. control_door(door_name="lobby", mode=2)          POST /doors/lobby/request

Rules:
- Use ONLY the tool names listed above. Never invent a tool (no custom_* tools).
- Never invent robot names. If unsure, list_robots first; the system fills in the robot.
- Use named waypoints for move_robot (e.g. main_door, store_room, charging_dock, zone_a).
- Always check the doors/lifts the route plausibly passes through — operators verify
  infrastructure before and during a move.
- Only ask ONE clarifying question (and emit no tool calls) if the command is truly
  impossible to act on.
- Keep the plan complete but minimal: every step must be necessary for the mission.
"""


# ---------------------------------------------------------------------------
# Tool-schema converters (shared between Claude, OpenAI, and Gemini code paths)
# ---------------------------------------------------------------------------


def to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Convert an MCP tool schema to Anthropic tool-use format."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": {
            "type": "object",
            "properties": tool.get("parameters", {}),
        },
    }


def to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
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


def to_gemini_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """
    Convert an MCP tool schema to Gemini function-declaration format.

    Gemini rejects empty `parameters` schemas, so for parameter-less tools
    we omit the field entirely.
    """
    decl: dict[str, Any] = {
        "name": tool["name"],
        "description": tool.get("description", ""),
    }
    params = tool.get("parameters") or {}
    if params:
        decl["parameters"] = {
            "type": "object",
            "properties": params,
        }
    return decl


class RMFAgent:
    """LLM-powered agent that translates natural language into RMF fleet operations."""

    def __init__(self, mcp_url: str | None = None) -> None:
        self.mcp_url = (mcp_url or settings.MCP_SERVER_URL).rstrip("/")
        self._tools_cache: list[dict[str, Any]] = []
        self._tools_fetched_at: float = 0.0
        self._http = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
        self._planner = TaskPlanner()
        self._setup_llm_client()

    def _setup_llm_client(self) -> None:
        """Initialise the appropriate LLM client based on config."""
        provider = settings.LLM_PROVIDER
        if provider == "anthropic":
            import anthropic  # type: ignore

            self._llm_provider = "anthropic"
            self._anthropic = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
            logger.info(f"LLM: Anthropic {settings.ANTHROPIC_MODEL}")
        elif provider == "gemini":
            from google import genai  # type: ignore

            self._llm_provider = "gemini"
            self._gemini = genai.Client(api_key=settings.GEMINI_API_KEY)
            logger.info(f"LLM: Gemini {settings.GEMINI_MODEL}")
        elif provider == "openai":
            from openai import AsyncOpenAI  # type: ignore

            self._llm_provider = "openai"
            self._openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
            logger.info(f"LLM: OpenAI {settings.OPENAI_MODEL}")
        else:
            raise ValueError(
                f"Unknown LLM_PROVIDER: {provider!r}. Must be one of: anthropic, openai, gemini."
            )

    # ------------------------------------------------------------------
    # Tool discovery
    # ------------------------------------------------------------------

    async def _get_tools(self) -> list[dict[str, Any]]:
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

    def _load_fallback_tools(self) -> list[dict[str, Any]]:
        """Load tool definitions from the local fallback JSON."""
        try:
            with Path(settings.FALLBACK_TOOLS_FILE).open() as fh:
                data = json.load(fh)
            logger.info(f"Loaded {len(data)} fallback tools from {settings.FALLBACK_TOOLS_FILE}")
            return data
        except Exception as exc:
            logger.error(f"Could not load fallback tools: {exc}")
            return []

    # ------------------------------------------------------------------
    # Planning
    # ------------------------------------------------------------------

    async def _plan_with_anthropic(self, command: str, tools: list[dict[str, Any]]) -> AgentPlan:
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

        steps: list[ToolCall] = []
        direct_answer: str | None = None

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

    async def _plan_with_gemini(self, command: str, tools: list[dict[str, Any]]) -> AgentPlan:
        """Use Gemini function-calling to create a structured plan."""
        function_declarations = [to_gemini_tool(t) for t in tools]
        gemini_tools = [{"function_declarations": function_declarations}]

        resp = await self._gemini.aio.models.generate_content(
            model=settings.GEMINI_MODEL,
            contents=command,
            config={
                "system_instruction": SYSTEM_PROMPT,
                "tools": gemini_tools,
                "temperature": 0,
            },
        )

        steps: list[ToolCall] = []
        direct_answer: str | None = None

        candidates = getattr(resp, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            for part in getattr(content, "parts", []) or []:
                fc = getattr(part, "function_call", None)
                if fc and getattr(fc, "name", None):
                    steps.append(
                        ToolCall(
                            tool=fc.name,
                            parameters=dict(fc.args) if fc.args else {},
                            reason=f"Gemini selected {fc.name}",
                        )
                    )
                else:
                    text = getattr(part, "text", None)
                    if text and text.strip():
                        direct_answer = text.strip()

        return AgentPlan(steps=steps, direct_answer=direct_answer if not steps else None)

    async def _plan_with_openai(self, command: str, tools: list[dict[str, Any]]) -> AgentPlan:
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
        steps: list[ToolCall] = []
        direct_answer: str | None = None

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
        if self._llm_provider == "gemini":
            return await self._plan_with_gemini(command, tools)
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
    async def _call_mcp(self, tool: str, params: dict[str, Any]) -> Any:
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
        context: dict[str, Any],
    ) -> StepResult:
        """Execute one tool call against the MCP server."""
        params = self._resolve_params(step.parameters, context)
        start = time.monotonic()
        try:
            result = await self._call_mcp(step.tool, params)
            duration = (time.monotonic() - start) * 1000
            # MCP wraps the tool's return as {tool, result, duration_ms, timestamp}.
            # Unwrap to the inner tool payload so the UI formatters and ID
            # extraction see the actual RMF response, not the envelope.
            inner = result.get("result", result) if isinstance(result, dict) else result
            self._extract_ids(inner, context)
            return StepResult(
                step_index=step_index,
                tool=step.tool,
                parameters=params,
                status=StepStatus.SUCCESS,
                result=inner,
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

    def _resolve_params(self, params: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
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

    def _extract_ids(self, result: Any, context: dict[str, Any]) -> None:
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
        indices: list[int],
        context: dict[str, Any],
    ) -> list[StepResult]:
        """Run all steps in one parallel group concurrently."""
        return await asyncio.gather(
            *[
                self._execute_step(
                    self._planner.enrich_step(plan.steps[i], context),
                    i,
                    context,
                )
                for i in indices
            ]
        )

    async def execute_plan(self, plan: AgentPlan, command: str) -> MissionResult:
        """
        Execute a plan using the TaskPlanner's parallel execution groups.

        Steps with no dependencies run concurrently; dependent steps wait
        for their predecessors. Mission aborts on the first failed step.
        """
        mission = MissionResult(command=command)
        context: dict[str, Any] = {}

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
        mission.success = len(mission.steps) == len(plan.steps) and all(
            s.status == StepStatus.SUCCESS for s in mission.steps
        )
        mission.summary = await self._summarise(command, mission)
        return mission

    async def _summarise(self, command: str, mission: MissionResult) -> str:
        """Ask the LLM to produce a human-readable mission summary."""
        results_json = json.dumps([s.model_dump() for s in mission.steps], indent=2)
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
            if self._llm_provider == "gemini":
                resp = await self._gemini.aio.models.generate_content(
                    model=settings.GEMINI_MODEL,
                    contents=prompt,
                    config={"temperature": 0.5, "max_output_tokens": 256},
                )
                text = getattr(resp, "text", None) or ""
                return text.strip() or self._fallback_summary(mission)
            resp = await self._openai.chat.completions.create(
                model=settings.OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.5,
                max_tokens=256,
            )
            return resp.choices[0].message.content.strip()
        except Exception as exc:
            logger.error(f"Summary generation failed: {exc}")
            return self._fallback_summary(mission)

    @staticmethod
    def _fallback_summary(mission: MissionResult) -> str:
        status = "successfully" if mission.success else "with errors"
        return f"Mission completed {status} in {len(mission.steps)} steps."

    # ------------------------------------------------------------------
    # Agentic loop (Gemini) — call → execute → feed results back → repeat
    # ------------------------------------------------------------------

    async def _agentic_gemini(
        self, command: str, mission: MissionResult
    ) -> AsyncIterator[tuple[str, dict]]:
        """
        True agentic loop for Gemini: the model calls a tool (or several), we
        execute against MCP, feed the REAL results back into the conversation,
        and call the model again — repeating until it stops requesting tools.

        This is what lets it compose genuine multi-step missions (verify robot →
        check door → move → dispatch → confirm → next leg …) and react to what
        each tool returns, instead of emitting one batch and stopping.

        Yields ("status"|"step_start"|"step_done"|"done", payload) tuples and
        populates `mission` in place.
        """
        from google.genai import types  # lazy import

        yield ("status", {"message": "Planning mission…"})

        tools = await self._get_tools()
        decls = [to_gemini_tool(t) for t in tools]
        # Plain-dict config — the same form the (previously working) single-shot
        # planner used, to avoid typed-builder version mismatches.
        config = {
            "system_instruction": SYSTEM_PROMPT,
            "tools": [{"function_declarations": decls}],
            "temperature": 0,
        }
        contents: list[Any] = [
            types.Content(role="user", parts=[types.Part(text=command)])
        ]
        context: dict[str, Any] = {}
        final_text: str | None = None

        for _iteration in range(_MAX_AGENT_ITERS):
            resp = await self._gemini.aio.models.generate_content(
                model=settings.GEMINI_MODEL, contents=contents, config=config
            )
            cand = (getattr(resp, "candidates", None) or [None])[0]
            content = getattr(cand, "content", None) if cand else None
            parts = (getattr(content, "parts", None) or []) if content else []

            fcs = [p.function_call for p in parts if getattr(p, "function_call", None)]
            texts = [p.text for p in parts if getattr(p, "text", None)]

            if not fcs:
                # No more tool calls → the model's text is the final answer.
                final_text = " ".join(t.strip() for t in texts if t and t.strip()) or None
                break

            # Record the model's tool-call turn in the conversation.
            contents.append(content)

            response_parts = []
            for fc in fcs:
                idx = len(mission.steps)
                args = dict(fc.args) if getattr(fc, "args", None) else {}
                yield ("step_start", {"index": idx, "tool": fc.name})

                params = self._resolve_params(args, context)
                sr = await self._execute_step(
                    ToolCall(tool=fc.name, parameters=params), idx, context
                )
                mission.steps.append(sr)
                yield ("step_done", sr.model_dump())

                payload = (
                    sr.result
                    if sr.status == StepStatus.SUCCESS
                    else {"error": sr.error or "failed"}
                )
                response_parts.append(
                    types.Part.from_function_response(
                        name=fc.name, response={"result": payload}
                    )
                )

            # Feed all tool results back for the next iteration.
            contents.append(types.Content(role="user", parts=response_parts))

        mission.success = bool(mission.steps) and all(
            s.status == StepStatus.SUCCESS for s in mission.steps
        )
        mission.summary = final_text or await self._summarise(command, mission)
        yield ("done", {"summary": mission.summary, "success": mission.success})

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self, command: str) -> MissionResult:
        """Full pipeline. Gemini uses the agentic loop; others use single-shot."""
        logger.info(f"Command received: {command!r}")

        if self._llm_provider == "gemini":
            mission = MissionResult(command=command)
            async for kind, data in self._agentic_gemini(command, mission):
                if kind == "done":
                    mission.summary = data.get("summary", mission.summary)
                    mission.success = data.get("success", False)
            if not mission.summary:
                mission.summary = self._fallback_summary(mission)
            return mission

        # Single-shot path (anthropic / openai)
        plan = await self.plan(command)
        if plan.direct_answer:
            return MissionResult(command=command, summary=plan.direct_answer, success=True)
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
        client can display live step-by-step progress. Any failure is emitted
        as a 'done' event with the error message (never crashes the stream).
        """
        # Gemini: stream the agentic loop's events directly.
        if self._llm_provider == "gemini":
            mission = MissionResult(command=command)
            try:
                async for kind, data in self._agentic_gemini(command, mission):
                    yield _sse(kind, data)
            except Exception as exc:
                logger.exception("stream_run (gemini agentic) failed")
                yield _sse("done", {"summary": f"Agent error: {exc}", "success": False})
            return

        try:
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
            context: dict[str, Any] = {}
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
            mission.success = len(mission.steps) == len(plan.steps) and all(
                s.status == StepStatus.SUCCESS for s in mission.steps
            )
            mission.summary = await self._summarise(command, mission)
            yield _sse("done", {"summary": mission.summary, "success": mission.success})
        except Exception as exc:
            logger.exception("stream_run failed")
            yield _sse("done", {"summary": f"Agent error: {exc}", "success": False})

    async def close(self) -> None:
        await self._http.aclose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
