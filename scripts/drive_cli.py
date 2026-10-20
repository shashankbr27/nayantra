"""
scripts/drive_cli.py — natural-language driver for isaac_demo.py.

Type a plain-English command in the terminal; Gemini maps it to one or more of
the warehouse's known waypoints, and each is POSTed to the running isaac_demo
control API, so Carter drives there live in the WebRTC stream.

Run on the Isaac host (localhost) or your laptop (point --host at the box):
    export GEMINI_API_KEY=...
    python drive_cli.py "send carter to the loading dock"
    python drive_cli.py --host 172.25.61.209 "go to the canteen then the board room"
    python drive_cli.py            # no args -> interactive REPL

Env: GEMINI_API_KEY, GEMINI_MODEL (default gemini-2.5-flash),
     ISAAC_HOST (default 127.0.0.1), CTRL_PORT (default 8900).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


def _post(url: str):
    req = urllib.request.Request(url, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def plan_waypoints(command: str, waypoints: dict) -> list[str]:
    """Use Gemini to turn an NL command into an ordered list of known waypoints."""
    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    names = list(waypoints)
    prompt = (
        "You route a warehouse robot. Known waypoints (use these EXACT names):\n"
        f"{names}\n\n"
        f'User command: "{command}"\n\n'
        "Return ONLY a JSON array of waypoint names from the list above, in the "
        "order the robot should visit them. For 'pick up at A and drop at B then "
        "go to C', return [A, B, C]. No prose, no code fences."
    )
    resp = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        contents=prompt,
        config={"temperature": 0},
    )
    text = (resp.text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[4:].strip() if text.lower().startswith("json") else text
    try:
        seq = json.loads(text)
        return [w for w in seq if w in waypoints]
    except Exception:
        # Fallback: any known waypoint name mentioned in the command.
        low = command.lower()
        return [w for w in names if w in low or w.replace("_", " ") in low]


def drive(base: str, command: str) -> None:
    waypoints = _get(f"{base}/waypoints")
    seq = plan_waypoints(command, waypoints)
    if not seq:
        print("No known waypoint matched. Known:", ", ".join(waypoints))
        return
    print(f"Plan: {' -> '.join(seq)}")
    for name in seq:
        x, y = waypoints[name]
        print(f"  -> {name} ({x}, {y})")
        _post(f"{base}/goto?waypoint={name}")
        while _get(f"{base}/state").get("moving"):
            time.sleep(0.3)
        print(f"     arrived at {name}")
    print("Done.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Natural-language driver for isaac_demo")
    ap.add_argument("command", nargs="*", help="NL command (omit for interactive REPL)")
    ap.add_argument("--host", default=os.getenv("ISAAC_HOST", "127.0.0.1"))
    ap.add_argument("--port", default=os.getenv("CTRL_PORT", "8900"))
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"

    # Fail early if the Isaac control API isn't reachable.
    try:
        _get(f"{base}/waypoints")
    except Exception as exc:  # noqa: BLE001
        print(f"Cannot reach isaac_demo control API at {base}: {exc}")
        print("Is isaac_demo.py running on the GPU host? Check --host/--port.")
        sys.exit(1)

    if args.command:
        drive(base, " ".join(args.command))
        return

    print(f"Connected to {base}. Type a command ('exit' to quit).")
    while True:
        try:
            cmd = input("\ndrive> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not cmd or cmd.lower() in ("exit", "quit"):
            break
        try:
            drive(base, cmd)
        except Exception as exc:  # noqa: BLE001
            print(f"Error: {exc}")


if __name__ == "__main__":
    main()
