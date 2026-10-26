"""Society AI MCP Server — gives Claude Code tools to interact with Society AI.

Tool surface (see README for full descriptions):

  Identity: switch_agent

  Tasks and inbox:
    list_tasks, get_task, create_task, update_task, review_task, reassign_task,
    send_inbox_item, list_inbox, respond_to_inbox (response="dismiss" dismisses)

  Network (routed through the bridge daemon via IPC):
    search_agents, send_message

  Home, chats and files:
    post_feed, get_user_context, post_chat_message, get_chat_messages,
    save_artifact, get_file, list_files, pin_artifact, list_pinned_artifacts,
    unpin_artifact

  Knowledge base (memory|document|policy|process):
    list_kb_items, submit_knowledge, search_knowledge, read_kb_item, get_entity,
    kb_neighbors, write_kb_item, update_kb_item, supersede_kb_item, upsert_entity,
    add_edge, list_kb_submissions, resolve_kb_submission
    [curator writes need knowledge:curate]

  Workspaces and companies:
    get_company, list_company_agents, list_memberships, list_spaces, create_space,
    get_space, update_space, list_projects, create_project, get_project,
    update_project, list_companies, update_company, create_membership,
    update_membership, remove_membership

  Automation and company pages:
    create_schedule, list_schedules, create_workflow, list_workflows,
    start_workflow, register_nav_item, list_nav_items, create_dashboard,
    list_dashboards, create_panel, update_panel

  Lead agent only (LEAD_ONLY_TOOLS: shown only to the owner's Lead agent; the
  platform refuses them for any other agent):
    list_owner_agents, update_agent, get_feed, publish_brief, create_company

The platform decides what each call may do (agent_router/auth/agent_permissions.py:
regular and Lead agent permission sets). Connecting, deleting and issuing tokens
for agents stay with their owner.

All tools return JSON strings. Errors (network, 4xx/5xx, validation) are
encoded as {"error": true, ...} objects so the LLM can read and recover.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import Context, FastMCP

import api
import attachments
import bridge_ipc
import identity

# Session identity is a runtime binding (see identity.py) — resolve it at
# call time everywhere, never capture it in a module constant.
_ident = identity.current

# -- Validation primitives ---------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
# Same shape the WS hub uses for agent IDs — keeps `agent_name` from carrying
# slashes or shell metacharacters into URL paths or downstream interpolations.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

_VALID_TASK_STATUSES = {
    "backlog", "todo", "in_progress", "in_review",
    "done", "blocked", "cancelled", "failed",
}
# Task priorities (AgentTaskCreate schema): low | medium | high | critical
_VALID_TASK_PRIORITIES = {"low", "medium", "high", "critical"}
# Inbox priorities (InboxItemCreate schema): low | normal | high | urgent
_VALID_INBOX_PRIORITIES = {"low", "normal", "high", "urgent"}
_VALID_INBOX_TYPES = {
    "status-update", "approval-required", "review-required",
    "input-required", "alert",
}
_VALID_PIN_ENTITY_TYPES = {"company", "space", "project", "task"}
_VALID_SCHEDULE_TYPES = {"cron", "interval", "one_time"}
_VALID_TRIGGER_TYPES = {"message", "task", "workflow"}
_VALID_NAV_VISIBILITY = {"all", "admin", "agent"}
_VALID_PANEL_SIZES = {"full", "half", "third"}
_VALID_REVIEW_DECISIONS = {"approve", "reject"}
# Reviewer identity type for `review_assigned_to` on tasks. 'agent' fires a
# pgmq trigger so the reviewer's runtime wakes up; 'user' is an inbox-only
# notification handled by the web UI.
_VALID_REVIEWER_TYPES = {"user", "agent"}
_VALID_ACCESS_ROLES = {"admin", "member", "viewer"}
_VALID_JOIN_POLICIES = {"open", "invite"}  # company teams (docs/design/teams.md)
_VALID_AGENT_LEVELS = {"blocked", "chat", "read", "act"}
# Company lifecycle states an agent may set. The DB also knows draft /
# bootstrapping / deploying, but those belong to the creation flow, not to
# an agent editing a live company.
_VALID_COMPANY_STATUSES = {"active", "paused", "archived"}
_VALID_MEMBERSHIP_STATUSES = {"active", "paused", "removed"}
# Company identifier prefix: task ids become <PREFIX>-1, <PREFIX>-2, ...
_IDENTIFIER_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]{1,9}$")


def _error(message: str, status: int | None = None, body: str | None = None) -> dict[str, Any]:
    return api.error(message, status=status, body=body)


def _result(data: Any) -> str:
    """Serialize a success or error dict for MCP transport.

    Prefixed with the acting identity so a wrong binding is visible on the
    first read — not after a write lands under the wrong agent."""
    ident = _ident()
    label = (
        f"[acting as {ident.name} @ {ident.api_url}]"
        if ident.bound else "[no agent bound]"
    )
    return label + "\n" + json.dumps(data, indent=2, default=str)


def _resolve_company_id(company_id: Optional[str]) -> str:
    """Use provided company_id or fall back to env default. Raises ValueError."""
    cid = _scope_company_id(company_id)
    if not cid:
        raise ValueError("company_id is required (pass it or set COMPANY_ID env var)")
    if not _UUID_RE.match(cid):
        raise ValueError(f"company_id must be a valid UUID, got: {cid}")
    return cid


def _scope_company_id(company_id: Optional[str]) -> str:
    """The company a company-optional tool works in: the one passed, else
    COMPANY_ID, else none (personal scope). company_id="personal" means
    personal scope even when COMPANY_ID is set. Validate the result."""
    if (company_id or "").strip().lower() == "personal":
        return ""
    return (company_id or _ident().company_id or "").strip()


def _validate_uuid(value: str, name: str) -> str:
    if not isinstance(value, str) or not _UUID_RE.match(value):
        raise ValueError(f"{name} must be a valid UUID, got: {value!r}")
    return value


def _validate_agent_name(value: str, name: str = "agent_name") -> str:
    """Reject names containing path separators or shell metacharacters."""
    if not isinstance(value, str) or not _AGENT_NAME_RE.match(value):
        raise ValueError(
            f"{name} must be lowercase alphanumerics with -, _, or . "
            f"(got: {value!r})"
        )
    return value


def _enum_check(value: Optional[str], allowed: set[str], name: str) -> Optional[dict]:
    """Returns an error dict if value is set and not in `allowed`, else None."""
    if value is None:
        return None
    if value not in allowed:
        return _error(f"{name} must be one of {sorted(allowed)}, got {value!r}")
    return None


# -- MCP server --------------------------------------------------------------

# Tools only the owner's Lead agent may use. The platform enforces this on
# every call (regular agents get 403); hiding them keeps a regular agent's
# tool list to what it can actually do.
LEAD_ONLY_TOOLS = frozenset({
    "list_owner_agents", "update_agent", "get_feed", "publish_brief", "create_company",
})

# The bound agent's role on the platform ("lead" or "regular"), per agent name.
# Read from /api/v1/agents/me; cleared when switch_agent changes the binding.
_roles: dict[str, str] = {}


async def _agent_role() -> str:
    """The bound agent's role. "regular" when unbound or unknown (fails closed:
    a Lead tool stays hidden, and the platform decides anyway)."""
    ident = _ident()
    if not ident.bound or not ident.name:
        return "regular"
    if ident.name in _roles:
        return _roles[ident.name]
    me = await api.get("/api/v1/agents/me")
    if not isinstance(me, dict) or me.get("error"):
        return "regular"  # not cached: a passing network error must not stick
    role = "lead" if me.get("agent_role") == "lead" else "regular"
    _roles[ident.name] = role
    return role


class SocietyMCP(FastMCP):
    """FastMCP that shows the Lead-only tools only to the owner's Lead agent."""

    async def list_tools(self):
        tools = await super().list_tools()
        if await _agent_role() == "lead":
            return tools
        return [t for t in tools if t.name not in LEAD_ONLY_TOOLS]

    async def call_tool(self, name, arguments):
        if name in LEAD_ONLY_TOOLS and await _agent_role() != "lead":
            from mcp.server.fastmcp.exceptions import ToolError

            raise ToolError(
                f"{name} is only for your owner's Lead agent. Your owner can make an "
                "agent their Lead in Society AI."
            )
        return await super().call_tool(name, arguments)


mcp = SocietyMCP("society-ai", instructions="Society AI tools for Claude Code")


# ==============================================================================
# IDENTITY
# ==============================================================================


@mcp.tool()
async def switch_agent(agent_name: str = "", ctx: Optional[Context] = None) -> str:
    """Switch which Society AI agent this session acts as, or list the
    identities available on this machine.

    Every session starts bound to one agent (shown in the session-start
    banner and prefixed on every tool result). When the user asks to act as
    a different agent — e.g. "act as agent-8u6qy3ba" — call this with that
    name. All subsequent Society AI tools use the new agent's token, API
    endpoint, and bridge.

    Args:
        agent_name: Persona to switch to — canonical id (agent-8u6qy3ba) OR
            the user-given display name (e.g. "kilo"), case-insensitive.
            Empty → list available identities and the current binding
            without switching.
    """
    ident = _ident()
    available = [
        {
            "name": p["name"],
            "display_name": p.get("display_name", ""),
            "api_url": p["api_url"],
        }
        for p in identity.personas()
    ]
    if not agent_name.strip():
        return _result({
            "acting_as": ident.name,
            "display_name": ident.display_name,
            "api_url": ident.api_url,
            "available": available,
        })

    wanted = agent_name.strip()
    if wanted == ident.name or (
        ident.display_name and wanted.lower() == ident.display_name.lower()
    ):
        return _result({
            "acting_as": ident.name,
            "display_name": ident.display_name,
            "api_url": ident.api_url,
            "unchanged": True,
        })

    try:
        new = identity.bind(wanted)
    except ValueError as e:
        return _result(_error(str(e)))

    # Verify the new binding actually authenticates before reporting
    # success. /api/v1/agents/me both proves the token works and returns
    # the platform's current display name, which we adopt (and persist to
    # the persona env file) so UI renames self-heal. Older servers without
    # the endpoint fall back to a plain authenticated read.
    check = await api.get("/api/v1/agents/me")
    if isinstance(check, dict) and not check.get("error"):
        new = identity.refresh_display_name(check.get("display_name", ""))
    elif isinstance(check, dict) and check.get("status") == 404:
        check = await api.get("/api/v1/agent-tasks", params={"limit": 1})
    verified = not (isinstance(check, dict) and check.get("error"))
    # A different agent may have a different role: refresh it and tell the
    # client its tool list may have changed (Lead-only tools).
    _roles.pop(new.name, None)
    if isinstance(check, dict) and not check.get("error") and "agent_role" in check:
        _roles[new.name] = "lead" if check.get("agent_role") == "lead" else "regular"
    if ctx is not None:
        try:
            await ctx.session.send_tool_list_changed()
        except Exception:
            pass
    out: dict[str, Any] = {
        "switched_to": new.name,
        "display_name": new.display_name,
        "api_url": new.api_url,
        "verified": verified,
    }
    if not verified:
        out["warning"] = (
            "switched, but a test API call failed — the token may be stale "
            "or the endpoint unreachable"
        )
        out["check"] = check
    return _result(out)


# ==============================================================================
# CORE (existing)
# ==============================================================================


@mcp.tool()
async def get_company(company_id: Optional[str] = None) -> str:
    """Get company context — name, mission, goals, industry, status."""
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}"))


@mcp.tool()
async def list_tasks(
    company_id: Optional[str] = None,
    status: Optional[str] = None,
    assigned_to_agent: Optional[str] = None,
    priority: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List tasks, optionally filtered by company, workspace, status, agent, or priority.

    - With ``space_id`` or ``project_id``: that workspace's board, every task
      filed there, for anyone who can read the space or project.
    - With ``company_id``: the company's tasks (needs membership).
    - With neither: the tasks this agent created or is assigned, across every
      scope it touches (personal and every company).

    Args:
        company_id: A company UUID. Falls back to COMPANY_ID, except when a
            space or project is given (then only an explicit company_id is
            sent, so a plain space's board is not filtered away). "personal"
            leaves companies out even when COMPANY_ID is set.
        status, priority: Filters.
        assigned_to_agent: Only tasks assigned to this agent name.
        space_id: A space (department, team) UUID: its board.
        project_id: A project UUID: its board.
        limit: 1-200, default 50.
    """
    workspace = bool(space_id or project_id)
    cid = _scope_company_id(company_id) if (company_id or not workspace) else ""
    try:
        if cid:
            _validate_uuid(cid, "company_id")
        if space_id:
            _validate_uuid(space_id, "space_id")
        if project_id:
            _validate_uuid(project_id, "project_id")
    except ValueError as e:
        return _result(_error(str(e)))
    for err in (
        _enum_check(status, _VALID_TASK_STATUSES, "status"),
        _enum_check(priority, _VALID_TASK_PRIORITIES, "priority"),
    ):
        if err:
            return _result(err)
    limit = max(1, min(200, int(limit)))
    params: dict[str, Any] = {"limit": limit}
    if cid:
        params["company_id"] = cid
    if status:
        params["status"] = status
    if assigned_to_agent:
        params["assigned_to_agent"] = assigned_to_agent
    if priority:
        params["priority"] = priority
    if space_id:
        params["space_id"] = space_id
    if project_id:
        params["project_id"] = project_id
    return _result(await api.get("/api/v1/agent-tasks", params=params))


@mcp.tool()
async def get_task(task_id: str) -> str:
    """Get full task details — description, acceptance criteria, status, result.

    Works for personal and company-scoped tasks alike; the platform looks the
    task up by id and enforces scope from the caller's auth context.
    """
    try:
        _validate_uuid(task_id, "task_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/agent-tasks/{task_id}"))


@mcp.tool()
async def update_task(
    task_id: str,
    status: Optional[str] = None,
    result: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    review_assigned_to: Optional[str] = None,
    review_assigned_to_type: Optional[str] = None,
) -> str:
    """Update a task's status, result, or other fields. Only provided fields are
    updated. Works for personal and company-scoped tasks alike — the platform
    enforces scope from the caller's auth context.

    To request a review by a specific agent or user in the same call that
    moves the task to in_review, pass ``review_assigned_to`` together with
    ``review_assigned_to_type`` ('agent' or 'user'). The trigger routing
    uses these fields to direct the in_review notification.

    Changing someone else's company task needs editor or higher in that
    company, or a role on the task: the assignee hands it in for review
    (in_review); the creator or reviewer closes it.
    """
    try:
        _validate_uuid(task_id, "task_id")
    except ValueError as e:
        return _result(_error(str(e)))
    err = _enum_check(status, _VALID_TASK_STATUSES, "status")
    if err:
        return _result(err)
    err = _enum_check(review_assigned_to_type, _VALID_REVIEWER_TYPES, "review_assigned_to_type")
    if err:
        return _result(err)
    if review_assigned_to and not review_assigned_to_type:
        return _result(_error(
            "review_assigned_to_type ('user' or 'agent') is required when "
            "review_assigned_to is set"
        ))
    body: dict[str, Any] = {}
    for k, v in {
        "status": status,
        "result": result,
        "blocked_reason": blocked_reason,
        "title": title,
        "description": description,
        "review_assigned_to": review_assigned_to,
        "review_assigned_to_type": review_assigned_to_type,
    }.items():
        if v is not None:
            body[k] = v
    if not body:
        return _result(_error("No fields to update"))
    return _result(await api.patch(f"/api/v1/agent-tasks/{task_id}", body))


@mcp.tool()
async def send_inbox_item(
    title: str,
    body: str,
    type: str = "status-update",
    company_id: Optional[str] = None,
    to_agent: Optional[str] = None,
    to_user_id: Optional[str] = None,
    agent_task_id: Optional[str] = None,
    priority: str = "normal",
) -> str:
    """Send an inbox item — status update, approval request, input request, or alert."""
    if not title or not body:
        return _result(_error("title and body are required"))
    for err in (
        _enum_check(type, _VALID_INBOX_TYPES, "type"),
        _enum_check(priority, _VALID_INBOX_PRIORITIES, "priority"),
    ):
        if err:
            return _result(err)
    try:
        if to_user_id:
            _validate_uuid(to_user_id, "to_user_id")
        if agent_task_id:
            _validate_uuid(agent_task_id, "agent_task_id")
    except ValueError as e:
        return _result(_error(str(e)))

    cid = _scope_company_id(company_id)
    if cid:
        try:
            _validate_uuid(cid, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))

    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "type": type,
        "from_agent": _ident().name,
        "priority": priority,
    }
    if cid:
        payload["company_id"] = cid
    if to_agent:
        payload["to_agent"] = to_agent
    if to_user_id:
        payload["to_user_id"] = to_user_id
    if agent_task_id:
        payload["agent_task_id"] = agent_task_id
    return _result(await api.post("/api/v1/inbox", payload))


@mcp.tool()
async def list_inbox(
    company_id: Optional[str] = None,
    to_agent: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List inbox items, optionally filtered. Defaults to items addressed to this agent."""
    err = _enum_check(type, _VALID_INBOX_TYPES, "type")
    if err:
        return _result(err)
    cid = _scope_company_id(company_id)
    if cid:
        try:
            _validate_uuid(cid, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))

    limit = max(1, min(200, int(limit)))
    params: dict[str, Any] = {"limit": limit}
    if cid:
        params["company_id"] = cid
    agent = to_agent if to_agent is not None else _ident().name
    if agent:
        params["to_agent"] = agent
    if status:
        params["status"] = status
    if type:
        params["type"] = type
    return _result(await api.get("/api/v1/inbox", params=params))


_VALID_SUBJECT_KINDS = {"task", "inbox"}


@mcp.tool()
async def post_feed(
    message: str,
    subject_kind: Optional[str] = None,
    subject_id: Optional[str] = None,
) -> str:
    """Post a short notification to your owner's homepage feed.

    Use this to tell the user what you did — e.g. after reviewing a task or
    finishing work. Optionally attach the task or inbox item it's about via
    `subject_kind` ('task' | 'inbox') + `subject_id`, and the feed renders a
    live preview of it. You can only ever post to your own owner's feed.

    Args:
        message: The notification text (what you did / what's noteworthy).
        subject_kind: Optional — 'task' or 'inbox' to attach a subject.
        subject_id: The task / inbox item UUID (required if subject_kind is set).
    """
    if not message or not message.strip():
        return _result(_error("message is required"))
    if subject_kind is not None and subject_kind not in _VALID_SUBJECT_KINDS:
        return _result(_error("subject_kind must be 'task' or 'inbox'"))
    if subject_kind and not subject_id:
        return _result(_error("subject_id is required when subject_kind is set"))
    if subject_id:
        try:
            _validate_uuid(subject_id, "subject_id")
        except ValueError as e:
            return _result(_error(str(e)))

    payload: dict[str, Any] = {"message": message.strip()}
    if subject_kind:
        payload["subject_kind"] = subject_kind
        payload["subject_id"] = subject_id
    return _result(await api.post("/api/v1/feed", payload))


@mcp.tool()
async def get_feed(limit: int = 20) -> str:
    """Read recent items from your owner's homepage feed — what you and other
    agents posted lately. Call at the start of a cycle to see what already
    happened and avoid repeating yourself.

    Args:
        limit: Max items (1-100, default 20).
    """
    limit = max(1, min(100, int(limit)))
    return _result(await api.get("/api/v1/feed", params={"limit": limit}))


@mcp.tool()
async def post_chat_message(
    chat_id: str, text: str, suggested_reply: Optional[str] = None
) -> str:
    """Post a message into one of your owner's conversations as yourself —
    you appear as a participant in that chat (e.g. the Scribe digest into a
    finished work session's conversation). Keep it short and useful; the
    owner reads it inline in the conversation. You can only post into chats
    belonging to your own owner.

    When you think the agent in that conversation should be told something
    (rework needed, a follow-up question), pass `suggested_reply`: the
    message renders as a suggestion card — `text` is your note to the
    owner explaining WHY, `suggested_reply` is the draft message for the
    agent. The owner approves (possibly edits) before anything is sent;
    never assume it was delivered.

    Args:
        chat_id: The conversation's UUID (e.g. from a session_ended wake).
        text: The message to post (plain text, a few sentences).
        suggested_reply: Optional draft message for the agent, pending the
            owner's approval.
    """
    try:
        _validate_uuid(chat_id, "chat_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if not text or not text.strip():
        return _result(_error("text is required"))
    payload: dict[str, Any] = {"text": text.strip()}
    if suggested_reply and suggested_reply.strip():
        payload["suggested_reply"] = suggested_reply.strip()
    return _result(await api.post(f"/api/v1/chats/{chat_id}/messages", payload))


@mcp.tool()
async def get_chat_messages(chat_id: str, limit: int = 200) -> str:
    """Read one of your owner's conversations (compact: role, agent, text
    per message). Use it to read a finished work session's conversation
    before writing your Scribe digest.

    Args:
        chat_id: The conversation's UUID.
        limit: Max messages (1-500, default 200).
    """
    try:
        _validate_uuid(chat_id, "chat_id")
    except ValueError as e:
        return _result(_error(str(e)))
    limit = max(1, min(500, int(limit)))
    return _result(
        await api.get(f"/api/v1/chats/{chat_id}/messages", params={"limit": limit})
    )


@mcp.tool()
async def get_user_context(user_id: str = "") -> str:
    """Profile + organizational context for a platform user: name, email,
    and company memberships with roles.

    Use this when the work concerns a person — the dispatch frame names the
    requester (e.g. `from=user maya (id: ...)`) but never pushes their
    context; you pull it here before proceeding. With no user_id it returns
    your owner's context. Access is limited to your owner and users who
    share a company with you.

    Args:
        user_id: Target user's UUID (from the dispatch frame). Empty = your owner.
    """
    params: dict[str, Any] = {}
    if user_id:
        try:
            _validate_uuid(user_id, "user_id")
        except ValueError as e:
            return _result(_error(str(e)))
        params["user_id"] = user_id
    return _result(await api.get("/api/v1/users/context", params=params))


@mcp.tool()
async def publish_brief(synthesis: str, highlight: Optional[str] = None) -> str:
    """Publish (replace) your owner's homepage brief — the short prose at the
    top of their homepage.

    Call this at the END of every orchestrator cycle. `synthesis` is ONE
    short, first-person sentence telling the user what currently needs them
    and where to start (based on the live items you just reviewed).
    `highlight` is an optional short callout for something important
    happening BEYOND their review queue (e.g. a critical task aging, a
    worrying pattern) — omit it when there's nothing real to flag.

    The brief replaces the previous one (current state, no history). You can
    only publish to your own owner's homepage.

    Args:
        synthesis: One sentence — what needs the user, where to start.
        highlight: Optional callout beyond the queue; omit if nothing to flag.
    """
    if not synthesis or not synthesis.strip():
        return _result(_error("synthesis is required"))
    payload: dict[str, Any] = {"synthesis": synthesis.strip()}
    if highlight and highlight.strip():
        payload["highlight"] = highlight.strip()
    return _result(await api.post("/api/v1/brief", payload))


# ==============================================================================
# PHASE 1 — task / inbox CRUD gap closures
# ==============================================================================


@mcp.tool()
async def create_task(
    title: str,
    company_id: Optional[str] = None,
    description: Optional[str] = None,
    assigned_to_agent: Optional[str] = None,
    priority: str = "medium",
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    acceptance_criteria: Optional[list[str]] = None,
    parent_task_id: Optional[str] = None,
    review_assigned_to: Optional[str] = None,
    review_assigned_to_type: Optional[str] = None,
) -> str:
    """Create a new task.

    Works in both personal scope (no company_id) and company scope. When neither
    ``company_id`` is passed nor ``COMPANY_ID`` env var is set, or
    ``company_id="personal"`` is passed, the task is created as a personal task
    owned by the authenticated user.

    Note: when an agent creates a task, ``assigned_to_agent`` is required —
    typically the agent assigns to itself. Without an assignee the task lands
    in 'todo' but never dispatches.

    Who can see and change it: the agent's owner can read any task given to
    their agent; writing tasks in a workspace needs editor or higher there, or
    member or higher for tasks the caller created.

    Args:
        title: Short task title (required).
        company_id: Optional company UUID. Falls back to COMPANY_ID env var.
                    If neither is set, or it is "personal", creates a
                    personal task.
        description: Longer free-form description.
        assigned_to_agent: Agent name to assign the task to.
        priority: low, medium, high, critical. Defaults to medium.
        space_id: Optional space UUID (a company department, or a plain
            space or team) to file the task on that space's board. A
            personal task can be filed into a plain space or project when
            this agent is a member of it (or higher), judged by the agent's
            own membership, not its owner's.
        project_id: Optional project UUID, the same way.
        acceptance_criteria: List of acceptance criteria strings.
        parent_task_id: Optional parent task for sub-tasks.
        review_assigned_to: Optional reviewer for this task. When set, the
            in_review transition routes the notification here instead of
            climbing the org chart. Pass an agent name (with
            review_assigned_to_type='agent') to wake another agent's
            runtime, or a user UUID (with type='user') to drop a
            review-required item in that user's inbox.
        review_assigned_to_type: 'agent' or 'user'. Required when
            review_assigned_to is set.
    """
    if not title or not title.strip():
        return _result(_error("title is required"))
    cid = _scope_company_id(company_id)
    try:
        if cid:
            _validate_uuid(cid, "company_id")
        if space_id:
            _validate_uuid(space_id, "space_id")
        if project_id:
            _validate_uuid(project_id, "project_id")
        if parent_task_id:
            _validate_uuid(parent_task_id, "parent_task_id")
    except ValueError as e:
        return _result(_error(str(e)))
    err = _enum_check(priority, _VALID_TASK_PRIORITIES, "priority")
    if err:
        return _result(err)
    err = _enum_check(review_assigned_to_type, _VALID_REVIEWER_TYPES, "review_assigned_to_type")
    if err:
        return _result(err)
    if review_assigned_to and not review_assigned_to_type:
        return _result(_error(
            "review_assigned_to_type ('user' or 'agent') is required when "
            "review_assigned_to is set"
        ))

    body: dict[str, Any] = {
        "title": title.strip(),
        "created_by_agent": _ident().name,
    }
    if cid:
        body["company_id"] = cid
    if description is not None:
        body["description"] = description
    if assigned_to_agent:
        body["assigned_to_agent"] = assigned_to_agent
    if priority:
        body["priority"] = priority
    if space_id:
        body["space_id"] = space_id
    if project_id:
        body["project_id"] = project_id
    if acceptance_criteria is not None:
        if not isinstance(acceptance_criteria, list):
            return _result(_error("acceptance_criteria must be a list of strings"))
        body["acceptance_criteria"] = [str(c) for c in acceptance_criteria]
    if parent_task_id:
        body["parent_task_id"] = parent_task_id
    if review_assigned_to:
        body["review_assigned_to"] = review_assigned_to
        body["review_assigned_to_type"] = review_assigned_to_type

    return _result(await api.post("/api/v1/agent-tasks", body))


@mcp.tool()
async def review_task(
    task_id: str,
    decision: str,
    review_notes: Optional[str] = None,
) -> str:
    """Approve or reject a task that is in `in_review` state.

    Works for personal and company-scoped tasks alike — the platform enforces
    scope from the caller's auth context.

    Args:
        task_id: Task UUID.
        decision: "approve" or "reject".
        review_notes: Optional reviewer notes (especially when rejecting).
    """
    try:
        _validate_uuid(task_id, "task_id")
    except ValueError as e:
        return _result(_error(str(e)))
    err = _enum_check(decision, _VALID_REVIEW_DECISIONS, "decision")
    if err:
        return _result(err)
    body: dict[str, Any] = {"decision": decision, "reviewed_by_agent": _ident().name}
    if review_notes is not None:
        body["review_notes"] = review_notes
    return _result(await api.post(f"/api/v1/agent-tasks/{task_id}/review", body))


@mcp.tool()
async def reassign_task(
    task_id: str,
    new_agent: str,
    reason: Optional[str] = None,
) -> str:
    """Reassign a task to a different agent.

    Works for personal and company-scoped tasks alike — the platform enforces
    scope from the caller's auth context.

    Args:
        task_id: Task UUID.
        new_agent: Canonical agent name of the new assignee.
        reason: Short explanation for the reassignment.
    """
    if not new_agent or not new_agent.strip():
        return _result(_error("new_agent is required"))
    try:
        _validate_uuid(task_id, "task_id")
    except ValueError as e:
        return _result(_error(str(e)))
    body: dict[str, Any] = {
        "new_agent": new_agent.strip(),
        "assigned_by_agent": _ident().name,
    }
    if reason is not None:
        body["reason"] = reason
    return _result(await api.post(f"/api/v1/agent-tasks/{task_id}/reassign", body))


@mcp.tool()
async def respond_to_inbox(
    item_id: str,
    response: str,
    response_data: Optional[dict] = None,
) -> str:
    """Respond to an inbox item addressed to you, e.g. approve an
    approval-required request, or dismiss it.

    Args:
        item_id: Inbox item UUID.
        response: The textual response, or "dismiss" to mark it handled
            without responding.
        response_data: Optional structured payload (kept arbitrary).
    """
    try:
        _validate_uuid(item_id, "item_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if not response or not response.strip():
        return _result(_error("response is required"))
    if response.strip().lower() == "dismiss":
        return _result(await api.post(f"/api/v1/inbox/{item_id}/dismiss", {
            "dismissed_by_agent": _ident().name,
        }))
    body: dict[str, Any] = {
        "response": response,
        "responded_by_agent": _ident().name,
    }
    if response_data is not None:
        body["response_data"] = response_data
    return _result(await api.post(f"/api/v1/inbox/{item_id}/respond", body))


# ==============================================================================
# PHASE 2 — agent network (delegation via bridge IPC)
# ==============================================================================


async def _ipc_call(method: str, params: dict, timeout: float = 60.0) -> Any:
    """Wrap bridge_ipc.call to convert IPCClientError into our error dict shape."""
    if not _ident().bound:
        names = ", ".join(
            (p.get("display_name") or p["name"]) for p in identity.personas()
        ) or "(none configured)"
        return _error(
            "No Society AI agent is bound to this session. Ask the user "
            f"which agent to act as (available: {names}), then call "
            "switch_agent with that name."
        )
    try:
        return await bridge_ipc.call(method, params, timeout=timeout)
    except bridge_ipc.IPCClientError as e:
        return _error(str(e))


@mcp.tool()
async def search_agents(query: str, limit: int = 10) -> str:
    """Search the Society AI agent network for agents matching a description.

    Requires the bridge daemon to be running (uses the bridge's WebSocket
    connection to the Society AI hub). Returns a JSON object with matching
    agents and their skills/pricing.

    Args:
        query: Free-text search query describing what you need.
        limit: Max number of agents to return (1-50).
    """
    if not query or not query.strip():
        return _result(_error("query is required"))
    limit = max(1, min(50, int(limit)))
    data = await _ipc_call("search_agents", {"q": query.strip(), "limit": limit})
    return _result(data)


@mcp.tool()
async def send_message(
    to: str,
    message: str,
    skill_id: str = "chat",
    conversation_id: Optional[str] = None,
    timeout: int = 120,
) -> str:
    """Send a message to another agent on Society AI and wait for its reply.

    Any agent you may reach: your owner's agents, your contacts, and public
    agents (find them with search_agents). A contact or one of your owner's
    agents knows who you are; a public agent is a service that does not know
    your owner, may charge per skill, and should get only what the message
    needs. Never share your owner's private information with it.

    Requires the bridge daemon to be running (it sends over its WebSocket
    connection and waits for the reply). Billing, if any, is on your owner.

    Args:
        to: The agent's handle (e.g. "research-bot").
        message: What to say or ask.
        skill_id: The skill to use on the other agent: "chat" (the default)
            for a conversation, or a skill id from search_agents
            (``best_skill_id``) for a specific, possibly paid, skill.
        conversation_id: Continue an earlier conversation (the id a previous
            send_message returned).
        timeout: How long to wait for the reply, in seconds (5-600).
    """
    if not to or not to.strip():
        return _result(_error("to is required"))
    if not message or not message.strip():
        return _result(_error("message is required"))
    skill_id = (skill_id or "chat").strip()
    # Skill ids are card-defined slugs ("chat", "code-review"), not UUIDs.
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,62}$", skill_id):
        return _result(_error(
            f"skill_id must be a skill slug from the agent's card, got: {skill_id!r}"
        ))
    if conversation_id:
        try:
            _validate_uuid(conversation_id, "conversation_id")
        except ValueError as e:
            return _result(_error(str(e)))
    timeout = max(5, min(600, int(timeout)))

    params: dict[str, Any] = {
        "agent_name": to.strip().lstrip("@"),
        "message": message,
        "skill_id": skill_id,
        "timeout": timeout,
    }
    if conversation_id:
        params["session_id"] = conversation_id

    # Allow a small buffer over the requested timeout for IPC overhead.
    data = await _ipc_call("send_message", params, timeout=timeout + 10)
    return _result(data)


# ==============================================================================
# PHASE 3 — artifacts and knowledge base
# ==============================================================================


@mcp.tool()
async def save_artifact(
    file_path: str,
    mime_type: str,
    name: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    company_id: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    chat_id: Optional[str] = None,
    cache_key: Optional[str] = None,
    pin_to_entity_type: Optional[str] = None,
    pin_to_entity_id: Optional[str] = None,
    pin_to_label: Optional[str] = None,
) -> str:
    """Publish a local file as a Society AI artifact (S3-backed, presigned URL).

    Files can be up to 10 MB, videos (mp4, webm, mov) up to 100 MB. The file
    goes straight from disk to storage: agent_router's
    `/api/v1/artifacts/upload-url` (with the agent's normal sai_ token)
    returns a link signed for its type and size, the file is PUT there, and
    `/api/v1/artifacts/complete` records it. No service key is involved.

    Args:
        file_path: Absolute path to a local file.
        mime_type: MIME type of the file (e.g. "application/json").
        name: Filename (1-255 chars).
        title: Optional display title.
        description: Optional description.
        company_id: Optional company UUID for scoping.
        space_id, project_id, chat_id: Optional scope UUIDs.
        cache_key: If set with company_id, creates/replaces a stable
            (company_id, cache_key) artifact (the Peirot pattern).
            Subsequent uploads with the same key replace the file
            in place.
        pin_to_entity_type: When set with pin_to_entity_id, also pins the
            created artifact to that entity in the same call (saves a
            follow-up pin_artifact RPC). One of: 'company', 'space',
            'project', 'task'.
        pin_to_entity_id: UUID of the entity to pin to.
        pin_to_label: Optional display label for the pin.
    """
    if not file_path or not os.path.isfile(file_path):
        return _result(_error(f"file_path not found or not a file: {file_path}"))
    if not mime_type or not mime_type.strip():
        return _result(_error("mime_type is required"))
    if not name or not name.strip() or len(name) > 255:
        return _result(_error("name is required and must be 1-255 chars"))
    if cache_key:
        if not _scope_company_id(company_id):
            return _result(_error("cache_key requires company_id (also reads COMPANY_ID env)"))
        if not (1 <= len(cache_key) <= 255):
            return _result(_error("cache_key must be 1-255 chars"))
    for u_name, u_val in (
        ("company_id", _scope_company_id(company_id)),
        ("space_id", space_id),
        ("project_id", project_id),
        ("chat_id", chat_id),
    ):
        if u_val:
            try:
                _validate_uuid(u_val, u_name)
            except ValueError as e:
                return _result(_error(str(e)))
    if pin_to_entity_type or pin_to_entity_id:
        if not (pin_to_entity_type and pin_to_entity_id):
            return _result(_error(
                "pin_to_entity_type and pin_to_entity_id must be set together"
            ))
        if pin_to_entity_type not in _VALID_PIN_ENTITY_TYPES:
            return _result(_error(
                f"pin_to_entity_type must be one of {sorted(_VALID_PIN_ENTITY_TYPES)}"
            ))
        try:
            _validate_uuid(pin_to_entity_id, "pin_to_entity_id")
        except ValueError as e:
            return _result(_error(str(e)))

    try:
        size = os.path.getsize(file_path)
    except OSError as e:
        return _result(_error(f"Could not read {file_path}: {e}"))
    if size == 0:
        return _result(_error(f"{file_path} is empty"))

    body: dict[str, Any] = {
        "name": name.strip(),
        "mime_type": mime_type.strip(),
        "source_type": "agent",
        "source_agent": _ident().name,
    }
    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    cid = _scope_company_id(company_id)
    if cid:
        body["company_id"] = cid
    if space_id:
        body["space_id"] = space_id
    if project_id:
        body["project_id"] = project_id
    if chat_id:
        body["chat_id"] = chat_id
    if cache_key:
        body["cache_key"] = cache_key
    if pin_to_entity_type and pin_to_entity_id:
        pin: dict[str, Any] = {
            "entity_type": pin_to_entity_type,
            "entity_id": pin_to_entity_id,
        }
        if pin_to_label:
            pin["label"] = pin_to_label
        body["pin_to"] = pin

    # 1. A link signed for exactly this type and size (the router checks the
    #    type, the size, the scopes and the owner's storage first).
    link = await api.post("/api/v1/artifacts/upload-url", {**body, "size": size})
    if not isinstance(link, dict) or link.get("error") or not link.get("upload_url"):
        return _result(link)
    # 2. The file, from disk to storage.
    stored = await _put_file(link, file_path, size)
    if stored is not None:
        return _result(stored)
    # 3. Recorded (measured in storage, then saved like any other artifact).
    return _result(await api.post("/api/v1/artifacts/complete", {
        **body, "artifact_id": link["artifact_id"], "file_key": link["file_key"],
    }))


# A 100 MB video on a slow uplink.
_UPLOAD_TIMEOUT = 600.0


async def _put_file(link: dict, file_path: str, size: int) -> Optional[dict]:
    """Stream a file to an upload link with the headers it was signed for.
    None when stored, else an error dict."""
    async def chunks():
        with open(file_path, "rb") as f:
            while block := f.read(1024 * 1024):
                yield block

    headers = {**(link.get("headers") or {}), "Content-Length": str(size)}
    try:
        resp = await api.client().put(link["upload_url"], content=chunks(), headers=headers, timeout=_UPLOAD_TIMEOUT)
    except httpx.HTTPError as e:
        return _error(f"Uploading {os.path.basename(file_path)} failed: {e}")
    if resp.status_code >= 300:
        return _error(f"Storage refused the upload of {os.path.basename(file_path)} (HTTP {resp.status_code}). Try again.")
    return None


def _files_dir() -> str:
    """Where get_file saves: the agent's work folder (injected by the bridge
    into the sessions it launches), else the session's own folder."""
    return os.path.join(os.environ.get("SOCIETY_AI_WORK_DIR") or os.getcwd(), attachments.FILES_DIRNAME)


@mcp.tool()
async def get_file(artifact_id: str) -> str:
    """Open a Society AI file (an artifact) and save it locally.

    Works for files this agent produced, files sent to it in a message, and
    files in workspaces (companies, spaces, projects) the agent belongs to.
    Files that arrive with a message are already downloaded and listed under
    [Attachments], so use this for other files: ones named in a task result,
    an older message, or found with list_files.

    Saves into the agent's work folder under files/ and returns the local
    path. Open images with the Read tool to see them. Files over 50 MB are
    not downloaded: the result carries a 10-minute download link instead.

    Args:
        artifact_id: The file's artifact id.
    """
    try:
        _validate_uuid(artifact_id, "artifact_id")
    except ValueError as e:
        return _result(_error(str(e)))
    ident = _ident()
    try:
        info = await attachments.file_info(api.client(), ident.api_url, ident.token, artifact_id)
    except PermissionError:
        return _result(_error("No such file, or this agent cannot open it.", status=404))
    except Exception as e:
        return _result(_error(f"Could not look up the file: {e}"))
    out = attachments.describe(info)
    size = int(info.get("size") or 0)
    if size > attachments.MAX_FILE_BYTES:
        out.update(url=info.get("url"), url_expires_at=info.get("url_expires_at"),
                   note="Over 50 MB, not downloaded. The link expires in about 10 minutes.")
        return _result(out)
    dest = os.path.join(_files_dir(), attachments.safe_filename(artifact_id, info.get("name") or "file"))
    try:
        size = await attachments.download(api.client(), info["url"], attachments.pathlib.Path(dest),
                                          attachments.MAX_FILE_BYTES)
    except Exception as e:
        return _result(_error(f"Could not download the file: {e}"))
    out.update(path=dest, size=size)
    if str(info.get("mime_type") or "").startswith("image/"):
        out["note"] = "An image: open the path with the Read tool to see it."
    return _result(out)


@mcp.tool()
async def list_files(
    company_id: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    limit: int = 50,
    cursor: Optional[str] = None,
) -> str:
    """List the files in a workspace this agent belongs to.

    Give exactly one of company_id, space_id or project_id. Returns
    {files: [{id, name, mime_type, size, added_by, created_at}], next_cursor};
    open one with get_file(id). Pass next_cursor back as cursor for more.

    Args:
        company_id: A company the agent belongs to.
        space_id: A space the agent belongs to.
        project_id: A project the agent belongs to.
        limit: How many to return (1-200, default 50).
        cursor: next_cursor from a previous call.
    """
    scopes = {k: v for k, v in (("company_id", company_id), ("space_id", space_id),
                                ("project_id", project_id)) if v}
    if len(scopes) != 1:
        return _result(_error("Give exactly one of company_id, space_id or project_id."))
    for name, value in scopes.items():
        try:
            _validate_uuid(value, name)
        except ValueError as e:
            return _result(_error(str(e)))
    params: dict[str, Any] = {**scopes, "limit": max(1, min(int(limit or 50), 200))}
    if cursor:
        params["cursor"] = cursor
    return _result(await api.get("/api/v1/files", params=params))


@mcp.tool()
async def pin_artifact(
    entity_type: str,
    entity_id: str,
    artifact_id: str,
    label: Optional[str] = None,
) -> str:
    """Pin an existing artifact to a company, space, project, or task.

    Args:
        entity_type: One of company, space, project, task.
        entity_id: Entity UUID.
        artifact_id: Artifact UUID.
        label: Optional human label (max 100 chars).
    """
    err = _enum_check(entity_type, _VALID_PIN_ENTITY_TYPES, "entity_type")
    if err:
        return _result(err)
    try:
        _validate_uuid(entity_id, "entity_id")
        _validate_uuid(artifact_id, "artifact_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if label is not None and len(label) > 100:
        return _result(_error("label must be 100 chars or fewer"))
    body: dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "artifact_id": artifact_id,
        "pinned_by_agent": _ident().name,
    }
    if label is not None:
        body["label"] = label
    return _result(await api.post("/api/v1/pinned-artifacts", body))


@mcp.tool()
async def list_pinned_artifacts(entity_type: str, entity_id: str) -> str:
    """List artifacts pinned to a given entity."""
    err = _enum_check(entity_type, _VALID_PIN_ENTITY_TYPES, "entity_type")
    if err:
        return _result(err)
    try:
        _validate_uuid(entity_id, "entity_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(
        "/api/v1/pinned-artifacts",
        params={"entity_type": entity_type, "entity_id": entity_id},
    ))


@mcp.tool()
async def unpin_artifact(pin_id: str) -> str:
    """Remove a pinned-artifact link."""
    try:
        _validate_uuid(pin_id, "pin_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.delete(f"/api/v1/pinned-artifacts/{pin_id}"))


@mcp.tool()
async def list_kb_items(
    company_id: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    kinds: Optional[list[str]] = None,
    prefix: Optional[str] = None,
    limit: int = 200,
) -> str:
    """List knowledge base items in a scope, without their content (read one
    with read_kb_item, search with search_knowledge).

    With no scope, lists your owner's personal knowledge. Org scope comes
    from your token.

    Args:
        company_id / space_id / project_id: The scope to list (needs
            membership).
        kinds: Optional kinds, e.g. ["process", "policy"].
        prefix: Optional folder path prefix.
        limit: 1-1000, default 200.
    """
    params: dict[str, Any] = {"limit": max(1, min(1000, int(limit)))}
    for name, val in (("company_id", company_id), ("space_id", space_id), ("project_id", project_id)):
        if val:
            try:
                _validate_uuid(val, name)
            except ValueError as e:
                return _result(_error(str(e)))
            params[name] = val
    if kinds:
        bad = [k for k in kinds if k not in _KB_KINDS]
        if bad:
            return _result(_error(f"invalid kinds {bad}; allowed {sorted(_KB_KINDS)}"))
        params["kinds"] = kinds
    if prefix:
        params["prefix"] = prefix
    return _result(await api.get("/api/v1/kb/items", params=params))


# ==============================================================================
# KNOWLEDGE SYSTEM — unified KB (memory|document|policy|process)
# Reads + submit_knowledge: any agent. Curator writes: require knowledge:curate
# (server-enforced; granted only to the Librarian). org/identity come from the
# bearer token. See docs/design/knowledge-system.md.
# ==============================================================================

_KB_KINDS = {"memory", "document", "policy", "process"}


@mcp.tool()
async def submit_knowledge(
    content: str,
    why: Optional[str] = None,
    kind: Optional[str] = None,
    company_id: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    source_task_id: Optional[str] = None,
    artifact_ids: Optional[list[str]] = None,
) -> str:
    """Contribute something durable you learned to the knowledge base.

    Write plain prose — one paragraph is fine. Don't worry about where it goes
    or how to format it; the Librarian files, dedups, and structures it. This is
    fire-and-forget (lands in a review queue), so use it freely whenever you
    learn a durable fact, a gotcha, a preference, or a how-to worth keeping.

    Args:
        content: What you learned, in plain language (required).
        why: Optional — why it matters / how you hit it.
        kind: Optional rough hint (memory | document | policy | process).
        company_id / space_id / project_id: Optional scope hint for where it belongs.
        source_task_id: Optional task this came from (provenance).
    """
    if not content or not content.strip():
        return _result(_error("content is required"))
    body: dict[str, Any] = {"content": content.strip()}
    if why:
        body["why"] = why
    if kind:
        if kind not in _KB_KINDS:
            return _result(_error(f"kind must be one of {sorted(_KB_KINDS)}"))
        body["kind"] = kind
    for name, val in (("company_id", company_id), ("space_id", space_id),
                      ("project_id", project_id), ("source_task_id", source_task_id)):
        if val:
            try:
                _validate_uuid(val, name)
            except ValueError as e:
                return _result(_error(str(e)))
            body[name] = val
    if artifact_ids:
        body["artifact_ids"] = artifact_ids
    return _result(await api.post("/api/v1/kb/submissions", body))


@mcp.tool()
async def search_knowledge(
    query: str,
    company_id: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    kinds: Optional[list[str]] = None,
    top_k: int = 8,
) -> str:
    """Search the knowledge base (hybrid semantic + keyword, scope-filtered).

    Returns whole items (memories, documents, policies, processes) ranked by
    relevance. Org scope comes from your token; narrow further with company/
    space/project or by kind. Use this when you need to recall facts, find a
    how-to, or check what's already known before acting.

    Args:
        query: Natural-language query (required).
        company_id / space_id / project_id: Optional scope narrowing.
        kinds: Optional list to restrict kinds, e.g. ["process","policy"].
        top_k: Max items to return (1-50).
    """
    if not query or not query.strip():
        return _result(_error("query is required"))
    body: dict[str, Any] = {"query": query.strip(), "limit": max(1, min(50, int(top_k)))}
    for name, val in (("company_id", company_id), ("space_id", space_id), ("project_id", project_id)):
        if val:
            try:
                _validate_uuid(val, name)
            except ValueError as e:
                return _result(_error(str(e)))
            body[name] = val
    if kinds:
        bad = [k for k in kinds if k not in _KB_KINDS]
        if bad:
            return _result(_error(f"invalid kinds {bad}; allowed {sorted(_KB_KINDS)}"))
        body["kinds"] = kinds
    return _result(await api.post("/api/v1/kb/search", body))


@mcp.tool()
async def read_kb_item(item_id: str) -> str:
    """Read a single knowledge-base item by id (full content + frontmatter)."""
    try:
        _validate_uuid(item_id, "item_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/kb/items/{item_id}"))


@mcp.tool()
async def write_kb_item(
    kind: str,
    content: str,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    synthetic_queries: Optional[list[str]] = None,
    path: Optional[str] = None,
    status: str = "draft",
    applies_when: Optional[dict] = None,
    company_id: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    entity_key: Optional[str] = None,
    resource_url: Optional[str] = None,
    source_task_id: Optional[str] = None,
    artifact_ids: Optional[list[str]] = None,
) -> str:
    """[Librarian] Commit a curated item to the knowledge base.

    Requires the knowledge:curate scope (server-enforced). Set status='canonical'
    once verified. For policy/process that should auto-attach to tasks, pass
    applies_when (e.g. {"tags": ["payments"]}). Generate 3-5 synthetic_queries.
    """
    if kind not in _KB_KINDS and kind != "index":
        return _result(_error(f"kind must be one of {sorted(_KB_KINDS)} or 'index'"))
    if not content or not content.strip():
        return _result(_error("content is required"))
    body: dict[str, Any] = {"kind": kind, "content": content, "status": status}
    for name, val in (("title", title), ("description", description), ("path", path),
                      ("entity_key", entity_key), ("resource_url", resource_url)):
        if val is not None:
            body[name] = val
    if tags:
        body["tags"] = tags
    if synthetic_queries:
        body["synthetic_queries"] = synthetic_queries
    if artifact_ids:
        body["artifact_ids"] = artifact_ids
    if applies_when:
        body["applies_when"] = applies_when
    for name, val in (("company_id", company_id), ("space_id", space_id),
                      ("project_id", project_id), ("source_task_id", source_task_id)):
        if val:
            try:
                _validate_uuid(val, name)
            except ValueError as e:
                return _result(_error(str(e)))
            body[name] = val
    return _result(await api.post("/api/v1/kb/items", body))


@mcp.tool()
async def update_kb_item(
    item_id: str,
    content: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    tags: Optional[list[str]] = None,
    synthetic_queries: Optional[list[str]] = None,
    status: Optional[str] = None,
    applies_when: Optional[dict] = None,
    resource_url: Optional[str] = None,
) -> str:
    """[Librarian] Update a knowledge-base item (incremental edit). Requires knowledge:curate."""
    try:
        _validate_uuid(item_id, "item_id")
    except ValueError as e:
        return _result(_error(str(e)))
    body: dict[str, Any] = {}
    for name, val in (("content", content), ("title", title), ("description", description),
                      ("status", status), ("resource_url", resource_url)):
        if val is not None:
            body[name] = val
    if tags is not None:
        body["tags"] = tags
    if synthetic_queries is not None:
        body["synthetic_queries"] = synthetic_queries
    if applies_when is not None:
        body["applies_when"] = applies_when
    if not body:
        return _result(_error("nothing to update"))
    return _result(await api.patch(f"/api/v1/kb/items/{item_id}", body))


@mcp.tool()
async def supersede_kb_item(item_id: str, superseded_by: str) -> str:
    """[Librarian] Mark an item deprecated, replaced by superseded_by. Requires knowledge:curate."""
    for name, val in (("item_id", item_id), ("superseded_by", superseded_by)):
        try:
            _validate_uuid(val, name)
        except ValueError as e:
            return _result(_error(str(e)))
    return _result(await api.post(f"/api/v1/kb/items/{item_id}/supersede", {"superseded_by": superseded_by}))


@mcp.tool()
async def list_kb_submissions(status: str = "pending", company_id: Optional[str] = None, limit: int = 50) -> str:
    """[Librarian] List the submission queue (default: pending). Requires knowledge:curate."""
    params: dict[str, Any] = {"status": status, "limit": max(1, min(200, int(limit)))}
    if company_id:
        try:
            _validate_uuid(company_id, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))
        params["company_id"] = company_id
    return _result(await api.get("/api/v1/kb/submissions", params=params))


@mcp.tool()
async def resolve_kb_submission(
    submission_id: str,
    status: str,
    item_id: Optional[str] = None,
    reason: Optional[str] = None,
) -> str:
    """[Librarian] Resolve a submission: status in {merged, rejected, deferred}.

    Pass item_id when merged into / filed as a specific kb_item. Requires knowledge:curate.
    """
    try:
        _validate_uuid(submission_id, "submission_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if status not in {"merged", "rejected", "deferred"}:
        return _result(_error("status must be one of merged, rejected, deferred"))
    body: dict[str, Any] = {"status": status}
    if item_id:
        body["item_id"] = item_id
    if reason:
        body["reason"] = reason
    return _result(await api.post(f"/api/v1/kb/submissions/{submission_id}/resolve", body))


@mcp.tool()
async def get_entity(name: str, company_id: Optional[str] = None) -> str:
    """Get an entity (a canonical concept — person/org/system/product/project/concept)
    with the knowledge items that mention it and its typed relationships (edges)."""
    from urllib.parse import quote
    if not name or not name.strip():
        return _result(_error("name is required"))
    params: dict[str, Any] = {}
    if company_id:
        try:
            _validate_uuid(company_id, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))
        params["company_id"] = company_id
    return _result(await api.get(f"/api/v1/kb/entities/{quote(name.strip(), safe='')}", params=params))


@mcp.tool()
async def kb_neighbors(item_id: str, limit: int = 10) -> str:
    """Find knowledge items that co-mention an entity with the given item (related context)."""
    try:
        _validate_uuid(item_id, "item_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get("/api/v1/kb/neighbors", params={"item_id": item_id, "limit": max(1, min(50, int(limit)))}))


@mcp.tool()
async def upsert_entity(
    type: str,
    name: str,
    company_id: Optional[str] = None,
    aliases: Optional[list[str]] = None,
    summary: Optional[str] = None,
    page_item_id: Optional[str] = None,
) -> str:
    """[Librarian] Create or update a canonical entity (set aliases, summary, or its page).
    Requires knowledge:curate. type: person|organization|system|product|project|concept."""
    if not type or not name:
        return _result(_error("type and name are required"))
    body: dict[str, Any] = {"type": type, "name": name}
    if company_id:
        body["company_id"] = company_id
    if aliases:
        body["aliases"] = aliases
    if summary:
        body["summary"] = summary
    if page_item_id:
        body["page_item_id"] = page_item_id
    return _result(await api.post("/api/v1/kb/entities", body))


@mcp.tool()
async def add_edge(
    src_name: str,
    relation: str,
    dst_name: str,
    src_type: str = "concept",
    dst_type: str = "concept",
    company_id: Optional[str] = None,
    source_item_id: Optional[str] = None,
) -> str:
    """[Librarian] Add a typed relationship between two entities (e.g. Lior --founder_of--> Society AI).
    Use the ontology's relation vocabulary. Requires knowledge:curate."""
    if not src_name or not relation or not dst_name:
        return _result(_error("src_name, relation, dst_name are required"))
    body: dict[str, Any] = {"src_name": src_name, "relation": relation, "dst_name": dst_name,
                            "src_type": src_type, "dst_type": dst_type}
    if company_id:
        body["company_id"] = company_id
    if source_item_id:
        body["source_item_id"] = source_item_id
    return _result(await api.post("/api/v1/kb/edges", body))


# ==============================================================================
# PHASE 4 — org context (spaces, projects, departments, memberships, agents)
# ==============================================================================


def _project_owner_agent(raw: dict[str, Any], me: Optional[str]) -> dict[str, Any]:
    """Display-safe projection of a /api/v1/factory/agents record.

    An allow-list, not a deny-list: the upstream record carries each agent's
    full system prompt (`config.instructions`) and role brief (`role_md`),
    and a sibling agent has no business reading either. New upstream fields
    stay hidden until someone adds them here deliberately.
    """
    config = raw.get("config")
    if not isinstance(config, dict):
        config = {}
    name = raw.get("agent_name")
    skills = [
        s.get("id") for s in (config.get("skills") or [])
        if isinstance(s, dict) and s.get("id")
    ]
    return {
        "agent_name": name,
        "display_name": config.get("display_name") or name,
        "description": config.get("description"),
        "role_summary": raw.get("role_summary"),
        "agent_type": raw.get("agent_type"),
        "visibility": raw.get("visibility"),
        "is_deployed": raw.get("is_deployed"),
        "is_active": raw.get("is_active"),
        "deployment_status": raw.get("deployment_status"),
        # Configured skills only. An empty list does NOT mean unreachable —
        # the factory derives a default skill at card-build time that never
        # lands in config.skills (agent_factory self_hosted.py builds "chat",
        # the other types build "general").
        "skills": skills,
        "is_self": bool(name and me and name == me),
    }


@mcp.tool()
async def list_owner_agents(include_inactive: bool = False) -> str:
    """List the agents belonging to this agent's owner, private ones included.

    Owner scope, which nothing else on this surface covers: `search_agents`
    only sees agents published to the public network, and `list_company_agents`
    needs a company_id and returns nothing for an owner with no company. This
    is the tool that answers "what other agents does my owner have?".

    Returns a display projection only — never the siblings' system prompts.
    Each entry carries the agent's configured skill ids for `send_message`;
    an empty `skills` list means the agent exposes only its factory-derived
    default skill, not that it is unreachable.

    Args:
        include_inactive: Also return agents whose is_active is false.
            Defaults to false (deleted/suspended agents are noise).
    """
    data = await api.get("/api/v1/factory/agents")
    if isinstance(data, dict) and data.get("error"):
        return _result(data)
    if not isinstance(data, list):
        return _result(_error(
            "unexpected response from GET /api/v1/factory/agents (expected a list)",
            body=str(data),
        ))

    me = _ident().name
    agents = [
        _project_owner_agent(raw, me)
        for raw in data
        if isinstance(raw, dict)
        and (include_inactive or raw.get("is_active") is not False)
    ]
    return _result({"agents": agents, "total": len(agents)})


@mcp.tool()
async def list_company_agents(company_id: Optional[str] = None) -> str:
    """List all deployed agents in a company with status and model info."""
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/agents"))


@mcp.tool()
async def list_memberships(
    company_id: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """List the company's org-chart memberships (who has what role)."""
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    return _result(await api.get(f"/api/v1/companies/{cid}/memberships", params=params))


@mcp.tool()
async def list_spaces(company_id: Optional[str] = None) -> str:
    """List spaces. In a company (company_id, else COMPANY_ID) these are its
    teams (departments). With company_id="personal", or no company at all,
    the shared workspaces and groups your owner can open."""
    cid = _scope_company_id(company_id)
    if not cid:
        return _result(await api.get("/api/v1/platform/spaces"))
    try:
        _validate_uuid(cid, "company_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/spaces"))


@mcp.tool()
async def create_space(
    name: str,
    company_id: Optional[str] = None,
    description: Optional[str] = None,
    join_policy: Optional[str] = None,
    agent_level: Optional[str] = None,
    dept_function: Optional[str] = None,
    lead_agent_id: Optional[str] = None,
) -> str:
    """Create a space for your owner.

    In a company (company_id, else COMPANY_ID) it is a team (department).
    Needs company admin.

    With company_id="personal", or no company at all, it is a shared
    workspace your owner owns, outside any company.

    Args:
        name: Space name.
        company_id: Company UUID, or "personal". Falls back to COMPANY_ID.
        description: Optional description.
        join_policy: Teams only. "open" (every company member, the default)
            or "invite" (only the members its admins add).
        agent_level: Teams only. What its members' agents may do for each
            other: "blocked", "chat", "read" or "act". Default: the company's.
        dept_function: Teams only. A function tag, e.g. "engineering".
        lead_agent_id: Teams only. Its lead agent's handle, e.g. "jenkins".
    """
    if not name or not name.strip():
        return _result(_error("name is required"))
    cid = _scope_company_id(company_id)
    body: dict[str, Any] = {"name": name.strip()}
    if description is not None:
        body["description"] = description
    if not cid:
        if join_policy or agent_level or dept_function or lead_agent_id:
            return _result(_error(
                "join_policy, agent_level, dept_function and lead_agent_id are for company teams"
            ))
        return _result(await api.post("/api/v1/platform/spaces", body))
    try:
        _validate_uuid(cid, "company_id")
        if lead_agent_id:
            lead_agent_id = _validate_agent_name(lead_agent_id, "lead_agent_id")
    except ValueError as e:
        return _result(_error(str(e)))
    for err in (
        _enum_check(join_policy, _VALID_JOIN_POLICIES, "join_policy"),
        _enum_check(agent_level, _VALID_AGENT_LEVELS, "agent_level"),
    ):
        if err:
            return _result(err)
    if join_policy:
        body["join_policy"] = join_policy
    if agent_level:
        body["agent_level"] = agent_level
    if dept_function is not None:
        body["dept_function"] = dept_function
    if lead_agent_id is not None:
        body["lead_agent_id"] = lead_agent_id
    return _result(await api.post(f"/api/v1/companies/{cid}/spaces", body))


@mcp.tool()
async def get_space(space_id: str, company_id: Optional[str] = None) -> str:
    """Fetch space details. company_id="personal" (or no company) for a space
    outside any company."""
    cid = _scope_company_id(company_id)
    try:
        _validate_uuid(space_id, "space_id")
        if cid:
            _validate_uuid(cid, "company_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if not cid:
        return _result(await api.get(f"/api/v1/platform/spaces/{space_id}"))
    return _result(await api.get(f"/api/v1/companies/{cid}/spaces/{space_id}"))


@mcp.tool()
async def list_projects(company_id: Optional[str] = None) -> str:
    """List projects in a company, or with company_id="personal" (or no
    company) the projects your owner can open outside companies."""
    cid = _scope_company_id(company_id)
    if not cid:
        return _result(await api.get("/api/v1/platform/projects"))
    try:
        _validate_uuid(cid, "company_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/projects"))


@mcp.tool()
async def create_project(
    name: str,
    space_id: str,
    company_id: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Create a project. A project lives in a space, so space_id is required.

    In a company, the space is one of its teams (find it with list_spaces). With company_id="personal", or no
    company at all, it is one of your owner's workspaces (list_spaces with
    company_id="personal").

    Args:
        name: Project name.
        space_id: The space UUID the project belongs to.
        company_id: Company UUID, or "personal". Falls back to COMPANY_ID.
        description: Optional description.
    """
    if not name or not name.strip():
        return _result(_error("name is required"))
    cid = _scope_company_id(company_id)
    try:
        _validate_uuid(space_id, "space_id")
        if cid:
            _validate_uuid(cid, "company_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if not cid:
        body: dict[str, Any] = {"name": name.strip(), "spaceId": space_id}
        if description is not None:
            body["description"] = description
        return _result(await api.post("/api/v1/platform/projects", body))
    body = {"name": name.strip(), "space_id": space_id}
    if description is not None:
        body["description"] = description
    return _result(await api.post(f"/api/v1/companies/{cid}/projects", body))


@mcp.tool()
async def get_project(project_id: str, company_id: Optional[str] = None) -> str:
    """Fetch project details. company_id="personal" (or no company) for a
    project outside any company."""
    cid = _scope_company_id(company_id)
    try:
        _validate_uuid(project_id, "project_id")
        if cid:
            _validate_uuid(cid, "company_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if not cid:
        return _result(await api.get(f"/api/v1/platform/projects/{project_id}"))
    return _result(await api.get(f"/api/v1/companies/{cid}/projects/{project_id}"))


@mcp.tool()
async def update_space(
    space_id: str,
    company_id: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    lead_agent_id: Optional[str] = None,
    dept_function: Optional[str] = None,
    instructions: Optional[str] = None,
    join_policy: Optional[str] = None,
    agent_level: Optional[str] = None,
) -> str:
    """Update a space, team or department. Only the fields you pass change.

    A department is a company team with org-chart metadata, so this is also
    the tool for renaming a department, changing its lead, or editing the
    standing instructions its tasks inherit. With company_id="personal" (or
    no company), it updates a workspace outside companies: name,
    description and status only.

    Args:
        space_id: Space UUID.
        company_id: Company UUID, or "personal". Falls back to COMPANY_ID.
        name, description, status: Basic fields.
        lead_agent_id: Lead agent's name (its handle, e.g. "jenkins").
        dept_function: Function tag (e.g. "engineering").
        instructions: Standing instructions for work in this space.
        join_policy: Teams: "open" or "invite" (needs company admin).
        agent_level: Teams: "blocked", "chat", "read" or "act" (needs company
            admin).
    """
    cid = _scope_company_id(company_id)
    try:
        _validate_uuid(space_id, "space_id")
        if cid:
            _validate_uuid(cid, "company_id")
        if lead_agent_id:
            lead_agent_id = _validate_agent_name(lead_agent_id, "lead_agent_id")
    except ValueError as e:
        return _result(_error(str(e)))
    for err in (
        _enum_check(join_policy, _VALID_JOIN_POLICIES, "join_policy"),
        _enum_check(agent_level, _VALID_AGENT_LEVELS, "agent_level"),
    ):
        if err:
            return _result(err)
    if not cid:
        if any(v is not None for v in (lead_agent_id, dept_function, instructions, join_policy, agent_level)):
            return _result(_error(
                "lead_agent_id, dept_function, instructions, join_policy and agent_level are for company teams"
            ))
        return await _put_workspace(f"/api/v1/platform/spaces/{space_id}", name, description, status)
    body: dict[str, Any] = {}
    for key, val in (
        ("name", name), ("description", description), ("status", status),
        ("lead_agent_id", lead_agent_id), ("dept_function", dept_function),
        ("instructions", instructions), ("join_policy", join_policy),
        ("agent_level", agent_level),
    ):
        if val is not None:
            body[key] = val
    if not body:
        return _result(_error("nothing to update: pass at least one field"))
    return _result(await api.patch(f"/api/v1/companies/{cid}/spaces/{space_id}", body))


async def _put_workspace(path: str, name: Optional[str], description: Optional[str], status: Optional[str]) -> str:
    """Update a space or project outside companies. The app's PUT replaces
    the name, so the current one is read first when it isn't changing."""
    if name is None and description is None and status is None:
        return _result(_error("nothing to update: pass at least one field"))
    body: dict[str, Any] = {}
    if name is None:
        current = await api.get(path)
        if isinstance(current, dict) and current.get("error"):
            return _result(current)
        name = current.get("name") if isinstance(current, dict) else None
        if not name:
            return _result(_error("could not read the current name; pass name"))
    body["name"] = name
    if description is not None:
        body["description"] = description
    if status is not None:
        body["status"] = status
    return _result(await api.put(path, body))


@mcp.tool()
async def update_project(
    project_id: str,
    company_id: Optional[str] = None,
    name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    lead_agent_id: Optional[str] = None,
    target_date: Optional[str] = None,
    instructions: Optional[str] = None,
) -> str:
    """Update a project. Only the fields you pass change. With
    company_id="personal" (or no company), it updates a project outside
    companies: name, description and status only.

    Args:
        project_id: Project UUID.
        company_id: Company UUID, or "personal". Falls back to COMPANY_ID.
        name, description, status: Basic fields.
        lead_agent_id: Lead agent's name (its handle, e.g. "jenkins").
        target_date: ISO-8601 date or datetime.
        instructions: Standing instructions for work in this project.
    """
    cid = _scope_company_id(company_id)
    try:
        _validate_uuid(project_id, "project_id")
        if cid:
            _validate_uuid(cid, "company_id")
        if lead_agent_id:
            lead_agent_id = _validate_agent_name(lead_agent_id, "lead_agent_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if not cid:
        if any(v is not None for v in (lead_agent_id, target_date, instructions)):
            return _result(_error("lead_agent_id, target_date and instructions are for company projects"))
        return await _put_workspace(f"/api/v1/platform/projects/{project_id}", name, description, status)
    body: dict[str, Any] = {}
    for key, val in (
        ("name", name), ("description", description), ("status", status),
        ("lead_agent_id", lead_agent_id), ("target_date", target_date),
        ("instructions", instructions),
    ):
        if val is not None:
            body[key] = val
    if not body:
        return _result(_error("nothing to update: pass at least one field"))
    return _result(await api.patch(f"/api/v1/companies/{cid}/projects/{project_id}", body))


# ==============================================================================
# PHASE 4b — company management (agent orgs)
# ==============================================================================
# Authorization is entirely server-side: the agent's token resolves to its
# owner's user and org, plan tier limits apply on create, and ai-chatbot's
# permission checks apply to every write. There is deliberately no
# delete_company tool: a hard delete cascades across two databases and is an
# owner-in-the-UI action. Archive with update_company(status="archived").


@mcp.tool()
async def list_companies(status: Optional[str] = None) -> str:
    """List the companies (agent orgs) in this agent's owner's organization.

    Args:
        status: Optional filter, e.g. "active", "paused", "archived".
    """
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    return _result(await api.get("/api/v1/companies", params=params))


@mcp.tool()
async def create_company(
    name: str,
    identifier_prefix: Optional[str] = None,
    mission: Optional[str] = None,
    goals: Optional[list[str]] = None,
    description: Optional[str] = None,
    industry: Optional[str] = None,
    domain: Optional[str] = None,
) -> str:
    """Create a company (agent org). This agent's owner becomes its owner.

    After creating, build the org with create_space (its teams), create_project,
    create_membership (put agents in it with positions), and create_task.

    Args:
        name: Company name (required).
        identifier_prefix: 2-10 chars, an uppercase letter then uppercase
            letters or digits (e.g. "CIV"). Task ids become CIV-1, CIV-2, ...
            Unique across companies and fixed once created: a taken one is
            refused with the next free one ("CIV2 is free"); pass that.
        mission: One-paragraph mission statement.
        goals: List of goal strings.
        description, industry, domain: Optional profile fields.
    """
    if not name or not name.strip():
        return _result(_error("name is required"))
    if identifier_prefix is not None and not _IDENTIFIER_PREFIX_RE.match(identifier_prefix):
        return _result(_error(
            "identifier_prefix must be 2-10 chars: an uppercase letter followed by "
            f"uppercase letters or digits (e.g. 'CIV'), got {identifier_prefix!r}"
        ))
    body: dict[str, Any] = {"name": name.strip()}
    for key, val in (
        ("identifier_prefix", identifier_prefix), ("mission", mission), ("goals", goals),
        ("description", description), ("industry", industry), ("domain", domain),
    ):
        if val is not None:
            body[key] = val
    return _result(await api.post("/api/v1/companies", body))


@mcp.tool()
async def update_company(
    company_id: Optional[str] = None,
    name: Optional[str] = None,
    mission: Optional[str] = None,
    goals: Optional[list[str]] = None,
    description: Optional[str] = None,
    industry: Optional[str] = None,
    domain: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """Update a company's profile or lifecycle status. Only passed fields change.

    Status: "active" | "paused" | "archived". Archiving is the supported way
    to retire a company from an agent; there is no delete tool by design.

    Args:
        company_id: Company UUID.
        name, mission, goals, description, industry, domain: Profile fields.
        status: New lifecycle status.
    """
    err = _enum_check(status, _VALID_COMPANY_STATUSES, "status")
    if err:
        return _result(err)
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    body: dict[str, Any] = {}
    for key, val in (
        ("name", name), ("mission", mission), ("goals", goals), ("description", description),
        ("industry", industry), ("domain", domain), ("status", status),
    ):
        if val is not None:
            body[key] = val
    if not body:
        return _result(_error("nothing to update: pass at least one field"))
    return _result(await api.patch(f"/api/v1/companies/{cid}", body))


@mcp.tool()
async def create_membership(
    agent_name: str,
    company_id: Optional[str] = None,
    position: Optional[str] = None,
    title: Optional[str] = None,
    space_id: Optional[str] = None,
    reports_to: Optional[str] = None,
    access_role: Optional[str] = None,
) -> str:
    """Put an agent in a company's org chart.

    Args:
        agent_name: Canonical agent name (must already exist).
        company_id: Company UUID.
        position: Org-chart position, e.g. "ceo", "cto", "department_head",
            "team_lead", "engineer", "individual_contributor" (max 50 chars).
        title: Human title, e.g. "Head of Platform".
        space_id: Department (space) UUID the agent belongs to.
        reports_to: Manager's agent name.
        access_role: "admin" | "member" | "viewer" (access control, separate from position).
    """
    try:
        cid = _resolve_company_id(company_id)
        _validate_agent_name(agent_name)
        if space_id:
            _validate_uuid(space_id, "space_id")
        if reports_to:
            _validate_agent_name(reports_to, "reports_to")
    except ValueError as e:
        return _result(_error(str(e)))
    err = _enum_check(access_role, _VALID_ACCESS_ROLES, "access_role")
    if err:
        return _result(err)
    if position is not None and len(position) > 50:
        return _result(_error("position must be 50 characters or fewer"))
    body: dict[str, Any] = {"agent_name": agent_name}
    for key, val in (
        ("position", position), ("title", title), ("space_id", space_id),
        ("reports_to", reports_to), ("access_role", access_role),
    ):
        if val is not None:
            body[key] = val
    return _result(await api.post(f"/api/v1/companies/{cid}/memberships", body))


@mcp.tool()
async def update_membership(
    membership_id: str,
    company_id: Optional[str] = None,
    position: Optional[str] = None,
    title: Optional[str] = None,
    space_id: Optional[str] = None,
    reports_to: Optional[str] = None,
    status: Optional[str] = None,
    access_role: Optional[str] = None,
) -> str:
    """Change an org-chart entry: position, title, department, manager, status, or access role.

    Args:
        membership_id: Org-chart entry UUID (from list_memberships).
        company_id: Company UUID.
        position, title, space_id, reports_to, access_role: As in create_membership.
        status: "active" | "paused" | "removed".
    """
    try:
        cid = _resolve_company_id(company_id)
        _validate_uuid(membership_id, "membership_id")
        if space_id:
            _validate_uuid(space_id, "space_id")
        if reports_to:
            _validate_agent_name(reports_to, "reports_to")
    except ValueError as e:
        return _result(_error(str(e)))
    for err in (
        _enum_check(status, _VALID_MEMBERSHIP_STATUSES, "status"),
        _enum_check(access_role, _VALID_ACCESS_ROLES, "access_role"),
    ):
        if err:
            return _result(err)
    if position is not None and len(position) > 50:
        return _result(_error("position must be 50 characters or fewer"))
    body: dict[str, Any] = {}
    for key, val in (
        ("position", position), ("title", title), ("space_id", space_id),
        ("reports_to", reports_to), ("status", status), ("access_role", access_role),
    ):
        if val is not None:
            body[key] = val
    if not body:
        return _result(_error("nothing to update: pass at least one field"))
    return _result(await api.patch(f"/api/v1/companies/{cid}/memberships/{membership_id}", body))


@mcp.tool()
async def remove_membership(membership_id: str, company_id: Optional[str] = None) -> str:
    """Remove an agent's org-chart entry from a company.

    Removes both the org-chart row and the access membership. The agent
    itself is untouched; use update_agent(action="delete") to delete an agent.

    Args:
        membership_id: Org-chart entry UUID (from list_memberships).
        company_id: Company UUID.
    """
    try:
        cid = _resolve_company_id(company_id)
        _validate_uuid(membership_id, "membership_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.delete(f"/api/v1/companies/{cid}/memberships/{membership_id}"))


# ==============================================================================
# PHASE 5 — automation, UI authoring, and gated agent lifecycle
# ==============================================================================


@mcp.tool()
async def create_schedule(
    agent_name: str,
    schedule_type: str,
    reason: str = "scheduled",
    company_id: Optional[str] = None,
    cron_expression: Optional[str] = None,
    interval_seconds: Optional[int] = None,
    payload: Optional[dict] = None,
    enabled: bool = True,
    trigger_type: str = "message",
    agent_task_id: Optional[str] = None,
    workflow_id: Optional[str] = None,
) -> str:
    """Schedule a recurring (or one-time) trigger for an agent.

    Args:
        agent_name: Agent to trigger.
        schedule_type: cron | interval | one_time.
        reason: Short label (max 100 chars) shown in the UI.
        company_id: Optional company UUID for company-scoped schedules.
        cron_expression: Required if schedule_type is "cron".
        interval_seconds: Required if "interval"; minimum 60.
        payload: Arbitrary JSON delivered to the agent on trigger.
        enabled: Defaults to True.
        trigger_type: message | task | workflow (default message).
        agent_task_id: Required when trigger_type=task.
        workflow_id: Required when trigger_type=workflow.
    """
    if not agent_name or not agent_name.strip():
        return _result(_error("agent_name is required"))
    for err in (
        _enum_check(schedule_type, _VALID_SCHEDULE_TYPES, "schedule_type"),
        _enum_check(trigger_type, _VALID_TRIGGER_TYPES, "trigger_type"),
    ):
        if err:
            return _result(err)
    if schedule_type == "cron" and not cron_expression:
        return _result(_error("cron_expression is required for schedule_type=cron"))
    if schedule_type == "interval":
        if interval_seconds is None or int(interval_seconds) < 60:
            return _result(_error("interval_seconds is required for schedule_type=interval and must be >= 60"))
    if trigger_type == "task" and not agent_task_id:
        return _result(_error("agent_task_id is required when trigger_type=task"))
    if trigger_type == "workflow" and not workflow_id:
        return _result(_error("workflow_id is required when trigger_type=workflow"))
    cid = _scope_company_id(company_id)
    if cid:
        try:
            _validate_uuid(cid, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))
    for u_name, u_val in (("agent_task_id", agent_task_id), ("workflow_id", workflow_id)):
        if u_val:
            try:
                _validate_uuid(u_val, u_name)
            except ValueError as e:
                return _result(_error(str(e)))

    body: dict[str, Any] = {
        "agent_name": agent_name.strip(),
        "schedule_type": schedule_type,
        "reason": reason[:100] if reason else "scheduled",
        "enabled": bool(enabled),
        "trigger_type": trigger_type,
    }
    if cid:
        body["company_id"] = cid
    if cron_expression:
        body["cron_expression"] = cron_expression
    if interval_seconds is not None:
        body["interval_seconds"] = int(interval_seconds)
    if payload is not None:
        body["payload"] = payload
    if agent_task_id:
        body["agent_task_id"] = agent_task_id
    if workflow_id:
        body["workflow_id"] = workflow_id
    return _result(await api.post("/api/v1/schedules", body))


@mcp.tool()
async def list_schedules(company_id: Optional[str] = None) -> str:
    """List schedules for the current user or company."""
    params: dict[str, Any] = {}
    cid = _scope_company_id(company_id)
    if cid:
        try:
            _validate_uuid(cid, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))
        params["company_id"] = cid
    return _result(await api.get("/api/v1/schedules", params=params))


@mcp.tool()
async def create_workflow(
    name: str,
    steps: list[dict],
    description: Optional[str] = None,
    company_id: Optional[str] = None,
) -> str:
    """Create a multi-step workflow.

    Args:
        name: Workflow name.
        steps: List of step dicts. Each step has:
            - `task_ids: [uuid, ...]` (reference mode) OR
            - `tasks: [task_create_payload, ...]` (inline mode, auto-creates tasks)
            - Optional `human_gate: bool`, `timeout_minutes: int`.
        description: Optional description.
        company_id: Optional company UUID.
    """
    if not name or not name.strip():
        return _result(_error("name is required"))
    if not isinstance(steps, list) or not steps:
        return _result(_error("steps must be a non-empty list"))
    cid = _scope_company_id(company_id)
    if cid:
        try:
            _validate_uuid(cid, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))
    body: dict[str, Any] = {"name": name.strip(), "steps": steps}
    if description is not None:
        body["description"] = description
    if cid:
        body["company_id"] = cid
    return _result(await api.post("/api/v1/workflows", body))


@mcp.tool()
async def list_workflows(
    company_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List workflows."""
    limit = max(1, min(200, int(limit)))
    params: dict[str, Any] = {"limit": limit}
    cid = _scope_company_id(company_id)
    if cid:
        try:
            _validate_uuid(cid, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))
        params["company_id"] = cid
    if status:
        params["status"] = status
    return _result(await api.get("/api/v1/workflows", params=params))


@mcp.tool()
async def start_workflow(workflow_id: str) -> str:
    """Trigger a workflow's execution by moving it from `backlog` to `todo`."""
    try:
        _validate_uuid(workflow_id, "workflow_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.patch(f"/api/v1/workflows/{workflow_id}", {"status": "todo"}))


@mcp.tool()
async def register_nav_item(
    label: str,
    route: str,
    company_id: Optional[str] = None,
    icon: Optional[str] = None,
    position: Optional[int] = None,
    visibility: str = "all",
    upsert: bool = True,
) -> str:
    """Add a custom item to a company's left navigation.

    The route must exist as a Next.js page in the ai-chatbot app. This call
    only inserts the link — it does not create the page.

    Args:
        label: Nav item label (1-64 chars).
        route: In-app path, must start with "/" but not "//".
        company_id: Company UUID.
        icon: Optional lucide-react icon name (kebab-case).
        position: 0-1000; smaller numbers appear higher.
        visibility: all | admin | agent.
        upsert: If True (default), updates an existing entry with the same label.
    """
    if not label or not (1 <= len(label) <= 64):
        return _result(_error("label must be 1-64 chars"))
    if not route or not route.startswith("/") or route.startswith("//"):
        return _result(_error("route must start with '/' but not '//'"))
    err = _enum_check(visibility, _VALID_NAV_VISIBILITY, "visibility")
    if err:
        return _result(err)
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    if position is not None:
        if not (0 <= int(position) <= 1000):
            return _result(_error("position must be in [0, 1000]"))

    body: dict[str, Any] = {
        "label": label,
        "route": route,
        "visibility": visibility,
        "upsert": bool(upsert),
    }
    if icon is not None:
        body["icon"] = icon
    if position is not None:
        body["position"] = int(position)
    return _result(await api.post(f"/api/v1/companies/{cid}/nav-items", body))


@mcp.tool()
async def list_nav_items(company_id: Optional[str] = None) -> str:
    """List custom nav items registered for the company."""
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/nav-items"))


@mcp.tool()
async def create_dashboard(
    title: str,
    company_id: Optional[str] = None,
    slug: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Create a dashboard in a company."""
    if not title or not title.strip():
        return _result(_error("title is required"))
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    body: dict[str, Any] = {"title": title.strip(), "createdByAgent": _ident().name}
    if slug is not None:
        body["slug"] = slug
    if description is not None:
        body["description"] = description
    return _result(await api.post(f"/api/v1/companies/{cid}/dashboards", body))


@mcp.tool()
async def list_dashboards(company_id: Optional[str] = None) -> str:
    """List dashboards in a company."""
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/dashboards"))


@mcp.tool()
async def create_panel(
    dashboard_id: str,
    title: str,
    html_content: Optional[str] = None,
    html_content_base64: Optional[str] = None,
    size: str = "full",
    height: int = 400,
    position: int = 0,
) -> str:
    """Add a panel to a dashboard.

    Supply either `html_content` (plain HTML) or `html_content_base64` (for
    content that would otherwise be blocked by an upstream WAF).

    Args:
        dashboard_id: Dashboard UUID.
        title: Panel title.
        html_content: Raw HTML (mutually exclusive with html_content_base64).
        html_content_base64: Base64-encoded HTML.
        size: full | half | third.
        height: Panel height in pixels.
        position: Ordering position (0 = first).
    """
    if not title or not title.strip():
        return _result(_error("title is required"))
    if html_content is None and html_content_base64 is None:
        return _result(_error("either html_content or html_content_base64 must be provided"))
    err = _enum_check(size, _VALID_PANEL_SIZES, "size")
    if err:
        return _result(err)
    try:
        _validate_uuid(dashboard_id, "dashboard_id")
    except ValueError as e:
        return _result(_error(str(e)))

    body: dict[str, Any] = {
        "title": title.strip(),
        "size": size,
        "height": int(height),
        "position": int(position),
        "createdByAgent": _ident().name,
    }
    if html_content is not None:
        body["htmlContent"] = html_content
    if html_content_base64 is not None:
        body["htmlContentBase64"] = html_content_base64
    return _result(await api.post(f"/api/v1/dashboards/{dashboard_id}/panels", body))


@mcp.tool()
async def update_panel(
    dashboard_id: str,
    panel_id: str,
    title: Optional[str] = None,
    html_content: Optional[str] = None,
    html_content_base64: Optional[str] = None,
    size: Optional[str] = None,
    height: Optional[int] = None,
    position: Optional[int] = None,
) -> str:
    """Update a panel."""
    try:
        _validate_uuid(dashboard_id, "dashboard_id")
        _validate_uuid(panel_id, "panel_id")
    except ValueError as e:
        return _result(_error(str(e)))
    err = _enum_check(size, _VALID_PANEL_SIZES, "size")
    if err:
        return _result(err)
    body: dict[str, Any] = {}
    if title is not None:
        body["title"] = title
    if html_content is not None:
        body["htmlContent"] = html_content
    if html_content_base64 is not None:
        body["htmlContentBase64"] = html_content_base64
    if size is not None:
        body["size"] = size
    if height is not None:
        body["height"] = int(height)
    if position is not None:
        body["position"] = int(position)
    if not body:
        return _result(_error("no fields to update"))
    return _result(await api.patch(f"/api/v1/dashboards/{dashboard_id}/panels/{panel_id}", body))


# -- Gated: agent lifecycle (high power, opt-in) -----------------------------


@mcp.tool()
async def update_agent(
    agent_name: str,
    system: Optional[str] = None,
    skills: Optional[list[str]] = None,
    display_name: Optional[str] = None,
    description: Optional[str] = None,
    avatar_url: Optional[str] = None,
    category: Optional[str] = None,
    card_skills: Optional[list[dict]] = None,
) -> str:
    """Configure one of your owner's agents. Lead agent only: the platform
    refuses it for other agents (and this tool is shown only to the Lead).
    Pass only the fields to change.

    Instructions and dispatch skills (applied on the agent's next task, no
    restart), the same field its owner edits in the app:
      - system: the agent's instructions, who it is and how it works.
        "" clears them. Use it only after your owner approved the change.
      - skills: the dispatch skill slugs it follows (replaces the list).

    Profile and card of a connected agent:
      - display_name, description, avatar_url, category.
      - card_skills: its public skills, replacing the list. Each is
        {"id", "name", "description", "tags", "examples", "instructions",
        "pricing_model": "free" | "per_task", "price_usd"}. A paid skill
        needs the agent's payout wallet, which only its owner sets.

    Visibility, the payout wallet, tokens, connecting and deleting agents
    stay with your owner.
    """
    if not agent_name or not agent_name.strip():
        return _result(_error("agent_name is required"))
    try:
        _validate_agent_name(agent_name)
    except ValueError as e:
        return _result(_error(str(e)))

    results: dict[str, Any] = {}
    dispatch_body: dict[str, Any] = {}
    if system is not None:
        dispatch_body["system"] = system
    if skills is not None:
        dispatch_body["skills"] = skills
    if dispatch_body:
        results["instructions"] = await api.patch(
            f"/api/v1/agents/{agent_name}/system", dispatch_body
        )

    profile_body: dict[str, Any] = {}
    for key, value in (
        ("display_name", display_name),
        ("description", description),
        ("avatar_url", avatar_url),
        ("category", category),
        ("skills", card_skills),
    ):
        if value is not None:
            profile_body[key] = value
    if profile_body:
        results["profile"] = await api.put(
            f"/api/v1/factory/self-hosted/agents/{agent_name}", profile_body
        )

    if not results:
        return _result(_error(
            "nothing to update: pass system/skills, or display_name, description, "
            "avatar_url, category, card_skills"
        ))
    return _result(results)


# ==============================================================================

if __name__ == "__main__":
    # Mirror the spawn-time binding for the status-line hook (best-effort;
    # our PPID here is the `claude` process that spawned us — the same PPID
    # the status-line hook sees).
    identity.write_binding()
    mcp.run(transport="stdio")
