"""
nayantra/agent/agent.py

The natural-language operator interface.

The LLM turns what the operator says into Nayantra operations by calling the
MCP tools (nayantra/mcp/tools.py) in a think → act → observe loop: every tool
result, including errors and "needs operator confirmation", is fed back so the
model reacts to live state instead of guessing. The LLM never controls robot
motion. It creates structured tasks and asks questions; the core allocates
robots, plans routes, coordinates traffic and enforces safety.

Providers: Anthropic Claude, OpenAI, Google Gemini (LLM_PROVIDER), all through
the same loop. Each tool call carries the mission id and the operator's
command, so every task the agent creates is traceable back to the sentence
that caused it.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from nayantra.agent.models import MissionResult, StepResult, StepStatus
from nayantra.config import settings

logger = logging.getLogger("nayantra.agent")

_MAX_AGENT_ITERS = 12

SYSTEM_PROMPT = """You are the operator assistant of Nayantra, a control plane for heterogeneous robot fleets \
(UGVs, UAVs, quadrupeds, humanoids). You turn the operator's request into Nayantra operations using the \
provided tools, which are the only actions that exist.

How Nayantra works:
- You describe the work; Nayantra decides who does it. For jobs ("deliver…", "patrol…", "inspect…", \
"send a robot to…"), call create_task and let Nayantra choose the robot from capability, distance, battery, \
workload and traffic. Only set `robot` (or use navigate_robot) when the operator named a specific robot.
- For N identical jobs ("deliver three packages"), call create_task once with count=N.
- Places are waypoint names/ids or area names such as "receiving", "storage", "charging station". \
If you are not sure a place exists, call get_map first. Never invent place names; if a tool says a place \
is unknown, use its suggestions or ask.
- Answer questions about the current situation ("which robots are charging?", "why is UGV-03 waiting?", \
"what route is robot 3 taking?") from live tools such as list_robots, get_robot_state, get_task_status and \
get_traffic_state rather than from assumptions.
- There is no motion or velocity tool. Nayantra plans routes, reserves lanes and enforces safety limits.
- Stopping all robots, emergency-stopping a fleet, removing a robot, and destinations inside restricted \
areas come back as confirmation_required. Tell the operator it is waiting for their confirmation in the \
dashboard; do not retry or work around it.
- If a tool returns ok=false, read the error and either correct the request or explain the problem.

Reply briefly in plain language: what you did (task ids, which robot Nayantra picked and why) or the facts \
the operator asked for."""


# ---------------------------------------------------------------------------
# Tool schema conversion (MCP → provider)
# ---------------------------------------------------------------------------


def _json_schema(tool: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": tool.get("parameters", {}) or {}}
    if tool.get("required"):
        schema["required"] = list(tool["required"])
    return schema


def to_anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """MCP tool schema → Anthropic tool-use format."""
    return {
        "name": tool["name"],
        "description": tool.get("description", ""),
        "input_schema": _json_schema(tool),
    }


def to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """MCP tool schema → OpenAI function-calling format."""
    return {
        "type": "function",
        "function": {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "parameters": _json_schema(tool),
        },
    }


def _flatten_for_gemini(prop: dict[str, Any]) -> dict[str, Any]:
    """Gemini's schema subset: no `anyOf [X, null]`, string-only enums, no defaults."""
    p = copy.deepcopy(prop)
    any_of = p.pop("anyOf", None)
    if any_of:
        non_null = [s for s in any_of if s.get("type") != "null"]
        base = non_null[0] if non_null else {"type": "string"}
        p = {**base, **p, "nullable": True}
    p.pop("default", None)
    p.pop("title", None)
    if "enum" in p and any(not isinstance(v, str) for v in p["enum"]):
        p["description"] = (
            f"{p.get('description', '')} (one of {', '.join(map(str, p.pop('enum')))})".strip()
        )
    if p.get("type") == "array" and isinstance(p.get("items"), dict):
        p["items"] = _flatten_for_gemini(p["items"])
    return p


def to_gemini_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """MCP tool schema → Gemini function declaration (omit empty parameter schemas)."""
    decl: dict[str, Any] = {"name": tool["name"], "description": tool.get("description", "")}
    params = tool.get("parameters") or {}
    if params:
        decl["parameters"] = {
            "type": "object",
            "properties": {k: _flatten_for_gemini(v) for k, v in params.items()},
            **({"required": list(tool["required"])} if tool.get("required") else {}),
        }
    return decl


# ---------------------------------------------------------------------------
# Provider adapters — one conversation, three wire formats
# ---------------------------------------------------------------------------


@dataclass
class Call:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class Turn:
    text: str
    calls: list[Call]
    stop: str  # "tool_use" | "end" | "max_tokens" | "refusal"


class _Provider(ABC):
    @abstractmethod
    def start(self, command: str, tools: list[dict[str, Any]]) -> None: ...

    @abstractmethod
    async def next_turn(self) -> Turn: ...

    @abstractmethod
    def add_results(self, results: list[tuple[Call, Any, bool]]) -> None:
        """(call, payload, is_error) for every call of the last turn, in one go."""

    @abstractmethod
    async def summarise(self, prompt: str) -> str: ...


class _AnthropicProvider(_Provider):
    def __init__(self) -> None:
        import anthropic  # lazy: heavy import

        self.client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY or None)
        self.model = settings.ANTHROPIC_MODEL

    def start(self, command: str, tools: list[dict[str, Any]]) -> None:
        self.tools = [to_anthropic_tool(t) for t in tools]
        self.messages: list[dict[str, Any]] = [{"role": "user", "content": command}]

    async def next_turn(self) -> Turn:
        resp = await self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=self.tools,
            messages=self.messages,
            cache_control={"type": "ephemeral"},
        )
        # Keep every block (tool_use ids, any thinking) for the next request.
        self.messages.append({"role": "assistant", "content": resp.content})
        text = " ".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [
            Call(b.id, b.name, dict(b.input or {})) for b in resp.content if b.type == "tool_use"
        ]
        stop = {"tool_use": "tool_use", "max_tokens": "max_tokens", "refusal": "refusal"}.get(
            resp.stop_reason or "", "end"
        )
        if stop == "tool_use" and not calls:
            stop = "end"
        return Turn(text, calls, stop)

    def add_results(self, results: list[tuple[Call, Any, bool]]) -> None:
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": c.id,
                        "content": json.dumps(payload, default=str),
                        "is_error": err,
                    }
                    for c, payload, err in results
                ],
            }
        )

    async def summarise(self, prompt: str) -> str:
        resp = await self.client.messages.create(
            model=self.model, max_tokens=512, messages=[{"role": "user", "content": prompt}]
        )
        return " ".join(b.text for b in resp.content if b.type == "text").strip()


class _OpenAIProvider(_Provider):
    def __init__(self) -> None:
        from openai import AsyncOpenAI  # lazy

        self.client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY or None)
        self.model = settings.OPENAI_MODEL

    def start(self, command: str, tools: list[dict[str, Any]]) -> None:
        self.tools = [to_openai_tool(t) for t in tools]
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": command},
        ]

    async def next_turn(self) -> Turn:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=self.tools,
            tool_choice="auto",
            temperature=0,
        )
        choice = resp.choices[0]
        msg = choice.message
        entry: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        calls = []
        if msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {"_invalid_json": tc.function.arguments}
                calls.append(Call(tc.id, tc.function.name, args))
        self.messages.append(entry)
        stop = (
            "tool_use" if calls else ("max_tokens" if choice.finish_reason == "length" else "end")
        )
        return Turn(msg.content or "", calls, stop)

    def add_results(self, results: list[tuple[Call, Any, bool]]) -> None:
        for c, payload, _ in results:
            self.messages.append(
                {"role": "tool", "tool_call_id": c.id, "content": json.dumps(payload, default=str)}
            )

    async def summarise(self, prompt: str) -> str:
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=300,
        )
        return (resp.choices[0].message.content or "").strip()


class _GeminiProvider(_Provider):
    def __init__(self) -> None:
        from google import genai  # lazy

        self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
        self.model = settings.GEMINI_MODEL

    def start(self, command: str, tools: list[dict[str, Any]]) -> None:
        from google.genai import types

        self.types = types
        self.config = {
            "system_instruction": SYSTEM_PROMPT,
            "tools": [{"function_declarations": [to_gemini_tool(t) for t in tools]}],
            "temperature": 0,
        }
        self.contents: list[Any] = [types.Content(role="user", parts=[types.Part(text=command)])]

    async def next_turn(self) -> Turn:
        resp = await self.client.aio.models.generate_content(
            model=self.model, contents=self.contents, config=self.config
        )
        cand = (getattr(resp, "candidates", None) or [None])[0]
        content = getattr(cand, "content", None) if cand else None
        parts = (getattr(content, "parts", None) or []) if content else []
        calls = [
            Call(f"g{i}", p.function_call.name, dict(p.function_call.args or {}))
            for i, p in enumerate(parts)
            if getattr(p, "function_call", None) and p.function_call.name
        ]
        text = " ".join(p.text.strip() for p in parts if getattr(p, "text", None)).strip()
        if calls and content is not None:
            self.contents.append(content)
        finish = str(getattr(cand, "finish_reason", "") or "")
        stop = "tool_use" if calls else ("max_tokens" if "MAX_TOKENS" in finish else "end")
        return Turn(text, calls, stop)

    def add_results(self, results: list[tuple[Call, Any, bool]]) -> None:
        t = self.types
        parts = [
            t.Part.from_function_response(
                name=c.name, response={"result": payload} if not err else {"error": payload}
            )
            for c, payload, err in results
        ]
        self.contents.append(t.Content(role="user", parts=parts))

    async def summarise(self, prompt: str) -> str:
        resp = await self.client.aio.models.generate_content(
            model=self.model, contents=prompt, config={"temperature": 0.3, "max_output_tokens": 300}
        )
        return (getattr(resp, "text", None) or "").strip()


_PROVIDERS = {"anthropic": _AnthropicProvider, "openai": _OpenAIProvider, "gemini": _GeminiProvider}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class RMFAgent:
    """LLM operator assistant that acts only through Nayantra's MCP tools."""

    def __init__(self, mcp_url: str | None = None, provider: _Provider | None = None) -> None:
        self.mcp_url = (mcp_url or settings.MCP_SERVER_URL).rstrip("/")
        self._tools_cache: list[dict[str, Any]] = []
        self._tools_fetched_at = 0.0
        self._http = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
        if provider is not None:
            self._provider_factory = lambda: provider
        else:
            name = settings.LLM_PROVIDER
            if name not in _PROVIDERS:
                raise ValueError(
                    f"Unknown LLM_PROVIDER: {name!r}. Must be one of: anthropic, openai, gemini."
                )
            cls = _PROVIDERS[name]
            cls()  # fail fast on a missing SDK
            self._provider_factory = cls
        self._llm_provider = settings.LLM_PROVIDER
        logger.info(f"LLM: {self._llm_provider}")

    # -- tools ---------------------------------------------------------------
    async def _get_tools(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if self._tools_cache and now - self._tools_fetched_at < 60:
            return self._tools_cache
        try:
            resp = await self._http.get(f"{self.mcp_url}/tools")
            resp.raise_for_status()
            self._tools_cache = resp.json()
            self._tools_fetched_at = now
        except httpx.HTTPError as exc:
            logger.warning(f"MCP tool fetch failed: {exc}; using cache/fallback")
            if not self._tools_cache:
                self._tools_cache = self._load_fallback_tools()
        return self._tools_cache

    def _load_fallback_tools(self) -> list[dict[str, Any]]:
        try:
            with Path(settings.FALLBACK_TOOLS_FILE).open(encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Could not load fallback tools: {exc}")
            return []

    async def _call_mcp(
        self, tool: str, params: dict[str, Any], mission: MissionResult
    ) -> tuple[Any, bool, str | None]:
        """Run one tool. Returns (payload for the LLM, ok, error text)."""
        try:
            resp = await self._http.post(
                f"{self.mcp_url}/run",
                json={
                    "tool": tool,
                    "parameters": params,
                    "context": {"mission_id": mission.mission_id, "command": mission.command},
                },
            )
        except httpx.TransportError as exc:
            msg = f"MCP server unreachable at {self.mcp_url}: {type(exc).__name__}"
            return {"ok": False, "error": msg}, False, msg
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text
            msg = str(detail or f"HTTP {resp.status_code}")
            return {"ok": False, "error": msg}, False, msg
        body = resp.json()
        result = body.get("result", body)
        ok = bool(body.get("ok", True))
        err = result.get("error") if isinstance(result, dict) and not ok else None
        return result, ok, err

    # -- loop ------------------------------------------------------------------
    async def _loop(self, command: str, mission: MissionResult) -> AsyncIterator[tuple[str, dict]]:
        yield ("status", {"message": "Thinking…"})
        provider = self._provider_factory()
        provider.start(command, await self._get_tools())
        final_text = ""
        finished = False
        for _ in range(_MAX_AGENT_ITERS):
            turn = await provider.next_turn()
            if turn.stop != "tool_use":
                final_text = turn.text
                finished = turn.stop == "end"
                if turn.stop == "refusal":
                    final_text = final_text or "The model declined this request."
                elif turn.stop == "max_tokens":
                    final_text = (final_text + " (response cut off)").strip()
                break
            results = []
            for call in turn.calls:
                idx = len(mission.steps)
                yield ("step_start", {"index": idx, "tool": call.name})
                t0 = time.monotonic()
                payload, ok, err = await self._call_mcp(call.name, call.args, mission)
                step = StepResult(
                    step_index=idx,
                    tool=call.name,
                    parameters=call.args,
                    status=StepStatus.SUCCESS if ok else StepStatus.FAILED,
                    result=payload,
                    error=err,
                    duration_ms=round((time.monotonic() - t0) * 1000, 2),
                )
                mission.steps.append(step)
                yield ("step_done", step.model_dump())
                results.append((call, payload, not ok))
            provider.add_results(results)
        else:
            final_text = final_text or f"Stopped after {_MAX_AGENT_ITERS} rounds of tool calls."
        mission.success = finished and not (
            mission.steps and all(s.status == StepStatus.FAILED for s in mission.steps)
        )
        mission.summary = final_text or await self._summarise(provider, command, mission)
        yield (
            "done",
            {
                "summary": mission.summary,
                "success": mission.success,
                "mission_id": mission.mission_id,
            },
        )

    async def _summarise(self, provider: _Provider, command: str, mission: MissionResult) -> str:
        results = json.dumps([s.model_dump() for s in mission.steps], default=str)[:6000]
        prompt = f"The operator asked: {command!r}\nTool results: {results}\nSummarise what happened in 2 sentences."
        try:
            return await provider.summarise(prompt) or self._fallback_summary(mission)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"Summary generation failed: {exc}")
            return self._fallback_summary(mission)

    @staticmethod
    def _fallback_summary(mission: MissionResult) -> str:
        status = "successfully" if mission.success else "with errors"
        return f"Mission completed {status} in {len(mission.steps)} steps."

    # -- public ---------------------------------------------------------------
    async def run(self, command: str) -> MissionResult:
        logger.info(f"Command received: {command!r}")
        mission = MissionResult(command=command)
        async for _ in self._loop(command, mission):
            pass
        return mission

    async def stream_run(self, command: str) -> AsyncIterator[str]:
        """SSE stream: status, step_start, step_done, done. Errors end as a 'done' event."""
        mission = MissionResult(command=command)
        try:
            async for kind, data in self._loop(command, mission):
                yield _sse(kind, data)
        except Exception as exc:  # noqa: BLE001
            logger.exception("stream_run failed")
            yield _sse("done", {"summary": f"Agent error: {exc}", "success": False})

    async def close(self) -> None:
        await self._http.aclose()


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
