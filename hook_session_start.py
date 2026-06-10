"""Inject a Society AI state snapshot at Claude Code session start.

Registered via configure_hooks.py as a Claude Code `SessionStart` hook.
Iterates every persona configured on this machine (.env + .env.<name> —
see hook_common.discover_personas), queries each agent's open tasks +
pending inbox, and prints a compact one-screen summary.

Stdout becomes a `<system-reminder>` in the conversation so Claude Code
can proactively flag anything needing the user's attention before the
user makes their first ask.

Designed to fail silently — if Society AI is unreachable, no env files
exist, anything errors, or there's nothing worth saying, the hook
prints nothing and the session starts normally.
"""

from __future__ import annotations

import sys
from typing import Any

from hook_common import discover_personas

HTTP_TIMEOUT_S = 3
OPEN_TASK_STATUSES = {"backlog", "todo", "in_progress", "in_review", "blocked"}
MAX_LISTED = 10


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_task(t: dict[str, Any]) -> str:
    ident = t.get("identifier") or "?"
    status = t.get("status") or "?"
    title = _truncate(t.get("title") or "", 60)
    return f"{ident} [{status}] {title}".rstrip()


def _format_inbox(i: dict[str, Any]) -> str:
    typ = i.get("type") or "?"
    from_agent = i.get("fromAgent") or i.get("from_agent") or ""
    title = _truncate(i.get("title") or "", 70)
    src = f" from {from_agent}" if from_agent else ""
    return f"{typ}{src}: {title}".rstrip()


def _fetch(api_url: str, token: str, agent_name: str) -> tuple[list, list]:
    """Return (open_tasks, pending_inbox). Silent on any error."""
    try:
        import httpx  # type: ignore
    except ImportError:
        return [], []

    headers = {"Authorization": f"Bearer {token}"}
    tasks: list = []
    inbox: list = []
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, headers=headers) as c:
            r = c.get(
                f"{api_url}/api/v1/agent-tasks",
                params={"assigned_to_agent": agent_name, "limit": 50},
            )
            if r.status_code == 200:
                tasks = [
                    t for t in r.json()
                    if (t.get("status") or "").lower() in OPEN_TASK_STATUSES
                ]
            r = c.get(
                f"{api_url}/api/v1/inbox",
                params={
                    "to_agent": agent_name,
                    "status": "pending",
                    "limit": 50,
                },
            )
            if r.status_code == 200:
                payload = r.json()
                inbox = (
                    payload
                    if isinstance(payload, list)
                    else payload.get("items", [])
                )
    except Exception:
        return [], []
    return tasks, inbox


def main() -> int:
    personas = discover_personas()
    if not personas:
        return 0

    multi = len(personas) > 1
    sections: list[str] = []
    for p in personas:
        tasks, inbox = _fetch(p["api_url"], p["token"], p["name"])
        if not tasks and not inbox:
            continue
        lines: list[str] = []
        prefix = "  " if not multi else "    "
        if multi:
            lines.append(f"  {p['name']}:")
        if tasks:
            label = "Open tasks assigned to you" if not multi else "Open tasks"
            lines.append(f"{prefix}{label} ({len(tasks)}):")
            for t in tasks[:MAX_LISTED]:
                lines.append(f"{prefix}  - {_format_task(t)}")
            if len(tasks) > MAX_LISTED:
                lines.append(
                    f"{prefix}  (+{len(tasks) - MAX_LISTED} more — list_tasks to see all)"
                )
        if inbox:
            lines.append(f"{prefix}Pending inbox items ({len(inbox)}):")
            for i in inbox[:MAX_LISTED]:
                lines.append(f"{prefix}  - {_format_inbox(i)}")
            if len(inbox) > MAX_LISTED:
                lines.append(
                    f"{prefix}  (+{len(inbox) - MAX_LISTED} more — list_inbox to see all)"
                )
        sections.append("\n".join(lines))

    if not sections:
        return 0  # nothing worth saying for any persona

    print("[Society AI state at session start]")
    print("\n".join(sections))
    return 0


if __name__ == "__main__":
    sys.exit(main())
