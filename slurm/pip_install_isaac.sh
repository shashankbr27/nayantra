#!/usr/bin/env bash
# =============================================================================
# slurm/pip_install_isaac.sh — one-time install of Isaac Sim 4.5 via pip
#
# Creates a conda env with Python 3.10 and installs NVIDIA's pip-distributed
# Isaac Sim (no container, no admin needed). Isaac Sim 4.5 wheels are Python
# 3.10 ONLY, so we provision 3.10 through conda regardless of the base Python.
#
# Prereqs:
#   - conda on PATH (your prompt shows "(base)", so you have it)
#   - NVIDIA driver compatible with CUDA 11.8 or 12.x (check with nvidia-smi)
#   - ~25 GB free disk in your conda envs dir
#   - Internet access on the login node for the ~15 GB pip download
#
# Usage:
#   bash slurm/pip_install_isaac.sh
#   # ...takes ~10-20 minutes...
#   bash slurm/isaac_warehouse_pip.slurm   # then run the sim
# =============================================================================

set -euo pipefail

CONDA_ENV="${CONDA_ENV:-isaac310}"
ISAAC_VERSION="${ISAAC_VERSION:-4.5.0}"

log()  { echo -e "\033[0;32m[pip-install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[pip-install]\033[0m $*"; }
err()  { echo -e "\033[0;31m[pip-install]\033[0m $*" >&2; }

# --- 1. Locate conda --------------------------------------------------------
if ! command -v conda >/dev/null 2>&1; then
    err "conda not on PATH. Your prompt showed '(base)' — make sure conda is initialised:"
    err "    source \$HOME/miniconda3/etc/profile.d/conda.sh   (or your conda install path)"
    exit 1
fi
log "conda: $(conda --version) at $(which conda)"

# Make `conda activate` usable inside this non-interactive script.
CONDA_BASE="$(conda info --base)"
# shellcheck disable=SC1091
source "$CONDA_BASE/etc/profile.d/conda.sh"

# --- 2. Create (or reuse) the Python 3.10 env -------------------------------
if conda env list | awk '{print $1}' | grep -qx "$CONDA_ENV"; then
    log "Reusing existing conda env: $CONDA_ENV"
else
    log "Creating conda env '$CONDA_ENV' with Python 3.10"
    conda create -y -n "$CONDA_ENV" python=3.10
fi

conda activate "$CONDA_ENV"
PYVER="$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
[ "$PYVER" = "3.10" ] || { err "Env python is $PYVER, expected 3.10"; exit 1; }
log "Activated env '$CONDA_ENV' → $(python --version) at $(which python)"

# --- 3. GPU/driver check (informational) ------------------------------------
if command -v nvidia-smi >/dev/null 2>&1; then
    DRIVER=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 || echo "")
    log "NVIDIA driver: ${DRIVER:-not detected on this node (may still be present on compute nodes)}"
else
    warn "nvidia-smi not on PATH here. GPU check skipped — compute nodes likely still have GPUs."
fi

# --- 4. Upgrade pip then install Isaac Sim ----------------------------------
log "Upgrading pip / wheel / setuptools"
python -m pip install --upgrade pip wheel setuptools --quiet

log "Installing isaacsim[all]==$ISAAC_VERSION from pypi.nvidia.com (~15 GB, ~10-20 min)"
python -m pip install \
    --extra-index-url https://pypi.nvidia.com \
    "isaacsim[all]==$ISAAC_VERSION"

# --- 5. Smoke-test the import ----------------------------------------------
log "Smoke-testing import..."
python - <<'PY'
import sys
try:
    import isaacsim
    print(f"[pip-install] isaacsim imports OK from {isaacsim.__file__}")
except Exception as exc:
    sys.exit(f"[pip-install] FAIL: cannot import isaacsim: {exc}")
PY

log "Done. Conda env ready: $CONDA_ENV"
echo ""
echo "Next steps:"
echo "  1. Allocate a GPU node:"
echo "       salloc --partition=<your-gpu-partition> --gres=gpu:1 --cpus-per-task=16 --mem=64G --time=04:00:00"
echo "  2. On the compute-node shell:"
echo "       CONDA_ENV=$CONDA_ENV bash slurm/isaac_warehouse_pip.slurm"
