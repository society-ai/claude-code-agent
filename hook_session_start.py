"""Inject a Society AI state snapshot at Claude Code session start.

Registered via configure_hooks.py as a Claude Code `SessionStart` hook.
Reads `.env` from the repo for the same `SOCIETY_AI_AUTH_TOKEN` the
bridge uses, then queries the agent's open tasks + pending inbox and
prints a compact one-screen summary.

Stdout becomes a `<system-reminder>` in the conversation so Claude Code
can proactively flag anything needing the user's attention before the
user makes their first ask.

Designed to fail silently — if Society AI is unreachable, `.env` is
missing, anything errors, or there's nothing worth saying, the hook
prints nothing and the session starts normally.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Any

REPO_DIR = pathlib.Path(__file__).resolve().parent
ENV_PATH = REPO_DIR / ".env"
DEFAULT_API_URL = "https://api.societyai.com"
HTTP_TIMEOUT_S = 3
OPEN_TASK_STATUSES = {"backlog", "todo", "in_progress", "in_review", "blocked"}
MAX_LISTED = 10


def _parse_env_file(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _resolve(env: dict[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or os.environ.get(key) or default).strip()


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
    env = _parse_env_file(ENV_PATH)
    token = _resolve(env, "SOCIETY_AI_AUTH_TOKEN")
    agent_name = _resolve(env, "AGENT_NAME")
    api_url = _resolve(env, "AGENT_ROUTER_API_URL", DEFAULT_API_URL).rstrip("/")
    if not token or not agent_name:
        # No usable config — stay silent. The user may be running Claude
        # Code locally without a connected agent.
        return 0

    tasks, inbox = _fetch(api_url, token, agent_name)
    if not tasks and not inbox:
        return 0  # nothing worth saying

    lines: list[str] = ["[Society AI state at session start]"]
    if tasks:
        lines.append(f"  Open tasks assigned to {agent_name} ({len(tasks)}):")
        for t in tasks[:MAX_LISTED]:
            lines.append(f"    - {_format_task(t)}")
        if len(tasks) > MAX_LISTED:
            lines.append(
                f"    (+{len(tasks) - MAX_LISTED} more — list_tasks to see all)"
            )
    if inbox:
        lines.append(f"  Pending inbox items ({len(inbox)}):")
        for i in inbox[:MAX_LISTED]:
            lines.append(f"    - {_format_inbox(i)}")
        if len(inbox) > MAX_LISTED:
            lines.append(
                f"    (+{len(inbox) - MAX_LISTED} more — list_inbox to see all)"
            )

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
