"""Claude Code `Stop` hook: mirror notify + in_progress task reminder.

Registered via configure_hooks.py. Two jobs, both failure-silent:

1. Mirror notify — tells every local bridge daemon (one IPC socket per
   persona) that a turn just ended, passing the session id from the hook
   input on stdin. A bridge ships the transcript delta ONLY if it launched
   that session; everything else is ignored, so the machine owner's own
   sessions are never mirrored by this path.

2. Task reminder — iterates every persona configured on this machine
   (.env + .env.<name>) and checks for tasks at `in_progress`; if any are
   found, prints a one-line reminder per persona so they don't go stale.

Silent when there's nothing to report — no noise on normal session
endings. Designed to fail silently if Society AI is unreachable.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from typing import Any

from hook_common import REPO_DIR, discover_personas, parse_env_file

HTTP_TIMEOUT_S = 3
MAX_LISTED_IDS = 5
IPC_TIMEOUT_S = 2


def _read_hook_input() -> dict[str, Any]:
    """Claude Code passes hook context as JSON on stdin (session_id,
    transcript_path, cwd, ...). Never block on a TTY."""
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _bridge_sockets() -> list[str]:
    """Every bridge IPC socket that may exist on this machine: the default
    persona's plus one per .env.<name> (mirroring setup.sh's layout)."""
    cache = os.path.join(os.path.expanduser("~"), ".cache", "society-ai")
    paths = []
    default_env = parse_env_file(REPO_DIR / ".env")
    paths.append(default_env.get("SOCIETY_AI_BRIDGE_SOCKET")
                 or os.path.join(cache, "bridge.sock"))
    for p in sorted(REPO_DIR.glob(".env.*")):
        if p.name == ".env.example":
            continue
        env = parse_env_file(p)
        sock_path = env.get("SOCIETY_AI_BRIDGE_SOCKET")
        if not sock_path:
            persona = p.name[len(".env."):]
            sock_path = os.path.join(cache, persona, "bridge.sock")
        paths.append(sock_path)
    # De-dupe, keep only existing sockets.
    seen: list[str] = []
    for sp in paths:
        if sp and sp not in seen and os.path.exists(sp):
            seen.append(sp)
    return seen


def _notify_mirrors(hook_input: dict[str, Any]) -> None:
    """Fire-and-forget mirror_notify to every live bridge. Each bridge
    decides for itself whether the session is one it owns."""
    session_id = hook_input.get("session_id")
    if not session_id:
        return
    frame = (json.dumps({
        "id": f"hook-{os.getpid()}",
        "method": "mirror_notify",
        "params": {
            "session_id": str(session_id),
            "transcript_path": hook_input.get("transcript_path"),
            "cwd": hook_input.get("cwd"),
        },
    }) + "\n").encode("utf-8")
    for sock_path in _bridge_sockets():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(IPC_TIMEOUT_S)
                s.connect(sock_path)
                s.sendall(frame)
                s.recv(4096)  # wait for the ack so the ship gets scheduled
        except OSError:
            continue  # bridge not running / not ours — fine


def _fetch_in_progress(api_url: str, token: str, agent_name: str) -> list[dict[str, Any]]:
    try:
        import httpx  # type: ignore
    except ImportError:
        return []
    try:
        with httpx.Client(
            timeout=HTTP_TIMEOUT_S,
            headers={"Authorization": f"Bearer {token}"},
        ) as c:
            r = c.get(
                f"{api_url}/api/v1/agent-tasks",
                params={
                    "assigned_to_agent": agent_name,
                    "status": "in_progress",
                    "limit": 50,
                },
            )
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        return data
    except Exception:
        return []


def main() -> int:
    _notify_mirrors(_read_hook_input())

    personas = discover_personas()
    if not personas:
        return 0

    reminders: list[str] = []
    for p in personas:
        tasks = _fetch_in_progress(p["api_url"], p["token"], p["name"])
        if not tasks:
            continue
        idents = ", ".join(
            (t.get("identifier") or "?") for t in tasks[:MAX_LISTED_IDS]
        )
        if len(tasks) > MAX_LISTED_IDS:
            idents += f" (+{len(tasks) - MAX_LISTED_IDS} more)"
        plural = "task" if len(tasks) == 1 else "tasks"
        reminders.append(
            f"{p['name']}: {len(tasks)} {plural} at 'in_progress' — {idents}"
        )

    if not reminders:
        return 0

    print(
        "[Society AI reminder] Unfinished in_progress tasks: "
        + "; ".join(reminders)
        + ". Move them to done / in_review / blocked before ending, or "
        "they'll go stale."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
