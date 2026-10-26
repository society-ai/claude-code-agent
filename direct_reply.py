"""Replying from Society AI into a session the user started in Claude Code
(Society AI design: docs/design/session-trackers.md D28, D29, §9).

Such a session has no Society AI channel, and a second `claude --resume`
process would fork its history, so a message is carried by the relay: a
throwaway `claude -p` run with Haiku whose only tools are Claude Code's own
ListAgents and SendMessage. It finds the session by its exact name, sends the
text unchanged and exits. The message arrives in the session as a turn of its
own ("Message from @Society AI"); the bridge confirms it in the transcript by
its message id and reads the reply that follows.

- Running session: relay into it, in the session's own permission mode, so
  Claude Code does not hold the message for the user to accept.
- Closed session: `claude -p --resume <id>` runs one turn in it under the same
  id and prints the reply. Not `--bg --resume`: a session that was ever in the
  background keeps its saved options there, and any flag then starts a copy
  under a new id (seen 2026-10-27), which is the fork this must never make.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import time
from dataclasses import dataclass
from typing import Optional

import session_tracker as st

logger = logging.getLogger("direct_reply")

# The relay's session name; Claude Code shows it as "Message from @Society-AI".
RELAY_NAME = "Society AI"
RELAY_TIMEOUT_S = 90.0
DELIVERY_WAIT_S = 30.0

# Transcript permission modes -> `claude --permission-mode` choices.
_MODES = {
    "default": "manual", "manual": "manual", "acceptEdits": "acceptEdits", "auto": "auto",
    "bypassPermissions": "bypassPermissions", "dontAsk": "dontAsk", "plan": "plan",
}

RELAY_PROMPT = """You deliver one message to one local Claude session and do nothing else.
1. Call ListAgents.
2. Find the peer sessions whose name is exactly: {name}
3. If exactly one matches, call SendMessage once with `to` set to that session's reference (the code in square brackets after its name) and `message` set to the text between <message> and </message>, unchanged.
4. Reply with one line: SENT <msg_id>, or NOT_FOUND, or AMBIGUOUS, or ERROR <text>.
<message>{text}</message>"""


@dataclass
class DirectSession:
    session_id: str
    cwd: str
    chat_id: str


class DirectReplyError(Exception):
    """A reply could not be delivered; the message says what the person can do."""


def direct_session_for_chat(chat_id: str) -> Optional[DirectSession]:
    """The session started in Claude Code whose chat page is `chat_id`, from
    the Tracker runner's state (written by whichever bridge tracks here)."""
    if not chat_id:
        return None
    try:
        data = json.loads((st.BASE_DIR / "state.json").read_text())
    except (OSError, ValueError):
        return None
    for sid, s in (data.get("sessions") or {}).items():
        if s.get("chat_id") == chat_id and s.get("origin") == "direct" and s.get("cwd"):
            return DirectSession(session_id=sid, cwd=s["cwd"], chat_id=chat_id)
    return None


def transcript_of(session: DirectSession) -> pathlib.Path:
    from transcript_shipper import transcript_path
    return transcript_path(session.cwd, session.session_id)


def permission_mode(transcript: pathlib.Path) -> str:
    """The session's current permission mode: the last one its transcript records."""
    mode = "default"
    records, _ = st.read_records(transcript, 0)
    for r in records:
        if r.get("type") == "user" and r.get("permissionMode"):
            mode = r["permissionMode"]
    return _MODES.get(mode, "manual")


def find_delivered(transcript: pathlib.Path, start: int, msg_id: str) -> Optional[int]:
    """Byte offset just past the relayed message's record, once it is in the transcript."""
    try:
        with transcript.open("rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return None
    offset = start
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        offset += len(line)
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if (r.get("origin") or {}).get("msg_id") == msg_id:
            return offset
    return None


def reply_after(transcript: pathlib.Path, offset: int) -> str:
    """The agent's text after the relayed message, up to the next prompt."""
    records, _ = st.read_records(transcript, offset)
    parts: list[str] = []
    for r in records:
        if r.get("isSidechain"):
            continue
        content = (r.get("message") or {}).get("content")
        if r.get("type") == "user":
            is_tool_result = isinstance(content, list) and any(
                isinstance(b, dict) and b.get("type") == "tool_result" for b in content
            )
            if not is_tool_result and not r.get("isMeta"):
                break  # the next prompt: the reply is over
            continue
        if r.get("type") == "assistant" and isinstance(content, list):
            parts.extend(
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
            )
    return "\n\n".join(parts).strip()


async def _run(cmd: list[str], cwd: str, timeout: float, env: Optional[dict] = None) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env or os.environ.copy(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


# Set by the bridge's launcher and its own sessions; never part of the
# owner's session. Everything in the bridge's .env files is added below.
_BRIDGE_ONLY_ENV = {
    "SOCIETY_AI_AUTH_TOKEN", "AGENT_NAME", "DISPLAY_NAME", "COMPANY_ID", "AGENT_ROUTER_API_URL",
    "SOCIETY_AI_BRIDGE_SOCKET", "SOCIETY_AI_WORK_DIR", "SOCIETY_AI_SESSION_KEY",
    "SOCIETY_AI_CHANNEL_SOCK", "SOCIETY_AI_DISPATCHED", st.TRACKER_ENV,
}


def owner_session_env() -> dict[str, str]:
    """The environment for a turn in the owner's own session: the bridge's
    agent settings removed (the session keeps whatever identity it has, not
    the bridge's), and marked dispatched so the session-start banner, meant
    for a person at the keyboard, is left out."""
    drop = set(_BRIDGE_ONLY_ENV)
    try:
        import hook_common
        for path in [hook_common.REPO_DIR / ".env", *hook_common.REPO_DIR.glob(".env.*")]:
            if path.is_file():
                drop.update(hook_common.parse_env_file(path).keys())
    except Exception:
        pass
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["SOCIETY_AI_DISPATCHED"] = "1"
    return env


async def ask_closed_session(session: DirectSession, text: str, mode: str, timeout: float) -> str:
    """One turn in a closed session: `claude -p --resume <id>` continues it under
    the same id (nothing else is running it) and prints the reply. Runs as a
    normal Claude Code turn, in the session's own permission mode and folder,
    so its hooks fire and its Tracker updates as for any other turn."""
    if st.running_session(session.session_id) is not None:
        raise DirectReplyError("The session just opened in Claude Code. Send again to reach it there.")
    try:
        code, out, err = await _run(
            ["claude", "-p", "--resume", session.session_id, "--permission-mode", mode,
             "--output-format", "json", text],
            cwd=session.cwd, timeout=timeout, env=owner_session_env(),
        )
    except asyncio.TimeoutError as e:
        raise DirectReplyError("The session did not answer within the time limit. Continue it in Claude Code.") from e
    except OSError as e:
        raise DirectReplyError(f"Could not reopen the session ({type(e).__name__}).") from e
    try:
        result = json.loads(out)
    except ValueError:
        result = {}
    if result.get("session_id") and result["session_id"] != session.session_id:
        # Never answer from a copy: say so rather than pretend.
        logger.error("Resume of %s answered as %s", session.session_id[:8], result["session_id"][:8])
        raise DirectReplyError("Claude Code answered from a copy of the session. Continue it in Claude Code.")
    if code != 0 or result.get("is_error"):
        raise DirectReplyError(f"The session could not answer ({(result.get('result') or err or out).strip()[:200]}).")
    return str(result.get("result") or "").strip()


async def relay(name: str, text: str, mode: str) -> str:
    """Send `text` to the running session called `name`; returns the message id."""
    prompt = RELAY_PROMPT.format(name=name, text=text)
    cmd = [
        "claude", "-p", "--model", "haiku", "-n", RELAY_NAME,
        "--permission-mode", mode,
        "--tools", "ListAgents,SendMessage",
        "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence",
        "--output-format", "json", prompt,
    ]
    runs = st.BASE_DIR / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, st.TRACKER_ENV: "1"}
    try:
        code, out, err = await _run(cmd, cwd=str(runs), timeout=RELAY_TIMEOUT_S, env=env)
    except (OSError, asyncio.TimeoutError) as e:
        raise DirectReplyError(f"The relay did not run ({type(e).__name__}).") from e
    try:
        result = str(json.loads(out).get("result") or "")
    except ValueError:
        result = out
    m = re.search(r"SENT\s+([0-9a-f-]{8,})", result)
    if m:
        return m.group(1)
    if "AMBIGUOUS" in result:
        raise DirectReplyError(
            "Several open Claude Code sessions have this session's name, so it was not sent. "
            "Rename the session in Claude Code, or continue there."
        )
    if "NOT_FOUND" in result:
        raise DirectReplyError("The session was not reachable. Open it in Claude Code to continue.")
    raise DirectReplyError(f"The relay could not send it: {result.strip()[:200] or err.strip()[:200]}")
