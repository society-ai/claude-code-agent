"""Render a compact Society AI indicator for Claude Code's status line.

Output format:
    📥 N · ✅ M · 🔔 K review

Where:
  📥 N    — pending inbox items addressed to this agent
  ✅ M    — open (non-terminal) tasks assigned to this agent
  🔔 K    — tasks at status='in_review' (typically things awaiting your action)

Cached to ~/.cache/society-ai/statusline.json for 30 seconds so we don't
hammer the API on every model turn (Claude Code calls the status-line
command after each turn). Silent when nothing's open or platform is
unreachable — Claude Code renders an empty status line cleanly.

Designed to be fast — uses a 1.5s network timeout and falls back to
cached data if the network call doesn't return in time.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Any

REPO_DIR = pathlib.Path(__file__).resolve().parent
ENV_PATH = REPO_DIR / ".env"
CACHE_DIR = pathlib.Path(os.path.expanduser("~")) / ".cache" / "society-ai"
CACHE_PATH = CACHE_DIR / "statusline.json"
CACHE_TTL_S = 30
HTTP_TIMEOUT_S = 1.5
DEFAULT_API_URL = "https://api.societyai.com"
OPEN_TASK_STATUSES = {"backlog", "todo", "in_progress", "in_review", "blocked"}


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


def _read_cache() -> dict[str, Any] | None:
    try:
        if not CACHE_PATH.exists():
            return None
        with CACHE_PATH.open() as f:
            data = json.load(f)
        if time.time() - float(data.get("at", 0)) > CACHE_TTL_S:
            return None
        return data
    except Exception:
        return None


def _write_cache(counts: dict[str, int]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump({"at": time.time(), "counts": counts}, f)
        tmp.replace(CACHE_PATH)
    except Exception:
        pass


def _fetch_counts(api_url: str, token: str, agent_name: str) -> dict[str, int] | None:
    try:
        import httpx  # type: ignore
    except ImportError:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, headers=headers) as c:
            r1 = c.get(
                f"{api_url}/api/v1/agent-tasks",
                params={"assigned_to_agent": agent_name, "limit": 100},
            )
            tasks = r1.json() if r1.status_code == 200 else []
            r2 = c.get(
                f"{api_url}/api/v1/inbox",
                params={
                    "to_agent": agent_name,
                    "status": "pending",
                    "limit": 100,
                },
            )
            inbox_payload = r2.json() if r2.status_code == 200 else []
            inbox = (
                inbox_payload
                if isinstance(inbox_payload, list)
                else inbox_payload.get("items", [])
            )
    except Exception:
        return None

    if not isinstance(tasks, list):
        tasks = []
    open_tasks = [
        t for t in tasks if (t.get("status") or "").lower() in OPEN_TASK_STATUSES
    ]
    in_review = [
        t for t in open_tasks if (t.get("status") or "").lower() == "in_review"
    ]
    return {
        "tasks": len(open_tasks),
        "inbox": len(inbox),
        "in_review": len(in_review),
    }


def main() -> int:
    counts: dict[str, int] | None = None

    cached = _read_cache()
    if cached:
        counts = cached.get("counts") or {}

    if counts is None:
        env = _parse_env_file(ENV_PATH)
        token = _resolve(env, "SOCIETY_AI_AUTH_TOKEN")
        agent_name = _resolve(env, "AGENT_NAME")
        api_url = _resolve(env, "AGENT_ROUTER_API_URL", DEFAULT_API_URL).rstrip("/")
        if not token or not agent_name:
            return 0
        counts = _fetch_counts(api_url, token, agent_name)
        if counts is not None:
            _write_cache(counts)

    if not counts:
        return 0

    parts: list[str] = []
    if counts.get("inbox", 0):
        parts.append(f"📥 {counts['inbox']}")
    if counts.get("tasks", 0):
        parts.append(f"✅ {counts['tasks']}")
    if counts.get("in_review", 0):
        parts.append(f"🔔 {counts['in_review']} review")

    if parts:
        print(" · ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
