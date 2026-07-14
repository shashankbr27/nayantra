#!/usr/bin/env python3
"""
scripts/nav_cli.py — natural-language driver for Carter via Nav2.

Type a plain-English command; Gemini (if configured) maps it to one or more named
warehouse waypoints; each is sent to Nav2 as a NavigateToPose goal, so Carter
plans a path and drives there — watch it live in the Isaac WebRTC stream.

This is the direct CLI -> Nav2 path, the most reliable way to first prove the
navigation works. The full "NL -> agent -> MCP -> RMF bridge -> Nav2" stack drives
the *same* NavigateToPose goals (via nayantra.rmf_bridge); once this works we flip
ROS2_ENABLED=true on the bridge and route NL through the agent instead.

Run on the box (ROS 2 sourced, same ROS_DOMAIN_ID as Isaac + Nav2):
    python3 scripts/nav_cli.py "take carter to the loading dock"
    python3 scripts/nav_cli.py zone_a          # exact waypoint name (no LLM needed)
    python3 scripts/nav_cli.py 3 -2            # raw x y (metres, map frame)
    python3 scripts/nav_cli.py                 # interactive REPL

Env:
    GEMINI_API_KEY   enable NL mapping (pip install --user google-genai). Without
                     it, exact waypoint names and raw "x y" still work.
    GEMINI_MODEL     default gemini-2.5-flash
    WAYPOINTS_FILE   optional JSON {"name":[x,y,yaw], ...} overriding the built-ins
    GOAL_FRAME       default 'map'
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

# Built-in warehouse waypoints (x, y, yaw) in metres, map frame. Carter spawns at
# (0,0). Tune these to the real warehouse layout once basic motion is confirmed.
WAYPOINTS: dict[str, tuple[float, float, float]] = {
    "center": (0.0, 0.0, 0.0),
    "zone_a": (3.0, 0.0, 0.0),
    "zone_b": (-3.0, 0.0, math.pi),
    "loading_dock": (5.0, -2.0, 0.0),
    "shelf_1": (4.0, 3.0, 0.0),
    "shelf_2": (-4.0, 3.0, 0.0),
    "charging_dock": (-5.0, -2.0, math.pi),
    "entrance": (-6.0, 0.0, 0.0),
}


def load_waypoints() -> dict[str, tuple]:
    path = os.getenv("WAYPOINTS_FILE", "")
    if path and Path(path).is_file():
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            return {k.lower(): tuple(v) for k, v in raw.items()}
        except Exception as exc:  # noqa: BLE001
            print(f"(bad WAYPOINTS_FILE: {exc}; using built-ins)")
    return dict(WAYPOINTS)


def plan(command: str, wps: dict) -> list:
    """NL -> ordered goals. Each goal is a waypoint name (str) or [x, y] /
    [x, y, yaw] raw map coordinates, so destinations do NOT have to be
    hardcoded waypoints. Gemini if available; else substring match on names."""
    names = list(wps)
    key = os.getenv("GEMINI_API_KEY")
    if key:
        try:
            from google import genai

            client = genai.Client(api_key=key)
            prompt = (
                "You route a warehouse robot on a map in metres. Known named "
                f"waypoints (name -> [x, y, yaw]): {json.dumps({k: list(v) for k, v in wps.items()})}. "
                "Map the command to an ordered JSON array of goals. Each goal is "
                "either one of those waypoint names (preferred when it matches) "
                "or an [x, y] coordinate pair for destinations the names don't "
                'cover (e.g. "2 metres east of the loading dock" -> offset the '
                "waypoint's coordinates; +x is east, +y is north). "
                f'Command: "{command}". Return ONLY the JSON array, no prose.'
            )
            resp = client.models.generate_content(
                model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
                contents=prompt,
                config={"temperature": 0},
            )
            text = (resp.text or "").strip().strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
            seq = []
            for g in json.loads(text):
                if isinstance(g, str) and g in wps:
                    seq.append(g)
                elif (
                    isinstance(g, (list, tuple))
                    and len(g) in (2, 3)
                    and all(isinstance(v, (int, float)) for v in g)
                ):
                    seq.append([float(v) for v in g])
            if seq:
                return seq
        except Exception as exc:  # noqa: BLE001
            print(f"(Gemini unavailable: {exc}; falling back to name match)")
    low = command.lower()
    return [w for w in names if w in low or w.replace("_", " ") in low]


def main() -> None:
    import rclpy
    from geometry_msgs.msg import PoseStamped
    from nav2_msgs.action import NavigateToPose
    from rclpy.action import ActionClient

    wps = load_waypoints()
    frame = os.getenv("GOAL_FRAME", "map")
    rclpy.init()
    node = rclpy.create_node("nav_cli")
    client = ActionClient(node, NavigateToPose, "navigate_to_pose")

    print("Waiting for Nav2 action server /navigate_to_pose ...")
    if not client.wait_for_server(timeout_sec=15.0):
        print("ERROR: Nav2 action server not available — is nav2_demo.launch.py running?")
        node.destroy_node()
        rclpy.shutdown()
        return

    def goto_xy(x: float, y: float, yaw: float, label: str) -> bool:
        goal = NavigateToPose.Goal()
        ps = PoseStamped()
        ps.header.frame_id = frame
        ps.header.stamp = node.get_clock().now().to_msg()
        ps.pose.position.x = float(x)
        ps.pose.position.y = float(y)
        ps.pose.orientation.z = math.sin(yaw / 2.0)
        ps.pose.orientation.w = math.cos(yaw / 2.0)
        goal.pose = ps
        print(f"  -> {label} ({x}, {y})  [Nav2 NavigateToPose, frame={frame}]")
        send = client.send_goal_async(goal)
        rclpy.spin_until_future_complete(node, send)
        handle = send.result()
        if not handle or not handle.accepted:
            print("     goal REJECTED by Nav2")
            return False
        result_future = handle.get_result_async()
        rclpy.spin_until_future_complete(node, result_future)
        status = result_future.result().status
        ok = status == 4  # GoalStatus.STATUS_SUCCEEDED
        print(f"     {'ARRIVED' if ok else f'ended (status={status})'}")
        return ok

    def run(command: str) -> None:
        parts = command.split()
        # raw "x y [yaw]"
        if parts and all(_is_num(p) for p in parts[:2]):
            x, y = float(parts[0]), float(parts[1])
            yaw = float(parts[2]) if len(parts) > 2 and _is_num(parts[2]) else 0.0
            goto_xy(x, y, yaw, "xy")
            return
        # exact waypoint name
        keyname = command.strip().lower().replace(" ", "_")
        seq = [keyname] if keyname in wps else plan(command, wps)
        if not seq:
            print("No destination matched. Known waypoints:", ", ".join(wps))
            return
        labels = [g if isinstance(g, str) else f"({g[0]:g}, {g[1]:g})" for g in seq]
        print("Plan:", " -> ".join(labels))
        for goal, label in zip(seq, labels, strict=True):
            coords = wps[goal] if isinstance(goal, str) else tuple(goal)
            x, y, yaw = coords if len(coords) == 3 else (*coords, 0.0)
            if not goto_xy(x, y, yaw, label):
                break

    args = sys.argv[1:]
    if args:
        run(" ".join(args))
    else:
        print("Connected to Nav2. Waypoints:", ", ".join(wps))
        print(
            "Type a command ('exit' to quit). Examples: 'go to the loading dock', 'zone_a', '3 -2'"
        )
        while True:
            try:
                cmd = input("\nnav> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not cmd or cmd.lower() in ("exit", "quit"):
                break
            try:
                run(cmd)
            except Exception as exc:  # noqa: BLE001
                print("Error:", exc)

    node.destroy_node()
    rclpy.shutdown()


def _is_num(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


if __name__ == "__main__":
    main()
