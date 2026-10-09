"""Files that arrive with a message, and files an agent opens on purpose.

Every file on Society AI is an artifact with a permanent `artifact_id`. A
message carries references to artifacts (file parts with
`metadata.artifact_id`), never the bytes. Opening one is
`GET /api/v1/files/{artifact_id}` with the agent's own token: the platform
checks access at that moment (owner, sent to this agent, or a workspace the
agent belongs to) and answers with a 10-minute download link.

The bridge downloads a message's files when it arrives and names them in the
prompt, because a message reaches a Claude Code session as text only: Claude
opens an image with its Read tool, which shows images. Where a file lands
depends on who sent it (see bridge._execute_via_session): the owner's into
the agent's work folder, a contact's into that contact's own session folder.

Design: docs/design/agent-files.md in the A2A repo.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger("attachments")

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_MESSAGE_BYTES = 100 * 1024 * 1024
# Claude's image input limits; larger images are noted, never resized here.
IMAGE_NOTE_BYTES = 5 * 1024 * 1024
FILES_DIRNAME = "files"
_NAME_MAX = 120


@dataclass(frozen=True)
class Attachment:
    artifact_id: str
    name: str
    mime_type: str


@dataclass
class Fetched:
    attachment: Attachment
    path: Optional[pathlib.Path] = None   # set when downloaded
    size: Optional[int] = None
    error: str = ""                       # why it was not downloaded


def parse_file_parts(parts: list) -> list[Attachment]:
    """File parts that reference an artifact, on today's wire format
    ({"type": "file", "file": {...}, "metadata": {"artifact_id": ...}}) and
    the A2A v1 shape ("kind": "file", or a bare "file" object). Parts without
    an artifact_id are ignored: the bridge only opens files through the
    platform's access check."""
    found: list[Attachment] = []
    seen: set[str] = set()
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        kind = part.get("type") or part.get("kind")
        file = part.get("file") if isinstance(part.get("file"), dict) else {}
        if kind != "file" and not file:
            continue
        meta = part.get("metadata") if isinstance(part.get("metadata"), dict) else {}
        fmeta = file.get("metadata") if isinstance(file.get("metadata"), dict) else {}
        artifact_id = str(meta.get("artifact_id") or fmeta.get("artifact_id") or "").strip()
        if not artifact_id or artifact_id in seen:
            continue
        seen.add(artifact_id)
        name = str(file.get("name") or part.get("name") or meta.get("name") or "file").strip()
        mime = str(file.get("mimeType") or file.get("mime_type") or part.get("mimeType")
                   or meta.get("mime_type") or "").strip()
        found.append(Attachment(artifact_id=artifact_id, name=name, mime_type=mime))
    return found


def safe_filename(artifact_id: str, name: str) -> str:
    """<artifact_id[:8]>-<name>, with no path separators, no leading dots, no
    control characters and a capped length. The id prefix keeps two files
    with the same name apart and makes a re-download land on the same path."""
    base = os.path.basename((name or "").replace("\\", "/"))
    base = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "-", base).lstrip(". ").strip() or "file"
    if len(base) > _NAME_MAX:
        stem, dot, ext = base.rpartition(".")
        base = (stem[: _NAME_MAX - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else base[:_NAME_MAX]
    prefix = re.sub(r"[^A-Za-z0-9]", "", artifact_id)[:8] or "file"
    return f"{prefix}-{base}"


async def file_info(client: httpx.AsyncClient, api_url: str, token: str, artifact_id: str) -> dict:
    """GET /api/v1/files/{id}: name, mime_type, size and a fresh short link.
    Raises PermissionError (no access or no such file) or RuntimeError."""
    resp = await client.get(
        f"{api_url.rstrip('/')}/api/v1/files/{artifact_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if resp.status_code in (403, 404):
        raise PermissionError("not available to this agent")
    if resp.status_code >= 400:
        raise RuntimeError(f"the platform answered {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict) or not data.get("url"):
        raise RuntimeError("the platform returned no download link")
    return data


async def download(client: httpx.AsyncClient, url: str, dest: pathlib.Path, max_bytes: int) -> int:
    """Stream `url` into `dest` (never executable), stopping at max_bytes.
    Writes to a temp name first, so a failed download never leaves a partial
    file where the prompt says one is."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    size = 0
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"download failed ({resp.status_code})")
            with open(tmp, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("too large")
                    f.write(chunk)
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
        return size
    finally:
        if tmp.exists():
            tmp.unlink()


async def fetch_all(
    client: httpx.AsyncClient, api_url: str, token: str,
    attachments: list[Attachment], dest_dir: pathlib.Path,
) -> list[Fetched]:
    """Download a message's files into dest_dir, within the per-file and
    per-message limits. One retry per file; a failure is reported, never
    raised, so the message is always delivered."""
    results: list[Fetched] = []
    budget = MAX_MESSAGE_BYTES
    for att in attachments:
        result = Fetched(att)
        results.append(result)
        dest = dest_dir / safe_filename(att.artifact_id, att.name)
        for attempt in (1, 2):
            try:
                info = await file_info(client, api_url, token, att.artifact_id)
                size = int(info.get("size") or 0)
                if size > MAX_FILE_BYTES or size > budget:
                    result.size = size
                    result.error = "too large to download automatically"
                    break
                if dest.exists() and size and dest.stat().st_size == size:
                    result.path, result.size = dest, size  # already here (a resend)
                else:
                    result.size = await download(client, info["url"], dest, min(MAX_FILE_BYTES, budget))
                    result.path = dest
                budget -= result.size or 0
                result.error = ""
                break
            except PermissionError as e:
                result.error = str(e)
                break  # retrying cannot change an access decision
            except ValueError:
                result.error = "too large to download automatically"
                break
            except Exception as e:
                result.error = f"could not be fetched ({type(e).__name__})"
                logger.warning("Attachment %s attempt %d failed: %s", att.artifact_id, attempt, e)
    return results


def _human(size: Optional[int]) -> str:
    if not size:
        return "size unknown"
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def prompt_block(results: list[Fetched], can_get_file: bool) -> str:
    """The [Attachments] section of the prompt. Names every file, so a file
    that could not be downloaded is never silently missing."""
    if not results:
        return ""
    lines = ["[Attachments]"]
    for r in results:
        a = r.attachment
        kind = a.mime_type or "unknown type"
        if r.path is not None:
            line = f"- {a.name} ({kind}, {_human(r.size)}): {r.path}"
            if kind.startswith("image/"):
                line += " (an image: open it with the Read tool to see it"
                line += "; it is over Claude's 5 MB image limit, so it may not display)" \
                    if (r.size or 0) > IMAGE_NOTE_BYTES else ")"
            lines.append(line)
        else:
            how = f"; use get_file(\"{a.artifact_id}\") to open it" if can_get_file else ""
            lines.append(f"- {a.name} ({kind}, {_human(r.size)}): not downloaded, {r.error}{how}")
    return "\n".join(lines)


def describe(info: dict[str, Any]) -> dict[str, Any]:
    """The fields of a file_info answer worth returning to a tool caller."""
    return {k: info.get(k) for k in ("artifact_id", "name", "mime_type", "size") if k in info}
