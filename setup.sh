#!/usr/bin/env bash
# Society AI + Claude Code — one-command setup
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

echo "========================================"
echo "  Society AI + Claude Code Setup"
echo "========================================"
echo ""

# 1. Check prerequisites
echo "[1/5] Checking prerequisites..."

if ! command -v python3 &>/dev/null; then
    echo "Error: python3 is required. Install it from https://python.org"
    exit 1
fi

if ! command -v claude &>/dev/null; then
    echo "Error: Claude Code CLI is required."
    echo "Install it: npm install -g @anthropic-ai/claude-code"
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
source venv/bin/activate
pip install -q -r requirements.txt
echo "  Dependencies installed"

# 3. Get API key
echo ""
echo "[3/5] Configuring API key..."
if [ -f ".env" ]; then
    echo "  .env already exists, skipping"
else
    echo ""
    echo "  You need a Society AI API key to connect."
    echo "  Get one at: https://societyai.com"
    echo ""
    read -rp "  Enter your API key (sai_...): " API_KEY
    # Write .env safely via Python to avoid sed/shell injection
    python3 - "${API_KEY:-}" <<'PYEOF'
import sys, shutil
key = sys.argv[1]
shutil.copy(".env.example", ".env")
if key:
    with open(".env") as f:
        content = f.read()
    content = content.replace("sai_your_api_key_here", key)
    with open(".env", "w") as f:
        f.write(content)
    print("  API key saved to .env")
else:
    print("  No key provided. You can set it later in .env")
PYEOF
fi

# 4. Configure Claude Code MCP server
echo ""
echo "[4/5] Configuring Claude Code MCP server..."

# Load env vars for the config script (set -a exports them)
set -a
source .env 2>/dev/null || true
set +a

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
