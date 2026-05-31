#!/usr/bin/env bash
# Manage the Society AI bridge as a macOS LaunchAgent — install, uninstall,
# status, restart, and logs subcommands. Linux equivalent (systemd user
# service) is documented in the README but not implemented here.

set -euo pipefail

LABEL="io.societyai.claude-code-bridge"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST_TEMPLATE="$REPO_DIR/services/io.societyai.claude-code-bridge.plist.template"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/.cache/society-ai"

usage() {
    cat <<EOF
Usage: $0 <command>

Commands:
  install    Install + start the bridge as a macOS LaunchAgent.
             Auto-starts at login. Auto-restarts on crash.
  uninstall  Stop the LaunchAgent and remove the plist.
  status     Show whether the agent is loaded and the bridge process state.
  restart    Stop and re-start the LaunchAgent (e.g. after editing .env).
  logs       Tail the bridge log (Ctrl-C to exit).

The bridge runs as your user. No sudo required. The plist contains no
secrets — bridge_launcher.sh sources .env at process start.
EOF
}

require_macos() {
    if [ "$(uname)" != "Darwin" ]; then
        echo "Error: $0 currently only supports macOS (uses launchd)." >&2
        echo "On Linux, see the systemd template in the README." >&2
        exit 1
    fi
}

require_setup() {
    if [ ! -f "$REPO_DIR/.env" ]; then
        echo "Error: .env not found. Run ./setup.sh first." >&2
        exit 1
    fi
    if [ ! -x "$REPO_DIR/venv/bin/python" ]; then
        echo "Error: venv missing. Run ./setup.sh first." >&2
        exit 1
    fi
    if [ ! -x "$REPO_DIR/bridge_launcher.sh" ]; then
        # Self-heal: make the launcher executable in case git lost the bit.
        chmod +x "$REPO_DIR/bridge_launcher.sh" 2>/dev/null || true
    fi
}

is_loaded() {
    # `launchctl list` only shows legacy-style services. Modern services
    # bootstrapped to gui/<uid> need `launchctl print`. Try the new way
    # first, fall back to the old way so the script works on both modern
    # and legacy macOS.
    launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || \
        launchctl list 2>/dev/null | grep -q "$LABEL"
}

cmd_install() {
    require_macos
    require_setup

    if [ ! -f "$PLIST_TEMPLATE" ]; then
        echo "Error: plist template not found at $PLIST_TEMPLATE" >&2
        exit 1
    fi

    mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

    # If the agent is already loaded (e.g. re-running install after editing
    # .env), unload it first. bootstrap will fail otherwise.
    if is_loaded; then
        echo "  Existing LaunchAgent found — unloading first..."
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || \
            launchctl unload "$PLIST_PATH" 2>/dev/null || true
    fi

    # Substitute placeholders. Use Python rather than sed because $REPO_DIR
    # might contain characters sed treats as delimiters.
    python3 - "$PLIST_TEMPLATE" "$PLIST_PATH" "$REPO_DIR" "$LOG_DIR" <<'PYEOF'
import sys
template_path, out_path, repo_dir, log_dir = sys.argv[1:5]
with open(template_path) as f:
    content = f.read()
content = content.replace("{{REPO_DIR}}", repo_dir)
content = content.replace("{{LOG_DIR}}", log_dir)
with open(out_path, "w") as f:
    f.write(content)
PYEOF

    chmod 0644 "$PLIST_PATH"

    # Prefer the modern bootstrap/kickstart pair; fall back to load -w on
    # macOS versions where bootstrap isn't available.
    if launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null; then
        launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
    else
        launchctl load -w "$PLIST_PATH"
    fi

    echo ""
    echo "Installed and started LaunchAgent: $LABEL"
    echo "  plist:  $PLIST_PATH"
    echo "  logs:   $LOG_DIR/bridge.log"
    echo ""
    echo "The bridge will now auto-start at login and auto-restart on crash."
    echo "Check status with:   ./service.sh status"
    echo "Stream logs with:    ./service.sh logs"
}

cmd_uninstall() {
    require_macos
    if is_loaded; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || \
            launchctl unload "$PLIST_PATH" 2>/dev/null || true
        echo "  LaunchAgent unloaded."
    else
        echo "  LaunchAgent is not loaded."
    fi
    if [ -f "$PLIST_PATH" ]; then
        rm -f "$PLIST_PATH"
        echo "  Removed plist at $PLIST_PATH"
    fi
    echo ""
    echo "Done. Logs at $LOG_DIR remain — delete by hand if you want."
}

cmd_status() {
    require_macos
    if is_loaded; then
        echo "LaunchAgent:  loaded"
        # Pull the running pid / last exit code from `launchctl print` (the
        # modern command shows them as kv pairs in its output).
        launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | \
            awk -F' = ' '/^[[:space:]]*(state|pid|last exit code) =/ {
                gsub(/^[[:space:]]+/, "", $1)
                printf "  %-12s%s\n", $1 ":", $2
            }'
    else
        echo "LaunchAgent:  NOT loaded"
    fi

    # Look for python bridge.py processes, excluding the shell wrapper that
    # ran this status command and any pgrep itself.
    local bridge_pids
    bridge_pids=$(pgrep -f "Python bridge.py" 2>/dev/null || true)
    if [ -n "$bridge_pids" ]; then
        echo "Bridge proc:  PID(s) $bridge_pids"
    else
        echo "Bridge proc:  not running"
    fi

    if [ -S "$HOME/.cache/society-ai/bridge.sock" ]; then
        echo "IPC socket:   present"
    else
        echo "IPC socket:   missing"
    fi
}

cmd_restart() {
    require_macos
    if is_loaded; then
        echo "  Restarting LaunchAgent..."
        launchctl kickstart -k "gui/$(id -u)/$LABEL" 2>/dev/null || true
        sleep 1
        cmd_status
    else
        echo "  LaunchAgent is not loaded. Run: $0 install"
        exit 1
    fi
}

cmd_logs() {
    # Python's `logging` writes to stderr by default, so the bridge's
    # operational log lines land in bridge.err.log, not bridge.log.
    # We tail both so future buffering changes don't surprise anyone.
    local stdout="$LOG_DIR/bridge.log"
    local stderr="$LOG_DIR/bridge.err.log"

    if [ ! -f "$stdout" ] && [ ! -f "$stderr" ]; then
        echo "  No log files yet under $LOG_DIR" >&2
        echo "  (Is the LaunchAgent installed? Run: $0 install)" >&2
        exit 1
    fi

    # Touch missing files so `tail -F` watches them for creation.
    [ -f "$stdout" ] || touch "$stdout"
    [ -f "$stderr" ] || touch "$stderr"

    exec tail -F "$stderr" "$stdout"
}

case "${1:-}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    status)    cmd_status ;;
    restart)   cmd_restart ;;
    logs)      cmd_logs ;;
    -h|--help|help|"") usage ;;
    *)
        echo "Error: unknown command: $1" >&2
        usage
        exit 1
        ;;
esac
