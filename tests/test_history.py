"""
tests/test_history.py

Tests for the SQLite mission history persistence layer.
Uses a tmp_path fixture so each test gets a clean in-memory-equivalent DB.
"""
from __future__ import annotations

import pytest
from pathlib import Path

from nayantra.agent.history import MissionStore
from nayantra.agent.models import MissionResult, StepResult, StepStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_mission(
    command: str = "test command",
    success: bool = True,
    n_steps: int = 2,
) -> MissionResult:
    steps = [
        StepResult(
            step_index=i,
            tool=f"tool_{i}",
            parameters={"p": i},
            status=StepStatus.SUCCESS if success else StepStatus.FAILED,
            result={"data": f"result_{i}"},
            duration_ms=float(100 + i * 50),
        )
        for i in range(n_steps)
    ]
    return MissionResult(
        command=command,
        summary=f"{'Done' if success else 'Failed'}: {command}",
        success=success,
        steps=steps,
    )


@pytest.fixture
def store(tmp_path: Path) -> MissionStore:
    db = tmp_path / "test_missions.db"
    return MissionStore(db_path=db)


# ---------------------------------------------------------------------------
# Save & retrieve
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_save_and_retrieve_mission(store):
    m = make_mission("send robot to dock")
    await store.save(m)

    record = await store.get(m.mission_id)
    assert record is not None
    assert record["command"] == "send robot to dock"
    assert record["success"] == 1


@pytest.mark.asyncio
async def test_retrieve_includes_steps(store):
    m = make_mission("test", n_steps=3)
    await store.save(m)

    record = await store.get(m.mission_id)
    assert len(record["steps"]) == 3
    assert record["steps"][0]["tool"] == "tool_0"
    assert record["steps"][2]["duration_ms"] == 200.0


@pytest.mark.asyncio
async def test_retrieve_nonexistent_returns_none(store):
    result = await store.get("nonexistent-id-abc")
    assert result is None


@pytest.mark.asyncio
async def test_save_failed_mission(store):
    m = make_mission("bad command", success=False)
    await store.save(m)

    record = await store.get(m.mission_id)
    assert record["success"] == 0


@pytest.mark.asyncio
async def test_save_mission_with_no_steps(store):
    m = MissionResult(
        command="direct answer",
        summary="3 robots available.",
        success=True,
        steps=[],
    )
    await store.save(m)
    record = await store.get(m.mission_id)
    assert record is not None
    assert len(record["steps"]) == 0


# ---------------------------------------------------------------------------
# Recent listing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recent_returns_all_missions(store):
    for i in range(5):
        await store.save(make_mission(f"command {i}"))
    results = await store.recent(limit=10)
    assert len(results) == 5


@pytest.mark.asyncio
async def test_recent_respects_limit(store):
    for i in range(10):
        await store.save(make_mission(f"command {i}"))
    results = await store.recent(limit=3)
    assert len(results) == 3


@pytest.mark.asyncio
async def test_recent_ordered_newest_first(store):
    for i in range(3):
        m = make_mission(f"mission {i}")
        await store.save(m)
    results = await store.recent()
    commands = [r["command"] for r in results]
    assert commands.index("mission 2") < commands.index("mission 0")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_search_finds_matching_command(store):
    await store.save(make_mission("send turtlebot to charging dock"))
    await store.save(make_mission("check all alerts"))
    results = await store.search("turtlebot")
    assert len(results) == 1
    assert "turtlebot" in results[0]["command"]


@pytest.mark.asyncio
async def test_search_returns_empty_for_no_match(store):
    await store.save(make_mission("send robot somewhere"))
    results = await store.search("xyz_nonexistent")
    assert results == []


@pytest.mark.asyncio
async def test_search_case_insensitive_partial(store):
    await store.save(make_mission("Navigate to Zone A"))
    results = await store.search("zone")
    assert len(results) == 1


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_total_count(store):
    for _ in range(4):
        await store.save(make_mission(success=True))
    for _ in range(2):
        await store.save(make_mission(success=False))
    stats = await store.stats()
    assert stats["total_missions"] == 6
    assert stats["successful"] == 4
    assert stats["failed"] == 2


@pytest.mark.asyncio
async def test_stats_success_rate(store):
    for _ in range(3):
        await store.save(make_mission(success=True))
    await store.save(make_mission(success=False))
    stats = await store.stats()
    assert stats["success_rate_pct"] == 75.0


@pytest.mark.asyncio
async def test_stats_empty_store(store):
    stats = await store.stats()
    assert stats["total_missions"] == 0
    assert stats["success_rate_pct"] == 0.0


@pytest.mark.asyncio
async def test_stats_top_tools(store):
    for _ in range(3):
        await store.save(make_mission(n_steps=2))  # tool_0 and tool_1
    stats = await store.stats()
    tools = {t["tool"] for t in stats["top_tools"]}
    assert "tool_0" in tools
    assert "tool_1" in tools


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_removes_mission(store):
    m = make_mission("to delete")
    await store.save(m)
    deleted = await store.delete(m.mission_id)
    assert deleted is True
    assert await store.get(m.mission_id) is None


@pytest.mark.asyncio
async def test_delete_returns_false_for_nonexistent(store):
    result = await store.delete("no-such-id")
    assert result is False


@pytest.mark.asyncio
async def test_clear_removes_all_records(store):
    for _ in range(5):
        await store.save(make_mission())
    await store.clear()
    results = await store.recent()
    assert results == []


# ---------------------------------------------------------------------------
# Idempotency — re-saving same mission replaces record
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resave_replaces_mission(store):
    m = make_mission("original")
    await store.save(m)

    # Mutate and re-save same ID
    m.summary = "updated summary"
    await store.save(m)

    record = await store.get(m.mission_id)
    assert record["summary"] == "updated summary"
    results = await store.recent()
    assert len(results) == 1   # not duplicated
