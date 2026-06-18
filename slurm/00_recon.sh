#!/usr/bin/env bash
# =============================================================================
# slurm/00_recon.sh — read-only environment survey of the H200 server
#
# Run remotely from the laptop:
#     ssh shashank@172.25.60.80 'bash -ls' < slurm/00_recon.sh
#
# Gathers everything we need to choose the deployment path
# (enroot container vs pip install, slurm vs direct, scratch vs home).
# Makes NO changes to the system.
# =============================================================================

section() { echo; echo "=================== $* ==================="; }

section "IDENTITY / OS"
hostname
whoami
id
uname -a
cat /etc/os-release 2>/dev/null | head -5
echo "glibc: $(ldd --version 2>/dev/null | head -1)"

section "ENVIRONMENT MODULES"
# 'module' is a shell function; ensure it's loaded in non-interactive shells
source /etc/profile.d/modules.sh 2>/dev/null || source /etc/profile.d/lmod.sh 2>/dev/null || true
if command -v module >/dev/null 2>&1 || type module >/dev/null 2>&1; then
    echo "--- modules matching gcc/cuda/python/conda/apptainer/enroot/ros ---"
    module avail 2>&1 | grep -iE 'cuda|gcc|gnu[0-9/]|python|conda|mamba|apptainer|singularity|enroot|ros' | head -40
else
    echo "module command NOT found (checked /etc/profile.d)"
fi

section "GPU (after module load)"
(module load cuda/12.6 2>/dev/null || module load cuda 2>/dev/null || true
 command -v nvidia-smi >/dev/null 2>&1 || export PATH=/usr/local/cuda-12.6/bin:/usr/bin:$PATH
 nvidia-smi 2>&1 | head -20
 echo "--- GPU list ---"
 nvidia-smi -L 2>&1
 echo "--- driver / vulkan relevant ---"
 nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv 2>&1)

section "SLURM"
if command -v sinfo >/dev/null 2>&1; then
    echo "--- partitions ---"
    sinfo -o "%P %a %D %G %N" 2>&1 | head -15
    echo "--- my jobs ---"
    squeue --me 2>&1 | head -5
    echo "--- my associations (account/qos) ---"
    sacctmgr show assoc user=$USER format=account,partition,qos -nP 2>/dev/null | head -10
else
    echo "Slurm NOT present — this is a standalone server (run things directly)"
fi

section "CONTAINER RUNTIMES (no-sudo options)"
for c in enroot apptainer singularity podman docker; do
    if command -v $c >/dev/null 2>&1; then
        echo "$c: $(command -v $c)  version: $($c --version 2>&1 | head -1)"
    else
        echo "$c: not found"
    fi
done
# pyxis = slurm plugin for enroot
srun --help 2>/dev/null | grep -q container-image && echo "pyxis: YES (srun has --container-image)" || echo "pyxis: not detected"

section "PYTHON / CONDA"
for p in python3 python3.11 python3.10 conda mamba micromamba; do
    command -v $p >/dev/null 2>&1 && echo "$p: $($p --version 2>&1 | head -1)"
done

section "DISK / QUOTA"
df -h "$HOME" 2>/dev/null
quota -s 2>/dev/null | head -10 || echo "(no quota command)"
for d in /scratch /scratch/$USER /raid /data /local /tmp; do
    [ -d "$d" ] && df -h "$d" 2>/dev/null | tail -1 | sed "s|^|$d : |"
done
echo "home usage: $(du -sh $HOME 2>/dev/null | cut -f1)"

section "INTERNET REACHABILITY (from this node)"
for url in https://nvcr.io/v2/ https://pypi.org/simple/ https://pypi.nvidia.com \
           https://omniverse-content-production.s3-us-west-2.amazonaws.com \
           https://github.com; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 "$url" 2>/dev/null)
    echo "$url -> HTTP ${code:-TIMEOUT}"
done

section "NETWORK INTERFACES"
hostname -I 2>/dev/null || ip -brief addr 2>/dev/null | head -5

section "EXISTING STATE IN HOME"
ls -la "$HOME" 2>/dev/null | head -20
[ -d "$HOME/enroot" ] && ls -la "$HOME/enroot"

echo; echo "=================== RECON COMPLETE ==================="
