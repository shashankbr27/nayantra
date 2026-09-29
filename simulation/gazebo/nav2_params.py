"""
simulation/gazebo/nav2_params.py — per-robot Nav2 parameters for the Gazebo fleet.

config/nav2/carter_nav2.yaml is written for one robot with global topic names
(/odom, /scan). Each Gazebo robot runs its own Nav2 stack under a namespace, so
this derives a per-robot file from it:

  * topics become relative ("odom", "scan"), so they resolve under /<ns>/
  * every node's parameters move under the namespace
  * use_sim_time is set on every node (Gazebo publishes /clock)

Kept free of ROS imports so it is unit-testable anywhere PyYAML is installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SOURCE = REPO / "config" / "nav2" / "carter_nav2.yaml"

#: keys whose absolute value is rewritten to a relative topic name
_TOPIC_KEYS = {"odom_topic", "topic"}


def _relative(value: Any) -> Any:
    return value.lstrip("/") if isinstance(value, str) and value.startswith("/") else value


def _walk(node: Any, use_sim_time: bool) -> Any:
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key in _TOPIC_KEYS:
                out[key] = _relative(value)
            elif key == "ros__parameters" and isinstance(value, dict):
                params = _walk(value, use_sim_time)
                params["use_sim_time"] = use_sim_time
                out[key] = params
            else:
                out[key] = _walk(value, use_sim_time)
        return out
    if isinstance(node, list):
        return [_walk(v, use_sim_time) for v in node]
    return node


def namespaced_params(namespace: str, use_sim_time: bool = True, source: Path = SOURCE) -> dict:
    """The Nav2 parameter tree for one robot, keyed under `/<namespace>`."""
    ns = namespace.strip("/")
    if not ns:
        raise ValueError("a namespace is required (one Nav2 stack per robot)")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    return {f"/{ns}": _walk(data, use_sim_time)}


def write_params(namespace: str, out_dir: Path, use_sim_time: bool = True) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"nav2_{namespace.strip('/')}.yaml"
    path.write_text(
        yaml.safe_dump(namespaced_params(namespace, use_sim_time), sort_keys=False),
        encoding="utf-8",
    )
    return path
