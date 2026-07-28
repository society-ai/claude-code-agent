"""Inject a Society AI identity banner + state snapshot at session start.

Registered via configure_hooks.py as a Claude Code `SessionStart` hook.

Two outputs, emitted as the JSON hook contract:

- `systemMessage` — a platform-rendered banner (the terminal shows it
  before the user types; the desktop app currently doesn't render it)
  stating which agent this session acts as, on which network, with its
  open workload, the other agents configured on this machine, and how to
  switch.

- `additionalContext` — the same facts for the model, plus an instruction
  to open its FIRST reply with a blockquote version of the banner — the
  only identity surface app users actually see — and to execute switch
  requests via the `switch_agent` MCP tool (which accepts canonical ids
  and display names alike).

Agents are labeled with the user-given display name when known
(`DISPLAY_NAME` in the persona's env file) alongside the canonical id,
and API URLs are labeled by network (Society AI Cloud / Local network).

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


def _network_label(url: str) -> str:
    """Friendly network name for an API URL, keeping the technical
    reference where it isn't obvious from the name."""
    host = (url or "").split("://", 1)[-1].split("/", 1)[0]
    bare = host.split(":")[0].lower()
    if bare in ("localhost", "127.0.0.1", "0.0.0.0"):
        return f"Local network ({host})"
    if bare == "api.societyai.com":
        return "Society AI Cloud"
    return host or "unknown network"


def _agent_label(name: str, display: str, md: bool) -> str:
    """'kilo (`agent-egrx5fzz`)' when a display name is known, else the id."""
    if display and display.lower() != (name or "").lower():
        return f"**{display}** (`{name}`)" if md else f"{display} ({name})"
    return f"**{name}**" if md else name


def _workload(tasks: list | None, inbox: list | None, md: bool) -> str:
    if tasks is None:
        return "status unavailable"
    if not tasks and not inbox:
        return "no open work"
    n = len(tasks)
    s = f"{n} open task" + ("" if n == 1 else "s")
    parts = [f"**{s}**" if md else s]
    if inbox:
        parts.append(f"{len(inbox)} inbox")
    return " · ".join(parts)


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


def _banner(
    acting_name: str,
    acting_display: str,
    acting_url: str,
    acting_tasks: list | None,
    acting_inbox: list | None,
    others: list[dict[str, Any]],
    md: bool,
) -> list[str]:
    """The 4-line banner, in plain text (systemMessage) or markdown
    blockquote flavor (first-reply). `others` entries: {name, display_name,
    api_url, tasks, inbox} with tasks/inbox possibly None."""
    lines = [
        ("> " if md else "") + ("⚡ **Society AI** · connected" if md else "⚡ Society AI · connected"),
    ]
    lines.append(
        ("> " if md else "")
        + f"🤖 Acting as {_agent_label(acting_name, acting_display, md)}"
        + f" · {_network_label(acting_url)}"
        + f" · {_workload(acting_tasks, acting_inbox, md)}"
    )
    if others:
        segs = []
        for o in others:
            seg = (
                _agent_label(o["name"], o.get("display_name", ""), md)
                + f" · {_network_label(o['api_url'])}"
            )
            if o.get("tasks") is not None and (o["tasks"] or o.get("inbox")):
                seg += f" · {_workload(o['tasks'], o.get('inbox'), md)}"
            segs.append(seg)
        lines.append(("> " if md else "") + "💤 Also on this machine: " + "; ".join(segs))

        # Point the switch hint at the most relevant other agent — the one
        # with open work if any, else the first.
        target = next((o for o in others if o.get("tasks")), others[0])
        say = target.get("display_name") or target["name"]
        if md:
            lines.append(f'> ↔️ To switch, just say *"act as {say}"*')
        else:
            lines.append(f'↔️ To switch, just say "act as {say}"')
    return lines


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

    acting_name = (acting or {}).get("name", "")
    acting_url = (acting or {}).get("api_url", "")
    if not acting_name:
        return 0

    context_lines: list[str] = ["[Society AI session identity]"]

    # -- Acting identity + its counts -----------------------------------------
    acting_persona = next((p for p in personas if p["name"] == acting_name), None)
    acting_display = (acting_persona or {}).get("display_name", "")
    acting_tasks: list | None = None
    acting_inbox: list | None = None
    if acting_persona:
        fetched = _fetch(acting_url, acting_persona["token"], acting_name)
        if fetched is not None:
            acting_tasks, acting_inbox = fetched

    context_lines.append(
        f"This session's society-ai MCP is acting as "
        f"{_agent_label(acting_name, acting_display, md=False)} "
        f"on {_network_label(acting_url)} ({acting_url})."
    )

    # -- Other personas -------------------------------------------------------
    others: list[dict[str, Any]] = []
    for p in personas:
        if p["name"] == acting_name:
            continue
        fetched = _fetch(p["api_url"], p["token"], p["name"])
        tasks, inbox = fetched if fetched is not None else (None, None)
        others.append({
            "name": p["name"],
            "display_name": p.get("display_name", ""),
            "api_url": p["api_url"],
            "tasks": tasks,
            "inbox": inbox,
        })
        if tasks or inbox:
            context_lines.append(
                f"  Open work under {_agent_label(p['name'], p.get('display_name', ''), md=False)}:"
            )
            for t in (tasks or [])[:MAX_LISTED]:
                context_lines.append(f"    - {_format_task(t)}")
            for i in (inbox or [])[:MAX_LISTED]:
                context_lines.append(f"    - {_format_inbox(i)}")

    # -- Acting agent's own open items (context only) --------------------------
    if acting_tasks:
        context_lines.append(f"  Open tasks assigned to {acting_name}:")
        for t in acting_tasks[:MAX_LISTED]:
            context_lines.append(f"    - {_format_task(t)}")
    if acting_inbox:
        context_lines.append(f"  Pending inbox for {acting_name}:")
        for i in acting_inbox[:MAX_LISTED]:
            context_lines.append(f"    - {_format_inbox(i)}")

    if others:
        context_lines.append(
            "  Other agents on this machine: "
            + ", ".join(
                _agent_label(o["name"], o.get("display_name", ""), md=False)
                for o in others
            )
            + ". The user may refer to an agent by display name or canonical "
            "id — the society-ai `switch_agent` tool accepts both. Call it "
            "when the user asks to act as / switch to another agent."
        )

    # Some surfaces (the desktop app) don't render the systemMessage banner,
    # so the model is the only way the user learns the binding. A blockquote
    # reads as a banner rather than as the agent talking; first reply only.
    quote = "\n".join(
        _banner(acting_name, acting_display, acting_url,
                acting_tasks, acting_inbox, others, md=True)
    )
    context_lines.append(
        "  Begin your FIRST reply of this session with exactly this "
        "blockquote, verbatim, then a blank line, then your normal answer:\n"
        + quote
        + "\n  Do not repeat it in later replies unless the binding changes "
        "(after a successful switch_agent, show the same blockquote once "
        "with the identities updated)."
    )

    banner_lines = _banner(
        acting_name, acting_display, acting_url,
        acting_tasks, acting_inbox, others, md=False,
    )

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
