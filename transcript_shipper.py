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

Offsets (byte position + next seq per session) persist across bridge
restarts in mirror_state.json next to the bridge socket, keeping seq
numbers stable so the server-side dedup works.
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


class TranscriptShipper:
    """Per-bridge shipper. One instance per persona/bridge process."""

    def __init__(self, api_url: str, token: str, state_dir: str):
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._machine = socket.gethostname()
        self._state_path = pathlib.Path(state_dir) / "mirror_state.json"
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
        if title:
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
            entries, new_pos, new_seq = self._read_new_entries(session_id, meta)
            if not entries and status is None:
                return True  # nothing new, nothing to flip

            ok = True
            # Chunk to the server's batch cap; status rides the last chunk so
            # 'ended' lands only after all content is in.
            chunks = [entries[i:i + MAX_ENTRIES_PER_BATCH]
                      for i in range(0, len(entries), MAX_ENTRIES_PER_BATCH)] or [[]]
            for i, chunk in enumerate(chunks):
                is_last = i == len(chunks) - 1
                ok = await self._post_batch(
                    session_id, meta, chunk, status=status if is_last else None
                )
                if not ok:
                    # Roll the cursor forward only past what was accepted.
                    shipped = sum(len(c) for c in chunks[:i])
                    if shipped:
                        meta["seq"] = entries[shipped - 1]["seq"] + 1
                        meta["pos"] = entries[shipped - 1]["_end_pos"]
                        self._save_state()
                    return False

            meta["pos"] = new_pos
            meta["seq"] = new_seq
            self._save_state()
            return ok

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
        line_start = pos
        for raw in complete.split(b"\n"):
            line_end = line_start + len(raw) + 1  # +1 for the newline
            if raw.strip():
                entry = self._to_entry(raw, seq)
                if entry is not None:
                    entry["_end_pos"] = line_end
                    entries.append(entry)
                    seq += 1
            line_start = line_end
        return entries, pos + len(complete), seq

    @staticmethod
    def _to_entry(raw: bytes, seq: int) -> Optional[dict]:
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            # Unparseable line: ship a stub so seq stays aligned with lines.
            return {"seq": seq, "entry_type": "unparsed",
                    "content": {"raw_prefix": raw[:200].decode("utf-8", "replace")}}
        if not isinstance(record, dict):
            return {"seq": seq, "entry_type": "unknown", "content": {"value": _trim(record)}}
        entry: dict[str, Any] = {
            "seq": seq,
            "entry_type": str(record.get("type") or "unknown")[:32],
            "content": _trim(record),
        }
        ts = record.get("timestamp")
        if isinstance(ts, str) and ts:
            entry["ts"] = ts[:64]
        return entry

    async def _post_batch(
        self, session_id: str, meta: dict, entries: list[dict], *, status: Optional[str]
    ) -> bool:
        body: dict[str, Any] = {
            "claude_session_id": session_id,
            "machine": self._machine,
            "cwd": meta.get("cwd"),
            "entries": [
                {k: v for k, v in e.items() if k != "_end_pos"} for e in entries
            ],
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
