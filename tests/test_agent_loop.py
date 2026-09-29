"""
Agent loop end to end with a scripted LLM:
agent → MCP server (ASGI) → Nayantra Core (ASGI) → simulator.
"""

from __future__ import annotations

import json

import httpx
import pytest_asyncio

from nayantra.agent import agent as agent_mod
from nayantra.agent.agent import Call, RMFAgent, Turn, _Provider, to_anthropic_tool, to_gemini_tool
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


def test_tool_schema_conversion_keeps_required_and_flattens_for_gemini():
    tool = next(t for t in get_all_tools() if t["name"] == "create_task")
    a = to_anthropic_tool(tool)
    assert a["input_schema"]["required"] == ["task_type"]
    g = to_gemini_tool(tool)
    props = g["parameters"]["properties"]
    assert "anyOf" not in json.dumps(props) and props["destination"]["nullable"] is True
    assert agent_mod.SYSTEM_PROMPT and "create_task" in agent_mod.SYSTEM_PROMPT
