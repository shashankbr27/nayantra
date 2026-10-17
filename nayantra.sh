#!/usr/bin/env bash
# =============================================================================
# nayantra.sh — set up, start and stop Nayantra together with a robot simulator.
#
#   ./nayantra.sh setup [--sim auto|gazebo|isaac] [--yes] [--dry-run]
#                                  one-time install. Pick the simulator, or let it check this machine:
#                                    gazebo  ROS 2 + Nav2 + Gazebo: three warehouse UGVs, runs on a laptop
#                                    isaac   ROS 2 + Nav2 + Isaac Sim: photoreal, needs a 16 GB RTX GPU on native Ubuntu
#                                    auto    choose from this machine's OS, GPU, memory and disk (the default)
#                                  Without --sim, a terminal gets a menu. --dry-run shows the plan and changes nothing.
#   ./nayantra.sh detect           check this machine and say which simulator it can run
#   ./nayantra.sh start            the chosen simulator + Nav2, then the core, MCP, agent API and the dashboard
#   ./nayantra.sh stop             stop everything this script started
#   ./nayantra.sh gui              (re)open the Gazebo window on the running simulation
#   ./nayantra.sh restart | status | logs [sim|isaac|nav2|core|mcp|agent|gui] | doctor
#
# start options:  --sim gazebo|isaac  run this simulator instead of the one chosen at setup
#                 --headless   no Gazebo window (the simulation still runs)
#                 --no-open    do not open the dashboard in a browser
#                 --reset      wipe the world-model database and re-seed the scenario
#
# Commands given on the dashboard go core -> Nav2 -> simulator, and robot poses
# flow back the same way, so the 3D world and the dashboard stay in step.
#
# On Windows this script (run from Git Bash, or `nayantra.cmd` from cmd/PowerShell)
# hands off to an Ubuntu 24.04 WSL2 distro, because ROS 2 Jazzy and Gazebo Harmonic
# run on Linux. `setup` creates that distro if it is missing. Isaac Sim does not run
# in WSL2, so Windows uses Gazebo; use a native Ubuntu machine for Isaac Sim.
#
# Env overrides: NAYANTRA_SIM (default for setup --sim), NAYANTRA_WSL_DISTRO (Ubuntu-24.04),
# NAYANTRA_WSL_DIR (where a new distro's disk goes), NAYANTRA_HOME, ROS_DOMAIN_ID,
# GZ_ROBOTS, CORE_PORT, NAYANTRA_GUI_RENDER (auto|gpu|software: how the Gazebo window draws),
# NAYANTRA_ISAAC_DIR (where setup installs Isaac Sim, default ~/.nayantra), NAYANTRA_ISAAC_ENV
# (an existing Isaac Sim venv to use), NAYANTRA_PUBLIC_IP (address the Isaac stream client connects to).
# =============================================================================
set -uo pipefail

SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DISTRO="${NAYANTRA_WSL_DISTRO:-Ubuntu-24.04}"
CORE_PORT="${CORE_PORT:-8000}"

usage() { sed -n '3,/^# =====/p' "$SELF/nayantra.sh" | sed '$d; s/^# \{0,1\}//'; }

if [ -t 1 ]; then G=$'\033[0;32m'; Y=$'\033[1;33m'; R=$'\033[0;31m'; N=$'\033[0m'; else G=; Y=; R=; N=; fi
info() { echo "${G}[nayantra]${N} $*"; }
warn() { echo "${Y}[nayantra]${N} $*" >&2; }
die()  { echo "${R}[nayantra]${N} $*" >&2; exit 1; }

# =============================================================================
# Choosing the simulator (shared by the Windows and Linux halves)
# =============================================================================
simname() { case "$1" in gazebo) echo Gazebo ;; isaac) echo "Isaac Sim" ;; *) echo neither ;; esac; }
is_tty() { [ -t 0 ] && [ -t 2 ]; }      # prompts go to stderr, so both ends must be a terminal

# setup [--sim X] [--yes] [--dry-run]; --system/--user are the two halves the Windows side runs separately.
parse_setup_args() {
  SIM_ARG="${NAYANTRA_SIM:-}"; ASSUME_YES=0; DRY_RUN=0; PHASE=both
  while [ $# -gt 0 ]; do
    case "$1" in
      --sim) [ $# -ge 2 ] || die "--sim needs auto, gazebo or isaac"; SIM_ARG="$2"; shift ;;
      --sim=*) SIM_ARG="${1#--sim=}" ;;
      --yes|-y) ASSUME_YES=1 ;;
      --dry-run) DRY_RUN=1 ;;
      --system) PHASE=system ;;
      --user) PHASE=user ;;
      *) die "unknown option: $1 (see ./nayantra.sh help)" ;;
    esac
    shift
  done
  case "${SIM_ARG:-auto}" in auto|gazebo|isaac) ;; *) die "unknown simulator '$SIM_ARG' (use auto, gazebo or isaac)" ;; esac
}

# The menu: prints auto, gazebo or isaac. $1 = what auto would pick, $2 = a note for the Isaac line.
ask_simulator() {
  local ans
  {
    echo
    echo "  Which simulator do you want to use?"
    echo
    echo "    1) Auto        check this machine and pick the best one (it would pick: $(simname "$1"))"
    echo "    2) Gazebo      open source, three warehouse UGVs; runs on a laptop; tested end to end"
    echo "    3) Isaac Sim   photoreal NVIDIA simulator with the Carter robot; needs a 16 GB RTX GPU on"
    echo "                   native Ubuntu and about 20 GB of downloads; not yet run live through the dashboard${2:+"  $2"}"
    echo
  } >&2
  while true; do
    printf '  Choose 1-3 [1]: ' >&2
    read -r ans || ans=1
    case "${ans:-1}" in 1|auto|a) echo auto; return ;; 2|gazebo|g) echo gazebo; return ;; 3|isaac|i) echo isaac; return ;; esac
    echo "  Please type 1, 2 or 3." >&2
  done
}

# confirm "question" default-when-there-is-no-terminal(0 yes|1 no): --yes always says yes.
confirm() {
  [ "$ASSUME_YES" = 1 ] && return 0
  is_tty || return "$2"
  local a; printf '  %s [Y/n] ' "$1" >&2; read -r a || a=n
  case "$a" in ""|y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# =============================================================================
# Windows host: forward into WSL (and create the distro on `setup`)
# =============================================================================
case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*) ON_WINDOWS=1 ;; *) ON_WINDOWS=0 ;; esac

if [ "$ON_WINDOWS" = 1 ]; then
  export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
  case "${1:-}" in ""|-h|--help|help) usage; exit 0 ;; esac
  command -v wsl.exe >/dev/null 2>&1 || die "WSL is not installed. In an admin PowerShell: wsl --install, reboot, then re-run."

  lin_path="$(echo "${SELF}" | sed -E 's|^/([a-zA-Z])/|/mnt/\L\1/|')"   # /d/navigation -> /mnt/d/navigation
  distro_exists() { wsl.exe -l -q 2>/dev/null | tr -d '\0\r' | grep -qx "$DISTRO"; }
  in_distro()      { wsl.exe -d "$DISTRO" --cd "$lin_path" -- "$@"; }          # as the distro's default user
  in_distro_root() { wsl.exe -d "$DISTRO" -u root --cd "$lin_path" -- "$@"; }

  create_distro() {
    info "Installing the $DISTRO WSL distro (large download, a few minutes) ..."
    local args=(--install -d "$DISTRO" --no-launch)
    [ -n "${NAYANTRA_WSL_DIR:-}" ] && args+=(--location "$NAYANTRA_WSL_DIR")
    wsl.exe "${args[@]}" || die "wsl ${args[*]} failed"
    local user; user="$(echo "${USERNAME:-nayantra}" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')"; user="${user:-nayantra}"
    in_distro_root ./nayantra.sh _wsl-init "$user" || die "could not create the Linux user"
    wsl.exe --terminate "$DISTRO" >/dev/null 2>&1                               # so wsl.conf takes effect
  }

  # Windows always runs the simulator in WSL2, where Isaac Sim does not work, so the choice is
  # Gazebo (auto included); asking for Isaac Sim is an error, not a silent substitution.
  windows_backend() {
    local want="$SIM_ARG"
    [ -z "$want" ] && is_tty && want="$(ask_simulator gazebo '[not possible on Windows: Isaac Sim does not run in WSL2]')"
    case "${want:-auto}" in
      auto|gazebo) echo gazebo ;;
      isaac) die "Isaac Sim cannot be set up on Windows: this launcher runs ROS 2 and Nav2 in WSL2, and Isaac Sim does not run in WSL2. Use a native Ubuntu machine with an RTX GPU, or choose Gazebo (./nayantra.sh setup --sim gazebo)." ;;
    esac
  }
  host_python() {
    local p; for p in python3 python py; do
      command -v "$p" >/dev/null 2>&1 && "$p" -c 'import sys' >/dev/null 2>&1 && { echo "$p"; return 0; }
    done; return 1
  }
  winpath() { cygpath -w "$1" 2>/dev/null || echo "$1"; }

  if [ "${1:-}" = setup ]; then
    parse_setup_args "${@:2}"
    backend="$(windows_backend)" || exit 1
    info "Simulator: $(simname "$backend") (in the $DISTRO WSL2 distro)"
    [ "$DRY_RUN" = 1 ] && { info "Dry run: would create the $DISTRO distro if missing, install ROS 2 + Nav2 + Gazebo in it, the Python env and the dashboard build. Nothing changed."; exit 0; }
    distro_exists || create_distro
    # System packages run as root so no sudo password is needed; the rest as the normal user.
    in_distro_root ./nayantra.sh setup --system --sim "$backend" || die "system setup failed"
    in_distro ./nayantra.sh setup --user --sim "$backend"
    exit $?
  fi
  if [ "${1:-}" = detect ]; then          # works before the distro exists: probe the Windows host itself
    if hp="$(host_python)"; then
      exec "$hp" "$(winpath "$SELF/scripts/detect_machine.py")" --path "$(winpath "${NAYANTRA_WSL_DIR:-$SELF}")" "${@:2}"
    fi
    distro_exists || die "No Python found on Windows and no $DISTRO distro yet - run: ./nayantra.sh setup"
    in_distro python3 scripts/detect_machine.py "${@:2}"
    exit $?
  fi
  distro_exists || die "No $DISTRO distro yet - run: ./nayantra.sh setup"

  # WSL stops a distro (killing everything in it) once its last session closes. While the
  # stack runs, one hidden session keeps it alive; `stop` ends it (see cmd_stop).
  keepalive_up() {
    in_distro pgrep -f '^sleep 2147483647$' >/dev/null 2>&1 && return 0
    powershell.exe -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath wsl.exe -ArgumentList '-d','$DISTRO','-e','sleep','2147483647'"
  }
  # WSL forwards the dashboard port to Windows, but not if a Windows program (often VS Code's
  # port forwarding) already listens there, so move to the next free port.
  free_port() {
    local p="$1"
    while netstat -ano 2>/dev/null | grep -qE "^ +TCP +[^ ]+:$p +[^ ]+ +LISTENING"; do p=$((p + 1)); done
    echo "$p"
  }
  case "${1:-}" in
    restart) "$SELF/nayantra.sh" stop; sleep 2; exec "$SELF/nayantra.sh" start "${@:2}" ;;
    start)
      keepalive_up
      port="$(free_port "$CORE_PORT")"
      [ "$port" != "$CORE_PORT" ] && warn "Windows port $CORE_PORT is in use; using $port for the dashboard"
      in_distro env CORE_PORT="$port" ./nayantra.sh start "${@:2}"
      exit $? ;;
  esac
  in_distro ./nayantra.sh "$@"
  exit $?
fi

# =============================================================================
# Linux (native or WSL)
# =============================================================================
ROOT="$SELF"
IN_WSL=0; grep -qi microsoft /proc/version 2>/dev/null && IN_WSL=1

# The Windows PATH is slow to search over 9p and carries Windows node/python; drop it.
[ "$IN_WSL" = 1 ] && PATH="$(echo "$PATH" | tr ':' '\n' | grep -v '^/mnt/' | paste -sd:)"

# State lives on the Linux filesystem when the repo is on a Windows drive (/mnt/*):
# SQLite locking and Python imports are unreliable/slow over 9p.
if [[ "$ROOT" == /mnt/* ]]; then STATE="${NAYANTRA_HOME:-$HOME/.nayantra}"; else STATE="${NAYANTRA_HOME:-$ROOT/runtime}"; fi
VENV="$STATE/venv"; PIDS="$STATE/pids"; LOGS="$STATE/logs"
NODE_DIR="$STATE/node"
if [[ "$ROOT" == /mnt/* ]]; then WEB_BUILD="$STATE/web"; else WEB_BUILD="$ROOT/web"; fi
export PATH="$NODE_DIR/bin:$PATH"

# Isaac Sim is tens of GB, so it lives under the home directory, never inside the repo.
ISAAC_DIR="${NAYANTRA_ISAAC_DIR:-$HOME/.nayantra}"

# Which simulator is in use. `setup` records the choice; `start --sim X` overrides it for one run.
# A machine set up before there was a choice has no record, and that was always Gazebo.
BACKEND_FILE="$STATE/backend"
active_backend() { local b; b="$(cat "$BACKEND_FILE" 2>/dev/null)"; case "$b" in gazebo|isaac) echo "$b" ;; *) echo gazebo ;; esac; }
use_backend() {
  BACKEND="$1"
  case "$BACKEND" in
    gazebo) SCENARIO=gazebo_warehouse; SIM_SVC=sim ;;       # SIM_SVC: the service whose death means the world is gone
    isaac)  SCENARIO=isaac_carter;     SIM_SVC=isaac ;;
    *) die "unknown simulator '$1' (use gazebo or isaac)" ;;
  esac
  DB="$STATE/$SCENARIO.db"
}
use_backend "$(active_backend)"

# The venv that holds Isaac Sim, if there is one: the one named in the environment, the one setup
# makes, or the ones people already tend to have.
isaac_env() {
  local d
  for d in "${NAYANTRA_ISAAC_ENV:-}" "$ISAAC_DIR/isaacsim-env" "$HOME/or_sig/isaacsim-env" "$HOME/isaacsim-env"; do
    [ -n "$d" ] && [ -x "$d/bin/python" ] \
      && "$d/bin/python" -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec("isaacsim") is None)' 2>/dev/null \
      && { echo "$d"; return 0; }
  done
  return 1
}

# The address the Isaac Sim stream client should connect to: the interface that reaches the network.
host_ip() {
  local ip; ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1)"
  echo "${ip:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
}

codename() { . /etc/os-release; echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}"; }
distro_for() { case "$1" in noble) echo jazzy ;; jammy) echo humble ;; *) echo "" ;; esac; }
ros_distro() {
  for d in jazzy humble; do [ -f "/opt/ros/$d/setup.bash" ] && { echo "$d"; return; }; done
  distro_for "$(codename)"
}
ros_setup() { echo "${ROS_SETUP:-/opt/ros/$(ros_distro)/setup.bash}"; }

# Source ROS 2 plus the settings every ROS process here shares.
ros_env() {
  local f; f="$(ros_setup)"
  [ -f "$f" ] || die "ROS 2 not found at $f - run ./nayantra.sh setup"
  set +u; . "$f"; set -u
  export ROS_DOMAIN_ID="${ROS_DOMAIN_ID:-0}"
  export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST                         # one machine: keep DDS off the LAN
  [ "${ROS_DISTRO:-}" = humble ] && export ROS_LOCALHOST_ONLY=1           # Humble predates the range setting
  export GZ_SIM_RESOURCE_PATH="$ROOT/simulation/gazebo/models${GZ_SIM_RESOURCE_PATH:+:$GZ_SIM_RESOURCE_PATH}"
  wsl_gfx_env
}

# Mesa falls back to the CPU renderer (llvmpipe) inside WSL unless told to use the D3D12 GPU
# driver; hybrid laptops also expose an iGPU, so name the NVIDIA adapter.
wsl_gfx_env() {
  [ "$IN_WSL" = 1 ] && [ -e /dev/dxg ] || return 0
  export GALLIUM_DRIVER="${GALLIUM_DRIVER:-d3d12}"
  export MESA_D3D12_DEFAULT_ADAPTER_NAME="${MESA_D3D12_DEFAULT_ADAPTER_NAME:-NVIDIA}"
  export LD_LIBRARY_PATH="/usr/lib/wsl/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
}

# --------------------------------------------------------------------- process control
alive() { [ -f "$PIDS/$1.pid" ] && kill -0 "$(cat "$PIDS/$1.pid")" 2>/dev/null; }

# Each service runs in its own session, so stop can take down its whole process tree
# (ros2 launch and gz sim fork many children).
spawn() {
  local name="$1"; shift
  alive "$name" && { warn "$name already running (pid $(cat "$PIDS/$name.pid"))"; return 0; }
  mkdir -p "$PIDS" "$LOGS"
  : > "$LOGS/$name.log"
  setsid bash -c 'echo $$ > "$1"; shift; exec "$@"' _ "$PIDS/$name.pid" "$@" >> "$LOGS/$name.log" 2>&1 < /dev/null &
  sleep 1
  alive "$name" || { warn "$name exited immediately:"; tail -n 15 "$LOGS/$name.log" >&2; return 1; }
}

kill_service() {
  local name="$1" pid; alive "$name" || { rm -f "$PIDS/$name.pid"; return 0; }
  pid="$(cat "$PIDS/$name.pid")"
  info "Stopping $name ..."
  kill -TERM "$pid" 2>/dev/null                        # leader first: ros2 launch / uvicorn shut their children down
  for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  kill -TERM -- "-$pid" 2>/dev/null; sleep 1
  pkill -KILL -s "$pid" 2>/dev/null                    # anything still in the session
  rm -f "$PIDS/$name.pid"
}

wait_http() {   # name url seconds
  for _ in $(seq 1 "$3"); do curl -sf "$2" >/dev/null 2>&1 && return 0; sleep 1; done
  warn "$1 not healthy after $3 s - see: ./nayantra.sh logs"; return 1
}

robot_names() {
  local spec="${GZ_ROBOTS:-$(python3 -c "import sys; sys.path.insert(0,'$ROOT/simulation/gazebo'); from robots import DEFAULT_ROBOTS; print(DEFAULT_ROBOTS)")}"
  echo "$spec" | tr ',' '\n' | cut -d: -f1
}

# The Nav2 lifecycle nodes that must be active before goals are accepted: one stack per Gazebo
# robot, a single un-namespaced one for Isaac Sim's Carter.
nav2_nodes() {
  if [ "$BACKEND" = isaac ]; then echo /bt_navigator; else local r; for r in $(robot_names); do echo "/$r/bt_navigator"; done; fi
}

wait_nav2() {   # every bt_navigator must be active, or goals are rejected
  local deadline=$((SECONDS + ${1:-240})) n pending
  while [ $SECONDS -lt $deadline ]; do
    pending=""
    for n in $(nav2_nodes); do
      ros2 lifecycle get "$n" 2>/dev/null | grep -q "^active" || pending="$pending $n"
    done
    [ -z "$pending" ] && return 0
    alive "$SIM_SVC" || { warn "the simulation stopped while starting - see: ./nayantra.sh logs $SIM_SVC"; return 1; }
    sleep 3
  done
  warn "Nav2 not active for:$pending (continuing; see ./nayantra.sh logs)"; return 1
}

wait_log() {    # service marker seconds: wait until the service has printed the marker
  local svc="$1" marker="$2" deadline=$((SECONDS + $3)) next=$((SECONDS + 60))
  while [ $SECONDS -lt $deadline ]; do
    grep -q "$marker" "$LOGS/$svc.log" 2>/dev/null && return 0
    alive "$svc" || { warn "$svc stopped while starting:"; tail -n 15 "$LOGS/$svc.log" >&2; return 1; }
    [ $SECONDS -ge $next ] && { info "still starting $svc ($((deadline - SECONDS)) s left) ..."; next=$((SECONDS + 60)); }
    sleep 3
  done
  warn "$svc not ready after $3 s - see: ./nayantra.sh logs $svc"; return 1
}

# --------------------------------------------------------------------- dashboard build
web_stale() {
  local idx="$WEB_BUILD/dist/index.html"
  [ -f "$idx" ] || return 0
  [ -n "$(find "$ROOT/web/src" "$ROOT/web/index.html" "$ROOT/web/package.json" -newer "$idx" -print -quit 2>/dev/null)" ]
}

build_web() {
  command -v node >/dev/null 2>&1 || { warn "node not installed - run ./nayantra.sh setup"; return 1; }
  if [ "$WEB_BUILD" != "$ROOT/web" ]; then   # build on the Linux disk; Windows' node_modules is for Windows
    mkdir -p "$WEB_BUILD"
    find "$WEB_BUILD" -mindepth 1 -maxdepth 1 ! -name node_modules ! -name dist -exec rm -rf {} +
    tar -C "$ROOT/web" --exclude=node_modules --exclude=dist -cf - . | tar -C "$WEB_BUILD" -xf -
  fi
  info "Building the dashboard ..."
  (cd "$WEB_BUILD" && { npm ci --no-audit --no-fund || npm install --no-audit --no-fund; } >"$LOGS/web-build.log" 2>&1 \
     && npm run build >>"$LOGS/web-build.log" 2>&1) || { warn "dashboard build failed - see $LOGS/web-build.log"; return 1; }
}

# =============================================================================
# setup
# =============================================================================
pkgs_missing() { local p; for p in "$@"; do dpkg -s "$p" >/dev/null 2>&1 || return 0; done; return 1; }

# ROS 2 and Nav2 for either simulator, plus Gazebo only when that is the one in use.
setup_system() {
  local SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  local cn; cn="$(codename)"; local rd; rd="$(distro_for "$cn")"
  [ -n "$rd" ] || die "Unsupported OS '$cn'. Use Ubuntu 24.04 (ROS 2 Jazzy) or 22.04 (Humble)."
  local base=(curl ca-certificates gnupg lsb-release software-properties-common
              build-essential python3-venv python3-pip python3-dev xz-utils mesa-utils)
  local ros=("ros-$rd-ros-base" "ros-$rd-navigation2" "ros-$rd-nav2-bringup" "ros-$rd-tf2-ros" python3-numpy python3-yaml)
  local what="ROS 2 $rd and Nav2"
  if [ "$BACKEND" = gazebo ]; then
    local gz="ros-$rd-ros-gz"; [ "$rd" = humble ] && gz="ros-humble-ros-gzharmonic"
    ros+=("$gz"); what="$what, Gazebo Harmonic"
  fi
  if ! pkgs_missing "${base[@]}" "${ros[@]}"; then info "System packages ($what) are already installed."; return 0; fi
  info "Installing $what (several GB; this takes a while) ..."
  export DEBIAN_FRONTEND=noninteractive
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq "${base[@]}"
  $SUDO add-apt-repository -y universe >/dev/null
  if [ ! -f /usr/share/keyrings/ros-archive-keyring.gpg ]; then
    $SUDO curl -fsSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key -o /usr/share/keyrings/ros-archive-keyring.gpg
  fi
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $cn main" \
    | $SUDO tee /etc/apt/sources.list.d/ros2.list >/dev/null
  $SUDO apt-get update -qq
  $SUDO apt-get install -y "${ros[@]}"
}

# Isaac Sim 6 from NVIDIA's pip index into its own venv (scripts/install_isaac_pip.sh), unless a venv
# that already has it can be found: nobody should download 20 GB twice.
install_isaac() {
  local found; if found="$(isaac_env)"; then info "Isaac Sim already installed at $found - not downloading it again."; return 0; fi
  info "Installing Isaac Sim into $ISAAC_DIR/isaacsim-env (10-20 GB; this takes a long while) ..."
  mkdir -p "$ISAAC_DIR"
  ENV_DIR="$ISAAC_DIR/isaacsim-env" BASE_DIR="$ISAAC_DIR" bash "$ROOT/scripts/install_isaac_pip.sh" || die "Isaac Sim install failed"
}

setup_user() {
  mkdir -p "$STATE" "$LOGS" "$PIDS"
  if [ ! -d "$VENV" ]; then
    info "Creating the Python environment ($VENV) ..."
    python3 -m venv --system-site-packages "$VENV" || die "venv failed (apt install python3-venv)"
  fi
  info "Installing Nayantra into it ..."
  "$VENV/bin/pip" install --quiet --upgrade pip
  "$VENV/bin/pip" install --quiet -e "$ROOT" || die "pip install failed"

  if ! command -v node >/dev/null 2>&1 || ! node -e 'process.exit(+(parseInt(process.versions.node)<20))'; then
    info "Installing Node.js 22 for the dashboard build ..."
    mkdir -p "$NODE_DIR"
    curl -fsSL https://nodejs.org/dist/v22.12.0/node-v22.12.0-linux-x64.tar.xz | tar -xJ -C "$NODE_DIR" --strip-components=1 \
      || die "could not download Node.js"
  fi
  build_web || true

  if [ ! -f "$ROOT/.env" ] && [ -f "$ROOT/config/.env.example" ]; then
    cp "$ROOT/config/.env.example" "$ROOT/.env"
    info "Created .env from config/.env.example - add an LLM API key there to use the dashboard's command bar."
  fi

  if [ "$BACKEND" = gazebo ]; then
    python3 "$ROOT/simulation/gazebo/generate_world.py" --check >/dev/null 2>&1 || python3 "$ROOT/simulation/gazebo/generate_world.py"
  else
    install_isaac
  fi
  echo "$BACKEND" > "$BACKEND_FILE"
  info "Setup complete ($(simname "$BACKEND")). Next: ./nayantra.sh start"
}

# NAYANTRA_DETECT_FROM=machine.json judges the machine described in that file (see `detect --json`)
# instead of this one: handy for support, and it lets the tests play any machine.
detect_machine() {
  python3 "$ROOT/scripts/detect_machine.py" --path "$ISAAC_DIR" ${NAYANTRA_DETECT_FROM:+--from-json "$NAYANTRA_DETECT_FROM"} "$@"
}

# Decide which simulator to set up and print it: the --sim flag (or NAYANTRA_SIM), else the menu on a
# terminal, else auto. "auto" asks detect_machine; Isaac Sim, which is a big download and
# cannot run everywhere, is only chosen with consent and never on a machine that cannot run it.
choose_backend() {
  local want="$SIM_ARG" rec isaac_lvl gz_lvl note=""
  read -r rec isaac_lvl gz_lvl < <(detect_machine --brief)
  [ -n "$rec" ] || die "could not check this machine (is python3 installed?)"
  if [ -z "$want" ] && is_tty; then
    case "$isaac_lvl" in
      no) note="[not possible on this machine]" ;;
      marginal) note="[below NVIDIA's minimum here]" ;;
    esac
    want="$(ask_simulator "$rec" "$note")"
  fi
  want="${want:-auto}"
  if [ "$want" = auto ]; then
    if [ "$rec" = none ]; then detect_machine >&2; die "this machine can run neither simulator (reasons above)"; fi
    want="$rec"
    info "Checked this machine: $(simname "$want") suits it best." >&2   # stdout is the answer
    if [ "$want" = isaac ] && ! isaac_env >/dev/null; then
      confirm "Isaac Sim is a 10-20 GB download. Install it?" 1 \
        || die "Isaac Sim needs a large download, so it needs a yes: re-run with --yes, or use --sim gazebo."
    fi
  elif [ "$want" = isaac ]; then
    case "$isaac_lvl" in
      no) detect_machine >&2; die "Isaac Sim cannot run on this machine (reasons above). Gazebo can: ./nayantra.sh setup --sim gazebo" ;;
      marginal)
        detect_machine >&2
        confirm "This machine is below NVIDIA's minimum for Isaac Sim; it may run poorly or not at all. Continue anyway?" 0 \
          || die "Cancelled. Gazebo needs far less: ./nayantra.sh setup --sim gazebo" ;;
    esac
  fi
  echo "$want"
}

describe_plan() {
  echo "  Plan"
  echo "    simulator   $(simname "$BACKEND")"
  if [ "$BACKEND" = gazebo ]; then
    echo "    installs    ROS 2 $(distro_for "$(codename)"), Nav2 and Gazebo Harmonic (apt), a Python env, the dashboard build"
  else
    echo "    installs    ROS 2 $(distro_for "$(codename)") and Nav2 (apt), a Python env, the dashboard build"
    local found; if found="$(isaac_env)"; then echo "    Isaac Sim   found at $found, no download"
    else echo "    Isaac Sim   downloaded from NVIDIA's pip index into $ISAAC_DIR/isaacsim-env (10-20 GB)"; fi
    echo "    licence     Isaac Sim is NVIDIA software under its own licence; starting it accepts NVIDIA's EULA"
  fi
}

cmd_setup() {
  parse_setup_args "$@"
  local backend
  if [ "$PHASE" = both ]; then
    backend="$(choose_backend)" || exit 1
  else                                    # the Windows side has already chosen
    backend="${SIM_ARG:-gazebo}"; [ "$backend" = auto ] && backend=gazebo
  fi
  use_backend "$backend"
  [ "$PHASE" = both ] && describe_plan
  if [ "$DRY_RUN" = 1 ]; then info "Dry run: nothing was changed."; return 0; fi
  [ "$PHASE" != user ] && setup_system
  [ "$PHASE" != system ] && setup_user
  return 0
}

# =============================================================================
# start / stop / status / logs / doctor
# =============================================================================
start_gazebo() {
  command -v gz >/dev/null 2>&1 || die "Gazebo (gz) not found - run ./nayantra.sh setup --sim gazebo"
  python3 "$ROOT/simulation/gazebo/generate_world.py" --check >/dev/null 2>&1 || python3 "$ROOT/simulation/gazebo/generate_world.py"

  # A lost DDS service reply can leave one robot's Nav2 lifecycle manager waiting forever (seen
  # roughly once in ten starts, and it cannot be nudged from outside), so if Nav2 is not active on
  # every robot in time, bring the simulation down and up again.
  local attempt
  for attempt in 1 2 3; do
    info "Starting Gazebo + Nav2 ($(robot_names | paste -sd, -)) ..."
    # The simulation server runs on its own; the window is a separate process (start_gui) so a
    # crash in the GPU driver's GL code takes down the window, not the simulation.
    GZ_HEADLESS=1 spawn sim ros2 launch "$ROOT/simulation/gazebo/launch/gazebo_warehouse.launch.py" || { kill_service sim; exit 1; }
    info "Waiting for Nav2 to come up on every robot (first run can take a minute) ..."
    wait_nav2 "${NAYANTRA_NAV2_WAIT:-90}" && break
    [ "$attempt" = 3 ] && { warn "Nav2 is still not active on every robot; continuing (see ./nayantra.sh logs sim)"; break; }
    warn "restarting the simulation (attempt $((attempt + 1)) of 3) ..."
    kill_service sim
  done
}

# Isaac Sim and Nav2 are started the way scripts/run_demo.sh does it (that script owns the recipe:
# robot asset, lidar, stream settings); this only orders the processes and waits for each to be ready.
start_isaac() {
  [ "$IN_WSL" = 1 ] && die "Isaac Sim does not run in WSL2 - use: ./nayantra.sh start --sim gazebo"
  local envdir; envdir="$(isaac_env)" || die "Isaac Sim is not installed - run ./nayantra.sh setup --sim isaac"
  ros2 pkg prefix nav2_bringup >/dev/null 2>&1 || die "Nav2 not found - run ./nayantra.sh setup --sim isaac"
  STREAM_IP="${NAYANTRA_PUBLIC_IP:-${PUBLIC_IP:-$(host_ip)}}"
  info "Starting Isaac Sim (the first launch compiles shaders and can take several minutes) ..."
  spawn isaac env VENV="$envdir" PUBLIC_IP="$STREAM_IP" bash "$ROOT/scripts/run_demo.sh" isaac || { kill_service isaac; exit 1; }
  wait_log isaac ROS2_READY "${NAYANTRA_ISAAC_WAIT:-900}" || { cmd_stop; exit 1; }

  local attempt
  for attempt in 1 2 3; do
    info "Starting Nav2 ..."
    spawn nav2 bash "$ROOT/scripts/run_demo.sh" nav2 || { cmd_stop; exit 1; }
    wait_nav2 "${NAYANTRA_NAV2_WAIT:-90}" && break
    [ "$attempt" = 3 ] && { warn "Nav2 is still not active; continuing (see ./nayantra.sh logs nav2)"; break; }
    warn "restarting Nav2 (attempt $((attempt + 1)) of 3) ..."
    kill_service nav2
  done
}

cmd_start() {
  local headless=0 open=1 reset=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --headless) headless=1 ;; --no-open) open=0 ;; --reset) reset="--reset" ;;
      --sim) [ $# -ge 2 ] || die "--sim needs gazebo or isaac"; use_backend "$2"; shift ;;
      --sim=*) use_backend "${1#--sim=}" ;;
      *) die "unknown option: $1" ;;
    esac
    shift
  done

  [ -x "$VENV/bin/nayantra-core" ] || die "not set up yet - run ./nayantra.sh setup"
  ros_env
  if alive core || alive sim || alive isaac || alive nav2; then die "already running - ./nayantra.sh status | stop | restart"; fi
  mkdir -p "$LOGS" "$PIDS"
  echo "$BACKEND" > "$PIDS/backend"

  web_stale && build_web
  "start_$BACKEND"

  # NAYANTRA_CORE_URL is how the MCP server and agent find the core: it must follow the port in use.
  export NAYANTRA_SCENARIO="$SCENARIO" NAYANTRA_DB_PATH="$DB" CORE_HOST=127.0.0.1 CORE_PORT="$CORE_PORT"          NAYANTRA_CORE_URL="http://127.0.0.1:$CORE_PORT"
  [ "$WEB_BUILD" != "$ROOT/web" ] && export WEB_DIST_DIR="$WEB_BUILD/dist"
  info "Starting the core and dashboard ..."
  spawn core "$VENV/bin/nayantra-core" --scenario "$SCENARIO" --db "$DB" --port "$CORE_PORT" $reset || { cmd_stop; exit 1; }
  echo "$CORE_PORT" > "$PIDS/core.port"
  wait_http core "http://127.0.0.1:$CORE_PORT/api/v1/health" 60 || true
  spawn mcp "$VENV/bin/nayantra-mcp-server"
  spawn agent "$VENV/bin/python" -m uvicorn nayantra.agent.api_v2:app --host 127.0.0.1 --port 8080
  [ "$BACKEND" = gazebo ] && [ "$headless" = 0 ] && start_gui

  local view logs
  if [ "$BACKEND" = isaac ]; then
    view="Isaac Sim   connect the Isaac Sim WebRTC Streaming Client to $STREAM_IP (TCP 49100)"
    logs="isaac|nav2|core|mcp|agent"
  else
    view="Gazebo      $(alive gui && echo "window on your desktop (./nayantra.sh gui reopens it)" || echo "no window (./nayantra.sh gui opens one)")"
    logs="sim|core|mcp|agent|gui"
  fi
  cat <<EOF

  ${G}Nayantra is running${N} with $(simname "$BACKEND")
    Dashboard   http://localhost:$CORE_PORT/
    API docs    http://localhost:$CORE_PORT/docs
    $view
    Logs        ./nayantra.sh logs [$logs]
    Stop        ./nayantra.sh stop
EOF
  [ "$open" = 1 ] && open_url "http://localhost:$CORE_PORT/"
  return 0
}

# Whole seconds of simulation time, or nothing if the server does not answer.
sim_sec() {
  timeout 5 gz topic -e -t /world/nayantra_warehouse/stats -n 1 2>/dev/null | tr '\n' ' ' \
    | grep -oE 'sim_time \{ +sec: [0-9]+' | grep -oE '[0-9]+$'
}
sim_advancing() { local a b; a="$(sim_sec)"; sleep 4; b="$(sim_sec)"; [ -n "$a" ] && [ -n "$b" ] && [ "$b" -gt "$a" ]; }
sim_unpause() {
  timeout 6 gz service -s /world/nayantra_warehouse/control --reqtype gz.msgs.WorldControl     --reptype gz.msgs.Boolean --timeout 4000 --req 'pause: false' >/dev/null 2>&1
}

# Open the Gazebo window on the running simulation. NAYANTRA_GUI_RENDER picks how it draws:
#   auto (default)  the GPU, falling back to software rendering if the window fails
#   gpu | software  force one
# The NVIDIA WSL driver is the weak link: the window can crash while it is created, and once it
# was seen to leave the simulation frozen. Either way the simulation is checked afterwards, and a
# stuck one is unpaused and the window reopened with software rendering.
open_gui() {
  alive sim || { warn "the simulation is not running - ./nayantra.sh start"; return 1; }
  local mode="${NAYANTRA_GUI_RENDER:-auto}"
  if [ "$mode" = software ]; then GALLIUM_DRIVER=llvmpipe spawn gui gz sim -g || true; return 0; fi
  spawn gui gz sim -g || true
  sleep 6
  if alive gui; then sim_advancing && return 0; warn "the simulation stopped when the window opened"; else
    warn "Gazebo window crashed on the GPU"; fi
  [ "$mode" = gpu ] && return 1
  kill_service gui
  sim_unpause; sim_advancing || warn "the simulation is still not advancing - see: ./nayantra.sh logs sim"
  warn "reopening the Gazebo window with software rendering ..."
  GALLIUM_DRIVER=llvmpipe spawn gui gz sim -g || true
  sleep 4
  alive gui || warn "could not open the Gazebo window - see: ./nayantra.sh logs gui"
}

# Frame the whole warehouse from above (the default camera sits low and to one side).
gui_camera() {
  sleep 2
  timeout 8 gz service -s /gui/move_to/pose --reqtype gz.msgs.GUICamera --reptype gz.msgs.Boolean --timeout 4000 \
    --req 'pose: {position: {x: 0, y: -15, z: 14}, orientation: {x: -0.2589, y: 0.2589, z: 0.658, w: 0.658}}' >/dev/null 2>&1
}

start_gui() { open_gui; alive gui && gui_camera; return 0; }

cmd_gui() {
  ros_env
  alive isaac && { info "Isaac Sim is viewed through the Isaac Sim WebRTC Streaming Client, not a Gazebo window."; return 0; }
  alive gui && { info "window already open"; return 0; }
  start_gui
}

open_url() {
  if [ "$IN_WSL" = 1 ] && [ -x /mnt/c/Windows/explorer.exe ]; then /mnt/c/Windows/explorer.exe "$1" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1 || true
  fi
}

cmd_stop() {
  local any=0
  # Whatever is running comes down, whichever simulator started it: clients first, then the simulator.
  for s in gui agent mcp core nav2 isaac sim; do alive "$s" && any=1; kill_service "$s"; done
  rm -f "$PIDS/backend"
  ( ros_env 2>/dev/null; command -v ros2 >/dev/null && ros2 daemon stop >/dev/null 2>&1 ) || true
  [ "${1:-}" != keep ] && pkill -f '^sleep 2147483647$' 2>/dev/null   # the Windows side's keep-alive session
  [ "$any" = 1 ] && info "Stopped." || info "Nothing was running."
}

cmd_status() {
  local b; b="$(cat "$PIDS/backend" 2>/dev/null)"; b="${b:-$(active_backend)}"   # the running stack's, else the chosen one
  echo "  simulator: $(simname "$b")"
  local s svcs="sim core mcp agent gui"; [ "$b" = isaac ] && svcs="isaac nav2 core mcp agent"
  for s in $svcs; do
    if alive "$s"; then printf "  %-6s running (pid %s)\n" "$s" "$(cat "$PIDS/$s.pid")"; else printf "  %-6s stopped\n" "$s"; fi
  done
  alive core && echo "  dashboard: http://localhost:$(cat "$PIDS/core.port" 2>/dev/null || echo "$CORE_PORT")/"
  return 0
}

cmd_logs() {
  local svc="${1:-core}"; [ "$svc" = all ] && svc="*"
  tail -n 60 -F $LOGS/$svc.log
}

cmd_doctor() {
  echo "OS         $(. /etc/os-release; echo "$PRETTY_NAME")$([ "$IN_WSL" = 1 ] && echo ' (WSL2)')"
  echo "repo       $ROOT"; echo "state      $STATE"
  echo "simulator  $(simname "$BACKEND") (chosen by setup; change it with ./nayantra.sh setup --sim ...)"
  local f; f="$(ros_setup)"
  if [ -f "$f" ]; then ( ros_env; echo "ROS 2      $ROS_DISTRO"
      if [ "$BACKEND" = gazebo ]; then
        printf "gazebo     "; gz sim --versions 2>/dev/null | head -1 || echo MISSING
        for p in nav2_bringup ros_gz_sim ros_gz_bridge; do printf "%-10s " "$p"; ros2 pkg prefix "$p" >/dev/null 2>&1 && echo OK || echo MISSING; done
      else
        printf "%-10s " nav2_bringup; ros2 pkg prefix nav2_bringup >/dev/null 2>&1 && echo OK || echo MISSING
      fi )
  else echo "ROS 2      not installed"; fi
  if [ "$BACKEND" = isaac ]; then
    local ie; if ie="$(isaac_env)"; then echo "isaac sim  $ie"; else echo "isaac sim  not installed - ./nayantra.sh setup --sim isaac"; fi
  fi
  [ -x "$VENV/bin/nayantra-core" ] && echo "python env OK" || echo "python env missing"
  [ -f "$WEB_BUILD/dist/index.html" ] && echo "dashboard  built" || echo "dashboard  not built"
  if [ "$IN_WSL" = 1 ]; then
    [ -e /dev/dxg ] && echo "GPU        /dev/dxg present (WSL GPU passthrough)" || echo "GPU        /dev/dxg MISSING - update the Windows NVIDIA driver / WSL"
    [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && echo "display    ${DISPLAY:-}${WAYLAND_DISPLAY:+ $WAYLAND_DISPLAY} (WSLg)" || echo "display    none - the Gazebo window needs WSLg (wsl --update)"
    command -v glxinfo >/dev/null && ( wsl_gfx_env; glxinfo -B 2>/dev/null | grep -E "OpenGL (renderer|version)" | sed 's/^/gl         /' )
  fi
  echo; detect_machine
}

# Run as root by the Windows side right after it creates the distro.
wsl_init() {
  local user="$1"
  id "$user" >/dev/null 2>&1 || useradd -m -s /bin/bash -G sudo,video,render,audio "$user"
  printf '[user]\ndefault=%s\n[interop]\nappendWindowsPath=false\n' "$user" > /etc/wsl.conf
  info "Created Linux user '$user' (no password; set one with: wsl -d $DISTRO -u root passwd $user)"
}

[ "${BASH_SOURCE[0]}" = "$0" ] || return 0     # sourced (by the tests): the functions are all that is wanted

case "${1:-}" in
  _wsl-init) wsl_init "${2:?user}" ;;
  setup)   cmd_setup "${@:2}" ;;
  detect)  detect_machine "${@:2}" ;;
  start)   cmd_start "${@:2}" ;;
  stop)    cmd_stop ;;
  restart) cmd_stop keep; cmd_start "${@:2}" ;;
  status)  cmd_status ;;
  logs)    cmd_logs "${2:-}" ;;
  gui)     cmd_gui ;;
  doctor)  cmd_doctor ;;
  ""|-h|--help|help) usage ;;
  *)       usage >&2; exit 1 ;;
esac
