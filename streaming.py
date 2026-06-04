"""Map Claude Code `--output-format stream-json` events to Society AI
DataPart frames in the wire format the chat UI actually consumes.

The ai-chatbot stream-transformer reads `part.data?.component` (nested) and
only forwards a whitelist of recognized components:

    workflow, workflow_step, widget, document, agent_log, artifact, tool_execution

For Claude Code's purposes:
  - `tool_execution` is the right component for internal tool calls — the
    UI's useToolExecutions hook upserts by `tool_id`, so a `running` event
    followed by a matching `success` / `error` event merges into one row.
    Renders in the agent-console sidebar with expandable args / result.
  - `agent_log` is right for thinking summaries and session-start banners.
    Renders in the logs panel as timestamped lines.

`workflow` / `workflow_step` are for OpenClaw-style **delegation to other
agents**, not internal tool calls. Using them for Claude Code's local
tools led to nothing rendering at all in v0.4.0 — wrong primitive.

Mapping table (v0.4.1):

  system.init         → agent_log     "Started working on your message"
  assistant.thinking  → agent_log     "💭 <first sentence>"
  assistant.tool_use  → tool_execution status=running, args=summary
  user.tool_result    → tool_execution status=success|error, result=summary
                        (same tool_id → merged into the same row)
  assistant.text      → accumulated for final task.complete
  result.success      → caller sends task.complete with final=true

Tool inputs are SANITIZED — only the extracted summary becomes `args`.
Raw input dicts never leave the bridge.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

_DEFAULT_FALLBACK_CHARS = 80
_THINKING_SUMMARY_CHARS = 120
_TOOL_RESULT_SUMMARY_CHARS = 600


def _truncate(s: str, n: int) -> str:
    s = (s or "").strip()
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _first_sentence(text: str, max_chars: int) -> str:
    """Return the first sentence (split on . ! ?) or first max_chars chars
    falling at a word boundary."""
    text = (text or "").strip()
    if not text:
        return ""
    match = re.search(r"[.!?](\s|$)", text[: max_chars + 50])
    if match and match.end() <= max_chars + 50:
        candidate = text[: match.start() + 1].strip()
        if candidate:
            return _truncate(candidate, max_chars)
    return _truncate(text, max_chars)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Per-tool argument summarizers. Each receives the tool's `input` dict and
# returns a short human-readable string used as the `args` field on the
# tool_execution DataPart. The UI shows args verbatim when expanded — keep
# them small and never include raw file contents, prompts, etc.

_TOOL_SUMMARIZERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "Bash":     lambda i: f"$ {_truncate(i.get('command', '?'), 100)}",
    "BashOutput": lambda i: f"$ output {_truncate(str(i.get('bash_id', '')), 20)}",
    "Read":     lambda i: f"📄 {_truncate(i.get('file_path', '?'), 100)}",
    "Edit":     lambda i: f"✏️  {_truncate(i.get('file_path', '?'), 100)}",
    "Write":    lambda i: f"💾 {_truncate(i.get('file_path', '?'), 100)}",
    "Glob":     lambda i: f"🔎 {_truncate(i.get('pattern', '?'), 80)}",
    "Grep":     lambda i: f"🔍 {_truncate(i.get('pattern', '?'), 60)} in {_truncate(i.get('path', '.'), 30)}",
    "WebFetch": lambda i: f"🌐 {_truncate(i.get('url', '?'), 100)}",
    "WebSearch": lambda i: f"🔎 web: {_truncate(i.get('query', '?'), 80)}",
    "TodoWrite": lambda i: f"✅ {len(i.get('todos', []) or [])} todo(s)",
    "Task":     lambda i: f"🤖 subagent: {_truncate(i.get('description', '?'), 80)}",
    "AskUserQuestion": lambda i: "❓ asking user",
    "NotebookEdit": lambda i: f"📓 {_truncate(i.get('notebook_path', '?'), 100)}",
}


def summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """Return a short, sanitized one-line summary of the tool invocation."""
    if not isinstance(tool_input, dict):
        tool_input = {}
    summarizer = _TOOL_SUMMARIZERS.get(tool_name)
    if summarizer is not None:
        try:
            return summarizer(tool_input)
        except Exception:
            pass
    # Generic fallback: tool name + key=value preview.
    if isinstance(tool_name, str) and tool_name.startswith("mcp__"):
        pretty_name = tool_name.rsplit("__", 1)[-1]
    else:
        pretty_name = tool_name or "?"
    try:
        preview = json.dumps(tool_input, separators=(",", ":"))
    except (TypeError, ValueError):
        preview = "{…}"
    return f"{pretty_name}({_truncate(preview, _DEFAULT_FALLBACK_CHARS)})"


def summarize_tool_result(content: Any) -> str:
    """Return a short, sanitized summary of a tool result."""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                else:
                    parts.append(f"[{block.get('type', 'block')}]")
            else:
                parts.append(str(block))
        flat = " ".join(parts)
    else:
        flat = str(content or "")
    return _truncate(flat, _TOOL_RESULT_SUMMARY_CHARS)


def _data_part(component: str, part_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a DataPart in the nested format the chat stream-transformer
    expects: `{type: 'data', data: {component, id, payload}}`.

    The chat UI reads `part.data?.component`; flat shapes are silently
    dropped (this was the v0.4.0 bug).
    """
    return {
        "type": "data",
        "data": {
            "component": component,
            "id": part_id,
            "payload": payload,
        },
    }


class StreamMapper:
    """Translate Claude Code stream-json events into chat-renderable parts.

    Tool calls become `tool_execution` DataParts keyed by Claude Code's
    `tool_use.id`. The result event with the same `tool_id` upserts the
    same row, so the UI shows one entry per tool call that animates from
    `running` → `success` / `error`.

    Thinking blocks become `agent_log` lines with the first sentence as
    the message — privacy-preserving but informative ("Thinking about
    checking your tasks…" style).

    The accumulated assistant text is returned by `final_text()` so the
    bridge can send it as the terminal `task.complete` body.
    """

    QUIET = "quiet"
    NORMAL = "normal"
    VERBOSE = "verbose"

    def __init__(self, task_id: str, verbosity: str = NORMAL):
        self.task_id = task_id
        self.verbosity = verbosity
        self._started_at: dict[str, str] = {}  # tool_id -> ISO timestamp
        self._known_tools: dict[str, str] = {}  # tool_id -> name (for orphan results)
        self._accumulated_text: list[str] = []
        self._final_result_text: Optional[str] = None
        self._log_counter = 0
        self.session_id: Optional[str] = None
        self.is_error: bool = False
        self.error_message: Optional[str] = None

    # -- helpers --------------------------------------------------------

    def _next_log_id(self) -> str:
        self._log_counter += 1
        return f"log-{self.task_id}-{self._log_counter}"

    def _log(self, message: str, level: str = "info") -> dict[str, Any]:
        return _data_part(
            component="agent_log",
            part_id=self._next_log_id(),
            payload={
                "id": self._next_log_id(),
                "message": message,
                "level": level,
                "timestamp": _now_iso(),
            },
        )

    # -- public surface -------------------------------------------------

    def consume(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Process one stream event, return the list of DataParts (possibly
        empty) the bridge should forward as task.status updates."""
        etype = event.get("type")
        if etype == "system":
            return self._consume_system(event)
        if etype == "assistant":
            return self._consume_assistant(event)
        if etype == "user":
            return self._consume_user(event)
        if etype == "result":
            return self._consume_result(event)
        return []

    def final_text(self) -> str:
        """The full accumulated assistant text — what the bridge sends in
        the terminal task.complete frame."""
        if self._final_result_text is not None:
            return self._final_result_text
        return "".join(self._accumulated_text).strip()

    # -- per-event handlers --------------------------------------------

    def _consume_system(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        # We capture session_id for diagnostics, but we don't emit a
        # canned "Started working" log line — the tool_execution rows
        # appearing in the Execution panel are sufficient signal that
        # the agent is alive. The Activity panel only shows entries we
        # explicitly emit, so by skipping this we keep it focused on
        # actual model state (thinking, errors) instead of placeholders.
        if event.get("subtype") == "init":
            self.session_id = event.get("session_id")
        return []

    def _consume_assistant(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        msg = event.get("message") or {}
        content = msg.get("content") or []
        out: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")

            if btype == "tool_use":
                tool_id = str(block.get("id") or "")
                tool_name = str(block.get("name") or "?")
                tool_input = block.get("input") or {}
                if not tool_id:
                    # Should not happen in practice, but synthesize one
                    # so the upsert key in the hook stays unique.
                    tool_id = f"tool-{self.task_id}-{len(self._known_tools)}"
                args_summary = summarize_tool_input(tool_name, tool_input)
                started = _now_iso()
                self._started_at[tool_id] = started
                self._known_tools[tool_id] = tool_name

                # The UI hook uses `tool_id` as the merge key. We embed it
                # both as the DataPart id (nested.data.id) and in the
                # payload so existing call+result events fuse cleanly.
                out.append(_data_part(
                    component="tool_execution",
                    part_id=tool_id,
                    payload={
                        "tool_id": tool_id,
                        "name": tool_name,
                        "args": args_summary,
                        "status": "running",
                        "started_at": started,
                    },
                ))

            elif btype == "thinking":
                if self.verbosity == self.QUIET:
                    continue
                text = str(block.get("text") or "")
                summary = _first_sentence(text, _THINKING_SUMMARY_CHARS)
                if not summary:
                    continue
                out.append(self._log(f"💭 {summary}", level="info"))

            elif btype == "text":
                text = str(block.get("text") or "")
                if text:
                    self._accumulated_text.append(text)
                # Verbose mode also emits a log line so the partial text
                # is visible in the logs panel before the final answer.
                if self.verbosity == self.VERBOSE and text:
                    out.append(self._log(_truncate(text, 400), level="info"))
            # tool_result inside an assistant message is malformed — skip.
        return out

    def _consume_user(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        msg = event.get("message") or {}
        content = msg.get("content") or []
        out: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tool_use_id = str(block.get("tool_use_id") or "")
            is_error = bool(block.get("is_error"))
            result_summary = summarize_tool_result(block.get("content"))
            tool_name = self._known_tools.get(tool_use_id, "tool")
            started_at = self._started_at.get(tool_use_id)
            payload: dict[str, Any] = {
                "tool_id": tool_use_id or self._next_log_id(),
                "name": tool_name,
                "status": "error" if is_error else "success",
                "ended_at": _now_iso(),
            }
            if started_at:
                payload["started_at"] = started_at
            if is_error:
                payload["error"] = result_summary
            else:
                payload["result"] = result_summary

            out.append(_data_part(
                component="tool_execution",
                part_id=payload["tool_id"],
                payload=payload,
            ))
        return out

    def _consume_result(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        subtype = event.get("subtype") or ""
        result_text = event.get("result")
        if isinstance(result_text, str) and result_text:
            self._final_result_text = result_text
        if subtype != "success" or event.get("is_error"):
            self.is_error = True
            self.error_message = (
                event.get("error") or result_text or "Claude Code exited with an error"
            )
        return []
