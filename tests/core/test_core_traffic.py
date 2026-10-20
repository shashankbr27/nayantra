"""Traffic coordination: conflict prevention, negotiation, and safe execution."""

from __future__ import annotations

import random

from core_helpers import TrafficWorld

from nayantra.core.models import ZoneCreate
from nayantra.core.routing import lane_res, node_res


def test_intersection_conflict_is_predicted_and_delayed(plus_world):
    w = plus_world
    w.add_robot("r1", "N")
    w.add_robot("r2", "W")
    a = w.route("r1", "S")
    assert a.status == "planned" and a.conflict is None
    b = w.route("r2", "E")
    assert b.status == "planned"
    assert b.conflict is not None
    assert b.conflict.kind == "intersection"
    assert b.conflict.location_name == "C"
    assert b.conflict.resolution.action == "delay"
    assert b.conflict.resolution.robot_id == "r2"
    # r2 reaches C only after r1 has left it.
    r1_at_c = a.plan.step(1).t_arrive
    r2_at_c = b.plan.step(1).t_arrive
    assert r2_at_c > r1_at_c + 1.0
    types = [e.type for e in w.bus.recent(20)]
    assert "TRAFFIC_CONFLICT_DETECTED" in types and "TRAFFIC_CONFLICT_RESOLVED" in types


def test_execution_order_follows_plan_even_if_first_robot_is_late(plus_world):
    w = plus_world
    w.add_robot("r1", "N")
    w.add_robot("r2", "W")
    w.route("r1", "S")
    w.route("r2", "E")
    # Much later than planned, r1 still hasn't moved: r2 must still wait for it.
    w.clock.advance(120)
    chk = w.traffic.check_step("r2")
    assert not chk.allowed and chk.blocker == "r1"
    assert "r1" in chk.reason
    assert w.traffic.snapshot().waits[0].holder == "r1"
    # r1 enters and clears the intersection → r2 may go.
    t = w.traffic
    assert t.check_step("r1").allowed
    t.start_step("r1", 1)
    t.complete_step("r1", 1)
    assert not t.check_step("r2").allowed  # r1 is sitting on C
    t.start_step("r1", 2)
    t.complete_step("r1", 2)
    assert t.check_step("r2").allowed


def test_head_on_in_single_corridor_waits_outside():
    nodes = {
        "W1": (-12, 2),
        "W2": (-12, -2),
        "W": (-10, 0),
        "A": (-5, 0),
        "B": (0, 0),
        "C": (5, 0),
        "E": (10, 0),
        "E1": (12, 2),
        "E2": (12, -2),
    }
    lanes = [
        ("W1", "W"),
        ("W2", "W"),
        ("W", "A"),
        ("A", "B"),
        ("B", "C"),
        ("C", "E"),
        ("E", "E1"),
        ("E", "E2"),
    ]
    w = TrafficWorld(nodes, lanes)
    w.add_robot("r1", "W1")
    w.add_robot("r2", "E2")
    a = w.route("r1", "E1")
    b = w.route("r2", "W2")
    assert a.status == b.status == "planned"
    assert b.conflict is not None and b.conflict.kind == "head_on"
    # r2 must not enter the corridor until r1 has passed through it.
    r2_enter = next(s for s in b.plan.steps if s.to_node == "C").t_depart
    r1_exit = next(s for s in a.plan.steps if s.from_node == "C").t_arrive
    assert r2_enter >= r1_exit - 1e-6


def test_parallel_close_lanes_conflict_as_crossing_resources():
    nodes = {"A": (0, 0), "B": (10, 0), "C": (0, 0.8), "D": (10, 0.8), "X": (-5, 0.4)}
    w = TrafficWorld(nodes, [("A", "B"), ("C", "D"), ("X", "A"), ("X", "C")])
    assert lane_res("L_C_D") in w.graph.conflicts[lane_res("L_A_B")]


def test_restricted_zone_blocks_lanes_unless_allowed():
    nodes = {"A": (0, 0), "B": (10, 0), "C": (5, 5)}
    zone = ZoneCreate(name="HV", type="restricted", polygon=[(4, -1), (6, -1), (6, 1), (4, 1)])
    w = TrafficWorld(nodes, [("A", "B"), ("A", "C"), ("C", "B")], zones=[zone])
    w.add_robot("r1", "A")
    res = w.route("r1", "B")
    assert res.status == "planned"
    assert res.plan.nodes == ["A", "C", "B"]  # detours around the restricted lane
    reason = w.graph.lane_block_reason(w.reg.get_lane("L_A_B"), w.traffic.profiles["r1"])
    assert "restricted" in reason


def test_idle_robot_on_destination_is_asked_to_make_way():
    nodes = {"P": (0, 0), "Q": (5, 0), "R": (10, 0), "S": (5, 5)}
    w = TrafficWorld(nodes, [("P", "Q"), ("Q", "R"), ("Q", "S")], types={"S": "parking"})
    w.add_robot("idle", "R")
    w.add_robot("r1", "P")
    calls = []
    w.traffic.make_way_handler = lambda rid, target, why: calls.append((rid, target, why)) or True
    res = w.route("r1", "R")
    assert res.status == "blocked" and res.retry
    assert calls and calls[0][0] == "idle" and calls[0][1] == "S"
    assert "make way" in res.reason
    # The idle robot moves aside; the request then succeeds.
    assert w.route("idle", "S").status == "planned"
    w.drive("idle")
    assert w.route("r1", "R").status == "planned"


def test_busy_robot_blocking_is_waited_for_not_moved():
    nodes = {"P": (0, 0), "Q": (5, 0), "R": (10, 0)}
    w = TrafficWorld(nodes, [("P", "Q"), ("Q", "R")])
    w.add_robot("worker", "Q")
    w.traffic.set_busy("worker", True)
    w.add_robot("r1", "P")
    w.traffic.make_way_handler = lambda *a: (_ for _ in ()).throw(AssertionError("must not move"))
    res = w.route("r1", "R")
    assert res.status == "blocked" and "worker" in res.reason


def test_swap_between_two_busy_robots_is_planned_together():
    # r1 wants r2's spot and vice versa; a side bay makes the swap possible.
    nodes = {"A": (0, 0), "B": (5, 0), "C": (10, 0), "BAY": (5, 4)}
    w = TrafficWorld(nodes, [("A", "B"), ("B", "C"), ("B", "BAY")])
    w.add_robot("r1", "A")
    w.add_robot("r2", "C")
    w.traffic.set_busy("r1", True)
    w.traffic.set_busy("r2", True)
    first = w.route("r1", "C")
    assert first.status == "blocked"  # r2 is on the destination
    second = w.route("r2", "A")  # r2's own destination is held by r1 → negotiate
    assert second.status == "planned"
    assert "r1" in w.traffic.plans and "r2" in w.traffic.plans
    visited = w.traffic.plans["r1"].nodes + w.traffic.plans["r2"].nodes
    assert "BAY" in visited  # one of them pulls into the bay to let the other pass


def test_higher_priority_robot_displaces_lower_priority_plan(plus_world):
    w = plus_world
    w.add_robot("low", "N", priority=2)
    w.add_robot("high", "W", priority=8)
    low = w.route("low", "S")
    low_eta = low.plan.eta
    high = w.route("high", "E")
    assert high.status == "planned"
    assert high.conflict is not None and high.conflict.resolution.action == "priority"
    new_low = w.traffic.plans["low"]
    assert new_low.eta > low_eta  # low now waits for high
    high_at_c = high.plan.step(1).t_arrive
    low_at_c = new_low.step(1).t_arrive
    assert low_at_c > high_at_c


def test_parking_on_a_planned_path_forces_replan(plus_world):
    w = plus_world
    w.add_robot("r1", "N")
    w.route("r1", "S")
    w.add_robot("r2", "E")
    w.traffic.freeze("r2")  # sits on nothing r1 needs → no replan
    assert "r1" not in w.traffic.needs_replan
    w.traffic.set_parked("r2", "m", "C")  # now blocks r1's future
    assert "r1" in w.traffic.needs_replan
    assert w.traffic.check_step("r1").replan


# ---------------------------------------------------------------------------
# Randomised execution: many robots, random delays → no physical conflict and
# everyone arrives (the ADG execution guarantee).
# ---------------------------------------------------------------------------


def _grid_world(seed: int) -> TrafficWorld:
    rng = random.Random(seed)
    nodes, lanes = {}, []
    size = 5
    for i in range(size):
        for j in range(size):
            nodes[f"n{i}{j}"] = (i * 4.0, j * 4.0)
    for i in range(size):
        for j in range(size):
            if i + 1 < size and rng.random() > 0.15:
                lanes.append((f"n{i}{j}", f"n{i + 1}{j}"))
            if j + 1 < size and rng.random() > 0.15:
                lanes.append((f"n{i}{j}", f"n{i}{j + 1}"))
    # parking bays hanging off the border
    for k in range(6):
        nid = f"bay{k}"
        nodes[nid] = (-3.0, k * 3.0)
        lanes.append((nid, f"n0{min(k, size - 1)}"))
    return TrafficWorld(nodes, lanes, types={f"bay{k}": "parking" for k in range(6)})


def _physical(w: TrafficWorld, rid: str, state: dict) -> set[str]:
    plan = w.traffic.plans.get(rid)
    if plan is None:
        node, lane = w.traffic.location(rid)
        return {node_res(node)} if node else set()
    if plan.moving:
        s = plan.step(plan.started)
        return {lane_res(s.lane_id), node_res(s.from_node), node_res(s.to_node)}
    return {node_res(plan.nodes[plan.progress])}


def _run_random(seed: int, n_robots: int = 7) -> None:
    rng = random.Random(seed)
    w = _grid_world(seed)
    graph = w.graph
    grid = [n for n in graph.nodes if n.startswith("n")]
    starts = rng.sample(grid, n_robots)
    free = [n for n in grid if n not in starts]
    state = {}
    for i, s in enumerate(starts):
        rid = f"r{i}"
        w.add_robot(rid, s, speed=rng.uniform(0.6, 1.4), priority=rng.randint(3, 7))
        reachable = [
            n for n in free if graph.plan_static(s, n, w.traffic.profiles[rid]) is not None
        ]
        goal = rng.choice(reachable) if reachable else s
        if goal in free:
            free.remove(goal)
        state[rid] = {"goal": goal, "phase": "route", "remaining": 0.0}

    def make_way(rid, target, why):
        state[rid]["goal"] = target
        state[rid]["phase"] = "route"
        w.traffic.set_busy(rid, True)  # the fleet executor marks make-way robots busy
        return True

    w.traffic.make_way_handler = make_way
    dt = 0.2
    for _ in range(6000):
        order = list(state)
        rng.shuffle(order)
        for rid in order:
            st = state[rid]
            t = w.traffic
            if st["phase"] == "route":
                node, _ = t.location(rid)
                res = t.request_route(rid, "m", node, st["goal"])
                if res.status == "planned":
                    st["phase"] = "exec"
                    if not res.plan.steps:
                        t.finish_route(rid)
                        t.set_busy(rid, False)
                        st["phase"] = "done"
                assert res.status != "unreachable", res.reason
                continue
            if st["phase"] != "exec":
                continue
            plan = t.plans.get(rid)
            if plan is None:
                st["phase"] = "route"
                continue
            if plan.moving:
                st["remaining"] -= dt * rng.uniform(0.0, 1.2)  # random stalls
                if st["remaining"] <= 0:
                    t.complete_step(rid, plan.started)
                continue
            if plan.finished:
                t.finish_route(rid)
                t.set_busy(rid, False)
                st["phase"] = "done"
                continue
            chk = t.check_step(rid)
            if chk.allowed:
                k = plan.started + 1
                t.start_step(rid, k)
                step = plan.step(k)
                st["remaining"] = step.t_arrive - step.t_depart
            elif chk.replan:
                t.freeze(rid)
                st["phase"] = "route"
        w.traffic.tick()
        # Safety: no two robots physically on conflicting resources.
        held = {rid: _physical(w, rid, state) for rid in state}
        for a in state:
            for b in state:
                if a >= b:
                    continue
                for ra in held[a]:
                    clash = graph.expand(ra) & held[b]
                    assert not clash, f"seed {seed}: {a} and {b} both on {ra}/{clash}"
        w.clock.advance(dt)
        if all(st["phase"] == "done" for st in state.values()):
            break
    stuck = {rid: st for rid, st in state.items() if st["phase"] != "done"}
    assert not stuck, f"seed {seed}: robots never arrived: {stuck}"


def test_random_execution_is_collision_and_deadlock_free():
    for seed in range(40):
        _run_random(seed)
