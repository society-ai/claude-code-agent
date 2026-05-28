#!/usr/bin/env bash
# Society AI + Claude Code — one-command setup
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "========================================"
echo "  Society AI + Claude Code Setup"
echo "========================================"
echo ""

# 1. Check prerequisites
echo "[1/5] Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is required. Install it from https://python.org" >&2
    exit 1
fi

if ! command -v claude &>/dev/null; then
    echo "Error: Claude Code CLI is required." >&2
    echo "Install it: npm install -g @anthropic-ai/claude-code" >&2
    exit 1
fi

echo "  python3 ... OK"
echo "  claude  ... OK"

# 2. Create virtual environment
echo ""
echo "[2/5] Setting up Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
    echo "  Created venv/"
else
    echo "  venv/ already exists"
fi
# shellcheck source=/dev/null
source venv/bin/activate
pip install -q -r requirements.txt
echo "  Dependencies installed"

# 3. Get API key + agent name
echo ""
echo "[3/5] Configuring API key..."
if [ -f ".env" ]; then
    echo "  .env already exists — leaving it alone."
    echo "  To reconfigure, delete .env and re-run ./setup.sh"
else
    echo ""
    echo "  You need a Society AI API key to connect."
    echo "  Get one at: https://societyai.com"
    echo ""
    # Read the key (suppress -r warning if not a real terminal)
    API_KEY=""
    read -rp "  Enter your API key (sai_...): " API_KEY || true
    API_KEY="${API_KEY#"${API_KEY%%[![:space:]]*}"}"  # trim leading whitespace
    API_KEY="${API_KEY%"${API_KEY##*[![:space:]]}"}"  # trim trailing whitespace

    if [ -z "$API_KEY" ]; then
        echo "  Error: API key is required to continue. Re-run ./setup.sh and paste your sai_... key." >&2
        exit 1
    fi
    case "$API_KEY" in
        sai_*) ;;
        *)
            echo "  Error: API key must start with 'sai_'. Got: ${API_KEY:0:8}..." >&2
            echo "  Get your key at https://societyai.com" >&2
            exit 1
            ;;
    esac

    # Default AGENT_NAME to a per-host value so two users on the same org
    # don't collide on the WS hub (which rejects duplicate connections).
    HOST_SHORT="$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/^-*//;s/-*$//' || true)"
    USER_SHORT="$(id -un 2>/dev/null | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9' '-' | sed 's/^-*//;s/-*$//' || true)"
    DEFAULT_AGENT_NAME="claude-code"
    if [ -n "$USER_SHORT" ] && [ -n "$HOST_SHORT" ]; then
        DEFAULT_AGENT_NAME="claude-code-${USER_SHORT}-${HOST_SHORT}"
    elif [ -n "$HOST_SHORT" ]; then
        DEFAULT_AGENT_NAME="claude-code-${HOST_SHORT}"
    fi

    # Write .env safely via Python to avoid sed/shell injection
    python3 - "$API_KEY" "$DEFAULT_AGENT_NAME" <<'PYEOF'
import os, sys, shutil, re
key, agent_name = sys.argv[1], sys.argv[2]
shutil.copy(".env.example", ".env")
with open(".env") as f:
    content = f.read()
content = content.replace("sai_your_api_key_here", key)
# Inject AGENT_NAME default if user didn't already uncomment it
if not re.search(r"^AGENT_NAME=", content, re.M):
    content = re.sub(r"# AGENT_NAME=claude-code", f"AGENT_NAME={agent_name}", content)
with open(".env", "w") as f:
    f.write(content)
# .env holds the API key — restrict to owner-only.
try:
    os.chmod(".env", 0o600)
except OSError:
    pass
print(f"  API key saved to .env (mode 0600)")
print(f"  AGENT_NAME defaulted to {agent_name}")
PYEOF
fi

# 4. Configure Claude Code MCP server
echo ""
echo "[4/5] Configuring Claude Code MCP server..."

# Load env vars for the config script (set -a exports them)
set -a
# shellcheck source=/dev/null
source .env 2>/dev/null || true
set +a

# Verify the env we just sourced is usable BEFORE writing settings.json
if [ -z "${SOCIETY_AI_AUTH_TOKEN:-}" ]; then
    echo "  Error: SOCIETY_AI_AUTH_TOKEN is empty after sourcing .env." >&2
    echo "  Open .env and set it, then re-run ./setup.sh" >&2
    exit 1
fi

# Use a dedicated Python script to safely handle paths with special characters
python3 "$REPO_DIR/configure_claude.py"

# 5. Done
echo ""
echo "[5/5] Setup complete!"
echo ""
echo "========================================"
echo "  Next steps:"
echo "========================================"
echo ""
echo "  1. Start the bridge (keeps Claude Code connected to Society AI):"
echo ""
echo "     source .env && source venv/bin/activate && python bridge.py"
echo ""
echo "  2. Chat with your Claude Code agent from societyai.com"
echo ""
echo "  3. Or use Society AI tools directly in Claude Code:"
echo ""
echo "     claude"
echo "     > list my tasks"
echo ""
echo "========================================"
