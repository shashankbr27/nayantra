"""nayantra.sh: the one-command launcher. Its runtime behaviour needs WSL/ROS 2/Gazebo; these
checks cover what can break without them."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "nayantra.sh"


def test_launcher_has_unix_line_endings():
    # A CRLF checkout (git autocrlf on Windows) breaks the shebang inside WSL.
    assert b"\r" not in SCRIPT.read_bytes()


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")
def test_launcher_parses():
    subprocess.run(["bash", "-n"], input=SCRIPT.read_bytes(), check=True)


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")
def test_launcher_usage_lists_every_command():
    out = subprocess.run(
        ["bash", str(SCRIPT), "--help"], capture_output=True, text=True, check=True
    ).stdout
    for command in ("setup", "detect", "start", "stop", "gui", "restart", "status", "logs", "doctor"):
        assert command in out
    assert "--sim" in out and "isaac" in out and "gazebo" in out


def test_windows_shim_calls_the_launcher():
    shim = (ROOT / "nayantra.cmd").read_text()
    assert "nayantra.sh" in shim


# --------------------------------------------------------------------------- choosing the simulator
# The launcher's decisions, played against described machines (NAYANTRA_DETECT_FROM) in a sandbox home,
# so they need no GPU and never touch a real install. `--dry-run` stops before anything is installed.
needs_bash = pytest.mark.skipif(sys.platform == "win32" or shutil.which("bash") is None, reason="needs bash")

WORKSTATION = {"system": "linux", "os_name": "Ubuntu 24.04.2 LTS", "ubuntu": "noble", "wsl": False, "ram_gb": 62.0,
               "cores": 32, "disk_free_gb": 800.0, "disk_path": "/home",
               "gpus": [{"name": "NVIDIA RTX 6000 Ada Generation", "vram_gb": 47.99, "driver": "580.65.06"}]}
LAPTOP = {**WORKSTATION, "os_name": "Ubuntu 24.04.5 LTS", "wsl": True, "ram_gb": 11.0, "cores": 12,
          "gpus": [{"name": "NVIDIA GeForce RTX 3050 6GB Laptop GPU", "vram_gb": 6.0, "driver": "581.86"}]}
MIDRANGE = {**WORKSTATION, "ram_gb": 31.0, "gpus": [{"name": "NVIDIA GeForce RTX 3070", "vram_gb": 8.0, "driver": "580.65.06"}]}


@pytest.fixture
def sandbox(tmp_path):
    """An environment whose home, state and Isaac Sim directory are empty and private."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("NAYANTRA_SIM", "NAYANTRA_ISAAC_ENV", "NAYANTRA_DETECT_FROM", "NAYANTRA_PUBLIC_IP", "PUBLIC_IP")}
    env.update(HOME=str(tmp_path), NAYANTRA_HOME=str(tmp_path / "state"), NAYANTRA_ISAAC_DIR=str(tmp_path / "isaac"))
    return env


def pretending_to_be(sandbox, machine: dict) -> dict:
    path = Path(sandbox["HOME"]) / "machine.json"
    path.write_text(json.dumps(machine), encoding="utf-8")
    return {**sandbox, "NAYANTRA_DETECT_FROM": str(path)}


def launcher(sandbox, machine, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True,
                          env=pretending_to_be(sandbox, machine), stdin=subprocess.DEVNULL)


def in_terminal(sandbox, machine, answers, *args):
    """Run the launcher on a pseudo-terminal and type `answers` at its prompts, one per prompt."""
    import pty
    import select

    env = pretending_to_be(sandbox, machine)
    master, slave = pty.openpty()
    proc = subprocess.Popen(["bash", str(SCRIPT), *args], stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True)
    os.close(slave)
    out, scan, pending, deadline = b"", 0, list(answers), time.time() + 60
    while time.time() < deadline:
        if select.select([master], [], [], 0.2)[0]:
            try:
                chunk = os.read(master, 4096)
            except OSError:  # the child has closed its end
                break
            if not chunk:
                break
            out += chunk
            if pending and re.search(rb"Choose 1-3|\[Y/n\]", out[scan:]):
                os.write(master, pending.pop(0).encode())
                scan = len(out)
        elif proc.poll() is not None:
            break
    else:
        proc.kill()
        raise AssertionError(f"launcher did not finish:\n{out.decode(errors='replace')}")
    os.close(master)
    return proc.wait(), out.decode(errors="replace")


@needs_bash
def test_auto_on_a_laptop_picks_gazebo(sandbox):
    r = launcher(sandbox, LAPTOP, "setup", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "simulator   Gazebo" in r.stdout and "nothing was changed" in r.stdout


@needs_bash
def test_auto_on_a_workstation_will_not_start_a_big_download_without_a_yes(sandbox):
    r = launcher(sandbox, WORKSTATION, "setup", "--dry-run")
    assert r.returncode != 0
    assert "--yes" in r.stderr and "Plan" not in r.stdout
    r = launcher(sandbox, WORKSTATION, "setup", "--dry-run", "--yes")
    assert r.returncode == 0, r.stderr
    assert "simulator   Isaac Sim" in r.stdout and "NVIDIA's pip index" in r.stdout and "EULA" in r.stdout


@needs_bash
def test_explicit_isaac_on_a_capable_machine_needs_no_confirmation(sandbox):
    r = launcher(sandbox, WORKSTATION, "setup", "--sim", "isaac", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "simulator   Isaac Sim" in r.stdout


@needs_bash
def test_explicit_isaac_is_refused_where_it_cannot_run(sandbox):
    r = launcher(sandbox, LAPTOP, "setup", "--sim", "isaac", "--dry-run")
    assert r.returncode != 0
    assert "WSL2" in r.stderr and "cannot run on this machine" in r.stderr and "--sim gazebo" in r.stderr
    assert "Plan" not in r.stdout


@needs_bash
def test_explicit_isaac_below_the_minimum_warns_and_goes_ahead_when_nobody_can_be_asked(sandbox):
    r = launcher(sandbox, MIDRANGE, "setup", "--sim", "isaac", "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "possible, with caveats" in r.stderr and "simulator   Isaac Sim" in r.stdout


@needs_bash
def test_explicit_gazebo_skips_the_judgement(sandbox):
    r = launcher(sandbox, WORKSTATION, "setup", "--sim", "gazebo", "--dry-run")
    assert r.returncode == 0 and "simulator   Gazebo" in r.stdout


@needs_bash
def test_the_environment_can_choose_the_simulator(sandbox):
    sandbox["NAYANTRA_SIM"] = "gazebo"
    r = launcher(sandbox, WORKSTATION, "setup", "--dry-run")
    assert r.returncode == 0 and "simulator   Gazebo" in r.stdout


@needs_bash
def test_bad_options_are_rejected(sandbox):
    assert launcher(sandbox, LAPTOP, "setup", "--sim", "mujoco", "--dry-run").returncode != 0
    assert launcher(sandbox, LAPTOP, "setup", "--bogus").returncode != 0
    assert launcher(sandbox, LAPTOP, "start", "--sim", "auto").returncode != 0


@needs_bash
def test_an_existing_isaac_install_is_reused_not_downloaded_again(sandbox, tmp_path):
    py = tmp_path / "isaac" / "isaacsim-env" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("#!/bin/sh\nexit 0\n")  # `import isaacsim` check passes
    py.chmod(0o755)
    r = launcher(sandbox, WORKSTATION, "setup", "--dry-run")  # no --yes: nothing to download, so nothing to ask
    assert r.returncode == 0, r.stderr
    assert "no download" in r.stdout and str(py.parent.parent) in r.stdout


@needs_bash
def test_detect_command_reports_for_the_machine(sandbox):
    r = launcher(sandbox, WORKSTATION, "detect", "--brief")
    assert r.stdout.split() == ["isaac", "ok", "ok"]
    r = launcher(sandbox, LAPTOP, "detect")
    assert "Recommended: Gazebo" in r.stdout and "WSL2" in r.stdout


@needs_bash
def test_menu_enter_takes_auto(sandbox):
    code, out = in_terminal(sandbox, LAPTOP, ["\n"], "setup", "--dry-run")
    assert code == 0, out
    assert "[not possible on this machine]" in out, "the menu must say Isaac Sim is out"
    assert "simulator   Gazebo" in out


@needs_bash
def test_menu_picks_isaac_after_a_typo(sandbox):
    code, out = in_terminal(sandbox, WORKSTATION, ["9\n", "3\n"], "setup", "--dry-run")
    assert code == 0, out
    assert "Please type 1, 2 or 3" in out and "simulator   Isaac Sim" in out


@needs_bash
def test_menu_picks_gazebo_even_where_isaac_would_be_recommended(sandbox):
    code, out = in_terminal(sandbox, WORKSTATION, ["2\n"], "setup", "--dry-run")
    assert code == 0, out
    assert "it would pick: Isaac Sim" in out and "simulator   Gazebo" in out


@needs_bash
def test_menu_auto_on_a_workstation_asks_before_the_big_download(sandbox):
    code, out = in_terminal(sandbox, WORKSTATION, ["1\n", "n\n"], "setup", "--dry-run")
    assert code != 0 and "Install it?" in out and "Plan" not in out
    code, out = in_terminal(sandbox, WORKSTATION, ["1\n", "\n"], "setup", "--dry-run")  # Enter means yes
    assert code == 0 and "simulator   Isaac Sim" in out


@needs_bash
def test_menu_is_skipped_when_the_simulator_is_given(sandbox):
    code, out = in_terminal(sandbox, LAPTOP, [], "setup", "--sim", "gazebo", "--dry-run")
    assert code == 0 and "Which simulator" not in out


# --------------------------------------------------------------------------- starting and stopping
def bash_with(sandbox, body: str):
    """Source the launcher (functions only) and run `body`, usually with the process control stubbed out."""
    script = f'cd "{ROOT}"; source ./nayantra.sh; LOGS="$HOME/logs"; PIDS="$HOME/pids"; mkdir -p "$LOGS" "$PIDS"\n{body}'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=sandbox, stdin=subprocess.DEVNULL)


ISAAC_STUBS = r"""
IN_WSL=0; BACKEND=isaac; SIM_SVC=isaac; tries=0
isaac_env() { echo /opt/isaacsim-env; }
ros2() { return 0; }
host_ip() { echo 10.1.2.3; }
spawn() { echo "spawn $*"; }
kill_service() { echo "kill $1"; }
cmd_stop() { echo "cmd_stop"; }
wait_log() { echo "wait_log $*"; return ${LOG_RC:-0}; }
wait_nav2() { tries=$((tries + 1)); echo "wait_nav2 $tries"; [ "$tries" -ge "${NAV2_OK_AT:-1}" ]; }
"""


@needs_bash
def test_isaac_starts_in_order_and_hands_the_recipe_to_run_demo(sandbox):
    r = bash_with(sandbox, ISAAC_STUBS + "start_isaac; echo done")
    assert r.returncode == 0, r.stderr
    lines = [line for line in r.stdout.splitlines() if line.startswith(("spawn", "wait", "kill", "cmd_stop", "done"))]
    assert lines == [
        f"spawn isaac env VENV=/opt/isaacsim-env PUBLIC_IP=10.1.2.3 bash {ROOT}/scripts/run_demo.sh isaac",
        "wait_log isaac ROS2_READY 900",
        f"spawn nav2 bash {ROOT}/scripts/run_demo.sh nav2",
        "wait_nav2 1",
        "done",
    ]


@needs_bash
def test_isaac_stream_address_can_be_set(sandbox):
    sandbox["NAYANTRA_PUBLIC_IP"] = "172.25.61.209"
    r = bash_with(sandbox, ISAAC_STUBS + "start_isaac")
    assert "PUBLIC_IP=172.25.61.209 " in r.stdout and "10.1.2.3" not in r.stdout


@needs_bash
def test_isaac_nav2_is_restarted_when_it_does_not_come_up(sandbox):
    r = bash_with(sandbox, ISAAC_STUBS + "NAV2_OK_AT=3; start_isaac; echo done")
    assert r.returncode == 0, r.stderr
    assert r.stdout.count("spawn nav2") == 3 and r.stdout.count("kill nav2") == 2 and "cmd_stop" not in r.stdout
    assert r.stdout.count("spawn isaac") == 1, "Isaac Sim is slow to start: only Nav2 is retried"


@needs_bash
def test_isaac_that_never_gets_ready_brings_everything_down(sandbox):
    r = bash_with(sandbox, ISAAC_STUBS + "LOG_RC=1; start_isaac; echo not-reached")
    assert r.returncode == 1 and "cmd_stop" in r.stdout and "spawn nav2" not in r.stdout and "not-reached" not in r.stdout


@needs_bash
def test_isaac_refuses_inside_wsl2(sandbox):
    r = bash_with(sandbox, ISAAC_STUBS + "IN_WSL=1; start_isaac")
    assert r.returncode == 1 and "WSL2" in r.stderr and "spawn" not in r.stdout


@needs_bash
def test_isaac_not_installed_says_how_to_install_it(sandbox):
    r = bash_with(sandbox, ISAAC_STUBS + "isaac_env() { return 1; }; start_isaac")
    assert r.returncode == 1 and "setup --sim isaac" in r.stderr


@needs_bash
def test_nav2_nodes_follow_the_simulator(sandbox):
    r = bash_with(sandbox, "BACKEND=isaac; nav2_nodes; BACKEND=gazebo; GZ_ROBOTS='a:0:0:0,b:1:1:0' nav2_nodes")
    assert r.stdout.split() == ["/bt_navigator", "/a/bt_navigator", "/b/bt_navigator"]


@needs_bash
def test_stop_takes_down_whatever_is_running_whichever_simulator_started_it(sandbox):
    body = ('echo isaac > "$PIDS/backend"; alive() { return 0; }; kill_service() { echo "kill $1"; }; cmd_stop keep; '
            '[ -e "$PIDS/backend" ] && echo record-kept || echo record-cleared')
    r = bash_with(sandbox, body)
    killed = [line.split()[1] for line in r.stdout.splitlines() if line.startswith("kill ")]
    assert killed == ["gui", "agent", "mcp", "core", "nav2", "isaac", "sim"], r.stdout  # clients first, simulator last
    assert "record-cleared" in r.stdout, "the note of which simulator was running goes with it"


@needs_bash
def test_status_lists_the_services_of_the_simulator_that_is_running(sandbox):
    r = bash_with(sandbox, 'echo isaac > "$PIDS/backend"; cmd_status')
    assert "simulator: Isaac Sim" in r.stdout and "isaac" in r.stdout and "nav2" in r.stdout and " gui " not in r.stdout
    r = bash_with(sandbox, 'rm -f "$PIDS/backend"; cmd_status')  # nothing running: the chosen simulator (Gazebo by default)
    assert "simulator: Gazebo" in r.stdout and " sim " in r.stdout and "nav2" not in r.stdout


@needs_bash
def test_the_chosen_simulator_is_remembered_and_a_machine_set_up_before_the_choice_is_gazebo(sandbox):
    assert bash_with(sandbox, "active_backend").stdout.strip() == "gazebo"
    assert bash_with(sandbox, 'mkdir -p "$STATE"; echo isaac > "$BACKEND_FILE"; active_backend').stdout.strip() == "isaac"
    assert bash_with(sandbox, 'mkdir -p "$STATE"; echo nonsense > "$BACKEND_FILE"; active_backend').stdout.strip() == "gazebo"


@needs_bash
def test_each_simulator_gets_its_own_scenario_and_database(sandbox):
    r = bash_with(sandbox, 'use_backend isaac; echo "$SCENARIO $(basename "$DB")"; use_backend gazebo; echo "$SCENARIO $(basename "$DB")"')
    assert r.stdout.split() == ["isaac_carter", "isaac_carter.db", "gazebo_warehouse", "gazebo_warehouse.db"]
