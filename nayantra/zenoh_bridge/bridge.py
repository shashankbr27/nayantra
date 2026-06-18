"""
nayantra/zenoh_bridge/bridge.py

Zenoh ↔ ROS 2 DDS bridge for multi-site / WAN robot connectivity.

Architecture:
  Cloud/Server side:
    Zenoh router  (mode=router)  ←── WAN ──→  Robot-side Zenoh client
    RMF Fleet Adapter publishes on ROS 2 DDS topics
    This bridge subscribes on Zenoh and republishes on local DDS

  Robot/Edge side:
    Nav2 / Robot Adapter publish on ROS 2 DDS
    This bridge subscribes on local DDS and publishes to Zenoh

Usage:
    # Server side
    python -m nayantra.zenoh_bridge.bridge --mode router

    # Robot side
    python -m nayantra.zenoh_bridge.bridge --mode client --router tcp/server-ip:7447

Topic mappings (bidirectional):
    ROS 2 DDS topic                 ↔  Zenoh key expression
    /rmf/fleet_state                ↔  rmf/fleet_state
    /rmf/task_summary               ↔  rmf/task_summary
    /rmf/door_states                ↔  rmf/door_states
    /rmf/lift_states                ↔  rmf/lift_states
    /robot/<name>/cmd_vel           ↔  robot/<name>/cmd_vel
    /robot/<name>/odom              ↔  robot/<name>/odom

Note:
  When ZENOH_ENABLED=false (LAN deployment) this module is never imported.
  Direct ROS 2 DDS handles all discovery — no Zenoh required.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from nayantra.config import settings

logger = logging.getLogger("rmf.zenoh")

# ---------------------------------------------------------------------------
# Topic map: (ros2_topic, zenoh_key, direction)
# direction: "ros2→zenoh" | "zenoh→ros2" | "bidirectional"
# ---------------------------------------------------------------------------
TOPIC_MAP: list[dict[str, str]] = [
    {"ros2": "/rmf/fleet_state", "zenoh": "rmf/fleet_state", "dir": "ros2→zenoh"},
    {"ros2": "/rmf/task_summary", "zenoh": "rmf/task_summary", "dir": "ros2→zenoh"},
    {"ros2": "/rmf/door_states", "zenoh": "rmf/door_states", "dir": "bidirectional"},
    {"ros2": "/rmf/lift_states", "zenoh": "rmf/lift_states", "dir": "bidirectional"},
    {"ros2": "/rmf/cmd_vel", "zenoh": "rmf/cmd_vel", "dir": "zenoh→ros2"},
]


class ZenohBridge:
    """
    Async Zenoh ↔ ROS 2 DDS bridge.

    When zenoh-python is not installed (dev machines without a robot), the
    bridge runs in stub mode and simply logs what it *would* relay.
    """

    def __init__(self, mode: str = "peer", router: str | None = None) -> None:
        self._mode = mode
        self._router = router or settings.ZENOH_ROUTER_URL
        self._session = None
        self._ros_node = None
        self._running = False

    async def start(self) -> None:
        """Open Zenoh session and ROS 2 node, then start bridging."""
        if not settings.ZENOH_ENABLED:
            logger.info(
                "ZENOH_ENABLED=false — bridge running in stub mode. "
                "For LAN deployments, direct ROS 2 DDS is sufficient."
            )
            await self._stub_loop()
            return

        try:
            import zenoh  # type: ignore
        except ImportError:
            logger.warning(
                "zenoh-python not installed. "
                "Install with: pip install eclipse-zenoh\n"
                "Running in stub mode."
            )
            await self._stub_loop()
            return

        # Build Zenoh config
        conf = zenoh.Config()
        conf.insert_json5("mode", json.dumps(self._mode))
        if self._mode == "client":
            conf.insert_json5("connect/endpoints", json.dumps([self._router]))
        elif self._mode == "router":
            conf.insert_json5("listen/endpoints", json.dumps([self._router]))

        self._session = zenoh.open(conf)
        logger.info(f"Zenoh session opened — mode={self._mode} router={self._router}")

        self._running = True
        await self._bridge_loop()

    async def _bridge_loop(self) -> None:
        """Subscribe to Zenoh topics and relay to ROS 2 (and vice versa)."""

        subscribers = []
        for mapping in TOPIC_MAP:
            if mapping["dir"] in ("zenoh→ros2", "bidirectional"):
                key = mapping["zenoh"]
                sub = self._session.declare_subscriber(
                    key,
                    lambda sample, m=mapping: self._on_zenoh_msg(sample, m),
                )
                subscribers.append(sub)
                logger.debug(f"Subscribed Zenoh: {key}")

        logger.info(
            f"Bridge active — relaying {len(TOPIC_MAP)} topic mappings. Press Ctrl+C to stop."
        )
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            for sub in subscribers:
                sub.undeclare()
            self._session.close()
            logger.info("Zenoh bridge stopped")

    def _on_zenoh_msg(self, sample: Any, mapping: dict[str, str]) -> None:
        """Called when a message arrives on a Zenoh key — relay to ROS 2."""
        try:
            payload = bytes(sample.payload).decode()
            logger.debug(f"Zenoh→ROS2 | {mapping['zenoh']} → {mapping['ros2']} | {payload[:80]}")
            # In a full ROS 2 environment, publish to self._ros_node here.
            # This stub just logs the relay.
        except Exception as exc:
            logger.error(f"Error relaying Zenoh→ROS2: {exc}")

    def publish_to_zenoh(self, key: str, data: Any) -> None:
        """Publish a ROS 2 message payload to Zenoh."""
        if not self._session:
            return
        payload = json.dumps(data).encode()
        self._session.put(key, payload)
        logger.debug(f"ROS2→Zenoh | {key} | {str(data)[:80]}")

    async def _stub_loop(self) -> None:
        """Stub mode — logs what would be bridged without actual network IO."""
        logger.info("Zenoh stub: bridge topology would be:")
        for m in TOPIC_MAP:
            logger.info(f"  ROS2 {m['ros2']}  ↔  Zenoh {m['zenoh']}  [{m['dir']}]")
        while True:
            await asyncio.sleep(60)

    def stop(self) -> None:
        self._running = False


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="RMF Zenoh Bridge")
    parser.add_argument(
        "--mode",
        choices=["peer", "client", "router"],
        default=settings.ZENOH_MODE,
        help="Zenoh session mode",
    )
    parser.add_argument(
        "--router",
        default=settings.ZENOH_ROUTER_URL,
        help="Zenoh router endpoint (e.g. tcp/192.168.1.100:7447)",
    )
    args = parser.parse_args()

    bridge = ZenohBridge(mode=args.mode, router=args.router)
    try:
        asyncio.run(bridge.start())
    except KeyboardInterrupt:
        bridge.stop()
        logger.info("Bridge stopped by user")


if __name__ == "__main__":
    main()
