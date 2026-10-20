"""Goal sequencing in Nav2Adapter, using fake ROS clients (the suite runs without ROS 2).

Found on a live Gazebo + Nav2 stack: a task counts as arrived inside ARRIVE_TOL_M, before Nav2
declares success, so the next task's goal reached Nav2 while the old one was still running and
bt_navigator rejected it ("another navigator is processing").
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from nayantra.core.adapters.nav2 import Nav2Adapter
from nayantra.core.models import Pose


class FakeFuture:
    def __init__(self, result=None, done=True, on_done=None):
        self._result, self._done, self._on_done = result, done, on_done

    def done(self):
        return self._done

    def result(self):
        return self._result

    def finish(self):
        self._done = True
        if self._on_done:
            self._on_done()


class FakeHandle:
    accepted = True

    def __init__(self, log, kind, cancel_delay):
        self.log, self.kind, self.cancel_delay = log, kind, cancel_delay

    def cancel_goal_async(self):
        self.log.append(f"cancel {self.kind}")
        fut = FakeFuture(done=False, on_done=lambda: self.log.append(f"{self.kind} cancelled"))
        asyncio.get_running_loop().call_later(self.cancel_delay, fut.finish)
        return fut

    def get_result_async(self):
        return FakeFuture(done=False)  # the goal keeps running until cancelled


class FakeClient:
    def __init__(self, log, kind, cancel_delay):
        self.log, self.kind, self.cancel_delay = log, kind, cancel_delay

    def send_goal_async(self, goal):
        self.log.append(f"send {self.kind}")
        return FakeFuture(FakeHandle(self.log, self.kind, self.cancel_delay))


class FakeGoal:
    def __init__(self):
        self.poses, self.pose = [], None


def make_adapter(log, cancel_delay=0.3) -> Nav2Adapter:
    a = object.__new__(Nav2Adapter)  # skip __init__: it needs real robot models and rclpy
    a.robot = SimpleNamespace(name="r")
    a.estop, a.online, a.paused, a.failed = False, True, False, ""
    a.path, a.path_index = [], -1
    a._goal_handle, a._goal_task, a._goal_seq, a._use_through = None, None, 0, True
    a._through = FakeClient(log, "through", cancel_delay)
    a._to_pose = FakeClient(log, "to_pose", cancel_delay)
    a._NavThrough = SimpleNamespace(Goal=FakeGoal)
    a._NavToPose = SimpleNamespace(Goal=FakeGoal)
    a._pose_stamped = lambda p: p
    return a


def test_new_path_waits_for_nav2_to_drop_the_running_goal():
    async def run():
        log: list[str] = []
        a = make_adapter(log)
        await a.follow_path([Pose(x=1, y=0), Pose(x=2, y=0)])  # NavigateThroughPoses
        await asyncio.sleep(0.2)  # goal accepted and running
        await a.follow_path([Pose(x=3, y=0)])  # next task: a single pose -> NavigateToPose
        await asyncio.sleep(0.2)
        return log

    assert asyncio.run(run()) == [
        "send through",
        "cancel through",
        "through cancelled",  # Nav2 has let go ...
        "send to_pose",  # ... only then is the next goal sent
    ]


def test_first_path_has_nothing_to_cancel():
    async def run():
        log: list[str] = []
        a = make_adapter(log)
        await a.follow_path([Pose(x=1, y=0), Pose(x=2, y=0)])
        await asyncio.sleep(0.1)
        return log

    assert asyncio.run(run()) == ["send through"]
