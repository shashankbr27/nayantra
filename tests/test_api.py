"""
tests/test_api.py

Structural tests for the agent API routes.
Tests verify routing and response schemas without starting LLM clients.
"""

from __future__ import annotations

from fastapi import FastAPI


def _route_paths(app: FastAPI) -> set:
    """Collect every route path, descending into included sub-routers.

    Starlette >= 1.3 represents ``include_router`` entries as lazy
    ``_IncludedRouter`` objects that have no ``.path`` of their own but
    expose their child routes via ``.routes`` — so we walk recursively
    and skip anything that is neither.
    """
    paths: set = set()

    def _collect(routes) -> None:
        for r in routes:
            path = getattr(r, "path", None)
            if path is not None:
                paths.add(path)
            sub = getattr(r, "routes", None)
            if sub:
                _collect(sub)

    _collect(app.routes)
    return paths


def test_v1_api_has_health_route():
    from nayantra.agent.api import app

    assert "/health" in _route_paths(app)


def test_v1_api_has_run_route():
    from nayantra.agent.api import app

    assert "/run" in _route_paths(app)


def test_v1_api_has_history_route():
    from nayantra.agent.api import app

    assert "/history" in _route_paths(app)


def test_v1_api_has_stats_route():
    from nayantra.agent.api import app

    assert "/stats" in _route_paths(app)


def test_v1_api_has_readiness_route():
    from nayantra.agent.api import app

    assert "/readiness" in _route_paths(app)


def test_v1_api_has_stream_route():
    from nayantra.agent.api import app

    assert "/stream" in _route_paths(app)


def test_v2_api_imports():
    from nayantra.agent import api_v2

    assert api_v2.app is not None


def test_v2_app_has_health_route():
    from nayantra.agent.api_v2 import app

    assert "/v2/health" in _route_paths(app)


def test_v2_app_has_root_route():
    from nayantra.agent.api_v2 import app

    assert "/" in _route_paths(app)
