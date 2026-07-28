"""Inject a Society AI identity banner + state snapshot at session start.

Registered via configure_hooks.py as a Claude Code `SessionStart` hook.

Two outputs, emitted as the JSON hook contract:

- `systemMessage` — a platform-rendered banner (terminal AND desktop app,
  no model turn involved) stating which agent this session is acting as,
  against which API, with its open task/inbox counts — plus any OTHER
  configured personas that have open work, and how to switch ("act as
  <name>"). This makes the session's true identity visible before the
  user types anything.

- `additionalContext` — the same facts for the model, so it can answer
  "which agent are you?" and execute a switch request via the
  `switch_agent` MCP tool without guessing.

The acting identity is resolved by expanding the `${VAR:-default}` values
of the society-ai MCP entry (project .mcp.json first, then ~/.claude.json)
against this hook's own environment — hooks and the MCP server are children
of the same `claude` process, so the expansion matches what the server got.

Designed to fail silently — if anything errors, the hook prints nothing
and the session starts normally.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from hook_common import discover_personas, resolve_mcp_identity

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


def _fetch(api_url: str, token: str, agent_name: str) -> tuple[list, list] | None:
    """Return (open_tasks, pending_inbox), or None if unreachable."""
    try:
        import httpx  # type: ignore
    except ImportError:
        return None

    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, headers=headers) as c:
            r = c.get(
                f"{api_url}/api/v1/agent-tasks",
                params={"assigned_to_agent": agent_name, "limit": 50},
            )
            if r.status_code != 200:
                return None
            tasks = [
                t for t in r.json()
                if (t.get("status") or "").lower() in OPEN_TASK_STATUSES
            ]
            r = c.get(
                f"{api_url}/api/v1/inbox",
                params={"to_agent": agent_name, "status": "pending", "limit": 50},
            )
            inbox: list = []
            if r.status_code == 200:
                payload = r.json()
                inbox = (
                    payload
                    if isinstance(payload, list)
                    else payload.get("items", []) or []
                )
        return tasks, inbox
    except Exception:
        return None


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        hook_input = {}
    cwd = hook_input.get("cwd") or ""

    personas = discover_personas()
    acting = resolve_mcp_identity(cwd)
    if acting is None and not personas:
        return 0  # society-ai not configured on this machine

    banner_lines: list[str] = []
    context_lines: list[str] = ["[Society AI session identity]"]

    acting_name = (acting or {}).get("name", "")
    acting_url = (acting or {}).get("api_url", "")

    # -- Acting identity + its counts -----------------------------------------
    acting_persona = next((p for p in personas if p["name"] == acting_name), None)
    acting_tasks: list | None = None
    acting_inbox: list | None = None
    if acting_persona:
        fetched = _fetch(acting_url, acting_persona["token"], acting_name)
        if fetched is not None:
            acting_tasks, acting_inbox = fetched

    if acting_name:
        line = f"Society AI: acting as {acting_name} → {acting_url}"
        if acting_tasks is not None:
            line += f" · {len(acting_tasks)} tasks · {len(acting_inbox or [])} inbox"
        elif acting_persona is None:
            line += " (no matching .env persona — counts unavailable)"
        else:
            line += " (unreachable — counts unavailable)"
        banner_lines.append(line)
        context_lines.append(
            f"This session's society-ai MCP is acting as {acting_name} "
            f"against {acting_url}."
        )

    # -- Other personas with open work ----------------------------------------
    others: list[str] = []
    other_names: list[str] = []
    for p in personas:
        if p["name"] == acting_name:
            continue
        other_names.append(p["name"])
        fetched = _fetch(p["api_url"], p["token"], p["name"])
        if fetched is None:
            continue
        tasks, inbox = fetched
        if tasks or inbox:
            seg = f"{p['name']} ({len(tasks)} tasks"
            if inbox:
                seg += f", {len(inbox)} inbox"
            seg += ")"
            others.append(seg)
            context_lines.append(f"  Open work under {p['name']}:")
            for t in tasks[:MAX_LISTED]:
                context_lines.append(f"    - {_format_task(t)}")
            for i in inbox[:MAX_LISTED]:
                context_lines.append(f"    - {_format_inbox(i)}")

    if others:
        first_other = others[0].split(" ")[0]
        banner_lines.append(
            "  open work under other identities: "
            + ", ".join(others)
            + f' · to switch, ask: "act as {first_other}"'
        )
    elif other_names:
        banner_lines.append(
            "  other identities available: "
            + ", ".join(other_names)
            + ' · to switch, ask: "act as <name>"'
        )

    # -- Acting agent's own open items (context only, keeps the banner short) --
    if acting_tasks:
        context_lines.append(f"  Open tasks assigned to {acting_name}:")
        for t in acting_tasks[:MAX_LISTED]:
            context_lines.append(f"    - {_format_task(t)}")
    if acting_inbox:
        context_lines.append(f"  Pending inbox for {acting_name}:")
        for i in acting_inbox[:MAX_LISTED]:
            context_lines.append(f"    - {_format_inbox(i)}")

    if other_names:
        context_lines.append(
            "  Other personas on this machine: "
            + ", ".join(other_names)
            + ". If the user asks to act as one of them, call the "
            "society-ai `switch_agent` tool with that name."
        )

    # Some surfaces (the desktop app) don't render the systemMessage banner,
    # so the model is the only way the user learns the binding. A blockquote
    # reads as a banner rather than as the agent talking; first reply only.
    if acting_name:
        quote = f"> ⚙ **Society AI**: acting as **{acting_name}** · {acting_url}"
        if other_names:
            first = other_names[0]
            quote += (
                f'\n> Other identities: {", ".join(other_names)}. '
                f'To switch, say "act as {first}".'
            )
        context_lines.append(
            "  Begin your FIRST reply of this session with exactly this "
            "blockquote, verbatim, then a blank line, then your normal "
            "answer:\n"
            + quote
            + "\n  Do not repeat it in later replies unless the binding "
            "changes (after a successful switch_agent, show the same "
            "blockquote once with the new identity)."
        )

    if not banner_lines:
        return 0

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(context_lines),
        },
        "systemMessage": "\n".join(banner_lines),
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
