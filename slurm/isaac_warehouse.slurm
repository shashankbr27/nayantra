#!/usr/bin/env bash
# =============================================================================
# slurm/isaac_warehouse.slurm — Isaac Sim + warehouse scene + robot, WebRTC
#                               (Enroot + Pyxis variant)
#
# Like slurm/isaac_webrtc.slurm but instead of launching the bare
# runheadless.webrtc.sh (empty stage), it runs slurm/isaac_boot.py via Isaac
# Sim's standalone python.sh. The boot script:
#   - enables livestream=2 (WebRTC) inside SimulationApp
#   - loads Simple_Warehouse
#   - spawns one TurtleBot3 (configurable via env)
#   - steps the sim forever
#
# Customise scene / robot at launch:
#   SCENE_USD="omniverse://server/.../my_factory.usd" \
#   ROBOT_NAME=tb1 ROBOT_X=2.0 ROBOT_Y=1.5 \
#   bash slurm/isaac_warehouse.slurm
#
# Multiple robots:
#   EXTRA_ROBOTS_JSON='[{"name":"tb2","x":3,"y":0},{"name":"tb3","x":-3,"y":0}]' \
#   bash slurm/isaac_warehouse.slurm
# =============================================================================

#SBATCH --job-name=isaac-warehouse
#SBATCH --partition=PARTITION_TODO     # <-- EDIT
#SBATCH --gres=gpu:1                   # <-- maybe gpu:h200:1
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --time=04:00:00
#SBATCH --output=isaac-warehouse-%j.log
# #SBATCH --account=ACCOUNT_TODO
# #SBATCH --qos=QOS_TODO

set -euo pipefail

# =============================================================================
# CONFIG (override via env)
# =============================================================================
SQSH_PATH="${SQSH_PATH:-$HOME/enroot/isaac-sim+4.5.0.sqsh}"
IMAGE_TAG="${IMAGE_TAG:-4.5.0}"
CACHE_ROOT="${CACHE_ROOT:-$HOME/isaac-cache}"
CONTAINER_NAME="${CONTAINER_NAME:-isaac-warehouse-$$}"

# Path to the navigation repo on the host. Defaults to the dir this script
# lives in (..). Must contain slurm/isaac_boot.py.
REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

# WebRTC ports — fixed by Isaac Sim 4.x and not configurable here.
WEBRTC_SIGNALING_PORT=49100
# =============================================================================

log() { echo -e "\033[0;32m[isaac]\033[0m $*"; }
err() { echo -e "\033[0;31m[isaac]\033[0m $*" >&2; }

# --- Preflight ---------------------------------------------------------------
command -v srun >/dev/null 2>&1 || { err "srun not on PATH"; exit 1; }
[ -f "$SQSH_PATH" ]                          || { err "Missing $SQSH_PATH"; exit 1; }
[ -f "$REPO_ROOT/slurm/isaac_boot.py" ]      || { err "Missing $REPO_ROOT/slurm/isaac_boot.py"; exit 1; }
log "Image:        $SQSH_PATH (Isaac $IMAGE_TAG)"
log "Repo root:    $REPO_ROOT  → /workspace inside container"
log "Boot script:  $REPO_ROOT/slurm/isaac_boot.py"

# --- Writable cache layout (same as bare runheadless variant) ----------------
mkdir -p \
  "$CACHE_ROOT"/kit \
  "$CACHE_ROOT"/ov \
  "$CACHE_ROOT"/pip \
  "$CACHE_ROOT"/glcache \
  "$CACHE_ROOT"/computecache \
  "$CACHE_ROOT"/logs \
  "$CACHE_ROOT"/data \
  "$CACHE_ROOT"/documents

# Bind the repo into /workspace as well so isaac_boot.py is reachable.
CONTAINER_MOUNTS="\
${REPO_ROOT}:/workspace:ro,\
${CACHE_ROOT}/kit:/isaac-sim/kit/cache,\
${CACHE_ROOT}/ov:/root/.cache/ov,\
${CACHE_ROOT}/pip:/root/.cache/pip,\
${CACHE_ROOT}/glcache:/root/.cache/nvidia/GLCache,\
${CACHE_ROOT}/computecache:/root/.nv/ComputeCache,\
${CACHE_ROOT}/logs:/root/.nvidia-omniverse/logs,\
${CACHE_ROOT}/data:/root/.local/share/ov/data,\
${CACHE_ROOT}/documents:/root/Documents"

# --- EULA / consent ----------------------------------------------------------
export ACCEPT_EULA=Y
export OMNI_KIT_ACCEPT_EULA=YES
export PRIVACY_CONSENT=Y
export OMNI_KIT_ALLOW_ROOT=1

# Propagate the boot-script overrides (only those that are set).
EXTRA_ENV="ACCEPT_EULA,OMNI_KIT_ACCEPT_EULA,PRIVACY_CONSENT,OMNI_KIT_ALLOW_ROOT"
for v in SCENE_USD ROBOT_USD ROBOT_NAME ROBOT_X ROBOT_Y EXTRA_ROBOTS_JSON \
         ISAAC_ASSETS_ROOT RENDER_WIDTH RENDER_HEIGHT RENDER_DT; do
    if [ -n "${!v:-}" ]; then
        export "$v"
        EXTRA_ENV="${EXTRA_ENV},${v}"
    fi
done

# --- Report client-connect info ----------------------------------------------
NODE_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
NODE_HOST="$(hostname -f 2>/dev/null || hostname)"
log "Compute node: ${NODE_HOST} (${NODE_IP})"
log "Once the log says 'Stage ready. WebRTC stream is live.':"
log "  Open the 'Isaac Sim WebRTC Streaming Client' on your laptop and use:"
log "      Server: ${NODE_IP}    (TCP ${WEBRTC_SIGNALING_PORT}, UDP 47998-48000)"

# --- Launch: python.sh isaac_boot.py inside the container --------------------
log "Starting Isaac Sim + warehouse + robot. First boot builds shader caches."
exec srun \
    --container-image="$SQSH_PATH" \
    --container-name="$CONTAINER_NAME" \
    --container-mounts="$CONTAINER_MOUNTS" \
    --container-workdir=/isaac-sim \
    --container-env="$EXTRA_ENV" \
    bash -lc 'exec ./python.sh /workspace/slurm/isaac_boot.py'
