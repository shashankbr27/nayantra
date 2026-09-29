"""
nayantra/core/server.py

The Nayantra Core HTTP server:

  /api/v1/...   REST API (core/api.py)
  /api/v1/ws    realtime world stream (core/ws.py)
  /             operator web UI (web/dist, built with `npm --prefix web run build`)
  /fleets, /tasks/dispatch_task, /building_map …  legacy rmf-web-shaped routes

Run:  nayantra-core            (or: python -m nayantra.core.server)
      nayantra-core --reset    wipe the database and re-seed the scenario
"""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from nayantra.config import settings
from nayantra.core import api, legacy_rmf, ws
from nayantra.core.errors import ConfirmationRequired, CoreError
from nayantra.core.runtime import VERSION, NayantraCore

logger = logging.getLogger("nayantra.core.server")

_NO_UI = """<!doctype html><html><head><meta charset="utf-8"><title>Nayantra Core</title>
<style>body{font:15px system-ui;background:#0b1016;color:#d8e1ea;display:grid;place-items:center;height:100vh;margin:0}
code{background:#16202b;padding:2px 6px;border-radius:4px}div{max-width:560px;line-height:1.6}</style></head>
<body><div><h2>Nayantra Core {version} is running</h2>
<p>The operator UI has not been built yet. Build it once:</p>
<p><code>npm --prefix web install &amp;&amp; npm --prefix web run build</code></p>
<p>then reload. API: <a style="color:#5eead4" href="/docs">/docs</a> · realtime: <code>ws://…/api/v1/ws</code></p>
</div></body></html>"""


def create_app(core: NayantraCore | None = None, start_core: bool = True) -> FastAPI:
    core = core or NayantraCore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_core:
            await core.start()
        yield
        if start_core:
            await core.stop()

    app = FastAPI(
        title="Nayantra Core",
        version=VERSION,
        description="Agentic operations control plane for heterogeneous robot fleets (Nayantra-native).",
        lifespan=lifespan,
    )
    app.state.core = core
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS + ["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(ConfirmationRequired)
    async def _confirm(_: Request, exc: ConfirmationRequired):
        return JSONResponse(
            status_code=202,
            content={
                "confirmation_required": True,
                "message": exc.message,
                "confirmation": exc.action,
            },
        )

    @app.exception_handler(CoreError)
    async def _core_error(_: Request, exc: CoreError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": type(exc).__name__, "message": exc.message, "details": exc.details},
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errors = [
            {
                "loc": ".".join(str(p) for p in e.get("loc", [])),
                "msg": e.get("msg"),
                "type": e.get("type"),
            }
            for e in exc.errors()
        ]
        first = errors[0] if errors else {"loc": "", "msg": "invalid request"}
        return JSONResponse(
            status_code=422,
            content={
                "error": "ValidationError",
                "message": f"{first['loc']}: {first['msg']}",
                "details": {"errors": errors},
            },
        )

    app.include_router(api.router)
    app.include_router(ws.router)

    dist = Path(settings.WEB_DIST_DIR)
    if (dist / "index.html").exists():
        assets = dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/", include_in_schema=False)
        async def index():
            return FileResponse(str(dist / "index.html"), headers={"Cache-Control": "no-cache"})

        for extra in ("favicon.svg", "favicon.ico"):
            if (dist / extra).exists():
                app.add_api_route(
                    f"/{extra}",
                    lambda e=extra: FileResponse(str(dist / e)),
                    include_in_schema=False,
                )
    else:

        @app.get("/", include_in_schema=False)
        async def index():
            return HTMLResponse(_NO_UI.replace("{version}", VERSION))

    # Legacy routes last so they never shadow the UI or /api/v1.
    app.include_router(legacy_rmf.router)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Nayantra Core — multi-fleet control plane")
    parser.add_argument("--host", default=settings.CORE_HOST)
    parser.add_argument("--port", type=int, default=settings.CORE_PORT)
    parser.add_argument("--scenario", default=None, help="scenario to seed into an empty database")
    parser.add_argument("--db", default=None, help="SQLite path (default NAYANTRA_DB_PATH)")
    parser.add_argument("--reset", action="store_true", help="wipe the database first, then seed")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, settings.LOGGING_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    db = args.db or settings.NAYANTRA_DB_PATH
    if args.reset and Path(db).exists():
        for suffix in ("", "-wal", "-shm"):
            p = Path(db + suffix)
            if p.exists():
                p.unlink()
        logger.info(f"Reset: removed {db}")
    import uvicorn

    app = create_app(NayantraCore(db_path=db, scenario=args.scenario))
    uvicorn.run(app, host=args.host, port=args.port, log_level=settings.LOGGING_LEVEL.lower())


if __name__ == "__main__":
    main()
