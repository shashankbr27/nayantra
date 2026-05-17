"""
tests/test_health.py

Tests for the startup health checker.
All external HTTP calls are mocked with respx.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from nayantra.agent.health import HealthChecker, HealthReport, HealthResult


@pytest.fixture
def checker():
    return HealthChecker()


# ---------------------------------------------------------------------------
# HealthReport
# ---------------------------------------------------------------------------


def test_report_ready_when_all_ok():
    report = HealthReport(
        results=[
            HealthResult("a", ok=True, message="ok"),
            HealthResult("b", ok=True, message="ok"),
        ]
    )
    assert report.ready is True


def test_report_not_ready_when_required_fails():
    report = HealthReport(
        results=[
            HealthResult("a", ok=True, message="ok"),
            HealthResult("b", ok=False, message="failed", optional=False),
        ]
    )
    assert report.ready is False


def test_report_ready_when_only_optional_fails():
    report = HealthReport(
        results=[
            HealthResult("a", ok=True, message="ok"),
            HealthResult("b", ok=False, message="failed", optional=True),
        ]
    )
    assert report.ready is True


def test_report_summary_contains_check_names():
    report = HealthReport(
        results=[
            HealthResult("config", ok=True, message="all good"),
            HealthResult("mcp_server", ok=False, message="refused"),
        ]
    )
    summary = report.summary
    assert "config" in summary
    assert "mcp_server" in summary


def test_report_to_dict_structure():
    report = HealthReport(
        results=[
            HealthResult("config", ok=True, message="ok", latency_ms=2.5),
        ]
    )
    d = report.to_dict()
    assert "ready" in d
    assert "checks" in d
    assert d["checks"][0]["name"] == "config"
    assert d["checks"][0]["latency_ms"] == 2.5


# ---------------------------------------------------------------------------
# Config check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_config_passes_with_keys(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.LLM_PROVIDER", "anthropic")
    monkeypatch.setattr("nayantra.agent.health.settings.ANTHROPIC_API_KEY", "sk-real-key")
    monkeypatch.setattr("nayantra.agent.health.settings.OPENRMF_API_TOKEN", "some-token")
    monkeypatch.setattr("nayantra.agent.health.settings.USE_AUTH", True)
    monkeypatch.setattr("nayantra.agent.health.settings.JWT_SECRET", "custom-secret")
    result = await checker._check_config()
    assert result.ok is True


@pytest.mark.asyncio
async def test_check_config_fails_without_api_key(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.LLM_PROVIDER", "anthropic")
    monkeypatch.setattr("nayantra.agent.health.settings.ANTHROPIC_API_KEY", "")
    monkeypatch.setattr("nayantra.agent.health.settings.OPENRMF_API_TOKEN", "tok")
    monkeypatch.setattr("nayantra.agent.health.settings.USE_AUTH", False)
    result = await checker._check_config()
    assert result.ok is False
    assert "ANTHROPIC_API_KEY" in result.message


@pytest.mark.asyncio
async def test_check_config_fails_without_gemini_key(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.LLM_PROVIDER", "gemini")
    monkeypatch.setattr("nayantra.agent.health.settings.GEMINI_API_KEY", "")
    monkeypatch.setattr("nayantra.agent.health.settings.OPENRMF_API_TOKEN", "tok")
    monkeypatch.setattr("nayantra.agent.health.settings.USE_AUTH", False)
    result = await checker._check_config()
    assert result.ok is False
    assert "GEMINI_API_KEY" in result.message


@pytest.mark.asyncio
async def test_check_config_passes_with_gemini_key(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.LLM_PROVIDER", "gemini")
    monkeypatch.setattr("nayantra.agent.health.settings.GEMINI_API_KEY", "AIza-fake-key")
    monkeypatch.setattr("nayantra.agent.health.settings.OPENRMF_API_TOKEN", "tok")
    monkeypatch.setattr("nayantra.agent.health.settings.USE_AUTH", False)
    result = await checker._check_config()
    assert result.ok is True


# ---------------------------------------------------------------------------
# MCP server check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_check_mcp_server_ok(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.MCP_SERVER_URL", "http://fake-mcp:7000")
    respx.get("http://fake-mcp:7000/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    result = await checker._check_mcp_server()
    assert result.ok is True
    assert result.latency_ms is not None


@pytest.mark.asyncio
@respx.mock
async def test_check_mcp_server_connection_refused(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.MCP_SERVER_URL", "http://fake-mcp:7000")
    respx.get("http://fake-mcp:7000/health").mock(side_effect=httpx.ConnectError("refused"))
    result = await checker._check_mcp_server()
    assert result.ok is False
    assert "refused" in result.message.lower() or "connection" in result.message.lower()


@pytest.mark.asyncio
@respx.mock
async def test_check_mcp_server_http_500(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.MCP_SERVER_URL", "http://fake-mcp:7000")
    respx.get("http://fake-mcp:7000/health").mock(return_value=httpx.Response(500))
    result = await checker._check_mcp_server()
    assert result.ok is False


# ---------------------------------------------------------------------------
# RMF API check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_check_rmf_api_ok(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.OPENRMF_API_URL", "http://fake-rmf:8000")
    monkeypatch.setattr("nayantra.agent.health.settings.DEBUG_MODE", False)
    respx.get("http://fake-rmf:8000/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    result = await checker._check_rmf_api()
    assert result.ok is True


@pytest.mark.asyncio
@respx.mock
async def test_check_rmf_api_404_is_ok(checker, monkeypatch):
    """A 404 means the server exists but has no /health endpoint — that's fine."""
    monkeypatch.setattr("nayantra.agent.health.settings.OPENRMF_API_URL", "http://fake-rmf:8000")
    monkeypatch.setattr("nayantra.agent.health.settings.DEBUG_MODE", False)
    respx.get("http://fake-rmf:8000/health").mock(return_value=httpx.Response(404))
    result = await checker._check_rmf_api()
    assert result.ok is True


@pytest.mark.asyncio
@respx.mock
async def test_check_rmf_api_refused_in_debug_is_optional(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.OPENRMF_API_URL", "http://fake-rmf:8000")
    monkeypatch.setattr("nayantra.agent.health.settings.DEBUG_MODE", True)
    respx.get("http://fake-rmf:8000/health").mock(side_effect=httpx.ConnectError("refused"))
    result = await checker._check_rmf_api()
    assert result.optional is True


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@respx.mock
async def test_run_all_returns_report(checker, monkeypatch):
    monkeypatch.setattr("nayantra.agent.health.settings.MCP_SERVER_URL", "http://fake-mcp:7000")
    monkeypatch.setattr("nayantra.agent.health.settings.OPENRMF_API_URL", "http://fake-rmf:8000")
    monkeypatch.setattr("nayantra.agent.health.settings.ISAAC_SIM_ENABLED", False)
    monkeypatch.setattr("nayantra.agent.health.settings.LLM_PROVIDER", "anthropic")
    monkeypatch.setattr("nayantra.agent.health.settings.ANTHROPIC_API_KEY", "sk-key")
    monkeypatch.setattr("nayantra.agent.health.settings.OPENRMF_API_TOKEN", "tok")
    monkeypatch.setattr("nayantra.agent.health.settings.USE_AUTH", False)

    respx.get("http://fake-mcp:7000/health").mock(return_value=httpx.Response(200, json={}))
    respx.get("http://fake-rmf:8000/health").mock(return_value=httpx.Response(200, json={}))

    report = await checker.run_all()
    assert isinstance(report, HealthReport)
    assert len(report.results) >= 3  # config + mcp + rmf
    await checker.close()
