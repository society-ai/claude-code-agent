"""Contact permissions: who sent a dispatch, and what this agent may do for them.

Every agent has contacts (other agents, other people). The owner gives each
contact a permission level on the platform; the router stamps the sender and
that level into every dispatch it sends us (`metadata.frame.from`). This
module turns those dispatch facts into a decision the session manager can
enforce on the machine:

  owner  — the agent's owner, typing in the Society AI app as themselves, or
           automation the owner set up. Runs exactly as before.
  chat   — reply from what the agent already knows. No tools at all.
  read   — chat, plus read and search the agent's work folders.
  act    — every tool, like the owner's own sessions.

Trust model. The bridge cannot authenticate senders itself: it only ever talks
to the router over its own authenticated WebSocket. The router authenticates
the sender and writes `frame.from`; the bridge additionally requires the
dispatch's `user_id` (pinned by the router from authentication) to match the
owner id it learned at login before treating anything as the owner. Anything
it cannot classify is a contact at `chat`.

The level is an upper limit, not an instruction. The agent is told who the sender
is and what it may do, and decides within that; the session's hard limits
make sure a request that talks the agent into more still cannot get it.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from typing import Optional

PERMISSION_LEVELS = ("chat", "read", "act")
DEFAULT_PERMISSION = "chat"

# Standing blocks that describe the OWNER's private context: the agent's open
# assignments and inbox ("activity") and the company/space/project the work
# belongs to ("scope"). A contact's request must never see them.
PRIVATE_BLOCK_KINDS = frozenset({"activity", "scope"})

# What the agent is told about the hard limit actually applied on this
# machine (the machine limit can make it lower than the platform's level).
LIMIT_TEXT = {
    "chat": (
        "In this conversation you cannot run commands, change files or use "
        "Society AI tools, and you cannot see your owner's files. You can only "
        "open files this sender attached, listed under [Attachments]. Answer "
        "from what you already know."
    ),
    "read": (
        "In this conversation you can read and search files in your work "
        "folders, but you cannot run commands, change files or use Society AI "
        "tools."
    ),
    "act": (
        "In this conversation you have all your tools, as you do for your "
        "owner."
    ),
}


def clamp(level: Optional[str], limit: Optional[str]) -> str:
    """The lower of `level` and the machine `limit`. Unknown or missing
    values fall back to chat (level) and act (limit: no extra cap)."""
    lvl = level if level in PERMISSION_LEVELS else DEFAULT_PERMISSION
    cap = limit if limit in PERMISSION_LEVELS else "act"
    return PERMISSION_LEVELS[min(PERMISSION_LEVELS.index(lvl), PERMISSION_LEVELS.index(cap))]


@dataclass(frozen=True)
class Sender:
    is_owner: bool
    # Contacts only. `key` namespaces this sender's sessions so a contact can
    # never land in the owner's (or another contact's) session.
    key: str = ""
    label: str = ""
    permission: str = DEFAULT_PERMISSION
    # Names the sidebar group of this sender's conversations,
    # "Society-AI-<group>".
    group: str = ""


OWNER = Sender(is_owner=True)


def _safe_key_part(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]", "-", value)[:80] or "unknown"


def classify_sender(frame: Optional[dict], metadata: dict, owner_id: Optional[str]) -> Sender:
    """Decide whether a dispatch comes from the owner or a contact.

    Owner requires BOTH: the router says so (`frame.from.kind == "owner"`) and
    the dispatch's `user_id` equals the owner id from our login. A label
    alone is never enough, and a matching user id alone is not either: the
    owner's own agents and supervisor carry the owner's user id but are
    contacts with their own permission level.
    """
    user_id = str(metadata.get("user_id") or "").strip()
    owner_match = bool(owner_id) and user_id == owner_id
    frm = frame.get("from") if isinstance(frame, dict) else None
    if not isinstance(frm, dict):
        frm = {}
    kind = str(frm.get("kind") or "")

    if kind == "owner" and owner_match:
        return OWNER

    name = str(frm.get("name") or "").strip()
    if kind == "agent" and name:
        key = f"agent:{_safe_key_part(name)}"
        owner_name = str(frm.get("owner_name") or "").strip()
        label = f"{name} ({owner_name})" if owner_name else name
        group = name[:1].upper() + name[1:] if name.islower() else name
    else:
        uid = str(frm.get("id") or user_id or "").strip()
        key = f"user:{_safe_key_part(uid)}"
        label = name or (f"user {uid[:8]}" if uid else "an unknown sender")
        group = name or (f"user-{uid[:8]}" if uid else "Unknown")
    permission = frm.get("permission")
    if permission not in PERMISSION_LEVELS:
        permission = DEFAULT_PERMISSION
    return Sender(is_owner=False, key=key, label=label, permission=permission, group=group)


def contact_work_item_key(sender: Sender, frame: Optional[dict], metadata: dict,
                          params: dict, task_id: str) -> str:
    """Session key for a contact's dispatch. Always prefixed with the sender,
    so whatever conversation id the sender supplies, it can only ever resume
    ITS OWN conversation, never the owner's or another contact's. Task work
    keys by the task; chat by the router's conversation id, then the
    sender's session id, so a conversation keeps its history."""
    agent_task_id = metadata.get("agent_task_id")
    if agent_task_id:
        return f"contact:{sender.key}:task:{agent_task_id}"
    conversation = (
        (frame or {}).get("conversation_id")
        or params.get("sessionId")
        or metadata.get("chat_id")
        or task_id
    )
    return f"contact:{sender.key}:{_safe_key_part(str(conversation))}"


def strip_private_blocks(metadata: dict) -> dict:
    """A copy of `metadata` without the owner-private standing blocks."""
    blocks = metadata.get("blocks")
    if not isinstance(blocks, list):
        return metadata
    kept = [b for b in blocks if not (isinstance(b, dict) and b.get("kind") in PRIVATE_BLOCK_KINDS)]
    return {**metadata, "blocks": kept}


def limit_line(sender: Sender) -> str:
    return f"[Your limits for this request from {sender.label}]\n{LIMIT_TEXT[sender.permission]}"


def owner_id_from_jwt(jwt: Optional[str]) -> Optional[str]:
    """The owner's user id: the `sub` claim of the WS JWT the router issued us
    for our own API key. Read without verifying the signature, which is fine
    here: the token came straight from the router over TLS, in exchange for
    our own key, and is never accepted from anyone else."""
    if not jwt or jwt.count(".") != 2:
        return None
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        sub = json.loads(base64.urlsafe_b64decode(payload)).get("sub")
        return str(sub) if sub else None
    except (ValueError, json.JSONDecodeError):
        return None
