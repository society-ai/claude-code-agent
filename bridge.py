"""Society AI Bridge — connects Claude Code to Society AI via WebSocket.

Receives task assignments and chat messages from Society AI, spawns Claude Code
CLI sessions to handle them, and reports results back.

Usage:
    python bridge.py

Env vars (see config.py for full validation):
    SOCIETY_AI_AUTH_TOKEN  — Your Society AI API key (required)
    AGENT_ROUTER_API_URL  — API URL (default: https://api.societyai.com)
    AGENT_NAME            — Agent name (default: claude-code)
    COMPANY_ID            — Default company UUID (optional)
    WORK_DIR              — Working directory for Claude Code (default: cwd)
    MAX_CONCURRENT_TASKS  — Max parallel tasks (default: 3)
    EXECUTION_MODE        — "standard" (direct) or "secured" (OpenShell sandbox)
    SANDBOX_NAME          — Sandbox name (default: society-ai-agent)
    SANDBOX_TIMEOUT       — Per-task timeout in seconds (default: 600)
    MAX_RESULT_CHARS      — Result truncation cap (default: 16000)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import ssl
import subprocess
import sys
import threading
import time
import uuid

import certifi
import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

import bridge_ipc
from dataclasses import dataclass, field
from config import (
    AGENT_ROUTER_API_URL,
    SOCIETY_AI_AUTH_TOKEN,
    AGENT_NAME,
    COMPANY_ID,
    API_HEADERS,
    EXECUTION_MODE,
    EXTRA_DIRS,
    MIRROR_LEVEL,
    MIRROR_SESSIONS,
    SANDBOX_NAME,
    SANDBOX_TIMEOUT,
    SESSION_MODE,
    SOCIETY_AI_BRIDGE_SOCKET,
    STATUS_VERBOSITY,
    __version__,
    ws_url,
)
from streaming import StreamMapper
from typing import Any, Awaitable, Callable, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bridge")

WORK_DIR = os.getenv("WORK_DIR", os.getcwd())
HEARTBEAT_INTERVAL = 60  # seconds (hub timeout is 90s)
MAX_RECONNECT_DELAY = 60
MAX_CONCURRENT_TASKS = max(1, int(os.getenv("MAX_CONCURRENT_TASKS", "3")))
MAX_TRACKED_SESSIONS = 1000  # cap history/lock maps to prevent memory leak
MAX_CHAT_HISTORY_TURNS = 10  # how many (user, assistant) pairs we keep per task
MAX_HISTORY_CHARS_PER_TURN = 8000  # per-side trim before stuffing into the next prompt
MAX_RESULT_CHARS = max(1000, int(os.getenv("MAX_RESULT_CHARS", "16000")))
TOOL_CONCURRENCY_RETRIES = 5  # retries when Claude API returns 400 for tool concurrency
TOOL_CONCURRENCY_BASE_DELAY = 3  # seconds between retries
JWT_EXCHANGE_TIMEOUT = 15
HTTP_FETCH_TIMEOUT = 30
# How much of a session transcript to scan when confirming channel delivery.
# The event we are looking for was pushed seconds ago, so it is always in the
# tail; a resumed session's full transcript can be megabytes.
_ACK_TAIL_BYTES = 256 * 1024
# Registration rejections that retrying can never clear. Anything else (a
# stale connection during a restart, a registry lookup that failed closed) is
# transient and worth backing off into. Matched case-insensitively on the
# hub's error string.
TERMINAL_REGISTRATION_ERRORS = (
    "owned by another user",
    "routing id mismatch",
)

# UUID pattern for input validation
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# -- Runtime identity ---------------------------------------------------------
#
# Two version axes travel with every registration, and they are easy to
# confuse:
#   framework / framework_version — the software we run *on top of*, i.e. the
#       Claude Code CLI installed on this machine (e.g. "2.1.220"). It changes
#       whenever the user updates the CLI, independently of this package.
#   adapter_version — the version of *this* integration package (config
#       __version__), i.e. the bridge code speaking to the hub.
# Both fields are optional on the wire: older hubs simply ignore unknown
# params, so nothing needs to negotiate.

FRAMEWORK = "claude_code"

CLAUDE_VERSION_TIMEOUT = 2  # seconds — `claude --version` must never stall us

# "2.1.220 (Claude Code)" / "2.1.220" -> "2.1.220"
_CLI_VERSION_RE = re.compile(r"\b(\d+\.\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.]+)?)\b")

_claude_cli_version: str | None = None
_claude_cli_version_probed = False
_claude_cli_version_lock = threading.Lock()


def parse_claude_version(output: str | None) -> str | None:
    """Extract the bare version token from `claude --version` output.

    Returns None for empty or unrecognized output rather than guessing.
    """
    if not output:
        return None
    match = _CLI_VERSION_RE.search(output.strip())
    return match.group(1) if match else None


def claude_cli_version() -> str | None:
    """Detected Claude Code CLI version, or None if it can't be determined.

    Probed at most once per process (the answer — including "unknown" — is
    cached), because registration happens on every reconnect and must never
    pay for a subprocess. Never raises: a missing binary, a non-zero exit,
    unexpected output or a hung process all resolve to None so that
    registration proceeds regardless.
    """
    global _claude_cli_version, _claude_cli_version_probed
    with _claude_cli_version_lock:
        if _claude_cli_version_probed:
            return _claude_cli_version
        _claude_cli_version_probed = True
        try:
            proc = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=CLAUDE_VERSION_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as e:
            # Binary missing, not executable, or timed out.
            logger.debug("Could not run `claude --version`: %s", e)
            return None
        if proc.returncode != 0:
            logger.debug(
                "`claude --version` exited %s: %s",
                proc.returncode,
                (proc.stderr or "").strip()[:200],
            )
            return None
        version = parse_claude_version(proc.stdout)
        if not version:
            logger.warning(
                "Unrecognized `claude --version` output: %r", (proc.stdout or "")[:200]
            )
            return None
        _claude_cli_version = version
        logger.debug("Claude Code CLI version: %s", version)
        return version


async def claude_cli_version_async() -> str | None:
    """Async wrapper — offloads the one-time probe so the event loop never
    blocks on the subprocess. Cache hits return without touching a thread."""
    if _claude_cli_version_probed:
        return _claude_cli_version
    try:
        return await asyncio.to_thread(claude_cli_version)
    except Exception as e:  # never let version detection break registration
        logger.debug("Claude Code version detection failed: %s", e)
        return None


# -- JWT token exchange -------------------------------------------------------

async def exchange_api_key_for_jwt(api_key: str, api_url: str = AGENT_ROUTER_API_URL) -> str:
    """Exchange a Society AI API key (sai_...) for a short-lived WS JWT.

    POST /auth/agent-token with {"api_key": "<key>"}
    Returns JWT valid for ~15 min. `api_url` is the backend the key was
    minted for — per-agent in a harness, never assumed from process env.

    Raises:
        AuthError on 401/403 (caller should not retry — bad credentials).
        httpx.HTTPError on transient failures (caller may retry with backoff).
    """
    url = f"{api_url}/auth/agent-token"
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    async with httpx.AsyncClient(timeout=JWT_EXCHANGE_TIMEOUT, verify=ssl_ctx) as client:
        resp = await client.post(url, json={"api_key": api_key})

    if resp.status_code in (401, 403):
        raise AuthError(
            f"Auth failed exchanging API key (HTTP {resp.status_code}). "
            f"Check that SOCIETY_AI_AUTH_TOKEN is a valid sai_... key. "
            f"Response: {resp.text[:200]}"
        )
    resp.raise_for_status()

    try:
        data = resp.json()
    except ValueError as e:
        raise RuntimeError(f"JWT exchange returned non-JSON response: {resp.text[:200]}") from e

    token = data.get("token") if isinstance(data, dict) else None
    if not token or not isinstance(token, str):
        raise RuntimeError(f"JWT exchange response missing 'token' field: {data}")

    expires_in = data.get("expires_in", 900) if isinstance(data, dict) else 900
    logger.info("JWT obtained (expires in %ds)", expires_in)
    return token


class AuthError(Exception):
    """Raised when API key authentication fails. Not retryable."""


# -- HTTP client for fetching task details ------------------------------------

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        _http_client = httpx.AsyncClient(timeout=HTTP_FETCH_TIMEOUT, verify=ssl_ctx)
    return _http_client


async def _close_http_client() -> None:
    global _http_client
    if _http_client and not _http_client.is_closed:
        try:
            await _http_client.aclose()
        except Exception as e:
            logger.warning("Error closing HTTP client: %s", e)
    _http_client = None


async def fetch_task_details(company_id: str, task_id: str) -> dict | None:
    """Fetch full task details from Society AI API."""
    if not _UUID_RE.match(company_id) or not _UUID_RE.match(task_id):
        logger.error("Invalid UUID in fetch_task_details: company=%s, task=%s", company_id, task_id)
        return None
    url = f"{AGENT_ROUTER_API_URL}/api/v1/companies/{company_id}/tasks/{task_id}"
    try:
        resp = await _get_http_client().get(url, headers=API_HEADERS)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to fetch task %s: %s", task_id, e)
        return None


async def fetch_company_details(company_id: str) -> dict | None:
    """Fetch company context from Society AI API."""
    if not _UUID_RE.match(company_id):
        logger.error("Invalid UUID in fetch_company_details: %s", company_id)
        return None
    url = f"{AGENT_ROUTER_API_URL}/api/v1/companies/{company_id}"
    try:
        resp = await _get_http_client().get(url, headers=API_HEADERS)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("Failed to fetch company %s: %s", company_id, e)
        return None


# -- Claude Code process spawner ----------------------------------------------

def build_prompt(company: dict, task: dict, sandboxed: bool = False) -> str:
    """Build the prompt for a Claude Code session.

    Args:
        company: Company context dict.
        task: Task dict.
        sandboxed: If True, omit references to the user's local codebase since
            in secured mode the sandbox does not have access to it.
    """
    company_name = company.get("name", "Unknown")
    mission = company.get("mission", "")
    goals = company.get("goals", [])

    task_id = task.get("id", "")
    identifier = task.get("identifier", "")
    title = task.get("title", "")
    description = task.get("description", "")
    acceptance = task.get("acceptanceCriteria") or task.get("acceptance_criteria") or []
    company_id = task.get("companyId") or task.get("company_id") or ""

    goals_str = "\n".join(f"  - {g}" for g in goals) if goals else "  (none)"
    criteria_str = "\n".join(f"  - {c}" for c in acceptance) if acceptance else "  (none specified)"

    if sandboxed:
        env_note = (
            "You are running inside an isolated sandbox. You do NOT have access to the user's local files. "
            "Use the Society AI MCP tools (and any cloud APIs you've been granted) to do your work."
        )
    else:
        env_note = (
            "You are running on the user's machine in their working directory. "
            "You may read and modify files there as part of doing the work."
        )

    return f"""You are working as an agent in a Society AI company.

Company: {company_name}
Mission: {mission}
Goals:
{goals_str}

Task: {identifier} — {title}
Task ID: {task_id}
Company ID: {company_id}
Description: {description}
Acceptance Criteria:
{criteria_str}

Environment: {env_note}

Instructions:
1. First, update the task status to "in_progress" using the update_task tool.
2. Do the work — read/write files (standard mode), call MCP tools, run tests, etc.
3. When done, update the task with your result using update_task (status: "in_review", result: "<summary of what you did>").
4. If you're blocked and need input, update status to "blocked" with a blocked_reason, and send an inbox item (type: "input-required") explaining what you need.
5. If you want to report progress, use send_inbox_item (type: "status-update").

Do the work, then report your results."""


async def stream_claude_code(
    prompt: str,
    on_event: "Callable[[dict], Awaitable[None]]",
    *,
    work_dir: str | None = None,
    extra_dirs: list[str] | None = None,
) -> tuple[int, str, str | None, bool]:
    # Per-agent dirs when called from a harness runner; fall back to the
    # process globals for the single-agent / legacy case.
    work_dir = work_dir or WORK_DIR
    if extra_dirs is None:
        extra_dirs = EXTRA_DIRS
    """Spawn `claude -p --output-format stream-json` and forward each
    parsed event to `on_event` as it arrives.

    Returns (exit_code, accumulated_text, session_id, had_error).
    `accumulated_text` is the assistant's final response text — sourced
    from `result.result` if Claude Code surfaced one, else stitched
    together from text content blocks. `had_error` is True if the
    `result` event reported `subtype != "success"` or `is_error: true`.

    This is the production path used by both the chat flow and the
    AgentOrg-task flow. We don't use `--resume` because Claude Code's
    session storage doesn't preserve thinking blocks byte-for-byte
    across replay (see v0.2.2 hotfix for details); instead the bridge
    inlines prior turns into the prompt itself.
    """
    cmd = [
        "claude",
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",  # stream-json requires --verbose to actually emit each event
        # Headless mode: no human at the terminal to press 'y' on each
        # tool-use approval prompt, so MCP tools and Bash invocations
        # silently fail without this. Trust boundary is the same as
        # running `claude -p` yourself interactively — only the agent's
        # owner can deliver tasks (WS hub enforces creator_id match),
        # and file ops are still scoped by WORK_DIR + EXTRA_DIRS.
        "--permission-mode", "bypassPermissions",
    ]
    for extra_dir in extra_dirs:
        cmd.extend(["--add-dir", extra_dir])

    logger.info("Spawning Claude Code (streaming) in %s", work_dir)
    # asyncio.StreamReader's default line buffer is 64 KiB. Claude Code's
    # stream-json events can blow past that for a single line — a tool_result
    # carrying a 50 KB file becomes >80 KB after JSON escaping, and a Read on
    # a longer file blows it out entirely. Hitting the limit raises
    # LimitOverrunError mid-stream and the whole task fails. Bump it to 16
    # MiB so any reasonable single event fits in one readline().
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=work_dir,
        limit=16 * 1024 * 1024,
    )

    accumulated_text: list[str] = []
    final_result_text: str | None = None
    session_id: str | None = None
    had_error = False

    assert process.stdout is not None
    async for raw in process.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Non-JSON stream line: %s", line[:200])
            continue

        # Surface to the bridge first — the caller's mapper builds DataParts
        # and sends them onward as task.status frames.
        try:
            await on_event(event)
        except Exception:
            # Forwarding shouldn't kill the run; the final task.complete
            # still goes out at the end.
            logger.exception("on_event handler raised; continuing stream")

        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            session_id = event.get("session_id")
        elif etype == "assistant":
            for block in (event.get("message") or {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text") or ""
                    if text:
                        accumulated_text.append(text)
        elif etype == "result":
            result_text = event.get("result")
            if isinstance(result_text, str) and result_text:
                final_result_text = result_text
            if event.get("subtype") != "success" or event.get("is_error"):
                had_error = True

    # Drain stderr (for diagnostics only — never re-sent to the hub).
    try:
        stderr = await process.stderr.read() if process.stderr else b""
    except Exception:
        stderr = b""
    await process.wait()
    exit_code = process.returncode or 0

    if stderr:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if stderr_text:
            logger.warning("Claude Code stderr: %s", stderr_text[:1000])

    output_text = final_result_text if final_result_text is not None else "".join(accumulated_text).strip()

    if exit_code != 0 or had_error:
        logger.error("Claude Code FAILED (exit=%d, had_error=%s): %s",
                     exit_code, had_error, (output_text or "")[:500])
    else:
        logger.info("Claude Code OK (session=%s, len=%d)",
                    session_id, len(output_text or ""))

    return exit_code, output_text, session_id, had_error


async def run_claude_code(
    prompt: str,
    session_id: str | None = None,
) -> tuple[int, str, str | None]:
    """Legacy non-streaming runner — kept for the secured-mode sandbox path
    until v0.4.x extends streaming into that flow too. Standard mode uses
    `stream_claude_code` instead.

    Retries automatically on tool-use concurrency errors (HTTP 400) which
    happen when another Claude Code session is actively using tools.
    """
    # Initialize before loop so we can always return safely, even if the
    # retry loop is configured to zero iterations.
    exit_code = 1
    output_text = ""
    returned_session_id = session_id

    for attempt in range(1, TOOL_CONCURRENCY_RETRIES + 1):
        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "json",
            "--permission-mode", "bypassPermissions",
        ]
        # Expand Claude Code's per-cwd file sandbox to include any directories
        # the user opted into via the EXTRA_DIRS env var. Each path becomes a
        # `--add-dir <path>` flag. Validation already happened at config load.
        for extra_dir in EXTRA_DIRS:
            cmd.extend(["--add-dir", extra_dir])
        if session_id:
            cmd.extend(["--resume", session_id])

        logger.info(
            "Spawning Claude Code in %s (session=%s, attempt=%d)",
            WORK_DIR, session_id or "new", attempt,
        )
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORK_DIR,
        )

        stdout, stderr = await process.communicate()
        exit_code = process.returncode or 0
        raw_output = stdout.decode("utf-8", errors="replace")

        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace")
            if stderr_text.strip():
                logger.warning("Claude Code stderr: %s", stderr_text[:1000])

        # Parse JSON output to extract result and session_id
        output_text = raw_output
        returned_session_id = session_id
        try:
            data = json.loads(raw_output)
            if isinstance(data, dict):
                output_text = data.get("result", raw_output)
                returned_session_id = data.get("session_id", session_id)
        except (json.JSONDecodeError, TypeError):
            pass  # Fall back to raw output

        # Retry on tool-use concurrency error
        if exit_code != 0 and "concurrency" in (output_text or "").lower():
            if attempt < TOOL_CONCURRENCY_RETRIES:
                delay = TOOL_CONCURRENCY_BASE_DELAY * attempt
                logger.warning(
                    "Tool concurrency conflict, retrying in %ds (attempt %d/%d)",
                    delay, attempt, TOOL_CONCURRENCY_RETRIES,
                )
                await asyncio.sleep(delay)
                continue
            else:
                logger.error(
                    "Tool concurrency conflict persisted after %d attempts",
                    TOOL_CONCURRENCY_RETRIES,
                )

        if exit_code != 0:
            logger.error("Claude Code FAILED (exit=%d): %s", exit_code, (output_text or "")[:500])
        else:
            logger.info("Claude Code OK (session=%s, len=%d)", returned_session_id, len(output_text or ""))
        return exit_code, output_text, returned_session_id

    return exit_code, output_text, returned_session_id


# -- WebSocket Bridge ---------------------------------------------------------

@dataclass
class AgentContext:
    """Everything that makes a bridge runner act AS one specific agent.

    Single-agent: built from the process env (`from_env`). Harness: each
    agent on the machine gets its own, built from its `.env` file. This is
    the unit that decouples 'which agent' (token + identity + local file
    access) from the shared execution machinery.
    """
    name: str
    token: str
    work_dir: str
    extra_dirs: list[str]
    company_id: str
    api_url: str       # the Society AI backend this agent belongs to
    socket: str        # this agent's IPC socket path
    state_dir: str     # shipper state + channel sock live here (= dirname(socket))

    @classmethod
    def from_env(cls) -> "AgentContext":
        """The single agent defined by the process environment (the default
        persona / back-compat path)."""
        return cls(
            name=AGENT_NAME,
            token=SOCIETY_AI_AUTH_TOKEN,
            work_dir=WORK_DIR,
            extra_dirs=list(EXTRA_DIRS),
            company_id=COMPANY_ID,
            api_url=AGENT_ROUTER_API_URL,
            socket=SOCIETY_AI_BRIDGE_SOCKET,
            state_dir=os.path.dirname(SOCIETY_AI_BRIDGE_SOCKET) or ".",
        )

    def session_env(self) -> dict:
        """Env injected into each spawned `claude` so its society-ai MCP acts
        as this agent (not the process default). Used by the harness."""
        return {
            "SOCIETY_AI_AUTH_TOKEN": self.token,
            "AGENT_NAME": self.name,
            "COMPANY_ID": self.company_id or "",
            # Explicit, not inherited: a session must hit the same backend
            # its token was minted for even if the launching process's env
            # says otherwise.
            "AGENT_ROUTER_API_URL": self.api_url,
            "SOCIETY_AI_BRIDGE_SOCKET": self.socket,
        }


class Bridge:
    def __init__(self, ctx: "AgentContext | None" = None, scheduler: "asyncio.Semaphore | None" = None):
        # ctx carries this runner's agent identity + local file access.
        # Default to the process env so single-agent / existing callers work
        # unchanged. `scheduler` is the SHARED machine-wide concurrency gate
        # when running under a harness; standalone gets its own.
        self.ctx = ctx or AgentContext.from_env()
        self.ws = None
        self.registered = False
        # Whether the current (or most recent) connection ever got past
        # registration. Drives the reconnect backoff reset; see run().
        self._conn_registered = False
        self.running = True
        self._active_tasks: set[str] = set()
        # Self-update (agent.update RPC). _inflight_dispatches counts
        # task.execute dispatches from acceptance (synchronously, in
        # handle_message) to completion — unlike _active_tasks it also
        # covers dispatches still queued on the semaphore, so an update
        # can never slip in between accepting a task and starting it.
        # _pending_update holds the approved update while work is in
        # flight; the last dispatch to finish starts it.
        self._inflight_dispatches = 0
        self._pending_update: dict | None = None
        # Shared scheduler = machine-wide concurrency cap across ALL agents in
        # a harness. Standalone falls back to a private per-agent cap.
        self._semaphore = scheduler or asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        self._msg_counter = 0
        self._ws_jwt: str | None = None
        # Per-task chat conversation history. Each entry is a list of
        # {"user": str, "assistant": str} dicts. We maintain history ourselves
        # and inline it into the next prompt rather than using `claude --resume`,
        # because resuming a session that contains extended-thinking blocks
        # trips the Anthropic API's "thinking blocks cannot be modified"
        # integrity check on multi-turn replay.
        self._chat_history: dict[str, list[dict[str, str]]] = {}
        # Per-task lock to serialize messages in the same conversation
        self._task_locks: dict[str, asyncio.Lock] = {}

        # Outbound JSON-RPC correlation tables, used by the IPC handlers
        # for search_agents and delegate_task.
        # _pending_requests:    req_id -> Future, resolved when the hub
        #                       sends back a regular {"id": req_id, "result": ...}
        # _pending_delegations: req_id -> Future, resolved when the hub
        #                       sends a `delegation.result` notification
        #                       carrying params.original_id == req_id.
        # Both maps use string keys per the connector convention.
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._pending_delegations: dict[str, asyncio.Future] = {}
        self._ipc_msg_counter = 0

        # Session mode (v0.7): persistent interactive sessions per work item.
        # Hub + manager are created here; started in run(). Standard mode only.
        self._session_mode = SESSION_MODE and EXECUTION_MODE != "secured"
        self._channel_hub = None
        self._session_mgr = None
        self._shipper = None  # transcript mirroring; set below in session mode
        if self._session_mode:
            from channel_hub import ChannelHub
            from session_manager import SessionManager
            from policy import default_policy, apply_local_env
            # Channel hub socket lives beside this agent's IPC socket.
            hub_sock = os.path.join(self.ctx.state_dir, "channels.sock")
            self._channel_hub = ChannelHub(hub_sock)
            self._session_mgr = SessionManager(hub_sock)
            # Register this agent's policy (defaults + local env override now;
            # platform-fetched config is overlaid in run() once connected).
            # session_env makes every spawned claude act as THIS agent — the
            # critical bit when many agents share one harness process.
            pol = default_policy(self.ctx.name, self.ctx.work_dir, list(self.ctx.extra_dirs))
            apply_local_env(pol, self.ctx.name)
            pol.session_env = self.ctx.session_env()
            self._session_mgr.set_policy(pol)
            # event_id -> future awaiting the session's response.
            self._pending_session_tasks: dict[str, str] = {}
            # claude session_id -> {task_id, cwd} for a dispatch whose turn
            # has not ended yet. The Stop hook is what closes these out.
            self._session_awaiting: dict[str, dict] = {}
            logger.info("SESSION_MODE enabled: work runs in persistent sessions")

            # Transcript mirroring (recorded workspace). Ships per-turn JSONL
            # deltas of bridge-launched sessions to the platform. MIRROR=false
            # disables it; spawn mode never mirrors.
            if MIRROR_SESSIONS:
                from transcript_shipper import TranscriptShipper
                # ctx.api_url, NOT the module-level default: the shipper must
                # hit the SAME backend this agent's token was minted for. In a
                # harness the process env has no AGENT_ROUTER_API_URL, and the
                # module default (prod) would ship a local agent's transcripts
                # to the wrong environment.
                self._shipper = TranscriptShipper(
                    self.ctx.api_url,
                    self.ctx.token,
                    state_dir=self.ctx.state_dir,
                    level=MIRROR_LEVEL,
                )
                self._session_mgr.on_reap = self._on_session_reap
                self._shipper.on_chat_link = self._on_mirror_chat_link
                logger.info(
                    "Session mirroring enabled (level=%s; MIRROR=false to disable)",
                    MIRROR_LEVEL,
                )

        # Sandbox executor for secured mode
        self._sandbox = None
        if EXECUTION_MODE == "secured":
            try:
                from sandbox import SandboxManager
            except ImportError:
                logger.error("EXECUTION_MODE=secured requires sandbox.py. Check your installation.")
                sys.exit(1)
            from config import ENABLE_AGENT_LIFECYCLE
            sandbox_env = {
                "SOCIETY_AI_AUTH_TOKEN": self.ctx.token,
                # Per-agent URL: the sandboxed session must hit the same
                # backend its token was minted for (matches session_env()).
                "AGENT_ROUTER_API_URL": self.ctx.api_url,
                "AGENT_NAME": self.ctx.name,
                "COMPANY_ID": self.ctx.company_id,
            }
            if ENABLE_AGENT_LIFECYCLE:
                sandbox_env["ENABLE_AGENT_LIFECYCLE"] = "true"
            self._sandbox = SandboxManager(
                work_dir=self.ctx.work_dir,
                env_vars=sandbox_env,
                sandbox_name=SANDBOX_NAME,
                timeout=SANDBOX_TIMEOUT,
            )
            logger.info("SECURED mode: Claude Code will run inside OpenShell sandbox")
        else:
            logger.info("STANDARD mode: Claude Code will run directly on host (no sandbox)")

    def _next_id(self) -> str:
        self._msg_counter += 1
        return f"msg-{self._msg_counter}"

    async def send(self, message: dict):
        """Send a JSON-RPC message."""
        if self.ws:
            await self.ws.send(json.dumps(message))

    async def send_rpc(self, method: str, params: dict, msg_id: str | None = None) -> None:
        """Send a JSON-RPC request."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if msg_id:
            msg["id"] = msg_id
        await self.send(msg)

    # -- Registration --------------------------------------------------------

    async def obtain_jwt(self):
        """Exchange API key for a short-lived WS JWT.

        Called once per (re)connect — the JWT is only used at register time;
        the hub does not re-validate it for the lifetime of the WS connection,
        so we don't need a proactive refresh loop. If the connection drops,
        the next reconnect attempt fetches a fresh JWT.
        """
        self._ws_jwt = await exchange_api_key_for_jwt(self.ctx.token, self.ctx.api_url)

    async def register(self):
        """Register this agent with the hub using the WS JWT."""
        if not self._ws_jwt:
            await self.obtain_jwt()
        # framework/framework_version describe the runtime we sit on top of
        # (the Claude Code CLI); adapter_version is this package. See the
        # "Runtime identity" block near the top for why both exist.
        params = {
            "agent_name": self.ctx.name,
            "auth_token": self._ws_jwt,
            "visibility": "private",
            "framework": FRAMEWORK,
            "adapter_version": __version__,
        }
        framework_version = await claude_cli_version_async()
        if framework_version:
            # Omitted entirely when undetectable — never send an empty string.
            params["framework_version"] = framework_version
        await self.send_rpc("agent.register", params, msg_id=self._next_id())

    # -- Heartbeat -----------------------------------------------------------

    async def heartbeat_loop(self):
        """Send heartbeats to keep the connection alive."""
        while self.running:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if self.ws and self.registered:
                try:
                    await self.send_rpc("heartbeat", {})
                    logger.debug("Heartbeat sent")
                except Exception as e:
                    logger.warning("Heartbeat failed: %s", e)
                    break

    def _next_ipc_id(self) -> str:
        """Generate a unique JSON-RPC id for IPC-initiated outbound requests."""
        self._ipc_msg_counter += 1
        return f"ipc-{os.getpid()}-{self._ipc_msg_counter}"

    def _resolve_pending(self, mapping: dict[str, asyncio.Future], key: str | None, value: Any) -> bool:
        """If key is registered, resolve its Future with value. Returns True if resolved."""
        if not key:
            return False
        fut = mapping.get(key)
        if fut is not None and not fut.done():
            fut.set_result(value)
            return True
        return False

    def _abort_all_pending(self, reason: str) -> None:
        """Resolve all pending IPC futures with an error — used on WS disconnect."""
        err = {"error": True, "message": reason}
        for mapping in (self._pending_requests, self._pending_delegations):
            for key, fut in list(mapping.items()):
                if not fut.done():
                    fut.set_result(err)
                mapping.pop(key, None)

    # -- Message handling ----------------------------------------------------

    async def handle_message(self, raw: str):
        """Route an incoming JSON-RPC message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON: %s", raw[:200])
            return
        if not isinstance(msg, dict):
            logger.warning("Non-object JSON-RPC frame: %s", str(raw)[:200])
            return

        msg_id_raw = msg.get("id")
        # Hub may send numeric ids; we always use string keys in our maps.
        msg_id = str(msg_id_raw) if msg_id_raw is not None else None

        # --- Responses to requests we sent --------------------------------
        if "result" in msg or "error" in msg:
            err = msg.get("error")
            result = msg.get("result")

            # If this id matches a pending IPC request, resolve it.
            payload = ({"error": True, "message": str(err)} if err is not None else result)
            if self._resolve_pending(self._pending_requests, msg_id, payload):
                return

            # Otherwise — legacy handling for the registration ack.
            if err is not None:
                logger.error("RPC error: %s", err)
                return
            if isinstance(result, dict) and "registered" in result:
                if result["registered"]:
                    self.registered = True
                    # Registration, not the socket handshake, is what makes
                    # this connection usable. The reconnect backoff resets
                    # here so that a hub which accepts the socket and then
                    # rejects us cannot hold the delay at its floor forever.
                    self._conn_registered = True
                    logger.info("Registered as %s", result.get("agent_id", self.ctx.name))
                else:
                    reason = str(result.get("error") or "")
                    if any(t in reason.lower() for t in TERMINAL_REGISTRATION_ERRORS):
                        # Nothing about this clears on its own. Retrying is a
                        # hot loop against the hub that can never succeed, so
                        # stop and leave the reason where the owner sees it.
                        logger.error(
                            "Registration refused permanently (%s). This will not "
                            "resolve by retrying; the agent's owner or routing id "
                            "has to be corrected on the platform. Stopping.",
                            reason,
                        )
                        self.running = False
                        try:
                            if self.ws:
                                await self.ws.close(code=1000, reason="registration refused")
                        except Exception:
                            pass
                        return
                    # Transient reject (commonly "already connected" during a
                    # restart or harness cutover, while the hub still holds
                    # this agent's prior connection): drop and let the
                    # reconnect loop retry, now with real backoff behind it.
                    # The stale connection clears on the hub's heartbeat
                    # timeout, after which re-registration succeeds.
                    logger.error(
                        "Registration failed (%s) — reconnecting to retry",
                        reason,
                    )
                    await asyncio.sleep(5)
                    try:
                        if self.ws:
                            await self.ws.close(code=1012, reason="re-register")
                    except Exception:
                        pass
            return

        # --- Notifications / inbound requests -----------------------------
        method = msg.get("method")
        params = msg.get("params", {}) or {}

        if method == "connection.established":
            logger.info("Connected: %s", params.get("connection_id"))
            await self.register()

        elif method == "heartbeat_ack":
            logger.debug("Heartbeat ack received")

        elif method == "task.execute":
            # Counted here (synchronously) rather than inside the task so a
            # just-accepted dispatch already defers an agent.update that
            # arrives on the very next frame.
            self._inflight_dispatches += 1
            asyncio.create_task(self.handle_task_execute(msg_id, params))

        elif method == "agent.update":
            await self.handle_agent_update(msg_id, params)

        elif method == "delegation.result":
            # Two-phase delegation: the hub correlates the result back to
            # our original agent.send_task via `original_id`.
            original_id_raw = params.get("original_id") if isinstance(params, dict) else None
            original_id = str(original_id_raw) if original_id_raw is not None else None
            if not self._resolve_pending(self._pending_delegations, original_id, params):
                logger.debug("delegation.result for unknown original_id=%s", original_id)

        else:
            logger.debug("Unhandled method: %s", method)

    # -- Outbound IPC handlers (used by the bridge IPC server) -------------

    async def ipc_search_agents(self, params: dict) -> dict:
        """Run an `agent.search` JSON-RPC request over our WS connection.

        Called by the IPC server on behalf of the MCP server's `search_agents`
        tool. Returns either the hub's search result or a structured error.
        Hub contract: method `agent.search`, params {query, limit} — the same
        names the OpenClaw connector uses.
        """
        if not self.ws or not self.registered:
            return {"error": True, "message": "Bridge is not currently connected to the Society AI hub"}
        q = params.get("q") or params.get("query")
        if not isinstance(q, str) or not q.strip():
            return {"error": True, "message": "search_agents requires a non-empty 'q'"}
        limit = int(params.get("limit", 10))
        limit = max(1, min(50, limit))

        req_id = self._next_ipc_id()
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending_requests[req_id] = fut
        try:
            await self.send_rpc("agent.search", {"query": q.strip(), "limit": limit}, msg_id=req_id)
            try:
                result = await asyncio.wait_for(fut, timeout=30)
            except asyncio.TimeoutError:
                return {"error": True, "message": "agent.search timed out after 30s"}
            return result if isinstance(result, dict) else {"agents": result}
        finally:
            self._pending_requests.pop(req_id, None)

    async def ipc_delegate_task(self, params: dict) -> dict:
        """Run an `agent.send_task` over our WS connection and wait for
        the asynchronous `delegation.result` notification.

        Hub contract: params {agent_id, message, skill_id (required),
        session_id?, task_id?}; correlation is the request's JSON-RPC id,
        echoed back as `original_id` on the delegation.result notification.

        Implements the two-phase pattern. Phase 2 (delegation result future)
        is pre-registered BEFORE Phase 1 (request send) to avoid the race
        where the hub's notification arrives before we install the handler.
        """
        if not self.ws or not self.registered:
            return {"error": True, "message": "Bridge is not currently connected to the Society AI hub"}

        agent_name = (params.get("agent_name") or "").strip()
        message = params.get("message")
        skill_id = (params.get("skill_id") or "").strip()
        if not agent_name:
            return {"error": True, "message": "delegate_task requires 'agent_name'"}
        if not isinstance(message, str) or not message:
            return {"error": True, "message": "delegate_task requires non-empty 'message'"}
        if not skill_id:
            return {
                "error": True,
                "message": (
                    "delegate_task requires 'skill_id' — the hub bills and routes "
                    "by skill. Use search_agents first and pass the target's "
                    "best_skill_id."
                ),
            }
        timeout = int(params.get("timeout", 120))
        timeout = max(5, min(600, timeout))

        req_id = self._next_ipc_id()
        loop = asyncio.get_event_loop()

        # Phase 2 future MUST be registered before Phase 1 send.
        delegation_fut = loop.create_future()
        self._pending_delegations[req_id] = delegation_fut

        # Phase 1 ack future — gives us the task_id and lets us fail fast
        # if the hub rejects the request (bad agent, no permission, etc.).
        ack_fut = loop.create_future()
        self._pending_requests[req_id] = ack_fut

        rpc_params: dict[str, Any] = {
            "agent_id": agent_name,
            "message": message,
            "skill_id": skill_id,
        }
        if params.get("session_id"):
            rpc_params["session_id"] = params["session_id"]
        if params.get("task_id"):
            rpc_params["task_id"] = params["task_id"]

        try:
            await self.send_rpc("agent.send_task", rpc_params, msg_id=req_id)

            try:
                ack = await asyncio.wait_for(ack_fut, timeout=15)
            except asyncio.TimeoutError:
                return {"error": True, "message": "Hub did not acknowledge the delegation within 15s"}
            if isinstance(ack, dict) and ack.get("error"):
                return ack

            try:
                final = await asyncio.wait_for(delegation_fut, timeout=timeout)
            except asyncio.TimeoutError:
                return {
                    "error": True,
                    "message": f"Delegation timed out after {timeout}s",
                    "ack": ack,
                }
            if isinstance(final, dict) and final.get("error"):
                return final
            return {"ack": ack, "result": final}
        finally:
            self._pending_requests.pop(req_id, None)
            self._pending_delegations.pop(req_id, None)

    # -- Task execution ------------------------------------------------------

    async def handle_task_execute(self, msg_id: str | None, params: dict):
        """Handle a task.execute message from the hub.

        Two flows:
        1. Chat flow — direct message from a user, no metadata.company_id.
           Respond with simple text via task.complete.
        2. AgentOrg trigger flow — has metadata.company_id + agent_task_id.
           Fetch full context, spawn Claude Code with MCP tools.
        """
        task_id = params.get("id", msg_id or str(uuid.uuid4()))
        metadata = params.get("metadata", {}) or {}
        company_id = metadata.get("company_id") or self.ctx.company_id
        agent_task_id = metadata.get("agent_task_id")

        # Persona context injected by the platform on every dispatch
        # (task_manager._inject_card_instructions). agent_instructions is
        # the agent-level text authored on the agent's edit page;
        # skill_instructions arrives pre-matched as the invoked skill's
        # string (the router injects skill_instructions[skill_used], not
        # the whole dict) — tolerate non-strings from older routers.
        agent_instructions = metadata.get("agent_instructions")
        if not isinstance(agent_instructions, str):
            agent_instructions = ""
        skill_instructions = metadata.get("skill_instructions")
        if not isinstance(skill_instructions, str):
            skill_instructions = ""
        instructions_block = self._format_instructions_block(
            agent_instructions.strip(), skill_instructions.strip()
        )

        # Extract user message text
        message = params.get("message", {}) or {}
        parts = message.get("parts", []) or []
        user_text = ""
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                user_text += part.get("text", "")
            elif isinstance(part, str):
                user_text += part

        # Composed dispatch (docs/design/agent-instruction-hierarchy.md):
        # routers that run the dispatch composer send structured fields —
        # frame (authenticated facts), blocks (labeled sections), body
        # (rendered work item), next_steps, protocol. When the frame is
        # present we render fields and skip every legacy text heuristic;
        # the legacy path stays as fallback for pre-composer routers and
        # is deleted when the router's dual-emit window closes.
        frame = metadata.get("frame") if isinstance(metadata.get("frame"), dict) else None

        # Send-as-supervisor (legacy path only): the platform relays a
        # supervisor-suggested message the owner approved. Composed
        # dispatches carry this in the frame (from=supervisor) instead.
        from_supervisor = metadata.get("from_supervisor")
        if frame is None and isinstance(from_supervisor, str) and from_supervisor.strip():
            user_text = (
                f"[Message from your supervisor ({from_supervisor.strip()}) — "
                "relayed with your owner's approval. Treat it as direction "
                "from the supervisor.]\n\n" + user_text
            )

        logger.info(
            "Task received: %s (agent_task=%s, company=%s)",
            task_id, agent_task_id, company_id,
        )
        logger.info("Message: %s", user_text[:200])

        # Per-task lock: serialize messages in the same conversation so
        # appends to chat_history happen in the same order the user sent them.
        if task_id not in self._task_locks:
            self._task_locks[task_id] = asyncio.Lock()
        task_lock = self._task_locks[task_id]

        # If we're at the concurrency cap, this acquire will block. Log it
        # after we know we're queuing (i.e. acquire didn't return immediately).
        if self._semaphore.locked() or len(self._active_tasks) >= MAX_CONCURRENT_TASKS:
            logger.info(
                "At max concurrent tasks (%d), queuing task %s",
                MAX_CONCURRENT_TASKS, task_id,
            )

        async with self._semaphore:
            async with task_lock:
                self._active_tasks.add(task_id)
                try:
                    if self._session_mode:
                        # v0.7: dispatch into a persistent per-work-item session.
                        # Stable key gives task rework continuity (resume same
                        # session); chat falls back to sessionId/chat/task_id.
                        work_item_key = (
                            agent_task_id
                            or params.get("sessionId")
                            or metadata.get("chat_id")
                            or task_id
                        )
                        protocol_text = ""
                        if frame is not None:
                            # v0.10: everything derives from frame FIELDS —
                            # no text sniffing. Session kind comes from the
                            # cause: task work (assignee/reviewer) is a real
                            # work session; messages are chats; wakes, status
                            # notifications and inbox pings are automation
                            # ('trigger' keeps them out of Sessions lists and
                            # never re-wakes the supervisor).
                            cause = str(frame.get("cause") or "message")
                            role = frame.get("role")
                            body = metadata.get("body")
                            work_text = body if isinstance(body, str) and body.strip() else user_text
                            if (
                                cause == "task"
                                and agent_task_id
                                and role in ("assignee", "reviewer")
                            ):
                                kind = "task_assigned"
                            elif cause == "message":
                                kind = "chat"
                            else:
                                kind = "trigger"
                            content = self._render_composed_prompt(metadata, frame, work_text)
                            protocol_text = self._protocol_text(metadata)
                            title = self._derive_session_title(work_text)
                        else:
                            # Legacy fallback (pre-composer router): sniff
                            # mode from text, prepend primers. Delete when
                            # the router dual-emit window closes.
                            is_background = bool(agent_task_id) or user_text.lstrip().startswith("[Trigger:")
                            primer = (
                                self._bridge_context_primer_background(trigger_reason="task")
                                if is_background
                                else self._bridge_context_primer_chat(work_item_key, 0)
                            )
                            content = primer + instructions_block + user_text
                            title = self._derive_session_title(user_text)
                            stripped = user_text.lstrip()
                            if agent_task_id and (
                                stripped.startswith("[Trigger: task_assigned]")
                                or not stripped.startswith("[Trigger:")
                            ):
                                kind = "task_assigned"   # real work on the task
                            elif is_background:
                                kind = "trigger"
                            else:
                                kind = "chat"
                        await self._execute_via_session(
                            task_id, str(work_item_key), title, content, kind=kind,
                            protocol_text=protocol_text,
                        )
                    elif agent_task_id and company_id:
                        # AgentOrg trigger flow — full context + Claude Code spawn
                        await self._execute_agentorg_task(
                            task_id, company_id, agent_task_id,
                            instructions_block=instructions_block,
                        )
                    else:
                        # Chat flow OR personal-task trigger (no company_id) —
                        # direct message, respond via Claude Code. Pass
                        # agent_task_id through so the chat task can pick the
                        # right context primer (active chat vs background).
                        await self._execute_chat_task(
                            task_id, user_text, agent_task_id=agent_task_id,
                            instructions_block=instructions_block,
                        )
                except Exception as e:
                    logger.exception("Task %s failed with unhandled exception", task_id)
                    await self._send_task_complete(
                        task_id, f"Internal error: {type(e).__name__}: {e}", exit_code=1
                    )
                finally:
                    self._active_tasks.discard(task_id)
                    self._inflight_dispatches = max(0, self._inflight_dispatches - 1)
                    # A user-approved update that arrived mid-work runs once
                    # the last in-flight dispatch drains.
                    if self._pending_update and self._inflight_dispatches == 0:
                        logger.info("Last in-flight task finished; starting the deferred update")
                        self._start_pending_update()

    # -- Self-update (agent.update) ------------------------------------------

    async def handle_agent_update(self, msg_id: str | None, params: dict):
        """Handle an `agent.update` RPC from the hub (sent when the owner
        approves an update). Ack immediately, then either start the update
        now (idle) or defer it until the last in-flight dispatch finishes.

        The update itself runs in ./update.sh, spawned fully detached — the
        updater restarts (and can roll back) this very process, so none of
        that logic may live in here.
        """
        latest = str((params or {}).get("latest_version") or "").strip()
        reason = str((params or {}).get("reason") or "").strip()
        if msg_id is not None:
            await self.send({"jsonrpc": "2.0", "id": msg_id, "result": {"status": "accepted"}})
        logger.info(
            "Update requested (latest_version=%s, reason=%s)", latest or "?", reason or "?"
        )
        self._pending_update = {"latest_version": latest, "reason": reason}
        if self._inflight_dispatches > 0:
            logger.info(
                "Update deferred: %d dispatch(es) in flight; it starts when the last one finishes",
                self._inflight_dispatches,
            )
        else:
            self._start_pending_update()

    def _start_pending_update(self) -> None:
        """Spawn ./update.sh fully detached and clear the pending flag.

        Detachment (start_new_session=True → its own session + process
        group, stdio to a log file, never waited on) is what lets the
        updater outlive this bridge: the restart it performs kills our
        process group, and the updater must survive that to verify the new
        version or roll back.
        """
        update = self._pending_update
        if not update:
            return
        self._pending_update = None
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(repo_dir, "update.sh")
        persona = self._derive_persona()
        cmd = [script] + ([persona] if persona else [])
        log_path = os.path.join(self.ctx.state_dir, "update.log")
        try:
            os.makedirs(self.ctx.state_dir, exist_ok=True)
            with open(log_path, "ab") as log_file:
                subprocess.Popen(
                    cmd,
                    cwd=repo_dir,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                    close_fds=True,
                )
            logger.info(
                "Updater spawned detached (%s); progress in %s",
                " ".join(cmd), log_path,
            )
        except Exception:
            logger.exception("Failed to spawn the updater")

    def _derive_persona(self) -> str:
        """Which persona this bridge runs as ("" = the primary), derived the
        same way the launchers map personas to env files: the primary sources
        .env, a persona sources .env.<name>. We reverse that mapping by
        finding the env file whose AGENT_NAME is ours (mirrors the
        discover_roster filters for backups/special files)."""
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        primary = _read_env_file(os.path.join(repo_dir, ".env"))
        if (primary.get("AGENT_NAME") or "").strip() == self.ctx.name:
            return ""
        try:
            entries = sorted(os.listdir(repo_dir))
        except OSError:
            return ""
        for entry in entries:
            if not entry.startswith(".env.") or entry in (".env.example", ".env.defaults"):
                continue
            persona = entry[len(".env."):]
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", persona):
                continue
            if re.search(r"\.?bak-\d+$", persona):
                continue
            env = _read_env_file(os.path.join(repo_dir, entry))
            if (env.get("AGENT_NAME") or "").strip() == self.ctx.name:
                return persona
        return ""

    async def _report_update_outcome(self) -> None:
        """If update.sh left an outcome marker, post it to the owner's feed
        and delete the marker. Fire-and-forget at startup: never blocks or
        fails the boot, and the marker is only deleted after a successful
        post so a failed post retries at the next start."""
        marker = os.path.join(self.ctx.state_dir, "update-result.json")
        try:
            if not os.path.exists(marker):
                return
            try:
                with open(marker, encoding="utf-8") as f:
                    data = json.load(f)
            except (ValueError, OSError) as e:
                # Unreadable marker: nothing to report, and retrying next
                # boot won't help — drop it.
                logger.warning("Unreadable update marker %s (%s); removing it", marker, e)
                try:
                    os.remove(marker)
                except OSError:
                    pass
                return

            from_v = str(data.get("from") or "an earlier version")
            to_v = str(data.get("to") or "the new version")
            if data.get("ok"):
                message = f"Updated from {from_v} to {to_v}."
            else:
                error = str(data.get("error") or "unknown error")
                if data.get("rolled_back"):
                    message = f"Update to {to_v} failed ({error}), rolled back to {from_v}."
                else:
                    message = f"Update to {to_v} failed ({error}); still on {from_v}."

            # Same endpoint + auth the MCP post_feed tool uses
            # (POST /api/v1/feed with the agent's own token) — but sent with
            # this bridge's ctx.token, so each harness agent reports as itself.
            headers = {
                "Authorization": f"Bearer {self.ctx.token}",
                "Content-Type": "application/json",
                "User-Agent": f"claude-code-agent/{__version__}",
            }
            resp = await _get_http_client().post(
                f"{self.ctx.api_url}/api/v1/feed",
                json={"message": message},
                headers=headers,
            )
            if resp.status_code < 400:
                try:
                    os.remove(marker)
                except OSError:
                    pass
                logger.info("Update outcome posted to the feed: %s", message)
            else:
                logger.warning(
                    "Feed post for the update outcome failed (HTTP %s); retrying next start",
                    resp.status_code,
                )
        except Exception as e:
            logger.warning("Could not report the update outcome: %s", e)

    def _record_chat_turn(self, task_id: str, user_msg: str, assistant_msg: str) -> None:
        """Append a (user, assistant) pair to a task's chat history.

        Trims each side to MAX_HISTORY_CHARS_PER_TURN, caps total turns at
        MAX_CHAT_HISTORY_TURNS, and prunes oldest tasks if we cross
        MAX_TRACKED_SESSIONS distinct conversations — keeping locks in
        lockstep so the lock map can't leak past the history map.
        """
        if not assistant_msg:
            return
        history = self._chat_history.setdefault(task_id, [])
        history.append({
            "user": user_msg[:MAX_HISTORY_CHARS_PER_TURN],
            "assistant": assistant_msg[:MAX_HISTORY_CHARS_PER_TURN],
        })
        if len(history) > MAX_CHAT_HISTORY_TURNS:
            self._chat_history[task_id] = history[-MAX_CHAT_HISTORY_TURNS:]
        if len(self._chat_history) > MAX_TRACKED_SESSIONS:
            # Drop the oldest half of history AND lock entries together.
            drop = list(self._chat_history.keys())[: MAX_TRACKED_SESSIONS // 2]
            for k in drop:
                self._chat_history.pop(k, None)
                self._task_locks.pop(k, None)

    @staticmethod
    def _protocol_text(metadata: dict) -> str:
        """The L1 platform protocol from a composed dispatch ('' if absent).

        Session-scoped standing text: rendered once per fresh Claude
        session (the session's own transcript carries it across resumes),
        never re-sent into a warm session.
        """
        proto = metadata.get("protocol")
        if isinstance(proto, dict):
            text = proto.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
        return ""

    @staticmethod
    def _render_composed_prompt(metadata: dict, frame: dict, work_text: str) -> str:
        """Render a composed dispatch into the session prompt, per the
        assembly contract of the instruction-hierarchy spec:

            blocks (labeled sections) -> frame line -> body -> [Next steps]

        The protocol is NOT included here — it is prepended only on fresh
        launches (see _execute_via_session). Every field is shape-checked;
        a malformed section is dropped, never fatal.
        """
        parts: list[str] = []
        for block in metadata.get("blocks") or []:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            label = block.get("label")
            if isinstance(label, str) and label.strip():
                parts.append(f"[{label.strip()}]\n{text.strip()}")
            else:
                parts.append(text.strip())
        rendered_frame = frame.get("rendered")
        if isinstance(rendered_frame, str) and rendered_frame.strip():
            parts.append(rendered_frame.strip())
        if work_text.strip():
            parts.append(work_text.strip())
        next_steps = metadata.get("next_steps")
        if isinstance(next_steps, str) and next_steps.strip():
            parts.append("[Next steps]\n" + next_steps.strip())
        return "\n\n".join(parts)

    @staticmethod
    def _format_instructions_block(
        agent_instructions: str, skill_instructions: str
    ) -> str:
        """Format platform-authored persona context for the spawned prompt.

        agent_instructions come from the agent's edit page ("Instructions"
        section); skill_instructions are the invoked skill's per-skill
        text, present only when the dispatch carried a matching
        `skill_used`. Returns "" when neither is set, so callers can
        unconditionally concatenate.
        """
        parts: list[str] = []
        if agent_instructions:
            parts.append(
                "[Agent instructions — authored by your creator on the "
                "platform; follow them throughout]\n" + agent_instructions
            )
        if skill_instructions:
            parts.append(
                "[Skill instructions for this request]\n" + skill_instructions
            )
        if not parts:
            return ""
        return "\n\n".join(parts) + "\n\n"

    @staticmethod
    def _bridge_context_primer_background(trigger_reason: str = "") -> str:
        """Prepend at the top of a spawned-Claude prompt to disambiguate mode.

        For trigger-initiated spawns: tells Claude explicitly that no user
        is watching this session. Without this, Claude can default to
        chat-style behavior (asking questions inline and waiting), which
        for autonomous work means waiting indefinitely — there's no one
        there to answer. Steers it toward `send_inbox_item` for any
        communication.
        """
        reason_clause = f" reason={trigger_reason}" if trigger_reason else ""
        return (
            f"[Context: background work{reason_clause} — no user is actively "
            "watching this session. Use `send_inbox_item` for any "
            "communication you need; don't ask questions inline and wait "
            "for replies.]\n\n"
        )

    @staticmethod
    def _bridge_context_primer_chat(chat_id: str, prior_turns: int) -> str:
        """Prepend at the top of a chat-spawned Claude prompt.

        Tells Claude this is a live interactive session — the user IS
        watching, communicate normally. Reinforces the CLAUDE.md
        active-chat-vs-background distinction so behavior is locked in
        from the first token.
        """
        turn_clause = (
            f"turn {prior_turns + 1} of this thread"
            if prior_turns
            else "first turn"
        )
        return (
            f"[Context: active chat with a user on Society AI "
            f"(chat_id={chat_id}, {turn_clause}). The user is typing live "
            "and watching your work in the agent console panel. "
            "Communicate normally — ask questions inline, narrate progress "
            "inline. Don't drop inbox items at them.]\n\n"
        )

    def _build_chat_prompt(self, user_text: str, history: list[dict[str, str]]) -> str:
        """Construct a single-shot prompt that inlines prior conversation.

        We deliberately don't use `claude --resume` because reloading a session
        with extended-thinking blocks fails Anthropic's integrity check
        (HTTP 400: "thinking blocks cannot be modified"). Inlining the
        cleaned (user, assistant) pairs preserves the conversational
        context without that hazard.
        """
        if self._sandbox:
            env_note = (
                "You are running inside an isolated sandbox. You do not have access "
                "to the user's local files; use the Society AI MCP tools and any "
                "cloud APIs you've been granted to do the work."
            )
        else:
            env_note = (
                "You are running on the user's machine in their working directory. "
                "You may read and modify files there as part of the answer if helpful."
            )

        if not history:
            return (
                "You received a direct message from a user on Society AI.\n\n"
                f"Environment: {env_note}\n\n"
                f"User message: {user_text}\n\n"
                "Respond helpfully."
            )

        history_block = "\n\n".join(
            f"User: {turn['user']}\nYou: {turn['assistant']}"
            for turn in history
        )
        return (
            "You are continuing a conversation with a user on Society AI.\n\n"
            f"Environment: {env_note}\n\n"
            "Prior conversation in this thread:\n"
            f"{history_block}\n\n"
            "New message from the user:\n"
            f"{user_text}\n\n"
            "Respond helpfully, with awareness of the prior exchange."
        )

    async def _stream_to_status_updates(self, task_id: str) -> "Callable[[dict], Awaitable[None]]":
        """Build an `on_event` callback for stream_claude_code that maps each
        event to DataParts and ships them out as `task.status` frames.

        Returns the callback; the caller can also pull the mapper off the
        returned closure's `__self__` if it needs final_text, error info, etc.
        """
        mapper = StreamMapper(task_id, verbosity=STATUS_VERBOSITY)

        async def on_event(event: dict) -> None:
            parts = mapper.consume(event)
            if parts:
                await self._send_status_update(task_id, parts)

        # Stash the mapper on the callback so callers can introspect.
        on_event.__mapper__ = mapper  # type: ignore[attr-defined]
        return on_event

    async def _execute_chat_task(
        self,
        task_id: str,
        user_text: str,
        agent_task_id: str | None = None,
        instructions_block: str = "",
    ):
        """Handle a direct chat message OR a personal-task trigger.

        Each call spawns a fresh streaming `claude -p` session — no `--resume`,
        per the v0.2.2 history fix. Intermediate work (tool calls, thinking
        markers, etc.) is forwarded to the Society AI chat UI as `task.status`
        DataParts; the final text goes out as `task.complete`.

        ``agent_task_id`` distinguishes a real chat (None) from a personal-task
        trigger (UUID present). The context primer flips accordingly so Claude
        doesn't try to chat with a user who isn't there (trigger case) or go
        silent on a user who is (chat case).
        """
        history = list(self._chat_history.get(task_id, []))
        is_followup = bool(history)
        # Fallback detection: chat-path triggers carry a [Trigger: ...] prefix
        # in user_text even when agent_task_id metadata is somehow missing.
        is_background = bool(agent_task_id) or user_text.lstrip().startswith("[Trigger:")
        logger.info(
            "Chat task %s: %s (followup=%s, prior_turns=%d, background=%s)",
            task_id, user_text[:100], is_followup, len(history), is_background,
        )

        primer = (
            self._bridge_context_primer_background(trigger_reason="task")
            if is_background
            else self._bridge_context_primer_chat(task_id, len(history))
        )
        prompt = primer + instructions_block + self._build_chat_prompt(user_text, history)

        if self._sandbox:
            # Secured mode still uses the legacy non-streaming runner. Streaming
            # support in the sandbox is a v0.4.x follow-up — it requires routing
            # stdout through SSH line-by-line, which the current SandboxManager
            # doesn't expose.
            exit_code, output, _ = await self._sandbox.exec_claude(prompt)
            result_text = (output or "").strip() or "I couldn't generate a response."
            if exit_code == 0:
                self._record_chat_turn(task_id, user_text, result_text)
            await self._send_task_complete(task_id, result_text, exit_code)
            return

        on_event = await self._stream_to_status_updates(task_id)
        exit_code, output, _session, had_error = await stream_claude_code(
            prompt, on_event, work_dir=self.ctx.work_dir, extra_dirs=self.ctx.extra_dirs)
        mapper: StreamMapper = on_event.__mapper__  # type: ignore[attr-defined]

        result_text = (output or "").strip() or (mapper.final_text() or "I couldn't generate a response.")

        if exit_code == 0 and not had_error:
            self._record_chat_turn(task_id, user_text, result_text)

        await self._send_task_complete(
            task_id, result_text, exit_code or (1 if had_error else 0),
        )

    async def _execute_agentorg_task(
        self,
        task_id: str,
        company_id: str,
        agent_task_id: str,
        instructions_block: str = "",
    ):
        """Handle an AgentOrg task — fetch context, spawn Claude Code, report results."""
        company = await fetch_company_details(company_id)
        if not company:
            company = {"name": "Unknown", "mission": "", "goals": []}

        task = await fetch_task_details(company_id, agent_task_id) if agent_task_id else None

        if not task:
            logger.warning(
                "Could not fetch task details for %s, using trigger message",
                agent_task_id,
            )
            task = {
                "id": agent_task_id or task_id,
                "identifier": "UNKNOWN",
                "title": "Check task queue",
                "description": (
                    "A trigger was received but task details could not be fetched. "
                    "Use list_tasks to find your assigned tasks."
                ),
                "acceptanceCriteria": [],
                "companyId": company_id,
            }

        # Prepend mode primer so Claude knows up-front this is background
        # work — the existing build_prompt() instructions already cover the
        # right behaviors near the bottom of the prompt, but the primer at
        # the top removes any ambiguity about whether a user is watching.
        prompt = (
            self._bridge_context_primer_background(trigger_reason="agentorg-task")
            + instructions_block
            + build_prompt(company, task, sandboxed=bool(self._sandbox))
        )
        logger.info(
            "Executing task: %s — %s",
            task.get("identifier", "unknown"), task.get("title", "unknown"),
        )

        if self._sandbox:
            exit_code, output, _ = await self._sandbox.exec_claude(prompt)
            result_text = (output or "").strip() or f"Task completed with exit code {exit_code}"
            await self._send_task_complete(task_id, result_text, exit_code)
        else:
            on_event = await self._stream_to_status_updates(task_id)
            exit_code, output, _session, had_error = await stream_claude_code(
            prompt, on_event, work_dir=self.ctx.work_dir, extra_dirs=self.ctx.extra_dirs)
            mapper: StreamMapper = on_event.__mapper__  # type: ignore[attr-defined]
            result_text = (
                (output or "").strip()
                or (mapper.final_text() or f"Task completed with exit code {exit_code}")
            )
            await self._send_task_complete(
                task_id, result_text, exit_code or (1 if had_error else 0),
            )
        logger.info("Task %s completed (exit=%d)", task.get("identifier", task_id), exit_code)

    async def _send_status_update(self, task_id: str, parts: list[dict]):
        """Send a `task.status` JSON-RPC frame with intermediate DataParts.

        Mirrors the OpenClaw connector's wire format: same method name, same
        params shape, `final: false`, `state: "working"`. The hub forwards
        these to the chat session via SSE and persists each part as a
        `DataPart` row attached to the agent's `Message`, so reloading the
        session later replays the full tool-call trace.
        """
        if not parts:
            return
        await self.send_rpc(
            "task.status",
            {
                "task_id": task_id,
                "status": {
                    "state": "working",
                    "message": {
                        "role": "agent",
                        "parts": parts,
                    },
                },
                "final": False,
            },
            msg_id=self._next_id(),
        )

    async def _send_task_complete(self, task_id: str, result_text: str, exit_code: int = 0):
        """Send task.complete to the hub.

        Long results are truncated to MAX_RESULT_CHARS; we log the original
        length so a missing tail can be diagnosed from the bridge logs.
        TODO: when save_artifact is wired up, upload long results to the
        artifact store and replace with a short link instead of truncating.
        """
        original_len = len(result_text or "")
        if original_len > MAX_RESULT_CHARS:
            logger.warning(
                "Result for task %s truncated: original=%d chars, cap=%d chars",
                task_id, original_len, MAX_RESULT_CHARS,
            )
            result_text = (
                result_text[:MAX_RESULT_CHARS]
                + f"\n\n... (truncated; original {original_len} chars)"
            )
        await self.send_rpc(
            "task.complete",
            {
                "task_id": task_id,
                "status": {
                    "state": "completed" if exit_code == 0 else "failed",
                    "message": {
                        "role": "agent",
                        "parts": [{"type": "text", "text": result_text}],
                    },
                },
                "final": True,
            },
            msg_id=self._next_id(),
        )

    # -- Session-mode dispatch (v0.7) ----------------------------------------

    @staticmethod
    def _derive_session_title(user_text: str) -> str:
        """Human title for a session: the task's own title when the dispatch
        is a task trigger, else the first meaningful line. Trigger banners
        ('[Trigger: ...]') never become titles."""
        m = re.search(r"new task \([A-Z0-9-]+\):\s*(.+)", user_text)
        if m:
            return m.group(1).strip()[:60]
        for line in user_text.strip().splitlines():
            line = re.sub(r"^\[Trigger:[^\]]*\]\s*", "", line.strip())
            if line:
                return line[:60]
        return "chat"

    async def _on_session_reap(self, rec) -> None:
        """SessionManager reap callback: ship the final transcript delta and
        flip the platform mirror to 'ended'."""
        if self._shipper is not None:
            await self._shipper.ship(rec.session_id, status="ended")

    async def _mirror_retry_loop(self) -> None:
        """Re-ship mirror batches whose status flip failed (network blip at
        reap time) — the session_ended wake and the Scribe depend on the
        'ended' flip reaching the platform."""
        while True:
            try:
                n = await self._shipper.retry_pending()
                if n:
                    logger.info("Mirror retry: re-shipped %d pending session(s)", n)
            except Exception:
                logger.exception("mirror retry loop error")
            await asyncio.sleep(120)

    async def _on_mirror_chat_link(self, session_id: str, chat_id: str) -> None:
        """The platform told us which chat a session projects into. Alias
        that chat id to the session's work item, so a user replying in the
        chat composer continues the SAME Claude Code session."""
        mgr = self._session_mgr
        if mgr is None:
            return
        for rec in list(mgr._sessions.values()):
            if rec.session_id == session_id:
                mgr.alias(chat_id, rec.work_item_key)
                logger.info(
                    "Chat %s aliased to session work item %s",
                    chat_id[:8], rec.work_item_key[:24],
                )
                return

    async def ipc_mirror_notify(self, params: dict) -> dict:
        """IPC from the Stop hook: a Claude Code turn ended on this machine.
        Ships the delta if the session is one the bridge launched; silently
        ignores everything else (the owner's personal sessions are never
        mirrored)."""
        session_id = str(params.get("session_id") or "")
        if not session_id:
            return {"shipped": False}

        # 'prompt' = UserPromptSubmit: the owner just typed into this session.
        # Ship soon so the message reaches the platform (and the web app
        # watching the chat) NOW, not at turn end — but in the background,
        # because this hook blocks the turn from starting until it returns,
        # and on a delay, because the prompt entry is flushed to the
        # transcript as the turn spins up, not synchronously with the hook.
        # Two staggered ships cover a slow flush; each is incremental and a
        # no-op when there is nothing new. The turn is NOT closed here — it
        # has only just begun.
        if str(params.get("event") or "stop") == "prompt":
            if self._shipper is not None and self._shipper.is_registered(session_id):
                async def _ship_soon() -> None:
                    for delay in (1.0, 3.0):
                        await asyncio.sleep(delay)
                        try:
                            await self._shipper.ship(session_id)
                        except Exception:
                            logger.exception("prompt ship failed for %s", session_id[:8])
                asyncio.ensure_future(_ship_soon())
                return {"shipped": True, "deferred": True}
            return {"shipped": False}

        ok = False
        if self._shipper is not None and self._shipper.is_registered(session_id):
            ok = await self._shipper.ship(session_id)

        # The Stop hook is also how a dispatch learns its turn is over. Ship
        # first so the platform has the transcript before the task completes,
        # then hand the turn's text back to whoever is waiting on it. This
        # runs even when mirroring is off, since it is the response path.
        closed = await self._close_turn_from_transcript(session_id)
        return {"shipped": ok, "turn_closed": closed}

    async def ipc_status(self, params: dict) -> dict:
        """Live in-process state for the local status panel (localhost only,
        via the bridge's unix socket). No secrets, no transcript content —
        connection health, effective config, and the live session list."""
        sessions: list[dict] = []
        if self._session_mgr is not None:
            try:
                sessions = self._session_mgr.snapshot()
            except Exception as e:
                logger.warning("status snapshot failed: %s", e)

        ws_connected = bool(self.ws) and getattr(self.ws, "close_code", None) is None
        return {
            "version": __version__,
            "agent_name": self.ctx.name,
            "pid": os.getpid(),
            "ws_connected": ws_connected,
            "registered": bool(self.registered),
            "api_url": self.ctx.api_url,
            "config": {
                "session_mode": self._session_mode,
                "mirror": MIRROR_SESSIONS,
                "mirror_level": MIRROR_LEVEL,
                "execution_mode": EXECUTION_MODE,
                "work_dir": self.ctx.work_dir,
                "extra_dirs": list(self.ctx.extra_dirs),
            },
            "active_tasks": len(getattr(self, "_active_tasks", []) or []),
            "pending_session_tasks": len(getattr(self, "_pending_session_tasks", {}) or {}),
            "sessions": sessions,
        }

    async def ipc_reap_session(self, params: dict) -> dict:
        """Reap (kill + ship-ended) a single live session by work-item key.
        Lets the panel clear a stuck session without restarting the bridge."""
        key = str(params.get("work_item_key") or "").strip()
        if not key:
            return {"error": True, "message": "work_item_key is required"}
        if self._session_mgr is None:
            return {"error": True, "message": "session mode is not enabled"}
        if self._session_mgr.get(key) is None:
            return {"error": True, "message": f"no live session for {key}"}
        try:
            await self._session_mgr.reap(key)
            return {"reaped": True, "work_item_key": key}
        except Exception as e:
            logger.warning("reap of %s failed: %s", key, e)
            return {"error": True, "message": str(e)}

    @staticmethod
    def _turn_reply_text(cwd: str, session_id: str, event_id: str) -> Optional[str]:
        """The prose a session wrote in response to one channel event.

        This is the response, full stop. There is no reply tool: asking the
        model to call one made delivery depend on it choosing to, which it
        often does not for plain chat, and left the desktop and web views
        showing different conversations. The transcript is the same record
        both surfaces are built from, so reading it is what keeps them
        consistent.

        Bounded on both ends: everything after our channel event, stopping
        at the next one, because a later dispatch is a different turn and
        folding it in would attribute the wrong text to this task. Sidechain
        (subagent) entries are skipped; only what the main session actually
        said counts. Assistant turns arrive as one transcript entry per
        content block, so text blocks are joined in order.

        Returns None when our event is not in the transcript at all, which
        is NOT the same as the agent saying nothing: a warm session can end
        an earlier turn while ours is still in flight, and treating that as
        an empty answer would complete the task against the wrong turn. ""
        means the event is there and the agent wrote no prose.
        """
        from transcript_shipper import transcript_path

        needles = (f'event_id="{event_id}"', f'event_id=\\"{event_id}\\"')
        try:
            with open(transcript_path(cwd, session_id), encoding="utf-8",
                      errors="replace") as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            return None
        except OSError as e:
            logger.warning("Could not read transcript for %s: %s", session_id[:8], e)
            return None

        start = -1
        for i, raw in enumerate(lines):
            if any(n in raw for n in needles):
                start = i
        if start < 0:
            return None

        out: list[str] = []
        for raw in lines[start + 1:]:
            try:
                d = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(d, dict) or d.get("isSidechain"):
                continue
            msg = d.get("message")
            kind = d.get("type")
            if kind == "user" and isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str) and "<channel source=" in content:
                    break  # the next dispatch; this turn is done
                continue
            if kind != "assistant" or not isinstance(msg, dict):
                continue
            for c in msg.get("content", []):
                if isinstance(c, dict) and c.get("type") == "text":
                    text = (c.get("text") or "").strip()
                    # Streaming can re-emit a block; never repeat it.
                    if text and (not out or out[-1] != text):
                        out.append(text)
        return "\n\n".join(out).strip()

    async def _close_turn_from_transcript(self, session_id: str) -> bool:
        """A turn ended in this session. If a dispatch is waiting on it,
        resolve it with what the session said. Returns True if one closed."""
        pending = self._session_awaiting.get(session_id)
        if not pending:
            return False
        task_id = pending["task_id"]
        fut = self._pending_session_tasks.get(task_id)
        if not isinstance(fut, asyncio.Future) or fut.done():
            return False

        # The hook can beat the transcript's last flush by a hair; a couple
        # of short retries costs nothing and avoids completing a task with
        # an empty body that the session did in fact write.
        text: Optional[str] = None
        for _ in range(3):
            text = self._turn_reply_text(pending["cwd"], session_id, task_id)
            if text:
                break
            await asyncio.sleep(0.4)

        if text is None:
            # A turn ended, but not ours — our event is not in the transcript
            # yet. Leave the watch in place for the Stop that does belong to
            # us rather than completing the task with someone else's silence.
            logger.info(
                "Turn ended in session %s before task %s landed; still waiting",
                session_id[:8], task_id,
            )
            return False

        if not fut.done():
            fut.set_result(text)
        logger.info(
            "Turn closed for task %s from session %s (%d chars)",
            task_id, session_id[:8], len(text),
        )
        return True

    async def _deliver_to_session(
        self,
        channel_key: str,
        session_id: str,
        cwd: str,
        content: str,
        meta: dict,
        *,
        attempts: int = 3,
        ack_timeout_s: float = 15.0,
    ) -> bool:
        """Push a channel event and confirm the session actually took it.

        A push that lands before the client has attached its channel handler
        is discarded silently: the write to the hub socket succeeds, the
        notification reaches the session's stdio, and nothing happens. The
        only trustworthy acknowledgement is the session's own transcript,
        where Claude Code records the injected event (a queue-operation
        entry, then the user turn) tagged with our event_id.

        Delivery is therefore at-least-once: push, watch the transcript, and
        re-push if the event never shows up. A retry can in principle double
        up if the first push lands in the moment between the last check and
        the re-push; we re-check immediately before each retry to keep that
        window as small as it can be. Returns True once acknowledged.
        """
        from transcript_shipper import transcript_path

        hub = self._channel_hub
        event_id = str(meta.get("event_id") or "")
        # The transcript stores the channel tag inside a JSON string, so the
        # attribute quotes arrive escaped. Match both forms rather than
        # depending on how the entry happens to be serialized.
        needles = (
            [f'event_id="{event_id}"', f'event_id=\\"{event_id}\\"']
            if event_id
            else []
        )
        path = transcript_path(cwd, session_id)

        def acked() -> bool:
            # No event_id means nothing to correlate on; treat the push as
            # fire-and-forget rather than retrying blind.
            if not needles:
                return True
            # Only the tail can hold an event we pushed seconds ago, and a
            # resumed session's transcript can be megabytes. Read the last
            # chunk rather than the whole file, once per second.
            try:
                with open(path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    fh.seek(max(0, fh.tell() - _ACK_TAIL_BYTES), os.SEEK_SET)
                    tail = fh.read()
            except FileNotFoundError:
                return False
            except OSError as e:
                logger.warning("Could not read transcript %s: %s", path, e)
                return False
            text = tail.decode("utf-8", "replace")
            return any(n in text for n in needles)

        for attempt in range(1, attempts + 1):
            if attempt > 1 and acked():
                return True
            pushed = await hub.push_event(channel_key, content, meta)
            if not pushed:
                logger.warning(
                    "Channel push %d/%d for %s returned not-connected",
                    attempt, attempts, channel_key,
                )
            deadline = time.time() + ack_timeout_s
            while time.time() < deadline:
                await asyncio.sleep(1.0)
                if acked():
                    if attempt > 1:
                        logger.info(
                            "Channel event %s acknowledged on attempt %d",
                            event_id, attempt,
                        )
                    return True
            logger.warning(
                "Channel event %s not acknowledged by %s within %.0fs "
                "(attempt %d/%d)",
                event_id, channel_key, ack_timeout_s, attempt, attempts,
            )
        return False

    async def _execute_via_session(
        self,
        task_id: str,
        work_item_key: str,
        title: str,
        content: str,
        kind: str,
        timeout_s: float = 600.0,
        protocol_text: str = "",
    ) -> None:
        """Dispatch a work item into a persistent session and wait for its
        reply, then complete the A2A task. The event_id we send IS the A2A
        task_id, so the reply correlates straight back."""
        mgr = self._session_mgr
        hub = self._channel_hub
        # Ensure (launch or resume) the work item's session. Trigger-kind
        # work (wakes, schedules, status echoes) runs headless — no Remote
        # Control sidebar row.
        try:
            rec = await mgr.ensure_session(
                work_item_key, self.ctx.name, title=title,
                background=(kind == "trigger"),
            )
        except Exception as e:
            logger.exception("Session launch failed for %s", work_item_key)
            await self._send_task_complete(
                task_id, f"Could not start a session: {type(e).__name__}: {e}", exit_code=1
            )
            return

        # L1 platform protocol: session-scoped standing text. A fresh
        # launch has no context — lead with the protocol; resumes and warm
        # sessions already carry it in their transcript.
        if protocol_text and getattr(rec, "fresh_launch", False):
            content = protocol_text + "\n\n" + content

        # Register the session for transcript mirroring, linked to the work
        # item that caused it. 'task' = a real platform task (agent_task_id);
        # 'trigger' = platform automation (orchestrator wakes, schedules) —
        # the distinction loop-guards the supervisor: scribe sessions ending
        # never re-wake the orchestrator.
        if self._shipper is not None:
            if kind == "chat":
                wi_kind = "chat"
            elif kind == "task_assigned":
                wi_kind = "task"
            else:
                wi_kind = "trigger"
            self._shipper.register(
                rec.session_id,
                cwd=mgr.policy(self.ctx.name).work_dir,
                title=title,
                work_item_kind=wi_kind,
                work_item_id=work_item_key,
            )

        # An aliased dispatch (e.g. a reply typed in the session's platform
        # chat) resolves to the canonical work item — the channel registered
        # under that key, so all hub operations use it.
        channel_key = rec.work_item_key

        # Wait briefly for the session's channel to connect (fresh launches
        # connect within ~1-2s; resumes faster).
        for _ in range(40):
            if hub.is_connected(channel_key):
                break
            await asyncio.sleep(0.25)
        if not hub.is_connected(channel_key):
            await self._send_task_complete(
                task_id, "Session started but its channel did not connect in time.", exit_code=1
            )
            return

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending_session_tasks[event_id := task_id] = fut
        # The session's Stop hook resolves this by reading the transcript;
        # registered before delivery so a fast turn cannot end before we are
        # listening for it.
        work_dir = mgr.policy(self.ctx.name).work_dir
        self._session_awaiting[rec.session_id] = {"task_id": task_id, "cwd": work_dir}
        try:
            # Two distinct failures, two distinct timeouts. Delivery either
            # lands in seconds or it never will, and reporting it as "the
            # agent did not reply" points the reader at the model when the
            # message never reached it.
            delivered = await self._deliver_to_session(
                channel_key,
                rec.session_id,
                work_dir,
                content,
                {"event_id": task_id, "kind": kind},
            )
            mgr.touch(channel_key)
            if not delivered:
                logger.error(
                    "Delivery failed for task %s into session %s (%s)",
                    task_id, rec.session_id[:8], channel_key,
                )
                await self._send_task_complete(
                    task_id,
                    "The session started but never picked up the message. "
                    "Nothing was lost on your side; sending it again should work.",
                    exit_code=1,
                )
                return
            try:
                text = await asyncio.wait_for(fut, timeout=timeout_s)
            except asyncio.TimeoutError:
                # The turn never ended: still working past the limit, or the
                # Stop hook never fired. Fall back to whatever the session
                # has written so far rather than discarding a real answer.
                text = self._turn_reply_text(work_dir, rec.session_id, task_id)
                if text:
                    logger.warning(
                        "Task %s timed out; returning %d chars written so far",
                        task_id, len(text),
                    )
                else:
                    await self._send_task_complete(
                        task_id, "The agent did not finish within the time limit.",
                        exit_code=1,
                    )
                    return
            await self._send_task_complete(task_id, text or "(no reply)", exit_code=0)
        finally:
            self._pending_session_tasks.pop(task_id, None)
            # Only clear the turn watch if it is still ours: a follow-up
            # dispatch into the same session registers its own, and popping
            # blindly would strand it with nothing listening for its turn.
            if self._session_awaiting.get(rec.session_id, {}).get("task_id") == task_id:
                self._session_awaiting.pop(rec.session_id, None)
            # Ship the turn's transcript delta in the background — never
            # blocks task completion, and failures only log.
            if self._shipper is not None:
                asyncio.ensure_future(self._shipper.ship(rec.session_id))

    # -- Main loop -----------------------------------------------------------

    async def run(self):
        """Connect to hub and process messages with auto-reconnect."""
        # If an update ran while we were down, report how it went (fire and
        # forget — a failed post retries at the next start, never blocks boot).
        asyncio.ensure_future(self._report_update_outcome())

        # Start sandbox in secured mode (hard fail if it can't start)
        if self._sandbox:
            try:
                await self._sandbox.start()
            except Exception as e:
                logger.error("SECURED MODE FAILED: %s", e)
                logger.error(
                    "Cannot run in secured mode. Fix the issue above or use EXECUTION_MODE=standard."
                )
                sys.exit(1)

        # Start session-mode infrastructure (channel hub + session manager),
        # and overlay platform-fetched policy onto this persona.
        if self._session_mode:
            try:
                await self._channel_hub.start()
                self._session_mgr.start()
                if self._shipper is not None:
                    # Retry parked mirror flips: immediately (catches flips
                    # dropped before a restart) and every 2 minutes.
                    asyncio.ensure_future(self._mirror_retry_loop())
                from policy import fetch_platform_policy, apply_platform, apply_local_env
                fetched = await fetch_platform_policy(
                    self.ctx.api_url, self.ctx.token, self.ctx.name
                )
                if fetched:
                    pol = self._session_mgr.policy(self.ctx.name)
                    apply_platform(pol, fetched)
                    apply_local_env(pol, self.ctx.name)  # local always wins
                    pol.session_env = self.ctx.session_env()  # keep per-agent env
                    logger.info("Applied platform policy for %s", self.ctx.name)
            except Exception:
                logger.exception("Session-mode startup failed; falling back to spawn path")
                self._session_mode = False

        reconnect_delay = 1

        while self.running:
            url = ws_url(self.ctx.api_url)
            logger.info("Connecting to %s", url)

            # Get fresh JWT before each connection attempt
            try:
                await self.obtain_jwt()
            except AuthError as e:
                # Bad credentials — do not retry endlessly; exit so the user sees it.
                logger.error("%s", e)
                self.running = False
                return
            except Exception as e:
                logger.error("Failed to obtain JWT: %s", e)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)
                continue

            try:
                # TLS only for wss:// - websockets rejects an ssl context
                # for plain ws:// (local/dev hubs).
                ssl_ctx = (
                    ssl.create_default_context(cafile=certifi.where())
                    if url.startswith("wss://")
                    else None
                )
                async with websockets.connect(url, ping_interval=None, ssl=ssl_ctx) as ws:
                    self.ws = ws
                    self.registered = False
                    # NOT reset here. A socket that connects and is then
                    # refused registration would otherwise pin the delay at
                    # 1s and retry forever at a fixed interval, which is
                    # exactly what MAX_RECONNECT_DELAY exists to prevent.
                    # handle_message sets this once the hub accepts us.
                    self._conn_registered = False

                    # Start heartbeat
                    heartbeat = asyncio.create_task(self.heartbeat_loop())

                    try:
                        async for raw in ws:
                            await self.handle_message(raw)
                    except ConnectionClosed as e:
                        logger.warning("Connection closed: %s", e)
                    finally:
                        heartbeat.cancel()
                        try:
                            await heartbeat
                        except (asyncio.CancelledError, Exception):
                            pass
                        self.ws = None
                        self.registered = False
                        # Anything waiting on a pending IPC reply must be
                        # told the WS is gone, otherwise the caller hangs
                        # until its own timeout.
                        self._abort_all_pending("WebSocket connection closed")

            except InvalidStatus as e:
                # HTTP-level failure on the WS handshake (e.g. 401 from bad JWT)
                logger.error("WS handshake failed: %s", e)
            except Exception as e:
                logger.error("Connection error: %s", e)

            # A connection that got as far as registering was a real one, so
            # the next failure starts its backoff from scratch.
            if self._conn_registered:
                reconnect_delay = 1

            if self.running:
                logger.info("Reconnecting in %ds...", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

    def stop(self):
        """Signal the bridge to stop. Safe to call from a signal handler.

        Does NOT await the sandbox shutdown — that happens in main()'s
        finally block where we still control the event loop.
        """
        if not self.running:
            return
        self.running = False
        logger.info("Shutting down...")


# -- Roster + Harness ---------------------------------------------------------

def _read_env_file(path: str) -> dict:
    """Minimal KEY=value parser for a persona .env file."""
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                if s.startswith("export "):
                    s = s[len("export "):]
                k, _, v = s.partition("=")
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out[k] = v
    except FileNotFoundError:
        pass
    return out


def _context_from_env_file(repo_dir: str, env_file: str, persona_arg: str) -> "AgentContext | None":
    env = _read_env_file(os.path.join(repo_dir, env_file))
    name = (env.get("AGENT_NAME") or "").strip()
    token = (env.get("SOCIETY_AI_AUTH_TOKEN") or "").strip()
    if not name or not token:
        return None
    cache = os.path.join(os.path.expanduser("~"), ".cache", "society-ai")
    socket = env.get("SOCIETY_AI_BRIDGE_SOCKET") or (
        os.path.join(cache, "bridge.sock") if not persona_arg
        else os.path.join(cache, persona_arg, "bridge.sock")
    )
    extra = [d.strip() for d in (env.get("EXTRA_DIRS") or "").split(",") if d.strip()]
    return AgentContext(
        name=name, token=token,
        work_dir=env.get("WORK_DIR") or os.getcwd(),
        extra_dirs=extra, company_id=env.get("COMPANY_ID", ""),
        api_url=(env.get("AGENT_ROUTER_API_URL") or AGENT_ROUTER_API_URL).rstrip("/"),
        socket=socket, state_dir=os.path.dirname(socket) or ".",
    )


def discover_roster() -> "list[AgentContext]":
    """Build the machine's agent roster from .env / .env.<persona> files —
    the same files the per-persona installs and the status panel use."""
    repo = os.path.dirname(os.path.abspath(__file__))
    roster: list[AgentContext] = []
    for entry in sorted(os.listdir(repo)):
        if entry == ".env":
            persona_arg = ""
        elif entry.startswith(".env.") and entry not in (".env.example", ".env.defaults"):
            persona_arg = entry[len(".env."):]
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", persona_arg):
                continue
            # setup.sh keeps timestamped backups (.env.bak-<epoch>,
            # .env.<persona>.bak-<epoch>) before overwriting a config. Running
            # them would connect a duplicate of a live agent under its old
            # credentials, so they are never part of the roster.
            if re.search(r"\.?bak-\d+$", persona_arg):
                continue
        else:
            continue
        ctx = _context_from_env_file(repo, entry, persona_arg)
        if ctx:
            roster.append(ctx)
    return roster


async def _teardown_bridge(bridge: "Bridge") -> None:
    """Per-agent graceful teardown: park ended flips, stop session infra so
    the next start ships the sessions and the Scribe still learns they ended."""
    if bridge._sandbox:
        try:
            await bridge._sandbox.stop()
        except Exception as e:
            logger.warning("sandbox stop (%s): %s", bridge.ctx.name, e)
    if getattr(bridge, "_session_mgr", None):
        if getattr(bridge, "_shipper", None):
            try:
                for rec in list(bridge._session_mgr._sessions.values()):
                    if rec.state == "ready":
                        bridge._shipper.park_status(rec.session_id, "ended")
            except Exception as e:
                logger.warning("park ended flips (%s): %s", bridge.ctx.name, e)
        try:
            await bridge._session_mgr.stop()
        except Exception as e:
            logger.warning("session mgr stop (%s): %s", bridge.ctx.name, e)
    if getattr(bridge, "_channel_hub", None):
        try:
            await bridge._channel_hub.stop()
        except Exception as e:
            logger.warning("channel hub stop (%s): %s", bridge.ctx.name, e)
    if getattr(bridge, "_shipper", None):
        try:
            await bridge._shipper.close()
        except Exception as e:
            logger.warning("shipper close (%s): %s", bridge.ctx.name, e)


class Harness:
    """Runs every agent on this machine in one supervised process. Each agent
    gets its own hub connection + IPC socket (identity plane); all share ONE
    scheduler — the machine-wide concurrency cap (execution plane). A failure
    in one agent's connection is isolated and doesn't tear down the others."""

    def __init__(self, roster: "list[AgentContext]", machine_cap: int):
        self.roster = roster
        self.machine_cap = machine_cap
        self.bridges: list[Bridge] = []
        self.ipc_servers: list = []
        self._scheduler = None

    async def run(self) -> None:
        self._scheduler = asyncio.Semaphore(self.machine_cap)
        for ctx in self.roster:
            b = Bridge(ctx, scheduler=self._scheduler)
            self.bridges.append(b)
            ipc = bridge_ipc.IPCServer(
                handlers={
                    "search_agents": b.ipc_search_agents,
                    "delegate_task": b.ipc_delegate_task,
                    "mirror_notify": b.ipc_mirror_notify,
                    "status": b.ipc_status,
                    "reap_session": b.ipc_reap_session,
                },
                path=ctx.socket,
            )
            try:
                await ipc.start()
            except Exception as e:
                logger.error("IPC server for %s failed to start: %s", ctx.name, e)
            self.ipc_servers.append(ipc)
            logger.info("Harness: agent %s online (socket=%s)", ctx.name, ctx.socket)
        await asyncio.gather(*(b.run() for b in self.bridges), return_exceptions=True)

    def stop(self) -> None:
        for b in self.bridges:
            b.stop()

    async def shutdown(self) -> None:
        for ipc in self.ipc_servers:
            try:
                await ipc.stop()
            except Exception as e:
                logger.warning("IPC server stop: %s", e)
        for b in self.bridges:
            await _teardown_bridge(b)
        try:
            await _close_http_client()
        except Exception as e:
            logger.warning("HTTP client cleanup: %s", e)


# -- Entry point --------------------------------------------------------------

def main():
    roster = discover_roster()

    # Single-agent mode. bridge_launcher.sh sources one agent's env file
    # (which always carries AGENT_NAME) before exec'ing us, so a per-agent
    # LaunchAgent must run ONLY that agent. Without this every per-agent
    # service would start the whole roster, so each agent got connected once
    # per installed service; the hub accepts the first and rejects the rest
    # with "already connected", and the losers reconnect forever. That churn
    # makes the agent undeliverable: tasks never reach a stable connection.
    # harness_launcher.sh deliberately sources only .env.defaults (no
    # AGENT_NAME), so the machine-wide harness still runs the full roster.
    only = (os.getenv("AGENT_NAME") or "").strip()
    if only:
        roster = [c for c in roster if c.name == only]
        if not roster:
            print(
                f"Error: AGENT_NAME={only} is set, but no .env file in this "
                "folder defines that agent. Check the env file this service "
                "sources, or run ./setup.sh again.",
                file=sys.stderr,
            )
            sys.exit(2)

    if not roster:
        print(
            "Error: no agents found. Set SOCIETY_AI_AUTH_TOKEN + AGENT_NAME in "
            ".env (or add personas with ./setup.sh --persona <name>).",
            file=sys.stderr,
        )
        sys.exit(2)

    # Machine-wide concurrency cap across ALL agents (overridable). This is
    # the bounded-parallelism limit a single per-agent process can't enforce.
    machine_cap = max(1, int(os.getenv("MAX_CONCURRENT_MACHINE", "8")))
    # Warms the one-time CLI version cache before any agent registers.
    logger.info(
        "claude-code-agent harness v%s on Claude Code CLI %s — %d agent(s): %s "
        "(mode=%s, machine cap=%d)",
        __version__, claude_cli_version() or "unknown",
        len(roster), ", ".join(c.name for c in roster),
        EXECUTION_MODE, machine_cap,
    )

    harness = Harness(roster, machine_cap)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _request_shutdown():
        logger.info("Shutdown requested")
        harness.stop()
        # Close each agent's WS so its run loop unblocks and returns —
        # otherwise the process ignores SIGTERM (the read loop stays parked
        # on `async for raw in ws`). launchd stops/restarts via SIGTERM, so
        # this is what lets a clean teardown (parked status flips) happen.
        for b in harness.bridges:
            if b.ws is not None:
                asyncio.ensure_future(b.ws.close())

    # Prefer loop-native signal handling (runs inside the loop, can schedule
    # the WS closes); fall back to plain signals where unavailable.
    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(_sig, _request_shutdown)
        except (NotImplementedError, RuntimeError):
            signal.signal(_sig, lambda *_: _request_shutdown())

    try:
        loop.run_until_complete(harness.run())
    except KeyboardInterrupt:
        harness.stop()
    finally:
        if any(b._active_tasks for b in harness.bridges):
            logger.info("Waiting for in-flight tasks...")
            try:
                loop.run_until_complete(asyncio.sleep(2))
            except Exception:
                pass
        try:
            loop.run_until_complete(harness.shutdown())
        except Exception as e:
            logger.warning("Harness shutdown error: %s", e)
        try:
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
