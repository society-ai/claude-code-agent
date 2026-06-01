"""Map Claude Code `--output-format stream-json` events to Society AI
DataPart-shaped task.status frames.

Wire format mirrors the OpenClaw / ZeroClaw connector convention:
`task.status` JSON-RPC with `final: false` carrying `parts` that look like
the platform's `DataPart` rows (component / action / sequence / payload /
metadata). The hub persists these as `DataPart` rows attached to the
agent's `Message`, and the chat UI renders them as workflow callouts —
the same rendering path OpenClaw delegation steps already use.

The mapping table:

  system.init         → workflow / start
  assistant.tool_use  → workflow_step / tool_call    (working)
  user.tool_result    → workflow_step / tool_result  (completed | failed)
  assistant.thinking  → workflow_step / thinking     (first sentence only)
  assistant.text      → accumulated for final task.complete
  result.success      → handled by caller (sends task.complete final=true)
  result.error        → handled by caller

Tool inputs are SANITIZED — only an extracted summary is forwarded, never
the raw `input` dict, which can contain prompts, file contents, etc.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

# Per-tool summarizers: each receives the tool's `input` dict and returns a
# short human-readable string. Fallback uses the tool name + 60-char JSON.
# These run in the bridge, never on the model side — tool inputs come back
# from Claude Code's output stream and are extracted here, not interpolated.

_DEFAULT_FALLBACK_CHARS = 60
_THINKING_SUMMARY_CHARS = 100
_TOOL_RESULT_SUMMARY_CHARS = 200


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


_TOOL_SUMMARIZERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "Bash":     lambda i: f"$ {_truncate(i.get('command', '?'), 80)}",
    "BashOutput": lambda i: f"$ output {_truncate(str(i.get('bash_id', '')), 20)}",
    "Read":     lambda i: f"📄 {_truncate(i.get('file_path', '?'), 80)}",
    "Edit":     lambda i: f"✏️  {_truncate(i.get('file_path', '?'), 80)}",
    "Write":    lambda i: f"💾 {_truncate(i.get('file_path', '?'), 80)}",
    "Glob":     lambda i: f"🔎 {_truncate(i.get('pattern', '?'), 60)}",
    "Grep":     lambda i: f"🔍 {_truncate(i.get('pattern', '?'), 40)} in {_truncate(i.get('path', '.'), 30)}",
    "WebFetch": lambda i: f"🌐 {_truncate(i.get('url', '?'), 80)}",
    "WebSearch": lambda i: f"🔎 web: {_truncate(i.get('query', '?'), 60)}",
    "TodoWrite": lambda i: f"✅ {len(i.get('todos', []) or [])} todo(s)",
    "Task":     lambda i: f"🤖 subagent: {_truncate(i.get('description', '?'), 60)}",
    "AskUserQuestion": lambda i: "❓ asking user",
    "NotebookEdit": lambda i: f"📓 {_truncate(i.get('notebook_path', '?'), 80)}",
}


def summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """Return a short, sanitized one-line summary of the tool invocation.

    Never returns the raw input dict — only fields explicitly extracted by
    the per-tool summarizer or, for unknown tools (e.g. MCP tools), a
    truncated JSON preview of the keys.
    """
    if not isinstance(tool_input, dict):
        tool_input = {}
    summarizer = _TOOL_SUMMARIZERS.get(tool_name)
    if summarizer is not None:
        try:
            return summarizer(tool_input)
        except Exception:
            pass
    # Generic fallback: tool name + key=value preview.
    # MCP tools that begin with `mcp__` get a friendlier label.
    if isinstance(tool_name, str) and tool_name.startswith("mcp__"):
        # mcp__society-ai__list_tasks -> "list_tasks"
        pretty_name = tool_name.rsplit("__", 1)[-1]
    else:
        pretty_name = tool_name or "?"
    try:
        preview = json.dumps(tool_input, separators=(",", ":"))
    except (TypeError, ValueError):
        preview = "{…}"
    return f"{pretty_name}({_truncate(preview, _DEFAULT_FALLBACK_CHARS)})"


def summarize_tool_result(content: Any, is_error: bool) -> str:
    """Return a short, sanitized summary of a tool result.

    Tool result content can be a string OR a list of content blocks (for
    structured outputs). We coerce to a string and truncate aggressively.
    """
    if isinstance(content, list):
        # Anthropic content-block list. Flatten to text.
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
    summary = _truncate(flat, _TOOL_RESULT_SUMMARY_CHARS)
    return f"⚠️  {summary}" if is_error else summary


class StreamMapper:
    """Stateful mapper from Claude Code stream-json events to task.status
    DataParts.

    Holds:
      - a monotonic sequence counter (so the hub orders the events)
      - a step_id allocator (so tool_call and tool_result for the same
        invocation get the same step_id — that's how the UI knows to
        update the same callout)
      - a tool_use_id → step_id index (for the result lookup)
      - the accumulated assistant text (for the final task.complete)
      - the resolved session_id (informational; we don't reuse it because
        the bridge maintains its own chat history per v0.2.2)
    """

    # Verbosity values must match the validator in config.py.
    QUIET = "quiet"
    NORMAL = "normal"
    VERBOSE = "verbose"

    def __init__(self, task_id: str, verbosity: str = NORMAL):
        self.task_id = task_id
        self.verbosity = verbosity
        self._seq = 0
        self._next_step_n = 1
        self._tool_use_to_step: dict[str, str] = {}
        self._accumulated_text: list[str] = []
        self._final_result_text: Optional[str] = None
        self.session_id: Optional[str] = None
        self.is_error: bool = False
        self.error_message: Optional[str] = None

    # -- helpers --------------------------------------------------------

    def _next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s

    def _next_step_id(self) -> str:
        s = self._next_step_n
        self._next_step_n += 1
        return f"step-{s}"

    def _data_part(
        self,
        component: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "type": "data",
            "component": component,
            "action": action,
            "sequence": self._next_seq(),
            "payload": payload,
            "metadata": {"stream_id": self.task_id},
        }

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
        # rate_limit_event and other unknown types are deliberately ignored.
        return []

    def final_text(self) -> str:
        """The full accumulated assistant text — what the bridge sends in
        the terminal task.complete frame."""
        # Prefer the result.result field if Claude Code surfaced one,
        # otherwise stitch together what we accumulated from text blocks.
        if self._final_result_text is not None:
            return self._final_result_text
        return "".join(self._accumulated_text).strip()

    # -- per-event handlers --------------------------------------------

    def _consume_system(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        if event.get("subtype") != "init":
            return []
        self.session_id = event.get("session_id")
        # One workflow envelope per task, so the UI groups all the steps
        # under one fold-able callout. Matches OpenClaw's "Agent Delegations"
        # envelope.
        return [self._data_part(
            component="workflow",
            action="start",
            payload={
                "workflow_id": self.task_id,
                "title": "Working…",
            },
        )]

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
                step_id = self._next_step_id()
                if tool_id:
                    self._tool_use_to_step[tool_id] = step_id
                out.append(self._data_part(
                    component="workflow_step",
                    action="tool_call",
                    payload={
                        "step_id": step_id,
                        "status": "working",
                        "tool_name": tool_name,
                        "input_summary": summarize_tool_input(tool_name, tool_input),
                    },
                ))

            elif btype == "thinking":
                if self.verbosity == self.QUIET:
                    continue
                text = str(block.get("text") or "")
                summary = _first_sentence(text, _THINKING_SUMMARY_CHARS)
                step_id = self._next_step_id()
                out.append(self._data_part(
                    component="workflow_step",
                    action="thinking",
                    payload={
                        "step_id": step_id,
                        "status": "completed",
                        "summary": summary,
                    },
                ))

            elif btype == "text":
                text = str(block.get("text") or "")
                if text:
                    self._accumulated_text.append(text)
                # In verbose mode we also emit a text_delta DataPart so the
                # UI can stream the partial answer instead of waiting for
                # the final task.complete. Useful for very long responses.
                if self.verbosity == self.VERBOSE and text:
                    out.append(self._data_part(
                        component="workflow_step",
                        action="text_delta",
                        payload={
                            "step_id": self._next_step_id(),
                            "status": "completed",
                            "text": _truncate(text, 1000),
                        },
                    ))
            # All other content-block types (e.g. tool_result inside a
            # malformed assistant message) are silently ignored.
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
            step_id = self._tool_use_to_step.get(tool_use_id)
            if step_id is None:
                # tool_result we never saw a tool_use for — shouldn't
                # happen in practice; allocate a fresh step_id so we can
                # still surface it as a standalone result.
                step_id = self._next_step_id()
            is_error = bool(block.get("is_error"))
            output_summary = summarize_tool_result(block.get("content"), is_error)
            out.append(self._data_part(
                component="workflow_step",
                action="tool_result",
                payload={
                    "step_id": step_id,
                    "status": "failed" if is_error else "completed",
                    "output_summary": output_summary,
                },
            ))
        return out

    def _consume_result(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        subtype = event.get("subtype") or ""
        # Claude Code's `result.result` is the authoritative final text.
        # Prefer it over our incremental accumulation if available.
        result_text = event.get("result")
        if isinstance(result_text, str) and result_text:
            self._final_result_text = result_text
        if subtype != "success" or event.get("is_error"):
            self.is_error = True
            self.error_message = (
                event.get("error") or result_text or "Claude Code exited with an error"
            )
        # The result event itself triggers the bridge's task.complete frame,
        # so no DataPart is emitted here.
        return []
