"""
The Gazebo simulation assets are checked here without Gazebo or ROS 2: the world
must match the map, the robot model must match the scenario, and the map must be
drivable by the robots that will spawn in it. Whether Gazebo itself accepts the
SDF is only verifiable on a machine with Gazebo (scripts/run_gazebo.sh check).
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
GZ = REPO / "simulation" / "gazebo"
sys.path.insert(0, str(GZ))

import generate_world  # noqa: E402
import nav2_params  # noqa: E402
import robots  # noqa: E402

MAP = json.loads((REPO / "config" / "maps" / "gazebo_warehouse.json").read_text(encoding="utf-8"))
SCENARIO = json.loads(
    (REPO / "config" / "scenarios" / "gazebo_warehouse.json").read_text(encoding="utf-8")
)
ROBOT_RADIUS = 0.45  # the footprint radius Nav2 and the fleet config assume


def test_committed_world_matches_the_map():
    assert generate_world.main(["--check"]) == 0


def test_world_is_well_formed_and_has_every_wall_and_shelf():
    root = ET.parse(GZ / "worlds" / "nayantra_warehouse.sdf").getroot()
    world = root.find("world")
    assert world is not None and world.get("name") == "nayantra_warehouse"
    names = {m.get("name") for m in world.findall("model")}
    expected = 1 + sum(len(line) - 1 for line in MAP["walls"]) + len(MAP["obstacles"])
    assert len(names) == expected
    assert {"ground", "wall_1", "shelf_1"} <= names


def test_robot_model_is_namespaced_and_bridged_topics_agree():
    for name, *_ in robots.parse_robots(robots.DEFAULT_ROBOTS):
        text = robots.render_model(name)
        assert "@NAME@" not in text
        model = ET.fromstring(text).find("model")
        assert model is not None and model.get("name") == name
        topics = {t.text for t in model.iter() if t.tag in ("topic", "odom_topic", "tf_topic")}
        for bridged in robots.bridge_args(name):
            topic = bridged.split("@")[0]
            assert topic in topics, f"{topic} is bridged but no Gazebo plugin/sensor publishes it"


def test_scenario_robots_match_the_gazebo_fleet():
    spawned = {name: (x, y) for name, x, y, _ in robots.parse_robots(robots.DEFAULT_ROBOTS)}
    waypoints = {w["id"]: (w["x"], w["y"]) for w in MAP["waypoints"]}
    assert {r["id"] for r in SCENARIO["robots"]} == set(spawned)
    for r in SCENARIO["robots"]:
        assert r["navigation"]["namespace"] == r["id"]
        assert r["navigation"]["map_id"] == MAP["id"]
        assert spawned[r["id"]] == waypoints[r["spawn"]["waypoint_id"]]


def _inflated_rects():
    for poly in MAP["obstacles"]:
        x0, x1, y0, y1 = generate_world.axis_aligned_rect(poly)
        yield x0 - ROBOT_RADIUS, x1 + ROBOT_RADIUS, y0 - ROBOT_RADIUS, y1 + ROBOT_RADIUS


def _segment_hits_rect(p, q, rect) -> bool:
    """Liang-Barsky clip of segment p-q against an axis-aligned rectangle."""
    x0, x1, y0, y1 = rect
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for pk, qk in ((-dx, p[0] - x0), (dx, x1 - p[0]), (-dy, p[1] - y0), (dy, y1 - p[1])):
        if pk == 0:
            if qk < 0:
                return False
            continue
        t = qk / pk
        if pk < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def test_waypoints_are_inside_the_walls_and_clear_of_shelves():
    b = MAP["bounds"]
    for w in MAP["waypoints"]:
        assert b["min_x"] + ROBOT_RADIUS <= w["x"] <= b["max_x"] - ROBOT_RADIUS, w["id"]
        assert b["min_y"] + ROBOT_RADIUS <= w["y"] <= b["max_y"] - ROBOT_RADIUS, w["id"]
        for x0, x1, y0, y1 in _inflated_rects():
            assert not (x0 < w["x"] < x1 and y0 < w["y"] < y1), f"{w['id']} is inside a shelf"


def test_every_lane_is_drivable_for_the_robot_footprint():
    wp = {w["id"]: (w["x"], w["y"]) for w in MAP["waypoints"]}
    for lane in MAP["lanes"]:
        for rect in _inflated_rects():
            assert not _segment_hits_rect(wp[lane["from_id"]], wp[lane["to_id"]], rect), lane["id"]


def test_nav_graph_is_connected():
    adj: dict[str, set[str]] = {w["id"]: set() for w in MAP["waypoints"]}
    for lane in MAP["lanes"]:
        adj[lane["from_id"]].add(lane["to_id"])
        adj[lane["to_id"]].add(lane["from_id"])
    seen, todo = set(), [next(iter(adj))]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo.extend(adj[n] - seen)
    assert seen == set(adj)


def test_nav2_params_are_namespaced_relative_and_sim_timed():
    tree = nav2_params.namespaced_params("ugv_01")
    assert list(tree) == ["/ugv_01"]
    nodes = tree["/ugv_01"]
    assert nodes["bt_navigator"]["ros__parameters"]["odom_topic"] == "odom"
    scan = nodes["local_costmap"]["local_costmap"]["ros__parameters"]["obstacle_layer"]["scan"]
    assert scan["topic"] == "scan"

    def params(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "ros__parameters":
                    yield v
                else:
                    yield from params(v)

    assert all(p["use_sim_time"] is True for p in params(nodes))
    with pytest.raises(ValueError):
        nav2_params.namespaced_params("")


def test_launch_file_compiles():
    compile(
        (GZ / "launch" / "gazebo_warehouse.launch.py").read_text(encoding="utf-8"), "launch", "exec"
    )


def test_core_loads_the_gazebo_scenario():
    from nayantra.core.runtime import NayantraCore
    from nayantra.core.server import create_app

    core = NayantraCore(db_path=":memory:", scenario="gazebo_warehouse")
    with TestClient(create_app(core)) as c:
        rs = c.get("/api/v1/robots").json()
        assert {r["id"] for r in rs} == {"ugv_01", "ugv_02"}
        m = c.get("/api/v1/maps/gazebo_warehouse")
        assert m.status_code == 200
        assert len(m.json()["waypoints"]) == len(MAP["waypoints"])
