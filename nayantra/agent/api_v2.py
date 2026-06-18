"""
nayantra/agent/api_v2.py

Agent API v2 — same routes as v1 at the same paths, plus:
  - WebSocket fleet monitor (/ws/fleet)
  - Dashboard at GET /
  - /v2/health version marker

v2 *reuses v1's FastAPI app instance directly* (rather than mounting it
under /v1) so that the standard paths /health, /run, /stream, /history,
/stats, /readiness all work at the root — which is what the dashboard
and the start scripts' health checks expect.
"""

from __future__ import annotations

import logging
from pathlib import Path

import uvicorn
from fastapi import HTTPException
from fastapi.responses import FileResponse, Response

from nayantra.agent.api import app  # reuse v1's FastAPI instance directly
from nayantra.api.ws_monitor import router as ws_router
from nayantra.config import settings

logger = logging.getLogger("nayantra.api_v2")

# Attach v2 extras to the shared v1 app
app.include_router(ws_router)

_DASHBOARD = Path(__file__).resolve().parents[2] / "nayantra" / "api" / "dashboard.html"


@app.get("/v2/health")
async def health_v2():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Browsers auto-request this; return an empty 204 to silence the 404."""
    return Response(status_code=204)


@app.get("/")
async def serve_dashboard():
    """
    Serve dashboard.html with anti-caching headers so edits to the file
    show up immediately on the next page load. Without these, browsers
    aggressively cache the SPA shell and keep running stale JS that
    targets old API paths (e.g. /command instead of /run).
    """
    if _DASHBOARD.exists():
        return FileResponse(
            str(_DASHBOARD),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    raise HTTPException(404, "Dashboard not found")


def main() -> None:
    uvicorn.run(
        "nayantra.agent.api_v2:app",
        host=settings.AGENT_API_HOST,
        port=settings.AGENT_API_PORT,
        reload=settings.DEBUG_MODE,
        log_level=settings.LOGGING_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
