#!/usr/bin/env bash
# Bridge launcher used by the launchd LaunchAgent.
#
# Usage: bridge_launcher.sh [persona]
#
# Sources the persona's env file from the repo (where the user keeps their
# sai_… key and AGENT_NAME) and execs bridge.py. No arg = the default
# persona (.env); a persona name sources .env.<persona> instead. Keeping
# secrets in env files rather than the plist means the LaunchAgent file
# itself contains no credentials and can safely sit in
# ~/Library/LaunchAgents/ at the default permissions.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

PERSONA="${1:-}"
ENV_FILE=".env"
if [ -n "$PERSONA" ]; then
    ENV_FILE=".env.$PERSONA"
fi

if [ ! -f "$ENV_FILE" ]; then
    echo "Error: $ENV_FILE not found in $REPO_DIR. Run ./setup.sh first." >&2
    [ -n "$PERSONA" ] && echo "(For additional personas: ./setup.sh --persona $PERSONA)" >&2
    exit 1
fi

if [ ! -x "venv/bin/python" ]; then
    echo "Error: venv/bin/python not found in $REPO_DIR. Run ./setup.sh first." >&2
    exit 1
fi

# Load config into the environment so bridge.py picks it up. Machine-wide
# defaults (.env.defaults, managed by the local status panel) load FIRST so
# the persona's own .env can still override them; per-agent identity and
# access directories live only in the persona file.
set -a
# shellcheck source=/dev/null
[ -f ".env.defaults" ] && source ".env.defaults"
# shellcheck source=/dev/null
source "$ENV_FILE"
set +a

# Per-persona IPC socket so two bridges never collide. The env file
# normally sets this explicitly; this is a safety net for hand-rolled
# persona env files that forgot it.
if [ -n "$PERSONA" ] && [ -z "${SOCIETY_AI_BRIDGE_SOCKET:-}" ]; then
    export SOCIETY_AI_BRIDGE_SOCKET="$HOME/.cache/society-ai/$PERSONA/bridge.sock"
fi

exec ./venv/bin/python bridge.py
