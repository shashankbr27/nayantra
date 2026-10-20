"""The dashboard bundle's top-level files (favicons in several variants, logos) must all be served."""

from __future__ import annotations

import httpx
import pytest_asyncio

from nayantra.config import settings
from nayantra.core.runtime import NayantraCore
from nayantra.core.server import create_app


@pytest_asyncio.fixture
async def client(tmp_path, monkeypatch):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>dashboard</html>")
    (tmp_path / "favicon.svg").write_text("<svg>default</svg>")
    (tmp_path / "favicon-dark.svg").write_text("<svg>dark</svg>")
    (tmp_path / "favicon-light.svg").write_text("<svg>light</svg>")
    (tmp_path / "apple-touch-icon.png").write_bytes(b"\x89PNG fake")
    monkeypatch.setattr(settings, "WEB_DIST_DIR", str(tmp_path))
    core = NayantraCore(db_path=":memory:", scenario="warehouse_demo")
    await core.start()
    app = create_app(core, start_core=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://core"
    ) as c:
        yield c
    await core.stop()


async def test_every_icon_variant_is_served(client):
    for name, body in (
        ("favicon.svg", "default"),
        ("favicon-dark.svg", "dark"),
        ("favicon-light.svg", "light"),
    ):
        r = await client.get(f"/{name}")
        assert r.status_code == 200 and body in r.text, name
    assert (await client.get("/apple-touch-icon.png")).status_code == 200


async def test_index_is_still_the_dashboard_and_unknown_files_404(client):
    assert "dashboard" in (await client.get("/")).text
    assert (await client.get("/does-not-exist.svg")).status_code == 404
