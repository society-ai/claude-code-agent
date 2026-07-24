#!/usr/bin/env bash
# Society AI Claude Code agent installer.
#
# Fetched and piped to bash by the one-command install the product renders:
#   curl -fsSL https://raw.githubusercontent.com/society-ai/claude-code-agent/main/install.sh \
#     | bash -s -- --token sai_... --name my-agent [--url http://...] --yes
#
# Clones the agent into ./claude-code-agent, or updates an existing checkout,
# then hands every argument to its setup.sh. Safe to re-run: an existing
# checkout is updated in place, never recreated, and setup.sh itself guards
# all of its own re-run cases.
set -u

REPO_URL="https://github.com/society-ai/claude-code-agent.git"
DIR="claude-code-agent"

if ! command -v git >/dev/null 2>&1; then
    echo ""
    echo "  Git is not installed. On a Mac it comes with Apple's developer"
    echo "  tools. Install them with:"
    echo ""
    echo "      xcode-select --install"
    echo ""
    echo "  Then run this command again."
    exit 1
fi

if [ -d "$DIR/.git" ]; then
    echo "  Found an existing $DIR folder. Updating it..."
    if ! git -C "$DIR" pull --ff-only >/dev/null 2>&1; then
        echo "  Could not update it (that is OK). Continuing with the version you have."
    fi
elif [ -e "$DIR" ]; then
    echo ""
    echo "  A '$DIR' file or folder already exists here, but it is not the"
    echo "  agent. Move or delete it, then run this command again."
    exit 1
else
    if ! git clone "$REPO_URL" "$DIR"; then
        echo ""
        echo "  Could not download the agent. Check your internet connection"
        echo "  and run this command again."
        exit 1
    fi
fi

cd "$DIR" || exit 1
exec ./setup.sh "$@"
