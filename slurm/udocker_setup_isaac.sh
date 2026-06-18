#!/usr/bin/env bash
# =============================================================================
# slurm/udocker_setup_isaac.sh — one-time user-space container setup for Isaac
#
# udocker is a pure user-space container runtime: no root, no daemon, no setuid.
# It pulls the NGC Isaac Sim Docker image (which carries its own Ubuntu 22.04 /
# glibc 2.34 userspace), sidestepping the RHEL 8 glibc 2.28 wall that blocks the
# pip and native-tarball installs.
#
# What this does (run ONCE on the login node):
#   1. pip-installs udocker into the conda env (or any python on PATH)
#   2. udocker install   — unpacks its user-space runtime tarball
#   3. logs in to nvcr.io with your NGC key
#   4. pulls nvcr.io/nvidia/isaac-sim:4.5.0   (~15 GB)
#   5. creates a named container "isaac"
#   6. injects host NVIDIA driver libs (udocker setup --nvidia)
#   7. sets execution mode F3 (Fakechroot) — best for GPU + heavy GL apps
#
# Prereqs:
#   - NGC API key.  export it before running:
#        export NGC_API_KEY=nvapi-xxxxxxxx
#     (get one at https://ngc.nvidia.com/setup/api-key)
#   - ~30 GB free disk in $HOME (udocker stores layers under ~/.udocker)
#   - Internet on the login node
#
# Usage:
#   export NGC_API_KEY=nvapi-...
#   bash slurm/udocker_setup_isaac.sh
# =============================================================================

set -euo pipefail

CONTAINER_NAME="${CONTAINER_NAME:-isaac}"
IMAGE_TAG="${IMAGE_TAG:-4.5.0}"
IMAGE_URL="${IMAGE_URL:-nvcr.io/nvidia/isaac-sim:${IMAGE_TAG}}"
# udocker stores everything here; keep it off any size-limited /tmp.
export UDOCKER_DIR="${UDOCKER_DIR:-$HOME/.udocker}"

log()  { echo -e "\033[0;32m[udocker]\033[0m $*"; }
warn() { echo -e "\033[1;33m[udocker]\033[0m $*"; }
err()  { echo -e "\033[0;31m[udocker]\033[0m $*" >&2; }

# --- 1. Install udocker ------------------------------------------------------
if ! command -v udocker >/dev/null 2>&1; then
    log "Installing udocker via pip (user space)"
    python -m pip install --user udocker
    # `pip --user` lands in ~/.local/bin which may not be on PATH yet.
    export PATH="$HOME/.local/bin:$PATH"
fi
command -v udocker >/dev/null 2>&1 || { err "udocker still not on PATH after install. Add ~/.local/bin to PATH."; exit 1; }
log "udocker: $(udocker --version 2>&1 | head -1)"

# --- 2. Unpack udocker's user-space runtime ---------------------------------
log "Running 'udocker install' (one-time runtime unpack)"
udocker install

# --- 3. Authenticate to nvcr.io ---------------------------------------------
if [ -z "${NGC_API_KEY:-}" ]; then
    err "NGC_API_KEY is not set. Run:  export NGC_API_KEY=nvapi-...  then re-run this script."
    err "Get a key at https://ngc.nvidia.com/setup/api-key"
    exit 1
fi
log "Logging in to nvcr.io"
# NGC uses the literal username '$oauthtoken' with the API key as the password.
udocker login --username '$oauthtoken' --password "$NGC_API_KEY" nvcr.io \
    || warn "udocker login returned non-zero; will still try the pull (creds may be cached)."

# --- 4. Pull the image -------------------------------------------------------
log "Pulling $IMAGE_URL  (~15 GB, 10-25 min first time)"
udocker pull "$IMAGE_URL"

# --- 5. Create the named container ------------------------------------------
if udocker ps 2>/dev/null | awk '{print $NF}' | grep -qx "$CONTAINER_NAME"; then
    log "Container '$CONTAINER_NAME' already exists — reusing"
else
    log "Creating container '$CONTAINER_NAME'"
    udocker create --name="$CONTAINER_NAME" "$IMAGE_URL"
fi

# --- 6. Inject host NVIDIA driver libraries ---------------------------------
# This copies the host's libcuda / libnvidia-* into the container so the GPU is
# usable. Must be re-run if the host driver changes.
log "Injecting NVIDIA host libraries (udocker setup --nvidia)"
udocker setup --nvidia "$CONTAINER_NAME" || warn "setup --nvidia returned non-zero; check nvidia-smi on a GPU node."

# --- 7. Set execution mode F3 (Fakechroot) ----------------------------------
# F3 properly uses the container's own glibc/loader (the whole point here) and
# is the fastest mode for GL/CUDA workloads. P1 (default PRoot) is slower and
# can break Omniverse.
log "Setting execution mode F3 (Fakechroot)"
udocker setup --execmode=F3 "$CONTAINER_NAME" || warn "Could not set F3; will fall back to default mode at run time."

log "Done. Container '$CONTAINER_NAME' is ready (UDOCKER_DIR=$UDOCKER_DIR)."
echo ""
echo "Next steps:"
echo "  1. Allocate a GPU node:"
echo "       salloc --partition=<your-gpu-partition> --gres=gpu:1 --cpus-per-task=16 --mem=64G --time=04:00:00"
echo "  2. On the compute-node shell:"
echo "       CONTAINER_NAME=$CONTAINER_NAME bash slurm/isaac_warehouse_udocker.slurm"
