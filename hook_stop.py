"""Remind to clean up in_progress tasks at Claude Code session end.

Registered via configure_hooks.py as a Claude Code `Stop` hook. Queries
tasks where the agent is assignee AND status is `in_progress`; if any
are found, prints a one-line reminder so they don't get left stale.

Silent when there's nothing to report — no noise on normal session
endings. Designed to fail silently if Society AI is unreachable.
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
MAX_LISTED_IDS = 5


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
    env = _parse_env_file(ENV_PATH)
    token = _resolve(env, "SOCIETY_AI_AUTH_TOKEN")
    agent_name = _resolve(env, "AGENT_NAME")
    api_url = _resolve(env, "AGENT_ROUTER_API_URL", DEFAULT_API_URL).rstrip("/")
    if not token or not agent_name:
        return 0

    tasks = _fetch_in_progress(api_url, token, agent_name)
    if not tasks:
        return 0

    idents = ", ".join((t.get("identifier") or "?") for t in tasks[:MAX_LISTED_IDS])
    if len(tasks) > MAX_LISTED_IDS:
        idents += f" (+{len(tasks) - MAX_LISTED_IDS} more)"

    plural = "task" if len(tasks) == 1 else "tasks"
    print(
        f"[Society AI reminder] You have {len(tasks)} {plural} at status "
        f"'in_progress' that haven't been moved: {idents}. Move them to "
        "done / in_review / blocked before ending, or they'll go stale."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
