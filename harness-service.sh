#!/usr/bin/env bash
# Manage the Society AI HARNESS LaunchAgent — one macOS service that runs
# every agent on this machine in a single process (the v0.11 model).
#
# This REPLACES the per-persona services (service.sh install [persona]).
# Run the harness OR the per-persona bridges, never both: two processes
# would double-register the same agents with the hub.
#
# Usage:
#   ./harness-service.sh install     # install + start the harness service
#   ./harness-service.sh uninstall   # stop + remove it
#   ./harness-service.sh status      # loaded? pid? agents online?
#   ./harness-service.sh restart     # restart (e.g. after editing .env)
#   ./harness-service.sh logs        # tail the harness log

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="io.societyai.claude-code-harness"
PLIST_TEMPLATE="$REPO_DIR/services/$LABEL.plist.template"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/.cache/society-ai/harness"
DOMAIN="gui/$(id -u)"

require_macos() {
    if [ "$(uname)" != "Darwin" ]; then
        echo "Error: the LaunchAgent is macOS-only." >&2
        exit 1
    fi
}

is_loaded() {
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1
}

per_persona_installed() {
    ls "$HOME/Library/LaunchAgents/io.societyai.claude-code-bridge"*.plist >/dev/null 2>&1
}

cmd_install() {
    require_macos
    if [ ! -x "$REPO_DIR/harness_launcher.sh" ]; then
        chmod +x "$REPO_DIR/harness_launcher.sh" 2>/dev/null || true
    fi
    if [ ! -f "$PLIST_TEMPLATE" ]; then
        echo "Error: template not found at $PLIST_TEMPLATE" >&2
        exit 1
    fi
    if per_persona_installed; then
        echo "  ⚠  Per-persona bridge LaunchAgents are still installed." >&2
        echo "     Remove them first so they don't double-register agents:" >&2
        echo "       ./service.sh uninstall            # default persona" >&2
        echo "       ./service.sh uninstall <persona>  # each extra persona" >&2
        exit 1
    fi

    mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"

    if is_loaded; then
        echo "  Existing harness service found — unloading first..."
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    fi

    python3 - "$PLIST_TEMPLATE" "$PLIST_PATH" "$REPO_DIR" "$LOG_DIR" "$LABEL" <<'PYEOF'
import sys
template_path, out_path, repo_dir, log_dir, label = sys.argv[1:6]
with open(template_path) as f:
    content = f.read()
content = content.replace("{{REPO_DIR}}", repo_dir)
content = content.replace("{{LOG_DIR}}", log_dir)
content = content.replace("{{LABEL}}", label)
with open(out_path, "w") as f:
    f.write(content)
PYEOF

    echo "  Installed plist at $PLIST_PATH"
    launchctl bootstrap "$DOMAIN" "$PLIST_PATH" 2>/dev/null || \
        launchctl load "$PLIST_PATH" 2>/dev/null || true
    sleep 1
    cmd_status
    echo ""
    echo "Harness running. It auto-starts at login and restarts on crash."
    echo "Logs: $LOG_DIR/harness.{log,err.log}"
}

cmd_uninstall() {
    require_macos
    if is_loaded; then
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || \
            launchctl unload "$PLIST_PATH" 2>/dev/null || true
        echo "  Harness service unloaded."
    else
        echo "  Harness service is not loaded."
    fi
    if [ -f "$PLIST_PATH" ]; then
        rm -f "$PLIST_PATH"
        echo "  Removed plist at $PLIST_PATH"
    fi
    echo ""
    echo "Done. To go back to per-persona bridges: ./service.sh install [persona]"
}

cmd_status() {
    require_macos
    if is_loaded; then
        echo "Harness:      loaded"
        launchctl print "$DOMAIN/$LABEL" 2>/dev/null | \
            awk -F' = ' '/^[[:space:]]*(state|pid|last exit code) =/ {
                gsub(/^[[:space:]]+/, "", $1); printf "  %-12s%s\n", $1 ":", $2
            }'
    else
        echo "Harness:      NOT loaded"
    fi
    # Agents online = count of registered lines since the last start.
    local logf="$LOG_DIR/harness.err.log"
    if [ -f "$logf" ]; then
        local n; n="$(grep -c 'Registered as' "$logf" 2>/dev/null || echo 0)"
        echo "  agents seen:  $(grep 'Registered as' "$logf" 2>/dev/null | sed 's/.*Registered as //' | sort -u | tr '\n' ' ')"
    fi
}

cmd_restart() {
    require_macos
    if is_loaded; then
        echo "  Restarting harness..."
        launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
        sleep 1
        cmd_status
    else
        echo "  Harness is not loaded. Run: $0 install" >&2
        exit 1
    fi
}

cmd_logs() {
    local out="$LOG_DIR/harness.log" err="$LOG_DIR/harness.err.log"
    [ -f "$out" ] || touch "$out"
    [ -f "$err" ] || touch "$err"
    exec tail -F "$err" "$out"
}

case "${1:-}" in
    install)   cmd_install ;;
    uninstall) cmd_uninstall ;;
    status)    cmd_status ;;
    restart)   cmd_restart ;;
    logs)      cmd_logs ;;
    -h|--help|help|"")
        sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'
        ;;
    *)
        echo "Error: unknown command: $1" >&2
        exit 1
        ;;
esac
