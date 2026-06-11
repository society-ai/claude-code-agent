"""Society AI MCP Server — gives Claude Code tools to interact with Society AI.

Tool surface (see README for full descriptions):

  Core (existing):
    get_company, list_tasks, get_task, update_task, send_inbox_item, list_inbox

  Phase 1 — task / inbox CRUD gap closures:
    create_task, review_task, reassign_task, get_my_tasks,
    respond_to_inbox, dismiss_inbox

  Phase 2 — agent network (routed through the bridge daemon via IPC):
    search_agents, delegate_task

  Phase 3 — artifacts and knowledge base:
    save_artifact, pin_artifact, list_pinned_artifacts, unpin_artifact,
    search_kb, list_kb_items

  Phase 4 — org context:
    list_company_agents, list_departments, create_department, list_memberships,
    list_spaces, create_space, get_space, list_projects, create_project, get_project

  Phase 5 — automation and UI authoring:
    create_schedule, list_schedules, create_workflow, list_workflows, start_workflow,
    register_nav_item, list_nav_items, create_dashboard, list_dashboards,
    create_panel, update_panel,
    deploy_agent, update_agent, restart_agent, delete_agent  [gated on ENABLE_AGENT_LIFECYCLE]

All tools return JSON strings. Errors (network, 4xx/5xx, validation) are
encoded as {"error": true, ...} objects so the LLM can read and recover.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

import api
import bridge_ipc
from config import (
    AGENT_NAME,
    COMPANY_ID,
    ENABLE_AGENT_LIFECYCLE,
)

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
_VALID_AGENT_LIFECYCLE_ROLES = {
    "ceo", "coo", "cto", "cfo", "cmo", "department_head",
    "team_lead", "individual_contributor",
}
_VALID_DEPLOY_MODES = {"serverless", "dedicated"}
_VALID_PLATFORMS = {"cloud_run", "gce"}
_VALID_AGENT_TYPES = {"openclaw", "zeroclaw"}
_VALID_ACCESS_ROLES = {"admin", "member", "viewer"}
_VALID_VISIBILITIES = {"private", "shared", "public"}


def _error(message: str, status: int | None = None, body: str | None = None) -> dict[str, Any]:
    return api.error(message, status=status, body=body)


def _result(data: Any) -> str:
    """Serialize a success or error dict for MCP transport."""
    return json.dumps(data, indent=2, default=str)


def _resolve_company_id(company_id: Optional[str]) -> str:
    """Use provided company_id or fall back to env default. Raises ValueError."""
    cid = (company_id or COMPANY_ID or "").strip()
    if not cid:
        raise ValueError("company_id is required (pass it or set COMPANY_ID env var)")
    if not _UUID_RE.match(cid):
        raise ValueError(f"company_id must be a valid UUID, got: {cid}")
    return cid


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

mcp = FastMCP("society-ai", instructions="Society AI tools for Claude Code")


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
    limit: int = 50,
) -> str:
    """List tasks, optionally filtered by company, status, agent, or priority.

    Works in both personal scope (no company_id) and company scope. When called
    by an agent without ``company_id`` and without an explicit ``assigned_to_agent``
    filter, the platform defaults to tasks where this agent is creator or assignee
    across every scope it touches.
    """
    cid = (company_id or COMPANY_ID or "").strip()
    if cid:
        try:
            _validate_uuid(cid, "company_id")
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

    cid = (company_id or COMPANY_ID or "").strip()
    if cid:
        try:
            _validate_uuid(cid, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))

    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "type": type,
        "from_agent": AGENT_NAME,
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
    cid = (company_id or COMPANY_ID or "").strip()
    if cid:
        try:
            _validate_uuid(cid, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))

    limit = max(1, min(200, int(limit)))
    params: dict[str, Any] = {"limit": limit}
    if cid:
        params["company_id"] = cid
    agent = to_agent if to_agent is not None else AGENT_NAME
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
async def post_chat_message(chat_id: str, text: str) -> str:
    """Post a message into one of your owner's conversations as yourself —
    you appear as a participant in that chat (e.g. the Scribe digest into a
    finished work session's conversation). Keep it short and useful; the
    owner reads it inline in the conversation. You can only post into chats
    belonging to your own owner.

    Args:
        chat_id: The conversation's UUID (e.g. from a session_ended wake).
        text: The message to post (plain text, a few sentences).
    """
    try:
        _validate_uuid(chat_id, "chat_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if not text or not text.strip():
        return _result(_error("text is required"))
    return _result(
        await api.post(f"/api/v1/chats/{chat_id}/messages", {"text": text.strip()})
    )


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
    ``company_id`` is passed nor ``COMPANY_ID`` env var is set, the task is
    created as a personal task owned by the authenticated user.

    Note: when an agent creates a task, ``assigned_to_agent`` is required —
    typically the agent assigns to itself. Without an assignee the task lands
    in 'todo' but never dispatches.

    Args:
        title: Short task title (required).
        company_id: Optional company UUID. Falls back to COMPANY_ID env var.
                    If neither is set, creates a personal task.
        description: Longer free-form description.
        assigned_to_agent: Agent name to assign the task to.
        priority: low, medium, high, critical. Defaults to medium.
        space_id: Optional space (department) UUID — company tasks only.
        project_id: Optional project UUID — company tasks only.
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
    cid = (company_id or COMPANY_ID or "").strip()
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
        "created_by_agent": AGENT_NAME,
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
    body: dict[str, Any] = {"decision": decision, "reviewed_by_agent": AGENT_NAME}
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
        "assigned_by_agent": AGENT_NAME,
    }
    if reason is not None:
        body["reason"] = reason
    return _result(await api.post(f"/api/v1/agent-tasks/{task_id}/reassign", body))


@mcp.tool()
async def get_my_tasks(
    company_id: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List tasks assigned to this agent (`AGENT_NAME`). Top-level route — works
    in both personal and company scopes.

    Args:
        company_id: Optional company UUID to restrict to one company.
        status: Filter by task status.
        limit: Max results (1-200, default 50).
    """
    err = _enum_check(status, _VALID_TASK_STATUSES, "status")
    if err:
        return _result(err)
    limit = max(1, min(200, int(limit)))
    params: dict[str, Any] = {
        "assigned_to_agent": AGENT_NAME,
        "limit": limit,
    }
    if status:
        params["status"] = status
    if company_id:
        try:
            _validate_uuid(company_id, "company_id")
        except ValueError as e:
            return _result(_error(str(e)))
        params["company_id"] = company_id
    return _result(await api.get("/api/v1/agent-tasks", params=params))


@mcp.tool()
async def respond_to_inbox(
    item_id: str,
    response: str,
    response_data: Optional[dict] = None,
) -> str:
    """Respond to an inbox item — e.g. approve an approval-required request.

    Args:
        item_id: Inbox item UUID.
        response: The textual response.
        response_data: Optional structured payload (kept arbitrary).
    """
    try:
        _validate_uuid(item_id, "item_id")
    except ValueError as e:
        return _result(_error(str(e)))
    if not response or not response.strip():
        return _result(_error("response is required"))
    body: dict[str, Any] = {
        "response": response,
        "responded_by_agent": AGENT_NAME,
    }
    if response_data is not None:
        body["response_data"] = response_data
    return _result(await api.post(f"/api/v1/inbox/{item_id}/respond", body))


@mcp.tool()
async def dismiss_inbox(item_id: str) -> str:
    """Dismiss an inbox item (mark as handled without sending a response)."""
    try:
        _validate_uuid(item_id, "item_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.post(f"/api/v1/inbox/{item_id}/dismiss", {
        "dismissed_by_agent": AGENT_NAME,
    }))


# ==============================================================================
# PHASE 2 — agent network (delegation via bridge IPC)
# ==============================================================================


async def _ipc_call(method: str, params: dict, timeout: float = 60.0) -> Any:
    """Wrap bridge_ipc.call to convert IPCClientError into our error dict shape."""
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
async def delegate_task(
    agent_name: str,
    message: str,
    skill_id: Optional[str] = None,
    session_id: Optional[str] = None,
    timeout: int = 120,
) -> str:
    """Delegate a task to another Society AI agent and wait for the result.

    Requires the bridge daemon to be running. Uses the existing WebSocket
    connection to send `tasks/sendSubscribe` and correlates the asynchronous
    `delegation.result` notification back to this caller. Billing (if any)
    is on the agent owning this `SOCIETY_AI_AUTH_TOKEN`.

    Args:
        agent_name: Target agent name (e.g. "research-bot").
        message: The task / question to delegate.
        skill_id: Optional specific skill UUID to invoke.
        session_id: Optional session UUID for multi-turn conversations.
        timeout: How long to wait for the delegation result, in seconds.
    """
    if not agent_name or not agent_name.strip():
        return _result(_error("agent_name is required"))
    if not message or not message.strip():
        return _result(_error("message is required"))
    if skill_id:
        try:
            _validate_uuid(skill_id, "skill_id")
        except ValueError as e:
            return _result(_error(str(e)))
    if session_id:
        try:
            _validate_uuid(session_id, "session_id")
        except ValueError as e:
            return _result(_error(str(e)))
    timeout = max(5, min(600, int(timeout)))

    params: dict[str, Any] = {
        "agent_name": agent_name.strip(),
        "message": message,
        "timeout": timeout,
    }
    if skill_id:
        params["skill_id"] = skill_id
    if session_id:
        params["session_id"] = session_id

    # Allow a small buffer over the requested timeout for IPC overhead.
    data = await _ipc_call("delegate_task", params, timeout=timeout + 10)
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

    Authenticates with the agent's normal sai_ token via agent_router's
    `/api/v1/artifacts` route — no service key required. The route
    internally proxies to ai-chatbot's service-auth ingest endpoint
    while the service secret stays in the backend.

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
        if not company_id and not COMPANY_ID:
            return _result(_error("cache_key requires company_id (also reads COMPANY_ID env)"))
        if not (1 <= len(cache_key) <= 255):
            return _result(_error("cache_key must be 1-255 chars"))
    for u_name, u_val in (
        ("company_id", company_id),
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
        with open(file_path, "rb") as f:
            content = f.read()
    except OSError as e:
        return _result(_error(f"Could not read {file_path}: {e}"))

    body: dict[str, Any] = {
        "name": name.strip(),
        "mime_type": mime_type.strip(),
        "bytes_base64": base64.b64encode(content).decode("ascii"),
        "source_type": "agent",
        "source_agent": AGENT_NAME,
    }
    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    cid = (company_id or COMPANY_ID or "").strip()
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

    return _result(await api.post("/api/v1/artifacts", body))


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
        "pinned_by_agent": AGENT_NAME,
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
async def search_kb(
    query: str,
    org_id: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    top_k: int = 5,
) -> str:
    """Semantic search across the Knowledge Base.

    Args:
        query: Natural-language query.
        org_id: Org UUID (required by the upstream API). If omitted, this
            tool tries to derive it from COMPANY_ID — but you should pass it
            explicitly when in doubt.
        space_id, project_id: Optional scope.
        top_k: Number of chunks to return (1-20).
    """
    if not query or not query.strip():
        return _result(_error("query is required"))
    resolved_org = (org_id or COMPANY_ID or "").strip()
    if not resolved_org:
        return _result(_error("org_id is required (or set COMPANY_ID env var)"))
    top_k = max(1, min(20, int(top_k)))
    body: dict[str, Any] = {"query": query.strip(), "org_id": resolved_org, "top_k": top_k}
    if space_id:
        try:
            _validate_uuid(space_id, "space_id")
        except ValueError as e:
            return _result(_error(str(e)))
        body["space_id"] = space_id
    if project_id:
        try:
            _validate_uuid(project_id, "project_id")
        except ValueError as e:
            return _result(_error(str(e)))
        body["project_id"] = project_id
    body["agent_id"] = AGENT_NAME
    return _result(await api.post("/api/v1/kb/retrieve", body))


@mcp.tool()
async def list_kb_items(
    org_id: Optional[str] = None,
    space_id: Optional[str] = None,
    project_id: Optional[str] = None,
    chat_id: Optional[str] = None,
) -> str:
    """List Knowledge Base documents within a scope."""
    resolved_org = (org_id or COMPANY_ID or "").strip()
    if not resolved_org:
        return _result(_error("org_id is required (or set COMPANY_ID env var)"))
    params: dict[str, Any] = {"org_id": resolved_org}
    for u_name, u_val in (("space_id", space_id), ("project_id", project_id), ("chat_id", chat_id)):
        if u_val:
            try:
                _validate_uuid(u_val, u_name)
            except ValueError as e:
                return _result(_error(str(e)))
            params[u_name] = u_val
    return _result(await api.get("/api/v1/kb/list-documents", params=params))


# ==============================================================================
# PHASE 4 — org context (spaces, projects, departments, memberships, agents)
# ==============================================================================


@mcp.tool()
async def list_company_agents(company_id: Optional[str] = None) -> str:
    """List all deployed agents in a company with status and model info."""
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/agents"))


@mcp.tool()
async def list_departments(company_id: Optional[str] = None) -> str:
    """List a company's departments (a department is a space with org-chart metadata)."""
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/spaces"))


@mcp.tool()
async def create_department(
    name: str,
    company_id: Optional[str] = None,
    description: Optional[str] = None,
    dept_function: Optional[str] = None,
    lead_agent_id: Optional[str] = None,
) -> str:
    """Create a department (space with department metadata) in a company.

    Args:
        name: Department name.
        company_id: Company UUID.
        description: Optional description.
        dept_function: Optional dept function tag (e.g. "engineering").
        lead_agent_id: Optional lead agent UUID.
    """
    if not name or not name.strip():
        return _result(_error("name is required"))
    try:
        cid = _resolve_company_id(company_id)
        if lead_agent_id:
            _validate_uuid(lead_agent_id, "lead_agent_id")
    except ValueError as e:
        return _result(_error(str(e)))
    body: dict[str, Any] = {"name": name.strip()}
    if description is not None:
        body["description"] = description
    if dept_function is not None:
        body["dept_function"] = dept_function
    if lead_agent_id is not None:
        body["lead_agent_id"] = lead_agent_id
    return _result(await api.post(f"/api/v1/companies/{cid}/spaces", body))


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
    """List spaces in a company (companies model departments as spaces)."""
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/spaces"))


@mcp.tool()
async def create_space(
    name: str,
    company_id: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Create a space in a company."""
    if not name or not name.strip():
        return _result(_error("name is required"))
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    body: dict[str, Any] = {"name": name.strip()}
    if description is not None:
        body["description"] = description
    return _result(await api.post(f"/api/v1/companies/{cid}/spaces", body))


@mcp.tool()
async def get_space(space_id: str, company_id: Optional[str] = None) -> str:
    """Fetch space details."""
    try:
        cid = _resolve_company_id(company_id)
        _validate_uuid(space_id, "space_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/spaces/{space_id}"))


@mcp.tool()
async def list_projects(company_id: Optional[str] = None) -> str:
    """List projects in a company."""
    try:
        cid = _resolve_company_id(company_id)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/projects"))


@mcp.tool()
async def create_project(
    name: str,
    company_id: Optional[str] = None,
    space_id: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Create a project in a company."""
    if not name or not name.strip():
        return _result(_error("name is required"))
    try:
        cid = _resolve_company_id(company_id)
        if space_id:
            _validate_uuid(space_id, "space_id")
    except ValueError as e:
        return _result(_error(str(e)))
    body: dict[str, Any] = {"name": name.strip()}
    if description is not None:
        body["description"] = description
    if space_id:
        body["space_id"] = space_id
    return _result(await api.post(f"/api/v1/companies/{cid}/projects", body))


@mcp.tool()
async def get_project(project_id: str, company_id: Optional[str] = None) -> str:
    """Fetch project details."""
    try:
        cid = _resolve_company_id(company_id)
        _validate_uuid(project_id, "project_id")
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.get(f"/api/v1/companies/{cid}/projects/{project_id}"))


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
    cid = (company_id or COMPANY_ID or "").strip()
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
    cid = (company_id or COMPANY_ID or "").strip()
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
    cid = (company_id or COMPANY_ID or "").strip()
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
    cid = (company_id or COMPANY_ID or "").strip()
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
    body: dict[str, Any] = {"title": title.strip(), "createdByAgent": AGENT_NAME}
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
        "createdByAgent": AGENT_NAME,
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


def _gate_or_error() -> Optional[str]:
    """Return a serialized error if the lifecycle gate is closed; else None."""
    if not ENABLE_AGENT_LIFECYCLE:
        return _result(_error(
            "Agent lifecycle tools (deploy/update/restart/delete) are disabled by default. "
            "Set ENABLE_AGENT_LIFECYCLE=true in the bridge environment if you really want "
            "Claude Code to be able to spawn/destroy real agents."
        ))
    return None


@mcp.tool()
async def deploy_agent(
    role_position: str,
    company_id: Optional[str] = None,
    display_name: Optional[str] = None,
    persona: Optional[str] = None,
    role_summary: Optional[str] = None,
    role_md: Optional[str] = None,
    display_description: Optional[str] = None,
    title: Optional[str] = None,
    model: Optional[str] = None,
    reports_to: Optional[str] = None,
    space_id: Optional[str] = None,
    access_role: str = "member",
    skill_packs: Optional[list[str]] = None,
    env_secrets: Optional[dict[str, str]] = None,
    api_keys: Optional[dict[str, str]] = None,
    visibility: str = "private",
    agent_type: str = "zeroclaw",
    platform: str = "cloud_run",
    deployment_mode: str = "serverless",
) -> str:
    """[GATED] Deploy a new agent into a company.

    Requires ENABLE_AGENT_LIFECYCLE=true on the bridge process. This is a
    high-power, real-money operation — Claude can spawn real cloud agents.
    """
    gate = _gate_or_error()
    if gate:
        return gate
    if not role_position or not role_position.strip():
        return _result(_error("role_position is required (e.g. 'ceo', 'individual_contributor')"))
    for err in (
        _enum_check(role_position, _VALID_AGENT_LIFECYCLE_ROLES, "role_position"),
        _enum_check(access_role, _VALID_ACCESS_ROLES, "access_role"),
        _enum_check(visibility, _VALID_VISIBILITIES, "visibility"),
        _enum_check(agent_type, _VALID_AGENT_TYPES, "agent_type"),
        _enum_check(platform, _VALID_PLATFORMS, "platform"),
        _enum_check(deployment_mode, _VALID_DEPLOY_MODES, "deployment_mode"),
    ):
        if err:
            return _result(err)
    try:
        cid = _resolve_company_id(company_id)
        if space_id:
            _validate_uuid(space_id, "space_id")
    except ValueError as e:
        return _result(_error(str(e)))

    org_chart: dict[str, Any] = {
        "position": role_position,
        "access_role": access_role,
    }
    if title is not None:
        org_chart["title"] = title
    if space_id is not None:
        org_chart["space_id"] = space_id
    if reports_to is not None:
        org_chart["reports_to"] = reports_to

    body: dict[str, Any] = {
        "agent_type": agent_type,
        "platform": platform,
        "deployment_mode": deployment_mode,
        "org_chart": org_chart,
        "visibility": visibility,
    }
    for k, v in (
        ("display_name", display_name),
        ("persona", persona),
        ("role_summary", role_summary),
        ("role_md", role_md),
        ("display_description", display_description),
        ("model", model),
    ):
        if v is not None:
            body[k] = v
    if skill_packs is not None:
        body["skill_packs"] = list(skill_packs)
    if env_secrets is not None:
        body["env_secrets"] = dict(env_secrets)
    if api_keys is not None:
        body["api_keys"] = dict(api_keys)
    return _result(await api.post(f"/api/v1/companies/{cid}/agents", body))


@mcp.tool()
async def update_agent(
    agent_name: str,
    company_id: Optional[str] = None,
    display_name: Optional[str] = None,
    display_description: Optional[str] = None,
    persona: Optional[str] = None,
    role_summary: Optional[str] = None,
    role_md: Optional[str] = None,
    model: Optional[str] = None,
    visibility: Optional[str] = None,
) -> str:
    """[GATED] Update a deployed agent's persona, model, or visibility."""
    gate = _gate_or_error()
    if gate:
        return gate
    if not agent_name or not agent_name.strip():
        return _result(_error("agent_name is required"))
    err = _enum_check(visibility, _VALID_VISIBILITIES, "visibility")
    if err:
        return _result(err)
    try:
        cid = _resolve_company_id(company_id)
        _validate_agent_name(agent_name)
    except ValueError as e:
        return _result(_error(str(e)))
    body: dict[str, Any] = {}
    for k, v in (
        ("display_name", display_name),
        ("display_description", display_description),
        ("persona", persona),
        ("role_summary", role_summary),
        ("role_md", role_md),
        ("model", model),
        ("visibility", visibility),
    ):
        if v is not None:
            body[k] = v
    if not body:
        return _result(_error("no fields to update"))
    return _result(await api.patch(f"/api/v1/companies/{cid}/agents/{agent_name}", body))


@mcp.tool()
async def restart_agent(agent_name: str, company_id: Optional[str] = None) -> str:
    """[GATED] Force a new container revision for a deployed agent."""
    gate = _gate_or_error()
    if gate:
        return gate
    if not agent_name or not agent_name.strip():
        return _result(_error("agent_name is required"))
    try:
        cid = _resolve_company_id(company_id)
        _validate_agent_name(agent_name)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.post(f"/api/v1/companies/{cid}/agents/{agent_name}/restart", {}))


@mcp.tool()
async def delete_agent(agent_name: str, company_id: Optional[str] = None) -> str:
    """[GATED] Permanently delete a deployed agent. Irreversible."""
    gate = _gate_or_error()
    if gate:
        return gate
    if not agent_name or not agent_name.strip():
        return _result(_error("agent_name is required"))
    try:
        cid = _resolve_company_id(company_id)
        _validate_agent_name(agent_name)
    except ValueError as e:
        return _result(_error(str(e)))
    return _result(await api.delete(f"/api/v1/companies/{cid}/agents/{agent_name}"))


# ==============================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
