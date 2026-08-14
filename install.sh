#!/usr/bin/env bash
# Society AI Claude Code agent installer.
#
# Fetched and piped to bash by the one-command install the product renders:
#   curl -fsSL https://raw.githubusercontent.com/society-ai/claude-code-agent/main/install.sh \
#     | bash -s -- --token sai_... --name my-agent [--url http://...] --yes
#
# Works from any folder. When this machine already has the agent installed,
# the existing checkout is found and updated in place wherever it lives, so
# the same command rotates a token or adds a persona without the user having
# to remember (or be told) which directory to stand in. Only a machine with
# no install at all clones, into ./claude-code-agent.
#
# Where it looks, in order:
#   1. The current folder, if it is itself a checkout.
#   2. ./claude-code-agent, if it is a checkout (the classic layout).
#   3. The folder recorded at ~/.cache/society-ai/install-dir.
#   4. The WorkingDirectory of every installed bridge LaunchAgent (macOS).
# Among 3 and 4, a checkout that already knows the --name being set up wins;
# failing that, a single known checkout is used. Two or more unrelated
# checkouts is the one case that stops and asks, because picking for the
# user could point a live agent's service at the wrong code.
#
# Everything past this file's own resolution is setup.sh's job: it receives
# every argument untouched and guards all of its own re-run cases.
set -u

REPO_URL="https://github.com/society-ai/claude-code-agent.git"
DIR="claude-code-agent"
STATE_DIR="$HOME/.cache/society-ai"
POINTER_FILE="$STATE_DIR/install-dir"
PLIST_DIR="$HOME/Library/LaunchAgents"

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

# ── Peek at the agent name ─────────────────────────────────────────────────
# Read-only pass over the arguments, used solely to pick between several
# existing checkouts. setup.sh parses all of them properly afterwards.
AGENT_NAME=""
expect_value=0
for arg in "$@"; do
    if [ "$expect_value" = 1 ]; then
        AGENT_NAME="$arg"
        expect_value=0
        continue
    fi
    case "$arg" in
        --name|--persona)     expect_value=1 ;;
        --name=*|--persona=*) AGENT_NAME="${arg#*=}" ;;
    esac
done

# ── Helpers ────────────────────────────────────────────────────────────────

# A directory is a usable install when it is a git checkout of this repo's
# working tree shape. setup.sh is the file we are about to exec, so its
# absence means the folder cannot serve us even if .git is there.
is_checkout() {
    [ -n "${1:-}" ] && [ -d "$1/.git" ] && [ -f "$1/setup.sh" ]
}

abs_path() {
    (cd "$1" 2>/dev/null && pwd)
}

# Does this checkout already hold the identity being set up? Either as an
# additional persona (.env.<name>) or as the primary one (AGENT_NAME in
# .env). Same read of AGENT_NAME that setup.sh does.
knows_agent() {
    local dir="$1" name="$2" primary
    [ -n "$name" ] || return 1
    [ -f "$dir/.env.$name" ] && return 0
    primary="$(grep -E '^AGENT_NAME=' "$dir/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)"
    [ "$primary" = "$name" ]
}

# WorkingDirectory out of an installed LaunchAgent plist. plistlib when
# python3 is around (it is what service.sh writes these with), otherwise a
# read of the XML the template produces.
plist_workdir() {
    local plist="$1" value=""
    if command -v python3 >/dev/null 2>&1; then
        value="$(python3 -c 'import plistlib, sys
with open(sys.argv[1], "rb") as f:
    print(plistlib.load(f).get("WorkingDirectory", ""))' "$plist" 2>/dev/null || true)"
    fi
    if [ -z "$value" ]; then
        value="$(grep -A1 '<key>WorkingDirectory</key>' "$plist" 2>/dev/null \
            | sed -n 's/.*<string>\(.*\)<\/string>.*/\1/p' | head -1 || true)"
    fi
    printf '%s' "$value"
}

CANDIDATES=""
add_candidate() {
    local dir resolved
    dir="${1:-}"
    [ -n "$dir" ] || return 0
    resolved="$(abs_path "$dir")" || return 0
    [ -n "$resolved" ] || return 0
    is_checkout "$resolved" || return 0
    printf '%s\n' "$CANDIDATES" | grep -qxF "$resolved" && return 0
    CANDIDATES="${CANDIDATES}${resolved}
"
}

remember_install_dir() {
    mkdir -p "$STATE_DIR" 2>/dev/null || return 0
    printf '%s\n' "$1" >"$POINTER_FILE" 2>/dev/null || true
}

# ── Resolve which checkout to use ──────────────────────────────────────────
TARGET=""

if is_checkout "$PWD"; then
    # Standing inside a checkout is an explicit choice: honour it, and never
    # let discovery second-guess it.
    TARGET="$PWD"
elif is_checkout "$PWD/$DIR"; then
    TARGET="$(abs_path "$PWD/$DIR")"
else
    if [ -f "$POINTER_FILE" ]; then
        add_candidate "$(head -1 "$POINTER_FILE" 2>/dev/null || true)"
    fi
    for plist in "$PLIST_DIR"/io.societyai.claude-code-bridge*.plist; do
        [ -f "$plist" ] || continue
        add_candidate "$(plist_workdir "$plist")"
    done

    NAMED=""
    NAMED_COUNT=0
    TOTAL_COUNT=0
    FIRST=""
    while IFS= read -r dir; do
        [ -n "$dir" ] || continue
        TOTAL_COUNT=$((TOTAL_COUNT + 1))
        [ -n "$FIRST" ] || FIRST="$dir"
        if knows_agent "$dir" "$AGENT_NAME"; then
            NAMED="$dir"
            NAMED_COUNT=$((NAMED_COUNT + 1))
        fi
    done <<EOF
$CANDIDATES
EOF

    if [ "$NAMED_COUNT" = 1 ]; then
        TARGET="$NAMED"
    elif [ "$NAMED_COUNT" -gt 1 ] || [ "$TOTAL_COUNT" -gt 1 ]; then
        echo ""
        echo "  This computer has the agent installed in more than one place:"
        echo ""
        printf '%s' "$CANDIDATES" | sed 's/^/      /'
        echo ""
        echo "  Open the folder you want to update, then run the same command"
        echo "  again from inside it. For example:"
        echo ""
        echo "      cd $FIRST"
        echo ""
        exit 1
    elif [ "$TOTAL_COUNT" = 1 ]; then
        TARGET="$FIRST"
    fi
fi

# ── Update in place, or clone when this machine has no agent yet ───────────
if [ -n "$TARGET" ]; then
    echo "  Using the agent already installed in $TARGET"
    if ! git -C "$TARGET" pull --ff-only >/dev/null 2>&1; then
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
    TARGET="$(abs_path "$DIR")"
    echo "  Installed into $TARGET"
fi

remember_install_dir "$TARGET"

cd "$TARGET" || exit 1
exec ./setup.sh "$@"
