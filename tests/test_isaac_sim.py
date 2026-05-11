"""
tests/test_isaac_sim.py

Tests for the Isaac Sim bridge and robot spawner.
All tests run in stub mode (ISAAC_SIM_ENABLED=false) — no GPU required.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import patch, AsyncMock

from nayantra.isaac_sim.sim_bridge import IsaacSimBridge
from nayantra.isaac_sim.robot_spawner import RobotConfig, RobotSpawner, RobotState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge():
    b = IsaacSimBridge()
    b._enabled = False   # Force stub mode
    return b


@pytest.fixture
def spawner():
    s = RobotSpawner()
    s._bridge._enabled = False   # Force stub mode
    return s


# ---------------------------------------------------------------------------
# IsaacSimBridge — stub mode
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bridge_connect_stub_returns_true(bridge):
    result = await bridge.connect()
    assert result is True


@pytest.mark.asyncio
async def test_bridge_spawn_robot_returns_stub(bridge):
    result = await bridge.spawn_robot("r1", x=1.0, y=2.0)
    assert result["source"] == "isaac_stub"
    assert "r1" in result["tag"]


@pytest.mark.asyncio
async def test_bridge_despawn_robot_returns_stub(bridge):
    result = await bridge.despawn_robot("r1")
    assert result["source"] == "isaac_stub"


@pytest.mark.asyncio
async def test_bridge_send_nav_goal_waypoint(bridge):
    result = await bridge.send_nav_goal("r1", waypoint="charging_dock")
    assert result["source"] == "isaac_stub"
    assert "charging_dock" in result["tag"]


@pytest.mark.asyncio
async def test_bridge_send_nav_goal_coordinates(bridge):
    result = await bridge.send_nav_goal("r1", x=3.0, y=1.5)
    assert result["source"] == "isaac_stub"


@pytest.mark.asyncio
async def test_bridge_get_robot_pose_returns_stub(bridge):
    result = await bridge.get_robot_pose("r1")
    assert result["source"] == "isaac_stub"


@pytest.mark.asyncio
async def test_bridge_reset_scene_stub(bridge):
    result = await bridge.reset_scene()
    assert result["source"] == "isaac_stub"


@pytest.mark.asyncio
async def test_bridge_close_ok(bridge):
    await bridge.close()   # should not raise


# ---------------------------------------------------------------------------
# RobotSpawner
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_spawner_connect(spawner):
    result = await spawner.connect()
    assert result is True


@pytest.mark.asyncio
async def test_spawner_spawn_fleet(spawner):
    configs = [
        RobotConfig("r1", x=0.0, y=0.0),
        RobotConfig("r2", x=3.0, y=1.5),
    ]
    results = await spawner.spawn_fleet(configs)
    assert results["r1"] is True
    assert results["r2"] is True
    assert "r1" in spawner.list_robots()
    assert "r2" in spawner.list_robots()


@pytest.mark.asyncio
async def test_spawner_navigate_sets_working_status(spawner):
    await spawner.spawn_fleet([RobotConfig("r1")])
    await spawner.navigate("r1", waypoint="zone_a")
    state = spawner.get_state("r1")
    assert state.status == "working"
    assert state.task_id != ""


@pytest.mark.asyncio
async def test_spawner_navigate_unknown_robot_raises(spawner):
    with pytest.raises(KeyError, match="not spawned"):
        await spawner.navigate("ghost", waypoint="dock")


@pytest.mark.asyncio
async def test_spawner_despawn_all(spawner):
    await spawner.spawn_fleet([RobotConfig("r1"), RobotConfig("r2")])
    assert len(spawner.list_robots()) == 2
    await spawner.despawn_all()
    assert len(spawner.list_robots()) == 0


@pytest.mark.asyncio
async def test_spawner_fleet_summary_shape(spawner):
    await spawner.spawn_fleet([RobotConfig("r1", fleet="fleet_a")])
    summary = spawner.fleet_summary()
    assert "r1" in summary
    assert summary["r1"]["fleet"] == "fleet_a"
    assert "x" in summary["r1"]
    assert "status" in summary["r1"]


@pytest.mark.asyncio
async def test_spawner_poll_states_returns_dict(spawner):
    await spawner.spawn_fleet([RobotConfig("r1")])
    states = await spawner.poll_states()
    assert "r1" in states
    assert isinstance(states["r1"], RobotState)


@pytest.mark.asyncio
async def test_spawner_get_state_none_for_unknown(spawner):
    state = spawner.get_state("nobody")
    assert state is None


@pytest.mark.asyncio
async def test_spawner_close_no_error(spawner):
    await spawner.close()  # should not raise


# ---------------------------------------------------------------------------
# RobotConfig defaults
# ---------------------------------------------------------------------------

def test_robot_config_defaults():
    cfg = RobotConfig(name="r1")
    assert cfg.fleet == "turtlebot_fleet"
    assert cfg.x == 0.0
    assert cfg.y == 0.0
    assert cfg.yaw == 0.0
    assert "turtlebot" in cfg.usd_path.lower()
