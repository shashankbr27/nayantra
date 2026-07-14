"""
scripts/isaac_demo.py — Isaac Sim 6.0 (pip): warehouse + Carter + WebRTC + motion.

Runs the simulation, streams it over WebRTC, and exposes a tiny HTTP control API
so a terminal client (scripts/drive_cli.py) can move Carter with NL commands.

Run in the venv on the GPU workstation (RTX 6000 Ada, etc.):
    source ~/or_sig/isaacsim-env/bin/activate
    export OMNI_KIT_ACCEPT_EULA=YES PRIVACY_CONSENT=Y
    python isaac_demo.py
Then connect the Isaac Sim WebRTC Streaming Client to this host's IP.

Control API (port 8900):
    GET  /state              -> {x,y,yaw,target,moving,...}
    GET  /waypoints          -> {name: [x,y], ...}
    POST /goto?waypoint=NAME -> send Carter to a named waypoint
    POST /goto?x=..&y=..     -> send Carter to a coordinate

Motion is kinematic (smooth glide to the target via USD xform ops) — robust and
visually clear. Swap to Nav2/wheel control later for true path planning.

Env: SCENE_USD, ROBOT_USD, ISAAC_ASSETS_ROOT, ROBOT_X, ROBOT_Y, CTRL_PORT, SPEED,
     RENDER_WIDTH, RENDER_HEIGHT.
"""

from __future__ import annotations

import json
import math
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# -----------------------------------------------------------------------------
# 1. SimulationApp with WebRTC livestream (needs an RTX GPU with a hardware video
#    encoder — RTX 6000 Ada / A6000 / RTX 4000-class).
# -----------------------------------------------------------------------------
from isaacsim import SimulationApp

simulation_app = SimulationApp(
    {
        "headless": True,
        "livestream": 2,  # 2 = WebRTC
        "renderer": "RayTracedLighting",
        "width": int(os.getenv("RENDER_WIDTH", "1280")),
        "height": int(os.getenv("RENDER_HEIGHT", "720")),
    }
)


def say(m: str) -> None:
    print(f"[isaac_demo] {m}", flush=True)


# Belt-and-suspenders: also enable the WebRTC livestream extension explicitly.
try:
    from isaacsim.core.utils.extensions import enable_extension

    enable_extension("omni.kit.livestream.webrtc")
    simulation_app.update()
except Exception as exc:  # noqa: BLE001
    say(f"livestream extension note (continuing): {exc}")

# -----------------------------------------------------------------------------
# 2. Core imports (valid only after SimulationApp exists)
# -----------------------------------------------------------------------------
import importlib  # noqa: E402

import omni.usd  # noqa: E402
from isaacsim.core.api import World  # noqa: E402
from pxr import Gf, Sdf, UsdGeom  # noqa: E402

# get_assets_root_path moved across versions:
#   6.0 -> isaacsim.storage.native ; 4.5 -> isaacsim.core.utils.nucleus ;
#   old -> omni.isaac.core.utils.nucleus. Try them in order.
get_assets_root_path = None
for _m in (
    "isaacsim.storage.native",
    "isaacsim.core.utils.nucleus",
    "omni.isaac.core.utils.nucleus",
):
    try:
        get_assets_root_path = importlib.import_module(_m).get_assets_root_path
        say(f"get_assets_root_path from {_m}")
        break
    except Exception:  # noqa: BLE001
        continue
if get_assets_root_path is None:

    def get_assets_root_path():  # type: ignore
        return None


# -----------------------------------------------------------------------------
# 3. Scene config + named waypoints
# -----------------------------------------------------------------------------
assets_root = os.getenv("ISAAC_ASSETS_ROOT") or get_assets_root_path()
say(f"assets_root = {assets_root}")


def _first_resolvable(paths: list[str], label: str) -> str:
    """Return the first USD path the asset resolver can open; if none resolve,
    fall back to the first usable candidate (and warn) so we still attempt a
    load. Lets the script auto-adapt to per-version asset path changes."""
    candidates = [p for p in paths if p and "None/" not in p]
    for p in candidates:
        try:
            if Sdf.Layer.FindOrOpen(p) is not None:
                say(f"{label} -> {p}")
                return p
        except Exception:  # noqa: BLE001
            continue
    if candidates:
        say(
            f"WARNING: no {label} candidate resolved; trying {candidates[0]!r}. "
            f"If it doesn't appear, set the env var to the correct USD path."
        )
        return candidates[0]
    raise RuntimeError(
        f"No {label} candidates available (assets_root={assets_root!r}). "
        "Set ISAAC_ASSETS_ROOT, or SCENE_USD / ROBOT_USD explicitly."
    )


SCENE_USD = _first_resolvable(
    [
        os.getenv("SCENE_USD", ""),
        f"{assets_root}/Isaac/Environments/Simple_Warehouse/warehouse.usd",
        f"{assets_root}/Isaac/Environments/Simple_Warehouse/warehouse_with_forklifts.usd",
    ],
    "SCENE_USD",
)
# Nova Carter moved under a vendor folder in 6.0 (Isaac/Robots/NVIDIA/Carter/...).
# Try the 6.0 layout first, then older layouts; an explicit ROBOT_USD wins.
ROBOT_USD = _first_resolvable(
    [
        os.getenv("ROBOT_USD", ""),
        f"{assets_root}/Isaac/Robots/NVIDIA/Carter/nova_carter/nova_carter.usd",
        f"{assets_root}/Isaac/Robots/NVIDIA/Carter/nova_carter.usd",
        f"{assets_root}/Isaac/Robots/Carter/nova_carter.usd",
    ],
    "ROBOT_USD",
)
ROBOT_PRIM = "/World/carter"

# Named places in the warehouse (x, y in metres). Edit/extend freely — the NL
# driver maps spoken names to these.
WAYPOINTS: dict[str, tuple[float, float]] = {
    "main_gate": (-6.0, 0.0),
    "loading_dock": (6.0, -2.0),
    "board_room": (3.0, 2.0),
    "meeting_room": (5.0, 2.0),
    "canteen": (-3.0, 2.0),
    "store_room": (5.0, -2.0),
    "charging_dock": (-5.0, -2.0),
    "zone_a": (-3.0, 0.0),
    "zone_b": (3.0, 0.0),
    "center": (0.0, 0.0),
}

world = World(stage_units_in_meters=1.0)
_stage = omni.usd.get_context().get_stage()


def _add_ref(usd_path: str, prim_path: str):
    """Add a USD reference using plain pxr APIs — stable across all Isaac
    versions (the isaacsim.* stage helpers were renamed in 6.0)."""
    prim = _stage.DefinePrim(prim_path, "Xform")
    prim.GetReferences().AddReference(usd_path)
    return prim


say(f"loading scene: {SCENE_USD}")
_add_ref(SCENE_USD, "/World/Warehouse")
say(f"spawning Carter: {ROBOT_USD}")
_carter_prim = _add_ref(ROBOT_USD, ROBOT_PRIM)

try:
    world.reset()
except Exception as exc:  # noqa: BLE001
    say(f"world.reset note (continuing): {exc}")

# Kinematic pose control via USD xform ops (version-independent). We own the
# /World/carter wrapper Xform, so clearing + re-adding its ops is safe.
_xf = UsdGeom.Xformable(_carter_prim)
_xf.ClearXformOpOrder()
_translate_op = _xf.AddTranslateOp()
_rotate_op = _xf.AddRotateZOp()

_state: dict = {
    "x": float(os.getenv("ROBOT_X", "0")),
    "y": float(os.getenv("ROBOT_Y", "0")),
    "yaw": 0.0,
    "tx": None,
    "ty": None,
    "moving": False,
    "target_name": None,
}


def _set_pose(x: float, y: float, yaw: float) -> None:
    _translate_op.Set(Gf.Vec3d(float(x), float(y), 0.0))
    _rotate_op.Set(float(math.degrees(yaw)))  # AddRotateZOp takes degrees


_set_pose(_state["x"], _state["y"], 0.0)


# -----------------------------------------------------------------------------
# 4. HTTP control API (threaded; the sim loop reads the shared _state)
# -----------------------------------------------------------------------------
def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, ctype="application/json") -> None:
    h.send_response(code)
    h.send_header("Content-Type", ctype)
    h.send_header("Content-Length", str(len(body)))
    h.end_headers()
    h.wfile.write(body)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        return

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/state":
            _send(self, 200, json.dumps(_state).encode())
        elif path == "/waypoints":
            _send(self, 200, json.dumps(WAYPOINTS).encode())
        else:
            _send(
                self,
                200,
                b"isaac_demo control API: POST /goto, GET /state|/waypoints",
                "text/plain",
            )

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(parsed.query)
        if parsed.path != "/goto":
            _send(self, 404, b"not found", "text/plain")
            return
        if "waypoint" in q:
            name = q["waypoint"][0]
            if name not in WAYPOINTS:
                _send(self, 404, f"unknown waypoint: {name}".encode(), "text/plain")
                return
            tx, ty = WAYPOINTS[name]
            _state["target_name"] = name
        else:
            tx = float(q.get("x", ["0"])[0])
            ty = float(q.get("y", ["0"])[0])
            _state["target_name"] = None
        _state["tx"], _state["ty"], _state["moving"] = tx, ty, True
        say(f"goto -> {_state['target_name'] or (tx, ty)}")
        _send(
            self,
            200,
            json.dumps({"ok": True, "target": [tx, ty], "name": _state["target_name"]}).encode(),
        )


def _serve() -> None:
    port = int(os.getenv("CTRL_PORT", "8900"))
    ThreadingHTTPServer(("0.0.0.0", port), _Handler).serve_forever()


threading.Thread(target=_serve, daemon=True).start()
say(f"control API on 0.0.0.0:{os.getenv('CTRL_PORT', '8900')}")
say("Stage ready. WebRTC streaming; drive Carter with scripts/drive_cli.py")

# -----------------------------------------------------------------------------
# 5. Sim loop: glide Carter toward the target each step, then render (streams).
# -----------------------------------------------------------------------------
SPEED = float(os.getenv("SPEED", "1.5"))  # m/s
DT = 1.0 / 60.0
try:
    while simulation_app.is_running():
        if _state["moving"] and _state["tx"] is not None:
            dx = _state["tx"] - _state["x"]
            dy = _state["ty"] - _state["y"]
            dist = math.hypot(dx, dy)
            if dist < 0.05:
                _state["moving"] = False
                say(f"arrived at {_state['target_name'] or (_state['tx'], _state['ty'])}")
            else:
                step = min(SPEED * DT, dist)
                _state["x"] += dx / dist * step
                _state["y"] += dy / dist * step
                _state["yaw"] = math.atan2(dy, dx)
                _set_pose(_state["x"], _state["y"], _state["yaw"])
        # Render + pump the WebRTC stream without stepping physics, so our
        # kinematic glide isn't fought by gravity/articulation dynamics.
        simulation_app.update()
except KeyboardInterrupt:
    pass
finally:
    simulation_app.close()
