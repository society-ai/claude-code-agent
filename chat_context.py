"""The context the System of Record needs on the bridge's calls to the router:
the app Chat a piece of agent work happens in, and which Society AI tool
made the call.

Every SoR event names the Chat it was made in. The router learns it from the
`X-Society-Chat` header on HTTP calls and from `chat_id` on the hub's
`agent.send_task`; it forwards the id to the app, which keeps it only when
that Chat belongs to the agent's owner. A wrong or foreign id is dropped
silently, so a best-effort value is fine, and nothing is sent when unknown.

Where the id comes from:
  - the bridge records it per session (the owner's chat message that
    dispatched the work, else the chat the session's recording projects
    into) and answers `session_chat` over its IPC socket;
  - a session's MCP server asks with its SOCIETY_AI_SESSION_KEY;
  - a one-shot spawn gets it directly as SOCIETY_AI_CHAT_ID.

Calls the society-ai MCP server makes for a tool also send `X-Society-Tool`
with the MCP tool name, so the router records them as that tool's calls
(on the hub, `tool` on `agent.send_task`).
The bridge's own housekeeping (recording uploads, delivery ticks) never
does. Claude Code's local tools (Bash, Read, ...) never reach the router.
"""

from __future__ import annotations

import contextvars
import os
import re
import time

import bridge_ipc

CHAT_HEADER = "X-Society-Chat"
TOOL_HEADER = "X-Society-Tool"

# The MCP tool being run, set around each call by the MCP server. Tasks a
# tool starts inherit it.
current_tool: contextvars.ContextVar[str] = contextvars.ContextVar("society_tool", default="")
_TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

# A session's chat can be learned mid-session (its first recording links
# it), so lookups are cached briefly rather than for the process lifetime.
_CACHE_TTL_S = 10.0
_cache: dict[str, tuple[float, str]] = {}


def valid_chat_id(value: object) -> str:
    """The id if it is a UUID, else "" (never put anything else in a header)."""
    if isinstance(value, str) and _UUID_RE.match(value.strip()):
        return value.strip().lower()
    return ""


def chat_headers(chat_id: object) -> dict[str, str]:
    chat = valid_chat_id(chat_id)
    return {CHAT_HEADER: chat} if chat else {}


def valid_tool_name(value: object) -> str:
    return value if isinstance(value, str) and _TOOL_RE.match(value) else ""


def tool_headers() -> dict[str, str]:
    tool = valid_tool_name(current_tool.get())
    return {TOOL_HEADER: tool} if tool else {}


async def request_headers() -> dict[str, str]:
    """Everything the MCP server adds to a call to the router."""
    return {**chat_headers(await current_chat_id()), **tool_headers()}


async def current_chat_id() -> str:
    """The Chat this process's session works in, or "" when there is none
    (the owner's own desktop sessions, automation, no bridge)."""
    direct = valid_chat_id(os.environ.get("SOCIETY_AI_CHAT_ID", ""))
    if direct:
        return direct
    key = os.environ.get("SOCIETY_AI_SESSION_KEY", "").strip()
    if not key:
        return ""
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_TTL_S:
        return hit[1]
    try:
        data = await bridge_ipc.call("session_chat", {"session_key": key}, timeout=2.0)
        chat = valid_chat_id((data or {}).get("chat_id")) if isinstance(data, dict) else ""
    except Exception:
        chat = ""
    _cache[key] = (now, chat)
    return chat
