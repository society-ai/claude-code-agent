"""Render a compact Society AI indicator for Claude Code's status line.

Single persona:
    📥 N · ✅ M · 🔔 K review
Multiple personas (one segment per persona with anything open):
    jenkins 📥1 ✅2 · saichi ✅1

Where:
  📥 N    — pending inbox items addressed to the agent
  ✅ M    — open (non-terminal) tasks assigned to the agent
  🔔 K    — tasks at status='in_review'

Cached to ~/.cache/society-ai/statusline.json for 30 seconds so we don't
hammer the API on every model turn (Claude Code calls the status-line
command after each turn). Silent when nothing's open or the platform is
unreachable — Claude Code renders an empty status line cleanly.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Any

from hook_common import discover_personas

CACHE_DIR = pathlib.Path(os.path.expanduser("~")) / ".cache" / "society-ai"
CACHE_PATH = CACHE_DIR / "statusline.json"
CACHE_TTL_S = 30
HTTP_TIMEOUT_S = 1.5
OPEN_TASK_STATUSES = {"backlog", "todo", "in_progress", "in_review", "blocked"}


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


def _write_cache(by_persona: dict[str, dict[str, int]]) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump({"at": time.time(), "personas": by_persona}, f)
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


def _segment(counts: dict[str, int]) -> str:
    parts: list[str] = []
    if counts.get("inbox", 0):
        parts.append(f"📥 {counts['inbox']}")
    if counts.get("tasks", 0):
        parts.append(f"✅ {counts['tasks']}")
    if counts.get("in_review", 0):
        parts.append(f"🔔 {counts['in_review']} review")
    return " · ".join(parts)


def main() -> int:
    by_persona: dict[str, dict[str, int]] | None = None

    cached = _read_cache()
    if cached:
        by_persona = cached.get("personas") or {}

    if by_persona is None:
        personas = discover_personas()
        if not personas:
            return 0
        by_persona = {}
        for p in personas:
            counts = _fetch_counts(p["api_url"], p["token"], p["name"])
            if counts is not None:
                by_persona[p["name"]] = counts
        if by_persona:
            _write_cache(by_persona)

    if not by_persona:
        return 0

    active = {n: c for n, c in by_persona.items() if any(c.values())}
    if not active:
        return 0

    if len(by_persona) == 1:
        # Single persona keeps the original compact format.
        print(_segment(next(iter(active.values()))))
    else:
        # Multi-persona: name-prefixed compact segments, only for personas
        # with anything open.
        segs = []
        for name, counts in active.items():
            compact = _segment(counts).replace(" · ", " ").replace("📥 ", "📥").replace("✅ ", "✅").replace("🔔 ", "🔔")
            segs.append(f"{name} {compact}")
        print(" · ".join(segs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
