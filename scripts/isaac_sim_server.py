"""
scripts/isaac_sim_server.py

A lightweight FastAPI server that runs *inside* Isaac Sim's Python REPL and
exposes the REST endpoints that nayantra/isaac_sim/sim_bridge.py expects.

The bridge expects these endpoints (see nayantra/isaac_sim/sim_bridge.py):
    GET  /health
    POST /stage/load
    POST /stage/reset
    POST /prim/create
    POST /prim/delete
    POST /prim/set_attribute
    POST /action_graph/execute
    GET  /robot_state?prim=...

USAGE (inside Isaac Sim's Script Editor or omni.kit.python.repl):

    import sys
    sys.path.insert(0, '/path/to/navigation')
    from scripts.isaac_sim_server import start
    start(host="0.0.0.0", port=8211)

Once running, set ISAAC_SIM_ENABLED=true in your .env and the bridge will
talk to this server.

Notes:
  • Isaac Sim ships its own Python (~3.10). FastAPI and uvicorn must be
    installed into that interpreter, OR you can run this as a standalone
    fallback (no real prim manipulation; useful for end-to-end tests).
  • The "real" implementation requires the omni.usd APIs. The fallback
    below simulates everything so the rest of the stack can be exercised.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Dict

logger = logging.getLogger("isaac_sim_server")

# ---------------------------------------------------------------------------
# Try to import Isaac Sim APIs. If unavailable, fall back to a pure-Python
# simulation that still satisfies the REST contract.
# ---------------------------------------------------------------------------
try:
    from omni.isaac.core import World  # type: ignore
    from omni.isaac.core.utils.stage import open_stage  # type: ignore
    from omni.isaac.core.utils.prims import create_prim, delete_prim  # type: ignore
    ISAAC_AVAILABLE = True
    logger.info("Isaac Sim APIs detected — running in live mode")
except ImportError:
    ISAAC_AVAILABLE = False
    logger.warning("Isaac Sim APIs NOT available — running in fallback simulation mode")


# In-memory robot pose registry (populated whether live or fallback)
_robots: Dict[str, Dict[str, float]] = {}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
def _build_app():
    from fastapi import FastAPI, HTTPException, Query
    from pydantic import BaseModel

    app = FastAPI(title="Isaac Sim REST Bridge", version="1.0.0")

    class StageLoadRequest(BaseModel):
        url: str

    class PrimCreateRequest(BaseModel):
        prim_path: str
        usd_path: str = ""
        attributes: Dict[str, Any] = {}

    class PrimDeleteRequest(BaseModel):
        prim_path: str

    class ActionGraphRequest(BaseModel):
        robot_prim: str
        action: str = "navigate"
        waypoint: str | None = None
        goal: Dict[str, float] | None = None

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "isaac_available": ISAAC_AVAILABLE,
            "tracked_robots": list(_robots.keys()),
        }

    @app.post("/stage/load")
    def stage_load(req: StageLoadRequest):
        if ISAAC_AVAILABLE:
            try:
                open_stage(req.url)
                return {"status": "loaded", "url": req.url}
            except Exception as exc:
                raise HTTPException(500, f"open_stage failed: {exc}")
        return {"status": "loaded_simulated", "url": req.url}

    @app.post("/stage/reset")
    def stage_reset():
        if ISAAC_AVAILABLE:
            try:
                World.instance().reset()
                return {"status": "reset"}
            except Exception as exc:
                raise HTTPException(500, f"reset failed: {exc}")
        return {"status": "reset_simulated"}

    @app.post("/prim/create")
    def prim_create(req: PrimCreateRequest):
        translate = req.attributes.get("xformOp:translate", [0.0, 0.0, 0.0])
        rotate = req.attributes.get("xformOp:rotateXYZ", [0.0, 0.0, 0.0])
        name = req.prim_path.rsplit("/", 1)[-1]
        _robots[name] = {
            "x": float(translate[0]), "y": float(translate[1]),
            "yaw": float(rotate[2]), "level_name": "L1",
            "prim_path": req.prim_path,
        }

        if ISAAC_AVAILABLE and req.usd_path:
            try:
                create_prim(
                    prim_path=req.prim_path,
                    usd_path=req.usd_path,
                    translation=translate,
                    orientation=None,
                )
            except Exception as exc:
                raise HTTPException(500, f"create_prim failed: {exc}")

        return {"status": "created", "prim_path": req.prim_path}

    @app.post("/prim/delete")
    def prim_delete(req: PrimDeleteRequest):
        name = req.prim_path.rsplit("/", 1)[-1]
        _robots.pop(name, None)
        if ISAAC_AVAILABLE:
            try:
                delete_prim(req.prim_path)
            except Exception as exc:
                raise HTTPException(500, f"delete_prim failed: {exc}")
        return {"status": "deleted", "prim_path": req.prim_path}

    @app.post("/prim/set_attribute")
    def prim_set_attribute(payload: Dict[str, Any]):
        return {"status": "ok", "received": payload}

    @app.post("/action_graph/execute")
    def action_graph(req: ActionGraphRequest):
        name = req.robot_prim.rsplit("/", 1)[-1]
        if name in _robots and req.goal:
            _robots[name]["x"] = float(req.goal.get("x", _robots[name]["x"]))
            _robots[name]["y"] = float(req.goal.get("y", _robots[name]["y"]))
        return {
            "status": "dispatched",
            "task_id": str(uuid.uuid4()),
            "robot": name,
            "action": req.action,
            "waypoint": req.waypoint,
            "goal": req.goal,
        }

    @app.get("/robot_state")
    def robot_state(prim: str = Query(...)):
        name = prim.rsplit("/", 1)[-1]
        if name not in _robots:
            raise HTTPException(404, f"Unknown robot {name!r}")
        return _robots[name]

    return app


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------
def start(host: str = "0.0.0.0", port: int = 8211) -> None:
    """Start the REST server (blocking). Call from inside Isaac Sim."""
    import uvicorn
    app = _build_app()
    logger.info(f"Isaac Sim REST bridge listening on {host}:{port}")
    logger.info(f"  Live Isaac APIs: {ISAAC_AVAILABLE}")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    # Standalone mode — useful for testing the agent stack without launching
    # Isaac Sim. The endpoints behave as a stub.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    start()
