"""
tests/test_version.py

The release version is written in several places (package metadata, the UI,
the core, both HTTP servers). They are edited by hand, so this keeps a release
bump from leaving one of them behind.
"""

from __future__ import annotations

import asyncio
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _release() -> str:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return str(tomllib.load(f)["project"]["version"])


def test_release_is_a_plain_semver():
    parts = _release().split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), _release()


def test_core_reports_the_release():
    from nayantra.core.runtime import VERSION

    assert VERSION == _release()


def test_mcp_server_reports_the_release():
    from nayantra.mcp.server import app

    assert app.version == _release()


def test_agent_api_reports_the_release():
    from nayantra.agent.api import app
    from nayantra.agent.api_v2 import health_v2

    assert app.version == _release()
    assert asyncio.run(health_v2())["version"] == _release()


def test_operator_ui_reports_the_release():
    package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "web" / "package-lock.json").read_text(encoding="utf-8"))

    assert package["version"] == _release()
    assert lock["version"] == _release()
    assert lock["packages"][""]["version"] == _release()
