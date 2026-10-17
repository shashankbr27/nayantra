"""scripts/detect_machine.py: which simulator can a machine run? The rules are tested against described
machines, so no GPU, WSL or Windows is needed."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import asdict
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "detect_machine.py"
spec = importlib.util.spec_from_file_location("detect_machine", SCRIPT)
dm = importlib.util.module_from_spec(spec)
sys.modules["detect_machine"] = dm  # dataclasses look their module up here
spec.loader.exec_module(dm)

Gpu, Machine = dm.Gpu, dm.Machine

# The RTX 6000 Ada workstation the Isaac Sim demo runs on.
WORKSTATION = Machine("linux", "Ubuntu 24.04.2 LTS", "noble", False, 62.0, 32, 800.0, "/home/u",
                      (Gpu("NVIDIA RTX 6000 Ada Generation", 47.99, "580.65.06"),))
# The development laptop: Ubuntu under WSL2, RTX 3050 6 GB.
WSL_LAPTOP = Machine("linux", "Ubuntu 24.04.5 LTS", "noble", True, 11.0, 12, 949.0, "/home/u",
                     (Gpu("NVIDIA GeForce RTX 3050 6GB Laptop GPU", 6.0, "581.86"),))


def variant(base: Machine, **changes) -> Machine:
    return Machine(**{**asdict(base), "gpus": base.gpus, **changes})


def gpu_of(name: str, vram_gb: float, driver: str = "580.65.06") -> tuple:
    return (Gpu(name, vram_gb, driver),)


def verdicts(m: Machine):
    isaac, gazebo = dm.assess_isaac(m), dm.assess_gazebo(m)
    return isaac, gazebo, dm.recommend(isaac, gazebo)


def check(v, label: str):
    return next(c for c in v.checks if c.label == label)


def test_workstation_runs_isaac_and_gets_it_recommended():
    isaac, gazebo, rec = verdicts(WORKSTATION)
    assert (isaac.level, gazebo.level, rec) == ("ok", "ok", "isaac")


def test_wsl_laptop_gets_gazebo_and_the_reasons_isaac_is_out():
    isaac, gazebo, rec = verdicts(WSL_LAPTOP)
    assert (isaac.level, gazebo.level, rec) == ("no", "ok", "gazebo")
    assert "WSL2" in check(isaac, "System").detail
    assert check(isaac, "VRAM").status == "fail" and check(isaac, "Memory").status == "fail"


def test_isaac_does_not_run_in_wsl2_even_on_a_big_gpu():
    isaac, _, rec = verdicts(variant(WORKSTATION, wsl=True))
    assert isaac.level == "no" and rec == "gazebo"
    assert [c.label for c in isaac.problems()] == ["System"]


def test_datacentre_gpu_without_rt_cores_is_refused():
    for name in ("NVIDIA H200", "NVIDIA A100-SXM4-80GB", "NVIDIA H100 80GB HBM3"):
        isaac, _, rec = verdicts(variant(WORKSTATION, gpus=gpu_of(name, 80.0)))
        assert isaac.level == "no" and rec == "gazebo", name
        assert "RT cores" in check(isaac, "GPU").detail


def test_a_card_just_under_the_minimum_is_a_caveat_not_a_refusal_and_is_not_recommended():
    m = variant(WORKSTATION, gpus=gpu_of("NVIDIA GeForce RTX 3070", 8.0), ram_gb=31.0)
    isaac, _, rec = verdicts(m)
    assert isaac.level == "marginal" and rec == "gazebo"
    assert check(isaac, "VRAM").status == "warn"


def test_below_the_floor_is_refused():
    assert dm.assess_isaac(variant(WORKSTATION, gpus=gpu_of("NVIDIA GeForce RTX 3050", 4.0))).level == "no"
    assert dm.assess_isaac(variant(WORKSTATION, ram_gb=8.0)).level == "no"
    assert dm.assess_isaac(variant(WORKSTATION, disk_free_gb=10.0)).level == "no"


def test_sizes_as_the_os_reports_them_still_count_as_the_nominal_size():
    # A 16 GB card reports 16376 MiB, "32 GB" of RAM leaves about 31 GiB, a 50 GB SSD has ~46 GiB free.
    m = variant(WORKSTATION, gpus=gpu_of("NVIDIA GeForce RTX 4080", 16376 / 1024), ram_gb=31.2, disk_free_gb=60.0)
    assert dm.assess_isaac(m).level == "ok"


def test_an_old_driver_warns_but_does_not_block():
    m = variant(WORKSTATION, gpus=gpu_of("NVIDIA RTX 6000 Ada Generation", 48.0, "535.104.05"))
    isaac = dm.assess_isaac(m)
    assert isaac.level == "marginal" and check(isaac, "Driver").status == "warn"


def test_windows_host_uses_gazebo_in_wsl_and_counts_half_the_ram():
    win = Machine("windows", "Windows 11 (build 26300)", ram_gb=23.0, cores=12, disk_free_gb=49.0, disk_path="D:\\",
                  gpus=gpu_of("NVIDIA GeForce RTX 4090", 24.0, "581.86"))
    isaac, gazebo, rec = verdicts(win)
    assert (gazebo.level, isaac.level, rec) == ("ok", "no", "gazebo")
    assert "WSL2" in check(isaac, "System").detail
    assert "11 GB" in check(gazebo, "Memory").detail or "12 GB" in check(gazebo, "Memory").detail
    # A Windows PC with 12 GB would leave WSL2 with 6 GB: still possible, with a caveat.
    assert dm.assess_gazebo(variant(win, ram_gb=12.0)).level == "marginal"


def test_no_gpu_still_gets_gazebo_in_software():
    isaac, gazebo, rec = verdicts(variant(WORKSTATION, gpus=()))
    assert (isaac.level, gazebo.level, rec) == ("no", "ok", "gazebo")
    assert "software" in check(gazebo, "Graphics").detail


def test_other_linux_and_macos_can_run_neither():
    for m in (variant(WORKSTATION, os_name="Fedora 41", ubuntu=""), Machine("darwin", "macOS", ram_gb=32.0, cores=10, disk_free_gb=300.0)):
        _, _, rec = verdicts(m)
        assert rec == "none"


def test_ubuntu_derivatives_count_as_ubuntu():
    assert verdicts(variant(WORKSTATION, os_name="Linux Mint 22", ubuntu="noble"))[2] == "isaac"
    assert verdicts(variant(WORKSTATION, ubuntu="jammy"))[2] == "isaac"


def test_too_little_memory_or_disk_rules_gazebo_out():
    assert dm.assess_gazebo(variant(WSL_LAPTOP, ram_gb=2.0)).level == "no"
    assert dm.assess_gazebo(variant(WSL_LAPTOP, disk_free_gb=3.0)).level == "no"
    assert dm.recommend(*verdicts(variant(WSL_LAPTOP, ram_gb=2.0))[:2]) == "none"


def test_the_biggest_gpu_is_the_one_judged():
    m = variant(WORKSTATION, gpus=(Gpu("NVIDIA GeForce RTX 3050", 6.0, "580.1"), Gpu("NVIDIA RTX 6000 Ada Generation", 48.0, "580.1")))
    assert dm.assess_isaac(m).level == "ok"


def test_machine_round_trips_through_json():
    assert Machine.from_dict(json.loads(json.dumps(asdict(WORKSTATION)))) == WORKSTATION
    doc = dm.to_json(WORKSTATION, *verdicts(WORKSTATION)[:2], "isaac")
    assert Machine.from_dict(doc) == WORKSTATION  # the whole --json document is accepted too


def test_cli_brief_and_json_and_report(tmp_path, capsys):
    f = tmp_path / "m.json"
    f.write_text(json.dumps(asdict(WORKSTATION)), encoding="utf-8")
    assert dm.main(["--from-json", str(f), "--brief"]) == 0
    assert capsys.readouterr().out.split() == ["isaac", "ok", "ok"]
    assert dm.main(["--from-json", str(f), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["recommended"] == "isaac"
    assert dm.main(["--from-json", str(f)]) == 0
    out = capsys.readouterr().out
    assert "Recommended: Isaac Sim" in out and "RTX 6000 Ada" in out
    assert out.isascii(), "the report must survive a Windows console"


def test_probe_describes_the_machine_it_runs_on(tmp_path):
    m = dm.probe(str(tmp_path / "does" / "not" / "exist"))  # a path that is yet to be made: its parent is measured
    assert m.system in ("linux", "windows", "darwin")
    assert m.cores > 0 and m.ram_gb > 0 and m.disk_free_gb > 0
    assert Path(m.disk_path).exists()
    verdicts(m)  # and the rules can judge it whatever it is
