#!/usr/bin/env python3
"""Which simulator can this machine run? Used by `nayantra.sh setup` and `nayantra.sh detect`.

Standard library only, so it runs before anything is installed (and on a Windows host).

    python3 scripts/detect_machine.py                  human-readable report
    python3 scripts/detect_machine.py --brief          one line: <recommended> <isaac level> <gazebo level>
    python3 scripts/detect_machine.py --json           facts + verdicts, for support requests
    python3 scripts/detect_machine.py --from-json F    judge the machine described in F instead of this one
    python3 scripts/detect_machine.py --path DIR       measure free disk space where DIR lives

Probing (`probe`) is kept apart from the decision (`assess_*`, `recommend`) so the rules can be tested
against any machine, and so a report can be reproduced from the facts alone.

Where the Isaac Sim numbers come from: NVIDIA's requirements page for 6.0.1
(docs.isaacsim.omniverse.nvidia.com/6.0.1/installation/requirements.html) lists Ubuntu 22.04/24.04 or
Windows 11, a GeForce RTX 4080 with 16 GB VRAM, 32 GB RAM and a 50 GB SSD as the minimum, and says GPUs
without RT cores (A100, H100) are not supported. Isaac Sim does not run in WSL2 (no Vulkan RT support).
Below the listed minimum it may still run, so those cases are warnings; the "floor" values are where we
stop offering it (our judgement, not NVIDIA's).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

UBUNTU_ROS = {"jammy": "Humble", "noble": "Jazzy"}

# (ok at or above, floor: below this it is not offered). Sizes are GiB as the OS reports them, so a
# "16 GB" card (16376 MiB) and "32 GB" of RAM (about 31 GiB usable) still count.
ISAAC_VRAM = (15.5, 7.5)
ISAAC_RAM = (30.0, 15.0)
ISAAC_DISK = (50.0, 25.0)
ISAAC_CORES = 4
ISAAC_DRIVER_MAJOR = 580  # the driver branch the 6.0.0 requirements list; newer is fine
GAZEBO_RAM = (7.5, 3.5)
GAZEBO_DISK = (10.0, 6.0)

# Data-centre GPUs without RT cores: Isaac Sim's renderer cannot use them.
NO_RT_CORES = re.compile(r"\b(A100|A800|A30|H100|H200|H800|GH200|B100|B200|GB200|V100|P100)\b", re.I)


@dataclass(frozen=True)
class Gpu:
    name: str
    vram_gb: float
    driver: str = ""


@dataclass(frozen=True)
class Machine:
    system: str  # linux | windows | darwin
    os_name: str = ""
    ubuntu: str = ""  # codename when Ubuntu or a derivative: noble, jammy, ...
    wsl: bool = False
    ram_gb: float = 0.0
    cores: int = 0
    disk_free_gb: float = 0.0
    disk_path: str = ""
    gpus: tuple[Gpu, ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> Machine:
        d = dict(d.get("machine", d))
        d["gpus"] = tuple(Gpu(**g) for g in d.get("gpus", ()))
        return cls(**d)

    @property
    def gpu(self) -> Gpu | None:
        return max(self.gpus, key=lambda g: g.vram_gb, default=None)


@dataclass(frozen=True)
class Check:
    label: str
    status: str  # ok | warn | fail
    detail: str


@dataclass(frozen=True)
class Verdict:
    backend: str
    checks: tuple[Check, ...]

    @property
    def level(self) -> str:
        """ok: runs well. marginal: should run, with caveats. no: will not work."""
        statuses = {c.status for c in self.checks}
        return "no" if "fail" in statuses else "marginal" if "warn" in statuses else "ok"

    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.status != "ok"]


# --------------------------------------------------------------------------- probing
def _existing_parent(path: str) -> Path:
    p = Path(path).expanduser().absolute()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def _os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            out[k] = v.strip().strip('"')
    except OSError:
        pass
    return out


def _windows_ram_gb() -> float:
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong)] + [
            (n, ctypes.c_ulonglong)
            for n in ("total", "avail", "page_total", "page_avail", "virt_total", "virt_avail", "virt_ext")
        ]

    st = MemoryStatus()
    st.dwLength = ctypes.sizeof(st)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))  # type: ignore[attr-defined]
    return st.total / 2**30


def _gpus() -> tuple[Gpu, ...]:
    exe = shutil.which("nvidia-smi") or ("/usr/lib/wsl/lib/nvidia-smi" if Path("/usr/lib/wsl/lib/nvidia-smi").exists() else "")
    if not exe:
        return ()
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ()
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.rsplit(",", 2)]
        if len(parts) == 3:
            try:
                gpus.append(Gpu(parts[0], float(parts[1]) / 1024, parts[2]))
            except ValueError:
                continue
    return tuple(gpus)


def probe(path: str = ".") -> Machine:
    system = platform.system().lower()
    where = _existing_parent(path)
    disk = shutil.disk_usage(where).free / 2**30
    common = {"cores": os.cpu_count() or 0, "disk_free_gb": disk, "disk_path": str(where), "gpus": _gpus()}
    if system == "windows":
        build = platform.version().rsplit(".", 1)[-1]  # Windows 11 still reports version 10.0.<build>
        name = f"Windows {'11' if build.isdigit() and int(build) >= 22000 else '10'} (build {build})"
        return Machine(system, name, ram_gb=_windows_ram_gb(), **common)
    ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30
    if system != "linux":
        return Machine(system, platform.platform(), ram_gb=ram, **common)
    rel = _os_release()
    ubuntu = ""
    if rel.get("ID") == "ubuntu" or "ubuntu" in rel.get("ID_LIKE", ""):
        ubuntu = rel.get("UBUNTU_CODENAME") or rel.get("VERSION_CODENAME", "")
    try:
        wsl = "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except OSError:
        wsl = False
    return Machine(system, rel.get("PRETTY_NAME", "Linux"), ubuntu, wsl, ram, **common)


# --------------------------------------------------------------------------- rules
def _tier(value: float, ok: float, floor: float) -> str:
    return "ok" if value >= ok else "warn" if value >= floor else "fail"


def _gb(x: float) -> str:
    return f"{x:.0f} GB"


def assess_gazebo(m: Machine) -> Verdict:
    checks = []
    ram = m.ram_gb
    if m.system == "windows":
        ram /= 2  # WSL2 is given half the RAM unless .wslconfig says otherwise
        checks.append(Check("System", "ok", f"{m.os_name}: runs in an Ubuntu 24.04 WSL2 distro that setup creates"))
    elif m.system == "linux" and m.ubuntu in UBUNTU_ROS:
        checks.append(Check("System", "ok", f"{m.os_name}{' (WSL2)' if m.wsl else ''}: ROS 2 {UBUNTU_ROS[m.ubuntu]}"))
    else:
        checks.append(Check("System", "fail", f"{m.os_name or m.system}: needs Ubuntu 22.04 or 24.04, or Windows 11 with WSL2"))
    note = " in WSL2 (half of the RAM by default)" if m.system == "windows" else ""
    checks.append(Check("Memory", _tier(ram, *GAZEBO_RAM), f"{_gb(ram)}{note}, 8 GB or more is comfortable"))
    checks.append(Check("Disk", _tier(m.disk_free_gb, *GAZEBO_DISK), f"{_gb(m.disk_free_gb)} free, needs about 10 GB"))
    gpu = m.gpu
    checks.append(Check(
        "Graphics", "ok",
        f"{gpu.name}: hardware rendering" if gpu else "no NVIDIA GPU: Gazebo draws in software (slower, still works)",
    ))
    return Verdict("gazebo", tuple(checks))


def assess_isaac(m: Machine) -> Verdict:
    checks = []
    if m.system == "windows":
        checks.append(Check("System", "fail", f"{m.os_name}: Nayantra runs ROS 2 and Nav2 in WSL2 on Windows, and Isaac Sim does not run in WSL2"))
    elif m.system != "linux" or m.ubuntu not in UBUNTU_ROS:
        checks.append(Check("System", "fail", f"{m.os_name or m.system}: Isaac Sim needs Ubuntu 22.04 or 24.04"))
    elif m.wsl:
        checks.append(Check("System", "fail", f"{m.os_name} under WSL2: Isaac Sim does not run in WSL2, use a native Ubuntu install"))
    else:
        checks.append(Check("System", "ok", m.os_name))
    gpu = m.gpu
    if gpu is None:
        checks.append(Check("GPU", "fail", "no NVIDIA GPU found (Isaac Sim needs an RTX card)"))
    elif NO_RT_CORES.search(gpu.name):
        checks.append(Check("GPU", "fail", f"{gpu.name} has no RT cores, which Isaac Sim's renderer needs"))
    else:
        checks.append(Check("GPU", "ok", gpu.name))
        checks.append(Check(
            "VRAM", _tier(gpu.vram_gb, *ISAAC_VRAM),
            f"{_gb(gpu.vram_gb)}, NVIDIA's minimum is 16 GB" + (" (8 GB is the least we will offer)" if gpu.vram_gb < ISAAC_VRAM[1] else ""),
        ))
        major = re.match(r"\d+", gpu.driver)
        if major and int(major.group()) < ISAAC_DRIVER_MAJOR:
            checks.append(Check("Driver", "warn", f"{gpu.driver}: Isaac Sim 6.0 lists {ISAAC_DRIVER_MAJOR}.x or newer"))
    checks.append(Check("Memory", _tier(m.ram_gb, *ISAAC_RAM), f"{_gb(m.ram_gb)}, NVIDIA's minimum is 32 GB"))
    checks.append(Check("CPU", "ok" if m.cores >= ISAAC_CORES else "warn", f"{m.cores} threads, NVIDIA's minimum is {ISAAC_CORES} cores"))
    checks.append(Check("Disk", _tier(m.disk_free_gb, *ISAAC_DISK), f"{_gb(m.disk_free_gb)} free, needs about 50 GB"))
    return Verdict("isaac", tuple(checks))


def recommend(isaac: Verdict, gazebo: Verdict) -> str:
    """Isaac Sim only when the machine meets its minimum cleanly; Gazebo runs on nearly anything."""
    if isaac.level == "ok":
        return "isaac"
    return "gazebo" if gazebo.level != "no" else "none"


# --------------------------------------------------------------------------- output
_MARK = {"ok": "ok  ", "warn": "warn", "fail": "NO  "}
_LEVEL = {"ok": "ready", "marginal": "possible, with caveats", "no": "not possible here"}


def report(m: Machine, isaac: Verdict, gazebo: Verdict, rec: str) -> str:
    gpu = m.gpu
    lines = [
        "Machine check",
        f"  System   {m.os_name or m.system}{' (WSL2)' if m.wsl else ''}",
        f"  CPU      {m.cores} threads",
        f"  Memory   {_gb(m.ram_gb)}",
        f"  Disk     {_gb(m.disk_free_gb)} free at {m.disk_path}",
        f"  GPU      {f'{gpu.name}, {_gb(gpu.vram_gb)} VRAM, driver {gpu.driver}' if gpu else 'no NVIDIA GPU found'}",
    ]
    for v, title in ((gazebo, "Gazebo"), (isaac, "Isaac Sim")):
        lines += ["", f"  {title}: {_LEVEL[v.level]}"]
        lines += [f"    [{_MARK[c.status]}] {c.label:<9}{c.detail}" for c in v.checks]
    lines += ["", f"  Recommended: {({'isaac': 'Isaac Sim', 'gazebo': 'Gazebo', 'none': 'neither (see above)'})[rec]}"]
    return "\n".join(lines)


def to_json(m: Machine, isaac: Verdict, gazebo: Verdict, rec: str) -> dict:
    def verdict(v: Verdict) -> dict:
        return {"level": v.level, "checks": [asdict(c) for c in v.checks]}

    return {"machine": asdict(m), "isaac": verdict(isaac), "gazebo": verdict(gazebo), "recommended": rec}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--path", default=".", help="measure free disk space where this path lives")
    ap.add_argument("--from-json", metavar="FILE", help="use the machine described in FILE instead of probing")
    out = ap.add_mutually_exclusive_group()
    out.add_argument("--brief", action="store_true", help="print: <recommended> <isaac level> <gazebo level>")
    out.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    if args.from_json:
        m = Machine.from_dict(json.loads(Path(args.from_json).read_text(encoding="utf-8")))
    else:
        m = probe(args.path)
    isaac, gazebo = assess_isaac(m), assess_gazebo(m)
    rec = recommend(isaac, gazebo)
    if args.brief:
        print(rec, isaac.level, gazebo.level)
    elif args.json:
        print(json.dumps(to_json(m, isaac, gazebo, rec), indent=2))
    else:
        print(report(m, isaac, gazebo, rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
