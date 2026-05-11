"""
tests/conftest.py

Shared pytest fixtures for the whole test suite.

Anything defined here is available to every test_*.py file without import.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import pytest_asyncio

from nayantra.agent.history import MissionStore
from nayantra.config import settings
from nayantra.rmf_client.client import OpenRMFClient

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def env_settings():
    """The shared Settings singleton."""
    return settings


@pytest.fixture(autouse=True)
def _force_debug_mode(monkeypatch):
    """
    Force debug-friendly defaults for all tests so nothing tries to hit
    a real LLM / RMF / Isaac Sim. Individual tests can monkeypatch to
    override these per-test if needed.
    """
    monkeypatch.setattr(settings, "DEBUG_MODE", True)
    monkeypatch.setattr(settings, "USE_AUTH", False)
    monkeypatch.setattr(settings, "ISAAC_SIM_ENABLED", False)
    monkeypatch.setattr(settings, "ZENOH_ENABLED", False)


# ---------------------------------------------------------------------------
# RMF client (simulated)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def debug_client():
    """A simulated OpenRMFClient for tests that don't need a real RMF server."""
    client = OpenRMFClient(debug=True)
    yield client
    await client.close()


# ---------------------------------------------------------------------------
# Mission store with isolated temp DB
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_mission_store(tmp_path: Path):
    """A MissionStore pointed at a per-test temporary SQLite file."""
    db = tmp_path / "missions.db"
    return MissionStore(db_path=db)


# ---------------------------------------------------------------------------
# asyncio event loop (session scoped so async fixtures share it)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def event_loop():
    """Override pytest-asyncio's default function-scoped loop with a session one."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()
