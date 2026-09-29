"""Fixtures for the Nayantra Core tests (helpers live in core_helpers.py)."""

from __future__ import annotations

import pytest
from core_helpers import TrafficWorld


@pytest.fixture
def plus_world():
    """Plus-shaped intersection: N/S/E/W arms meeting at C, 10 m each."""
    nodes = {"N": (0, 10), "S": (0, -10), "W": (-10, 0), "E": (10, 0), "C": (0, 0)}
    lanes = [("N", "C"), ("C", "S"), ("W", "C"), ("C", "E")]
    return TrafficWorld(nodes, lanes)
