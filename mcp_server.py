"""Society AI MCP Server — gives Claude Code tools to interact with Society AI.

Tools: get_company, list_tasks, get_task, update_task, send_inbox_item, list_inbox.
All calls go to the Society AI API with Bearer token auth.
"""

import json
import re
from typing import Optional

import httpx
from mcp.server.fastmcp import FastMCP

from config import AGENT_ROUTER_API_URL, API_HEADERS, AGENT_NAME, COMPANY_ID

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

mcp = FastMCP("society-ai", instructions="Society AI tools for Claude Code")

# Shared HTTP client
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=30)
    return _client


async def _api_get(path: str, params: dict | None = None) -> dict | list:
    """GET request to Society AI API."""
    url = f"{AGENT_ROUTER_API_URL}{path}"
    resp = await _get_client().get(url, headers=API_HEADERS, params=params)
    resp.raise_for_status()
    return resp.json()


async def _api_patch(path: str, body: dict) -> dict:
    """PATCH request to Society AI API."""
    url = f"{AGENT_ROUTER_API_URL}{path}"
    resp = await _get_client().patch(url, headers=API_HEADERS, json=body)
    resp.raise_for_status()
    return resp.json()


async def _api_post(path: str, body: dict) -> dict:
    """POST request to Society AI API."""
    url = f"{AGENT_ROUTER_API_URL}{path}"
    resp = await _get_client().post(url, headers=API_HEADERS, json=body)
    resp.raise_for_status()
    return resp.json()


def _resolve_company_id(company_id: Optional[str]) -> str:
    """Use provided company_id or fall back to env default."""
    cid = company_id or COMPANY_ID
    if not cid:
        raise ValueError("company_id is required (pass it or set COMPANY_ID env var)")
    if not _UUID_RE.match(cid):
        raise ValueError(f"company_id must be a valid UUID, got: {cid}")
    return cid


def _validate_uuid(value: str, name: str) -> str:
    """Validate that a string is a valid UUID."""
    if not _UUID_RE.match(value):
        raise ValueError(f"{name} must be a valid UUID, got: {value}")
    return value


# -- Tools -------------------------------------------------------------------


@mcp.tool()
async def get_company(company_id: Optional[str] = None) -> str:
    """Get company context — name, mission, goals, industry, status.

    Args:
        company_id: Company UUID. Falls back to COMPANY_ID env var if not provided.
    """
    cid = _resolve_company_id(company_id)
    data = await _api_get(f"/api/v1/companies/{cid}")
    return json.dumps(data, indent=2)


@mcp.tool()
async def list_tasks(
    company_id: Optional[str] = None,
    status: Optional[str] = None,
    assigned_to_agent: Optional[str] = None,
    priority: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List tasks in the company, optionally filtered by status, agent, or priority.

    Args:
        company_id: Company UUID. Falls back to COMPANY_ID env var.
        status: Filter by status (backlog, todo, in_progress, in_review, done, blocked, cancelled, failed).
        assigned_to_agent: Filter by assigned agent name.
        priority: Filter by priority (low, medium, high, critical).
        limit: Max results (1-200, default 50).
    """
    cid = _resolve_company_id(company_id)
    params = {"limit": limit}
    if status:
        params["status"] = status
    if assigned_to_agent:
        params["assigned_to_agent"] = assigned_to_agent
    if priority:
        params["priority"] = priority
    data = await _api_get(f"/api/v1/companies/{cid}/tasks", params=params)
    return json.dumps(data, indent=2)


@mcp.tool()
async def get_task(task_id: str, company_id: Optional[str] = None) -> str:
    """Get full task details — description, acceptance criteria, status, result.

    Args:
        task_id: Task UUID.
        company_id: Company UUID. Falls back to COMPANY_ID env var.
    """
    cid = _resolve_company_id(company_id)
    _validate_uuid(task_id, "task_id")
    data = await _api_get(f"/api/v1/companies/{cid}/tasks/{task_id}")
    return json.dumps(data, indent=2)


@mcp.tool()
async def update_task(
    task_id: str,
    company_id: Optional[str] = None,
    status: Optional[str] = None,
    result: Optional[str] = None,
    blocked_reason: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
) -> str:
    """Update a task's status, result, or other fields. Only provided fields are updated.

    Args:
        task_id: Task UUID.
        company_id: Company UUID. Falls back to COMPANY_ID env var.
        status: New status (todo, in_progress, in_review, done, blocked, cancelled, failed).
        result: Summary of work done (set when completing a task).
        blocked_reason: Why the task is blocked (set when status=blocked).
        title: New title.
        description: New description.
    """
    cid = _resolve_company_id(company_id)
    _validate_uuid(task_id, "task_id")
    body = {}
    if status is not None:
        body["status"] = status
    if result is not None:
        body["result"] = result
    if blocked_reason is not None:
        body["blocked_reason"] = blocked_reason
    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    if not body:
        return json.dumps({"error": "No fields to update"})
    data = await _api_patch(f"/api/v1/companies/{cid}/tasks/{task_id}", body)
    return json.dumps(data, indent=2)


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
    """Send an inbox item — status update, approval request, input request, or alert.

    Args:
        title: Short title for the inbox item.
        body: Detailed message body.
        type: Item type — status-update, approval-required, review-required, input-required, alert.
        company_id: Company UUID. Falls back to COMPANY_ID env var.
        to_agent: Recipient agent name (optional).
        to_user_id: Recipient user UUID (optional).
        agent_task_id: Related task UUID (optional).
        priority: low, normal, high, urgent (default: normal).
    """
    cid = company_id or COMPANY_ID
    payload: dict = {
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
    data = await _api_post("/api/v1/inbox", payload)
    return json.dumps(data, indent=2)


@mcp.tool()
async def list_inbox(
    company_id: Optional[str] = None,
    to_agent: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    limit: int = 50,
) -> str:
    """List inbox items, optionally filtered. Defaults to items addressed to this agent.

    Args:
        company_id: Filter by company UUID.
        to_agent: Filter by recipient agent name. Defaults to this agent's name.
        status: Filter by status (pending, responded, dismissed).
        type: Filter by type (status-update, approval-required, review-required, input-required, alert).
        limit: Max results (1-200, default 50).
    """
    params: dict = {"limit": limit}
    cid = company_id or COMPANY_ID
    if cid:
        params["company_id"] = cid
    # Default to showing items for this agent
    agent = to_agent if to_agent is not None else AGENT_NAME
    if agent:
        params["to_agent"] = agent
    if status:
        params["status"] = status
    if type:
        params["type"] = type
    data = await _api_get("/api/v1/inbox", params=params)
    return json.dumps(data, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
