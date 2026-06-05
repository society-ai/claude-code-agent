"""Register the Society AI SessionStart and Stop hooks with Claude Code.

Called by setup.sh — not intended to be run directly.

Adds idempotent entries to ~/.claude/settings.json:
- SessionStart -> hook_session_start.py  (injects state snapshot)
- Stop         -> hook_stop.py           (reminds about in_progress tasks)

Identifies own entries by their command path containing our script
filenames, so re-running the script replaces in place rather than
appending duplicates. Other hooks the user has configured are left
untouched.
"""

from __future__ import annotations

import json
import os
import sys

SETTINGS_PATH = os.path.join(
    os.path.expanduser("~"), ".claude", "settings.json"
)

# Marker filenames used to identify entries we own. Don't rename without
# also writing a migration that prunes the old filename from settings.
SESSION_START_SCRIPT = "hook_session_start.py"
STOP_SCRIPT = "hook_stop.py"

# Per-hook timeout in seconds. SessionStart hits the platform with two
# GETs; 5s is generous for the common case and short enough that a hung
# network doesn't make every session feel broken.
HOOK_TIMEOUT_S = 5


def _read_settings() -> dict:
    if not os.path.isfile(SETTINGS_PATH):
        return {}
    try:
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_settings(data: dict) -> None:
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, SETTINGS_PATH)
    try:
        os.chmod(SETTINGS_PATH, 0o600)
    except OSError:
        pass


def _entry_owns_script(entry: dict, script_filename: str) -> bool:
    """Return True if this matcher entry contains a hook command we own."""
    if not isinstance(entry, dict):
        return False
    for h in entry.get("hooks", []) or []:
        if not isinstance(h, dict):
            continue
        cmd = h.get("command", "")
        if isinstance(cmd, str) and script_filename in cmd:
            return True
    return False


def _upsert_event(
    hooks: dict, event_name: str, command: str, script_filename: str
) -> None:
    """Replace any existing entry that owns script_filename, then append fresh."""
    entries = hooks.get(event_name)
    if not isinstance(entries, list):
        entries = []
    # Drop any entry we previously installed (matches by script filename
    # in the command string). Leaves all foreign entries untouched.
    entries = [e for e in entries if not _entry_owns_script(e, script_filename)]
    entries.append({
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": HOOK_TIMEOUT_S,
            }
        ]
    })
    hooks[event_name] = entries


def main() -> None:
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    venv_py = os.path.join(repo_dir, "venv", "bin", "python")
    session_script = os.path.join(repo_dir, SESSION_START_SCRIPT)
    stop_script = os.path.join(repo_dir, STOP_SCRIPT)

    if not os.path.isfile(venv_py):
        print(
            f"  Error: venv Python not found at {venv_py}. "
            "Re-run setup.sh to create the venv.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not os.path.isfile(session_script) or not os.path.isfile(stop_script):
        print(
            "  Error: hook scripts missing in repo. Reinstall claude-code-agent.",
            file=sys.stderr,
        )
        sys.exit(2)

    session_cmd = f"{venv_py} {session_script}"
    stop_cmd = f"{venv_py} {stop_script}"

    data = _read_settings()
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        # Some other shape — back away rather than clobber. Hooks must
        # be a dict in Claude Code 2.x.
        print(
            "  Warning: ~/.claude/settings.json has a non-dict `hooks` field; "
            "leaving it untouched.",
            file=sys.stderr,
        )
        return

    _upsert_event(hooks, "SessionStart", session_cmd, SESSION_START_SCRIPT)
    _upsert_event(hooks, "Stop", stop_cmd, STOP_SCRIPT)
    data["hooks"] = hooks

    _write_settings(data)
    print(
        "  Registered SessionStart + Stop hooks in ~/.claude/settings.json"
    )


if __name__ == "__main__":
    main()
