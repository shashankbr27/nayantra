"""Small 2D geometry helpers (metres, map frame)."""

from __future__ import annotations

import math
from collections.abc import Sequence

Pt = Sequence[float]


def dist(a: Pt, b: Pt) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def angle_wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def point_in_polygon(p: Pt, poly: Sequence[Pt]) -> bool:
    x, y = p[0], p[1]
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i][0], poly[i][1]
        xj, yj = poly[j][0], poly[j][1]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def _orient(a: Pt, b: Pt, c: Pt) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Pt, b: Pt, p: Pt, eps: float = 1e-9) -> bool:
    return (
        min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    )


def segments_intersect(p1: Pt, p2: Pt, q1: Pt, q2: Pt) -> bool:
    d1, d2 = _orient(q1, q2, p1), _orient(q1, q2, p2)
    d3, d4 = _orient(p1, p2, q1), _orient(p1, p2, q2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 * d2 < 0 and d3 * d4 < 0:
        return True
    eps = 1e-12
    return (
        (abs(d1) < eps and _on_segment(q1, q2, p1))
        or (abs(d2) < eps and _on_segment(q1, q2, p2))
        or (abs(d3) < eps and _on_segment(p1, p2, q1))
        or (abs(d4) < eps and _on_segment(p1, p2, q2))
    )


def point_segment_distance(p: Pt, a: Pt, b: Pt) -> float:
    return dist(p, project_on_segment(p, a, b)[1])


def project_on_segment(p: Pt, a: Pt, b: Pt) -> tuple[float, tuple[float, float]]:
    """Return (t in [0,1], closest point) of p onto segment ab."""
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    denom = dx * dx + dy * dy
    if denom == 0:
        return 0.0, (ax, ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / denom))
    return t, (ax + t * dx, ay + t * dy)


def segment_segment_distance(a: Pt, b: Pt, c: Pt, d: Pt) -> float:
    if segments_intersect(a, b, c, d):
        return 0.0
    return min(
        point_segment_distance(a, c, d),
        point_segment_distance(b, c, d),
        point_segment_distance(c, a, b),
        point_segment_distance(d, a, b),
    )


def segment_intersects_polygon(a: Pt, b: Pt, poly: Sequence[Pt]) -> bool:
    """True if segment ab enters the polygon (crosses an edge or lies inside)."""
    if point_in_polygon(a, poly) or point_in_polygon(b, poly):
        return True
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    if point_in_polygon(mid, poly):
        return True
    n = len(poly)
    return any(segments_intersect(a, b, poly[i], poly[(i + 1) % n]) for i in range(n))


def polyline_length(points: Sequence[Pt]) -> float:
    return sum(dist(points[i], points[i + 1]) for i in range(len(points) - 1))


def point_along(points: Sequence[Pt], s: float) -> tuple[float, float]:
    """Point at arc length s along a polyline (clamped)."""
    if not points:
        return (0.0, 0.0)
    for i in range(len(points) - 1):
        seg = dist(points[i], points[i + 1])
        if s <= seg or i == len(points) - 2:
            t = 0.0 if seg == 0 else min(1.0, max(0.0, s / seg))
            return (
                points[i][0] + t * (points[i + 1][0] - points[i][0]),
                points[i][1] + t * (points[i + 1][1] - points[i][1]),
            )
        s -= seg
    return (points[-1][0], points[-1][1])
