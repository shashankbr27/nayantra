"""
Agent loop end to end with a scripted LLM:
agent → MCP server (ASGI) → Nayantra Core (ASGI) → simulator.
"""

from __future__ import annotations

import json

import httpx
import pytest_asyncio

from nayantra.agent import agent as agent_mod
from nayantra.agent.agent import (
    Call,
    RMFAgent,
    Turn,
    _Provider,
    to_anthropic_tool,
    to_gemini_tool,
    with_history,
)
from nayantra.core.runtime import NayantraCore
from nayantra.core.server import create_app
from nayantra.mcp import server as mcp_server
from nayantra.mcp.core_client import CoreClient
from nayantra.mcp.tools import get_all_tools


class ScriptedProvider(_Provider):
    """Replays a fixed list of turns and records what the agent fed back."""

    def __init__(self, turns: list[Turn]) -> None:
        self.turns = list(turns)
        self.results: list[list[tuple[Call, object, bool]]] = []
        self.tools: list[dict] = []

    def start(self, command, tools):
        self.command = command
        self.tools = tools

    async def next_turn(self):
        return self.turns.pop(0)

    def add_results(self, results):
        self.results.append(results)

    async def summarise(self, prompt):
        return "summary"


@pytest_asyncio.fixture
async def stack():
    core = NayantraCore(db_path=":memory:", scenario="warehouse_demo")
    await core.start()
    core_app = create_app(core, start_core=False)
    mcp_server.core_client = CoreClient(
        base_url="http://core", transport=httpx.ASGITransport(app=core_app)
    )
    yield core
    await mcp_server.core_client.close()
    mcp_server.core_client = None
    await core.stop()


def make_agent(provider: ScriptedProvider) -> RMFAgent:
    agent = RMFAgent(mcp_url="http://mcp", provider=provider)
    agent._http = httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_server.app), timeout=30)
    return agent


async def test_agent_creates_structured_tasks_and_reads_results(stack):
    provider = ScriptedProvider(
        [
            Turn(
                "",
                [
                    Call(
                        "c1",
                        "create_task",
                        {
                            "task_type": "delivery",
                            "pickup": "receiving",
                            "dropoff": "storage",
                            "count": 3,
                        },
                    )
                ],
                "tool_use",
            ),
            Turn("Created three deliveries.", [], "end"),
        ]
    )
    agent = make_agent(provider)
    mission = await agent.run("Deliver three packages from receiving to storage")
    await agent.close()
    assert mission.success and mission.summary == "Created three deliveries."
    assert provider.tools and all("name" in t for t in provider.tools)
    ((call, payload, is_error),) = provider.results[0]
    assert not is_error and len(payload["tasks"]) == 3
    tasks = list(stack.tasks.tasks.values())
    assert len(tasks) == 3
    assert all(t.trace.source == "llm" and t.trace.mission_id == mission.mission_id for t in tasks)
    assert all(t.trace.command == "Deliver three packages from receiving to storage" for t in tasks)
    assert len({t.assigned_robot for t in tasks}) == 3  # three different robots allocated


async def test_tool_errors_are_fed_back_for_recovery(stack):
    provider = ScriptedProvider(
        [
            Turn(
                "",
                [
                    Call(
                        "c1",
                        "navigate_robot",
                        {"robot": "UGV-01", "destination": "Recieving Bay 2"},
                    )
                ],
                "tool_use",
            ),
            Turn(
                "",
                [
                    Call(
                        "c2",
                        "navigate_robot",
                        {"robot": "UGV-01", "destination": "Receiving Bay 2"},
                    )
                ],
                "tool_use",
            ),
            Turn("UGV-01 is on its way.", [], "end"),
        ]
    )
    agent = make_agent(provider)
    mission = await agent.run("send ugv 1 to recieving bay 2")
    await agent.close()
    first = provider.results[0][0]
    assert first[2] is True and "Did you mean" in first[1]["error"]
    assert provider.results[1][0][2] is False
    assert mission.success and [s.status for s in mission.steps] == ["failed", "success"]


async def test_invalid_tool_parameters_are_reported_not_raised(stack):
    provider = ScriptedProvider(
        [
            Turn("", [Call("c1", "create_task", {"task_type": "teleport"})], "tool_use"),
            Turn("I can't do that.", [], "end"),
        ]
    )
    agent = make_agent(provider)
    await agent.run("teleport")
    await agent.close()
    assert "Invalid parameters" in provider.results[0][0][1]["error"]


async def test_dangerous_request_waits_for_operator(stack):
    provider = ScriptedProvider(
        [
            Turn("", [Call("c1", "stop_all_robots", {})], "tool_use"),
            Turn("Waiting for your confirmation.", [], "end"),
        ]
    )
    agent = make_agent(provider)
    await agent.run("stop all robots")
    await agent.close()
    assert provider.results[0][0][1]["confirmation_required"] is True
    pending = stack.confirmations.list()
    assert len(pending) == 1 and pending[0].requested_by == "mcp"


async def test_stream_emits_step_events(stack):
    provider = ScriptedProvider(
        [Turn("", [Call("c1", "list_fleets", {})], "tool_use"), Turn("Four fleets.", [], "end")]
    )
    agent = make_agent(provider)
    events = [chunk async for chunk in agent.stream_run("which fleets exist?")]
    await agent.close()
    kinds = [e.split("\n")[0].removeprefix("event: ") for e in events]
    assert kinds == ["status", "step_start", "step_done", "done"]
    done = json.loads(events[-1].split("data: ", 1)[1])
    assert done["success"] is True and done["summary"] == "Four fleets."


async def test_chat_history_reaches_the_model_but_not_the_mission_record(stack):
    provider = ScriptedProvider([Turn("Sent the other one too.", [], "end")])
    agent = make_agent(provider)
    history = [
        {"role": "user", "text": "Send UGV-1 to the loading zone"},
        {"role": "assistant", "text": "UGV-1 is on its way."},
    ]
    events = [chunk async for chunk in agent.stream_run("now the other one too", history)]
    await agent.close()
    assert "Send UGV-1 to the loading zone" in provider.command
    assert "UGV-1 is on its way." in provider.command
    assert provider.command.endswith("Current request: now the other one too")
    assert "Sent the other one too." in events[-1]


def test_with_history_is_a_no_op_without_history():
    assert with_history("go", None) == "go"
    assert with_history("go", []) == "go"
    assert with_history("go", [{"role": "user", "text": "   "}]) == "go"


def test_with_history_keeps_the_newest_turns_and_bounds_the_size():
    history = [{"role": "user" if i % 2 == 0 else "assistant", "text": f"message {i} " + "x" * 700} for i in range(20)]
    prompt = with_history("what now?", history)
    assert "message 19" in prompt and "message 0 " not in prompt  # oldest dropped
    assert len(prompt) < 3500  # 3000 chars of history plus the framing
    assert prompt.index("message 18") < prompt.index("message 19")  # still oldest-first
    assert prompt.endswith("Current request: what now?")
    assert "Operator: message 18" in prompt and "Assistant: message 19" in prompt


def test_tool_schema_conversion_keeps_required_and_flattens_for_gemini():
    tool = next(t for t in get_all_tools() if t["name"] == "create_task")
    a = to_anthropic_tool(tool)
    assert a["input_schema"]["required"] == ["task_type"]
    g = to_gemini_tool(tool)
    props = g["parameters"]["properties"]
    assert "anyOf" not in json.dumps(props) and props["destination"]["nullable"] is True
    assert agent_mod.SYSTEM_PROMPT and "create_task" in agent_mod.SYSTEM_PROMPT


def test_every_tool_is_accepted_by_geminis_schema_model():
    # One tool Gemini rejects (create_fleet's `gt=0` emitted exclusiveMinimum) fails EVERY
    # command, because the whole tool list is sent with each request.
    from google.genai import types

    for tool in get_all_tools():
        types.FunctionDeclaration.model_validate(to_gemini_tool(tool))


def test_gemini_conversion_keeps_exclusive_bounds_in_the_description():
    tool = next(t for t in get_all_tools() if t["name"] == "create_fleet")
    speed = to_gemini_tool(tool)["parameters"]["properties"]["max_speed"]
    assert "exclusiveMinimum" not in speed
    assert "greater than 0" in speed["description"] and speed["maximum"] == 6


def _gemini_provider(failures):
    """A _GeminiProvider whose client raises the given errors, then answers "ok"."""
    from types import SimpleNamespace

    calls = []

    async def generate_content(**kwargs):
        calls.append(kwargs)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]
        return "ok"

    prov = object.__new__(agent_mod._GeminiProvider)
    prov.RETRY_DELAYS = (0, 0, 0)
    prov.client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    return prov, calls


def _gemini_error(cls, code):
    return cls(code, {"error": {"code": code, "message": "x", "status": "X"}})


async def test_gemini_retries_a_503_overload_then_succeeds():
    from google.genai import errors

    overload = _gemini_error(errors.ServerError, 503)
    prov, calls = _gemini_provider([overload, overload])
    assert await prov._generate(model="m") == "ok"
    assert len(calls) == 3


async def test_gemini_retries_a_429_rate_limit():
    from google.genai import errors

    prov, calls = _gemini_provider([_gemini_error(errors.ClientError, 429)])
    assert await prov._generate(model="m") == "ok" and len(calls) == 2


async def test_gemini_does_not_retry_a_bad_request():
    import pytest
    from google.genai import errors

    prov, calls = _gemini_provider([_gemini_error(errors.ClientError, 400)])
    with pytest.raises(errors.ClientError):
        await prov._generate(model="m")
    assert len(calls) == 1


async def test_gemini_gives_up_after_the_last_retry():
    import pytest
    from google.genai import errors

    overload = _gemini_error(errors.ServerError, 503)
    prov, calls = _gemini_provider([overload] * 10)
    with pytest.raises(errors.ServerError):
        await prov._generate(model="m")
    assert len(calls) == 4  # first try + three retries
