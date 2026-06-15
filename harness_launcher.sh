#!/usr/bin/env bash
# Harness launcher — runs EVERY agent on this machine in one process.
#
# Unlike bridge_launcher.sh (one persona per process), this sources only the
# machine-wide settings (.env.defaults) into the environment and lets the
# harness discover the agent roster from the .env / .env.<persona> files
# itself — each agent keeps its own token, identity, folders, and IPC socket;
# they share one scheduler (the machine-wide concurrency cap).
#
# This REPLACES the per-persona LaunchAgents: run this OR the per-persona
# bridges, never both (two processes would double-register the same agents).

set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

if [ ! -x "venv/bin/python" ]; then
    echo "Error: venv/bin/python not found in $REPO_DIR. Run ./setup.sh first." >&2
    exit 1
fi

# Machine-wide settings only (recording, exec mode, limits). Per-agent
# identity is read from the .env files by the harness, not sourced here.
set -a
# shellcheck source=/dev/null
[ -f ".env.defaults" ] && source ".env.defaults"
set +a

# Optional: machine-wide concurrency cap (default 8). Override in .env.defaults.
export MAX_CONCURRENT_MACHINE="${MAX_CONCURRENT_MACHINE:-8}"

exec ./venv/bin/python bridge.py
