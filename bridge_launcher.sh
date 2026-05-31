#!/usr/bin/env bash
# Bridge launcher used by the launchd LaunchAgent.
#
# Sources .env from the repo (where the user keeps their sai_… key and
# AGENT_NAME) and execs bridge.py. Keeping secrets in .env rather than the
# plist means the LaunchAgent file itself contains no credentials and can
# safely sit in ~/Library/LaunchAgents/ at the default permissions.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

if [ ! -f ".env" ]; then
    echo "Error: .env not found in $REPO_DIR. Run ./setup.sh first." >&2
    exit 1
fi

if [ ! -x "venv/bin/python" ]; then
    echo "Error: venv/bin/python not found in $REPO_DIR. Run ./setup.sh first." >&2
    exit 1
fi

# Load .env into the environment so bridge.py picks it up.
set -a
# shellcheck source=/dev/null
source .env
set +a

exec ./venv/bin/python bridge.py
