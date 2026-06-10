"""Remind to clean up in_progress tasks at Claude Code session end.

Registered via configure_hooks.py as a Claude Code `Stop` hook. Iterates
every persona configured on this machine (.env + .env.<name>) and checks
for tasks at `in_progress`; if any are found, prints a one-line reminder
per persona so they don't get left stale.

Silent when there's nothing to report — no noise on normal session
endings. Designed to fail silently if Society AI is unreachable.
"""

from __future__ import annotations

import sys
from typing import Any

from hook_common import discover_personas

HTTP_TIMEOUT_S = 3
MAX_LISTED_IDS = 5


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
