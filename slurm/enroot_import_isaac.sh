#!/usr/bin/env bash
# =============================================================================
# slurm/enroot_import_isaac.sh — one-time import of the Isaac Sim NGC image
#
# Pulls nvcr.io/nvidia/isaac-sim into a local .sqsh bundle that Pyxis can mount
# via `srun --container-image=...`. Re-importing is only needed when the upstream
# tag changes; jobs themselves should NOT re-import (slow + wastes network).
#
# Prereqs:
#   1. `enroot` on PATH (usually true if Pyxis/Enroot is installed cluster-wide).
#   2. NGC API key in your enroot credentials file. If you don't have one:
#        - generate at https://ngc.nvidia.com/setup/api-key
#        - then:
#            mkdir -p ~/.config/enroot
#            cat > ~/.config/enroot/.credentials <<'EOF'
#            machine nvcr.io login $oauthtoken password <PASTE_NGC_API_KEY>
#            EOF
#            chmod 600 ~/.config/enroot/.credentials
#
# Usage:
#   bash slurm/enroot_import_isaac.sh                 # defaults to 4.5.0
#   IMAGE_TAG=4.2.0 bash slurm/enroot_import_isaac.sh
# =============================================================================

set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-4.5.0}"
IMAGE_URL="${IMAGE_URL:-nvcr.io/nvidia/isaac-sim:${IMAGE_TAG}}"
SQSH_DIR="${SQSH_DIR:-$HOME/enroot}"
SQSH_PATH="${SQSH_PATH:-$SQSH_DIR/isaac-sim+${IMAGE_TAG}.sqsh}"

log() { echo -e "\033[0;32m[enroot]\033[0m $*"; }
err() { echo -e "\033[0;31m[enroot]\033[0m $*" >&2; }

command -v enroot >/dev/null 2>&1 || { err "enroot not on PATH. Ask your admin which module to load."; exit 1; }
[ -f "$HOME/.config/enroot/.credentials" ] \
  || err "WARNING: ~/.config/enroot/.credentials missing — pull may fail with 401 from nvcr.io"

mkdir -p "$SQSH_DIR"

if [ -f "$SQSH_PATH" ]; then
    log "Image already imported: $SQSH_PATH"
    log "Delete that file and re-run if you want a fresh pull."
    exit 0
fi

log "Importing $IMAGE_URL  →  $SQSH_PATH"
log "(this downloads ~15 GB from nvcr.io and may take 10–20 minutes the first time)"

# enroot wants the URL prefixed with docker://
cd "$SQSH_DIR"
enroot import "docker://${IMAGE_URL}"

# enroot writes a default-named .sqsh in CWD; normalise it.
DEFAULT_NAME="nvidia+isaac-sim+${IMAGE_TAG}.sqsh"
if [ -f "$DEFAULT_NAME" ] && [ "$DEFAULT_NAME" != "$(basename "$SQSH_PATH")" ]; then
    mv "$DEFAULT_NAME" "$SQSH_PATH"
fi

log "Done.  Now run:"
log "    SQSH_PATH=$SQSH_PATH sbatch slurm/isaac_webrtc.slurm"
log "or (interactive):"
log "    SQSH_PATH=$SQSH_PATH bash slurm/isaac_webrtc.slurm"
