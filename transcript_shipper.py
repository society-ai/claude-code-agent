"""TranscriptShipper — mirror Claude Code session transcripts to Society AI.

Phase 2 of the recorded-workspace model: every session the bridge launches
(SESSION_MODE) gets its local JSONL transcript shipped to the platform in
per-turn delta batches, POST /api/v1/session-mirror (sai_ token auth — the
bridge never holds a service key). The platform upserts the session row and
appends entries idempotently on (session, seq), so re-shipping after a crash
or retry is a no-op.

Scope: only sessions the bridge *registered* are shipped. Stop-hook
notifications for unknown session ids are ignored — a machine owner's own
desktop/terminal sessions are never mirrored unless a later opt-in
(observe tier) registers them. MIRROR=false disables shipping entirely.

WHAT ships is a security boundary (MIRROR_LEVEL, local-only):
  messages (default) — the conversation: user/dispatch text, assistant
      text, and per-tool "activity" stubs carrying only the tool name and
      a safe target (file path, Bash description, host). Tool inputs,
      command lines, tool results, attachments, and Claude Code internal
      records NEVER leave the machine at this level — that's where
      secrets live (env output, file contents, tokens in command lines).
  full — trimmed raw records, including tool inputs/results. Debug
      opt-in for mirroring your own agents only.

Offsets (byte position + next seq per session) persist across bridge
restarts in mirror_state.json next to the bridge socket, keeping seq
numbers stable so the server-side dedup works. The record→entry transform
is deterministic, so re-reading the same byte range regenerates identical
(seq, entry) pairs and re-ships stay idempotent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
import socket
from itertools import islice
from typing import Any, Optional

import httpx

logger = logging.getLogger("transcript_shipper")

# Server-side caps (mirror agent_router's SessionMirrorBatch limits).
MAX_ENTRIES_PER_BATCH = 500

# Trimming: transcripts can embed file dumps / base64 blobs. We keep the
# record's shape but cap every string and collection so one entry stays
# well under the server's per-entry limit.
MAX_STR_CHARS = 4000
MAX_COLLECTION_ITEMS = 100
MAX_READ_BYTES = 8 * 1024 * 1024   # per ship() call; the rest ships next turn
MAX_TRACKED_SESSIONS = 200

_PROJECTS_DIR = pathlib.Path.home() / ".claude" / "projects"


def transcript_path(cwd: str, session_id: str) -> pathlib.Path:
    """Path of a session's local transcript. Claude Code munges the cwd by
    replacing every non-alphanumeric character with '-'
    (/Users/x/.claude → -Users-x--claude)."""
    munged = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))
    return _PROJECTS_DIR / munged / f"{session_id}.jsonl"


def _trim(value: Any, depth: int = 0) -> Any:
    """Recursively cap strings and collections, preserving record shape."""
    if depth > 12:
        return "…[depth-trimmed]"
    if isinstance(value, str):
        if len(value) > MAX_STR_CHARS:
            return value[:MAX_STR_CHARS] + f"…[+{len(value) - MAX_STR_CHARS} chars]"
        return value
    if isinstance(value, list):
        out = [_trim(v, depth + 1) for v in value[:MAX_COLLECTION_ITEMS]]
        if len(value) > MAX_COLLECTION_ITEMS:
            out.append(f"…[+{len(value) - MAX_COLLECTION_ITEMS} items]")
        return out
    if isinstance(value, dict):
        out = {k: _trim(v, depth + 1) for k, v in islice(value.items(), MAX_COLLECTION_ITEMS)}
        if len(value) > MAX_COLLECTION_ITEMS:
            out["…"] = f"+{len(value) - MAX_COLLECTION_ITEMS} keys trimmed"
        return out
    return value


def _clip(s: Any, n: int = 200) -> str:
    s = s if isinstance(s, str) else ""
    s = " ".join(s.split())
    return s[:n]


def _activity_target(name: str, inp: dict) -> str:
    """The one safe, human-meaningful string for a tool call. Never the
    Bash command line, never request bodies, never MCP payloads — those
    can carry secrets inline."""
    if not isinstance(inp, dict):
        return ""
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return _clip(inp.get("file_path"))
    if name == "Bash":
        return _clip(inp.get("description"))  # human label only, NEVER command
    if name in ("Glob", "Grep"):
        target = _clip(inp.get("pattern"), 80)
        path = _clip(inp.get("path"), 100)
        return f"{target} in {path}" if path else target
    if name in ("WebFetch", "WebSearch"):
        url = inp.get("url") or ""
        m = re.match(r"^\w+://([^/?#]+)", url) if isinstance(url, str) else None
        if m:
            return m.group(1)  # host only — query strings can carry tokens
        return _clip(inp.get("query"), 100)
    if name in ("Task", "Agent"):
        return _clip(inp.get("description"))
    # MCP/platform tools and anything unknown: tool name alone is enough.
    return ""


def _messages_entries(record: dict) -> list[dict]:
    """Project one raw transcript record into zero or more shippable
    entries at MIRROR_LEVEL=messages. Deterministic — same record always
    yields the same entries in the same order."""
    rtype = record.get("type")

    if rtype == "summary":
        s = _clip(record.get("summary"), MAX_STR_CHARS)
        return [{"entry_type": "summary", "content": {"summary": s}}] if s else []

    if rtype == "user":
        content = (record.get("message") or {}).get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for b in content:
                # tool_result blocks are command/file output — never ship.
                if isinstance(b, dict) and b.get("type") == "text":
                    texts.append(b.get("text") or "")
        text = "\n".join(t for t in texts if t.strip()).strip()
        if not text:
            return []
        return [{"entry_type": "user", "content": {"text": _trim(text)}}]

    if rtype == "assistant":
        content = (record.get("message") or {}).get("content")
        out: list[dict] = []
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    out.append({
                        "entry_type": "assistant",
                        "content": {"text": _trim(b["text"])},
                    })
                elif b.get("type") == "tool_use":
                    name = str(b.get("name") or "tool")[:80]
                    stub: dict = {"tool": name}
                    target = _activity_target(name, b.get("input") or {})
                    if target:
                        stub["target"] = target
                    out.append({"entry_type": "activity", "content": stub})
                # thinking blocks: internal reasoning, never shipped
        elif isinstance(content, str) and content.strip():
            out.append({"entry_type": "assistant", "content": {"text": _trim(content)}})
        return out

    # Everything else (queue-operation, attachment, mode, last-prompt,
    # bridge-session, progress, file-history-snapshot, ...) is Claude Code
    # internal state — attachments in particular can embed file contents.
    return []


class TranscriptShipper:
    """Per-bridge shipper. One instance per persona/bridge process."""

    def __init__(self, api_url: str, token: str, state_dir: str, level: str = "messages"):
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._level = level if level in ("messages", "full") else "messages"
        self._machine = socket.gethostname()
        self._state_path = pathlib.Path(state_dir) / "mirror_state.json"
        # Fired (awaited) when the platform reports which chat a session
        # projects into: async (session_id, chat_id) -> None. The bridge
        # uses it to alias chat-composer sends back into the same session.
        self.on_chat_link = None
        # session_id -> {"pos": int, "seq": int, "cwd": str, "title": str,
        #                "work_item_kind": str|None, "work_item_id": str|None}
        self._state: dict[str, dict] = self._load_state()
        self._locks: dict[str, asyncio.Lock] = {}
        self._client: Optional[httpx.AsyncClient] = None

    # -- registry --------------------------------------------------------------

    def register(
        self,
        session_id: str,
        *,
        cwd: str,
        title: str = "",
        work_item_kind: Optional[str] = None,
        work_item_id: Optional[str] = None,
    ) -> None:
        """Mark a session as bridge-owned and shippable. Idempotent; metadata
        refreshes on every call (a resume can update the title)."""
        rec = self._state.setdefault(session_id, {"pos": 0, "seq": 0})
        rec["cwd"] = cwd
        # First title wins: later dispatches into the same session (trigger
        # echoes, follow-up turns) must not overwrite the human title.
        if title and not rec.get("title"):
            rec["title"] = title[:500]
        if work_item_kind:
            rec["work_item_kind"] = work_item_kind
        if work_item_id:
            rec["work_item_id"] = str(work_item_id)[:255]
        self._prune()
        self._save_state()

    def is_registered(self, session_id: str) -> bool:
        return session_id in self._state

    # -- shipping ---------------------------------------------------------------

    async def ship(self, session_id: str, *, status: Optional[str] = None) -> bool:
        """Ship new transcript lines (and/or a status flip) for a registered
        session. Serialized per session; safe to call from multiple places
        (turn end, stop hook, reap). Returns True when the platform accepted
        the batch (or there was nothing to send)."""
        meta = self._state.get(session_id)
        if meta is None:
            logger.debug("ship: unknown session %s (ignored)", session_id[:8])
            return False

        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            # A status flip that failed to ship earlier (network blip at
            # reap time) is retried on the next ship of any kind.
            if status is None and meta.get("pending_status"):
                status = meta["pending_status"]

            entries, new_pos, new_seq = self._read_new_entries(session_id, meta)
            if not entries and status is None:
                return True  # nothing new, nothing to flip

            # Chunk to the server's batch cap; status rides the last chunk so
            # 'ended' lands only after all content is in. On any failure the
            # cursor stays put and the whole delta re-ships next time — the
            # server dedups on (mirror, seq), so retries are free.
            chunks = [entries[i:i + MAX_ENTRIES_PER_BATCH]
                      for i in range(0, len(entries), MAX_ENTRIES_PER_BATCH)] or [[]]
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                ok = await self._post_batch(
                    session_id, meta, chunk, status=status if is_last else None
                )
                if not ok:
                    # Status flips must not be lost — the session_ended wake
                    # (and the Scribe) depend on them. Park for retry.
                    if status:
                        meta["pending_status"] = status
                        self._save_state()
                    return False

            meta["pos"] = new_pos
            meta["seq"] = new_seq
            meta.pop("pending_status", None)
            self._save_state()
            return True

    async def retry_pending(self) -> int:
        """Re-ship sessions whose status flip (or delta) failed earlier.
        Called at bridge start and periodically. Returns retries attempted."""
        attempted = 0
        for sid in [s for s, m in self._state.items() if m.get("pending_status")]:
            attempted += 1
            await self.ship(sid)
        return attempted

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # -- internals ----------------------------------------------------------------

    def _read_new_entries(
        self, session_id: str, meta: dict
    ) -> tuple[list[dict], int, int]:
        """Read complete new JSONL lines from the transcript past the byte
        cursor. Returns (entries, new_byte_pos, next_seq). Partial trailing
        lines (mid-write) stay unread until the next call."""
        path = transcript_path(meta.get("cwd", ""), session_id)
        pos = int(meta.get("pos", 0))
        seq = int(meta.get("seq", 0))
        try:
            size = path.stat().st_size
        except OSError:
            return [], pos, seq
        if size <= pos:
            return [], pos, seq

        try:
            with path.open("rb") as f:
                f.seek(pos)
                blob = f.read(MAX_READ_BYTES)
        except OSError as e:
            logger.warning("transcript read failed for %s: %s", session_id[:8], e)
            return [], pos, seq

        # Only complete lines; leave a trailing partial for the next pass.
        last_nl = blob.rfind(b"\n")
        if last_nl < 0:
            return [], pos, seq
        complete = blob[: last_nl + 1]

        entries: list[dict] = []
        for raw in complete.split(b"\n"):
            if not raw.strip():
                continue
            for partial in self._line_entries(raw):
                partial["seq"] = seq
                entries.append(partial)
                seq += 1
        return entries, pos + len(complete), seq

    def _line_entries(self, raw: bytes) -> list[dict]:
        """Project one transcript line into shippable entries (level-aware).
        Deterministic, so re-reads regenerate identical seq assignment."""
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return []  # unparseable internals are never worth shipping
        if not isinstance(record, dict):
            return []

        ts = record.get("timestamp")
        ts = ts[:64] if isinstance(ts, str) and ts else None

        if self._level == "full":
            entry: dict[str, Any] = {
                "entry_type": str(record.get("type") or "unknown")[:32],
                "content": _trim(record),
            }
            if ts:
                entry["ts"] = ts
            return [entry]

        out = _messages_entries(record)
        if ts:
            for e in out:
                e["ts"] = ts
        return out

    async def _post_batch(
        self, session_id: str, meta: dict, entries: list[dict], *, status: Optional[str]
    ) -> bool:
        body: dict[str, Any] = {
            "claude_session_id": session_id,
            "machine": self._machine,
            "cwd": meta.get("cwd"),
            "entries": entries,
        }
        for k in ("title", "work_item_kind", "work_item_id"):
            if meta.get(k):
                body[k] = meta[k]
        if status:
            body["status"] = status

        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        try:
            r = await self._client.post(
                f"{self._api_url}/api/v1/session-mirror", json=body
            )
            if r.status_code >= 400:
                logger.warning(
                    "mirror ship rejected for %s: %s %s",
                    session_id[:8], r.status_code, r.text[:200],
                )
                return False
            # The platform reports which chat this session projects into;
            # surface it once so the bridge can alias chat sends → session.
            try:
                chat_id = (r.json() or {}).get("chatId")
            except ValueError:
                chat_id = None
            if chat_id and meta.get("chat_id") != chat_id:
                meta["chat_id"] = chat_id
                self._save_state()
                if self.on_chat_link is not None:
                    try:
                        await self.on_chat_link(session_id, chat_id)
                    except Exception:
                        logger.exception("on_chat_link callback failed")
            logger.debug("shipped %d entries for %s", len(entries), session_id[:8])
            return True
        except httpx.HTTPError as e:
            logger.warning("mirror ship failed for %s: %s", session_id[:8], e)
            return False

    # -- state persistence ---------------------------------------------------------

    def _load_state(self) -> dict[str, dict]:
        try:
            data = json.loads(self._state_path.read_text())
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_state(self) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state))
            os.replace(tmp, self._state_path)
        except OSError as e:
            logger.warning("could not persist mirror state: %s", e)

    def _prune(self) -> None:
        if len(self._state) <= MAX_TRACKED_SESSIONS:
            return
        # Insertion order ≈ registration order; drop the oldest beyond cap.
        for k in list(self._state.keys())[: len(self._state) - MAX_TRACKED_SESSIONS]:
            self._state.pop(k, None)
            self._locks.pop(k, None)
