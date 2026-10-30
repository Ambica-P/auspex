# Sourced by the other scripts. Bash, WSL2.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Core scripts run in the uv virtualenv. A ROS-sourced shell (.bashrc) exports a PYTHONPATH
# that leaks ROS packages and pytest plugins into it, so drop it here. Scripts that need ROS
# (run-sim, check-levers) source ROS explicitly after this, via source_ros.
unset PYTHONPATH

die() { echo "FAIL $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' not found: $2"; }

# docker compose reads .env from the repository root by itself.
require_env_file() {
  [[ -f .env ]] || die ".env missing: cp .env.example .env and fill it in"
}

# Read one KEY=value from .env without sourcing the whole file. Prints the default if unset.
env_value() {
  local key="$1" default="${2:-}" line
  line="$(grep -E "^${key}=" .env 2>/dev/null | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  echo "${line:-$default}"
}

# Source ROS 2 Kilted and the rmf_demos overlay (paths from .env, else the defaults).
source_ros() {
  local ros_setup overlay
  ros_setup="$(env_value ROS_SETUP /opt/ros/kilted/setup.bash)"
  overlay="$(env_value ROS_OVERLAY_SETUP "$HOME/rmf_ws/install/setup.bash")"
  [[ -f "$ros_setup" ]] || die "ROS setup not found: $ros_setup (set ROS_SETUP in .env)"
  [[ -f "$overlay" ]] || die "overlay setup not found: $overlay (set ROS_OVERLAY_SETUP in .env)"
  set +u
  # shellcheck disable=SC1090
  source "$ros_setup"
  # shellcheck disable=SC1090
  source "$overlay"
  set -u
  # Fast DDS over UDP only. Its shared-memory transport left processes hung or invisible to
  # each other on WSL2 (tinyRobot adapter, 2026-10-26); with UDPv4 both fleets were seen.
  # Override with FASTDDS_BUILTIN_TRANSPORTS in .env (e.g. DEFAULT) if ever needed.
  FASTDDS_BUILTIN_TRANSPORTS="$(env_value FASTDDS_BUILTIN_TRANSPORTS UDPv4)"
  export FASTDDS_BUILTIN_TRANSPORTS
}
