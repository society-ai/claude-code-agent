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
import sys
import uuid

import certifi
import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

import bridge_ipc
from config import (
    AGENT_ROUTER_API_URL,
    SOCIETY_AI_AUTH_TOKEN,
    AGENT_NAME,
    COMPANY_ID,
    API_HEADERS,
    EXECUTION_MODE,
    SANDBOX_NAME,
    SANDBOX_TIMEOUT,
    SOCIETY_AI_BRIDGE_SOCKET,
    __version__,
    ws_url,
)
from typing import Any

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
MAX_TRACKED_SESSIONS = 1000  # cap session/lock maps to prevent memory leak
MAX_RESULT_CHARS = max(1000, int(os.getenv("MAX_RESULT_CHARS", "16000")))
TOOL_CONCURRENCY_RETRIES = 5  # retries when Claude API returns 400 for tool concurrency
TOOL_CONCURRENCY_BASE_DELAY = 3  # seconds between retries
JWT_EXCHANGE_TIMEOUT = 15
HTTP_FETCH_TIMEOUT = 30

# UUID pattern for input validation
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# -- JWT token exchange -------------------------------------------------------

async def exchange_api_key_for_jwt(api_key: str) -> str:
    """Exchange a Society AI API key (sai_...) for a short-lived WS JWT.

    POST /auth/agent-token with {"api_key": "<key>"}
    Returns JWT valid for ~15 min.

    Raises:
        AuthError on 401/403 (caller should not retry — bad credentials).
        httpx.HTTPError on transient failures (caller may retry with backoff).
    """
    url = f"{AGENT_ROUTER_API_URL}/auth/agent-token"
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


async def run_claude_code(
    prompt: str,
    session_id: str | None = None,
) -> tuple[int, str, str | None]:
    """Spawn a Claude Code CLI session and collect output.

    Retries automatically on tool-use concurrency errors (HTTP 400) which
    happen when another Claude Code session is actively using tools.

    Args:
        prompt: The message to send.
        session_id: If provided, resumes this session (multi-turn conversation).

    Returns (exit_code, output_text, session_id).
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
        ]
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

class Bridge:
    def __init__(self):
        self.ws = None
        self.registered = False
        self.running = True
        self._active_tasks: set[str] = set()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
        self._msg_counter = 0
        self._ws_jwt: str | None = None
        # Map task_id -> Claude Code session_id for multi-turn conversations
        self._task_sessions: dict[str, str] = {}
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

        # Sandbox executor for secured mode
        self._sandbox = None
        if EXECUTION_MODE == "secured":
            try:
                from sandbox import SandboxManager
            except ImportError:
                logger.error("EXECUTION_MODE=secured requires sandbox.py. Check your installation.")
                sys.exit(1)
            from config import SOCIETY_AI_SERVICE_KEY, ENABLE_AGENT_LIFECYCLE
            sandbox_env = {
                "SOCIETY_AI_AUTH_TOKEN": SOCIETY_AI_AUTH_TOKEN,
                "AGENT_ROUTER_API_URL": AGENT_ROUTER_API_URL,
                "AGENT_NAME": AGENT_NAME,
                "COMPANY_ID": COMPANY_ID,
            }
            if SOCIETY_AI_SERVICE_KEY:
                sandbox_env["SOCIETY_AI_SERVICE_KEY"] = SOCIETY_AI_SERVICE_KEY
            if ENABLE_AGENT_LIFECYCLE:
                sandbox_env["ENABLE_AGENT_LIFECYCLE"] = "true"
            self._sandbox = SandboxManager(
                work_dir=WORK_DIR,
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
        self._ws_jwt = await exchange_api_key_for_jwt(SOCIETY_AI_AUTH_TOKEN)

    async def register(self):
        """Register this agent with the hub using the WS JWT."""
        if not self._ws_jwt:
            await self.obtain_jwt()
        await self.send_rpc(
            "agent.register",
            {
                "agent_name": AGENT_NAME,
                "auth_token": self._ws_jwt,
                "visibility": "private",
            },
            msg_id=self._next_id(),
        )

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
                    logger.info("Registered as %s", result.get("agent_id", AGENT_NAME))
                else:
                    logger.error("Registration failed: %s", result.get("error"))
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
            asyncio.create_task(self.handle_task_execute(msg_id, params))

        elif method == "delegation.result":
            # Two-phase delegation: the hub correlates the result back to
            # our original tasks/sendSubscribe via `original_id`.
            original_id_raw = params.get("original_id") if isinstance(params, dict) else None
            original_id = str(original_id_raw) if original_id_raw is not None else None
            if not self._resolve_pending(self._pending_delegations, original_id, params):
                logger.debug("delegation.result for unknown original_id=%s", original_id)

        else:
            logger.debug("Unhandled method: %s", method)

    # -- Outbound IPC handlers (used by the bridge IPC server) -------------

    async def ipc_search_agents(self, params: dict) -> dict:
        """Run an `agents/search` JSON-RPC request over our WS connection.

        Called by the IPC server on behalf of the MCP server's `search_agents`
        tool. Returns either the hub's search result or a structured error.
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
            await self.send_rpc("agents/search", {"q": q.strip(), "limit": limit}, msg_id=req_id)
            try:
                result = await asyncio.wait_for(fut, timeout=30)
            except asyncio.TimeoutError:
                return {"error": True, "message": "agents/search timed out after 30s"}
            return result if isinstance(result, dict) else {"agents": result}
        finally:
            self._pending_requests.pop(req_id, None)

    async def ipc_delegate_task(self, params: dict) -> dict:
        """Run a `tasks/sendSubscribe` over our WS connection and wait for
        the asynchronous `delegation.result` notification.

        Implements the two-phase pattern. Phase 2 (delegation result future)
        is pre-registered BEFORE Phase 1 (request send) to avoid the race
        where the hub's notification arrives before we install the handler.
        """
        if not self.ws or not self.registered:
            return {"error": True, "message": "Bridge is not currently connected to the Society AI hub"}

        agent_name = (params.get("agent_name") or "").strip()
        message = params.get("message")
        if not agent_name:
            return {"error": True, "message": "delegate_task requires 'agent_name'"}
        if not isinstance(message, str) or not message:
            return {"error": True, "message": "delegate_task requires non-empty 'message'"}
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
            "agent_name": agent_name,
            "message": message,
            "original_id": req_id,
        }
        if params.get("skill_id"):
            rpc_params["skill_id"] = params["skill_id"]
        if params.get("session_id"):
            rpc_params["session_id"] = params["session_id"]

        try:
            await self.send_rpc("tasks/sendSubscribe", rpc_params, msg_id=req_id)

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
        company_id = metadata.get("company_id") or COMPANY_ID
        agent_task_id = metadata.get("agent_task_id")

        # Extract user message text
        message = params.get("message", {}) or {}
        parts = message.get("parts", []) or []
        user_text = ""
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                user_text += part.get("text", "")
            elif isinstance(part, str):
                user_text += part

        logger.info(
            "Task received: %s (agent_task=%s, company=%s)",
            task_id, agent_task_id, company_id,
        )
        logger.info("Message: %s", user_text[:200])

        # Per-task lock: serialize messages in the same conversation so
        # we don't try to --resume a Claude Code session that's still running
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
                    if agent_task_id and company_id:
                        # AgentOrg trigger flow — full context + Claude Code spawn
                        await self._execute_agentorg_task(task_id, company_id, agent_task_id)
                    else:
                        # Chat flow — direct message, respond via Claude Code
                        await self._execute_chat_task(task_id, user_text)
                except Exception as e:
                    logger.exception("Task %s failed with unhandled exception", task_id)
                    await self._send_task_complete(
                        task_id, f"Internal error: {type(e).__name__}: {e}", exit_code=1
                    )
                finally:
                    self._active_tasks.discard(task_id)

    def _record_session(self, task_id: str, session_id: str | None) -> None:
        """Track a Claude Code session_id for a task, with bounded memory."""
        if not session_id:
            return
        self._task_sessions[task_id] = session_id
        if len(self._task_sessions) > MAX_TRACKED_SESSIONS:
            # Drop the oldest half of session AND lock entries together so the
            # two maps stay aligned and neither leaks unbounded memory.
            drop = list(self._task_sessions.keys())[: MAX_TRACKED_SESSIONS // 2]
            for k in drop:
                self._task_sessions.pop(k, None)
                self._task_locks.pop(k, None)

    async def _execute_chat_task(self, task_id: str, user_text: str):
        """Handle a direct chat message — spawn Claude Code and return response.

        Uses --resume for follow-up messages in the same conversation (same task_id).
        """
        existing_session = self._task_sessions.get(task_id)
        is_followup = existing_session is not None
        logger.info("Chat task %s: %s (followup=%s)", task_id, user_text[:100], is_followup)

        if is_followup:
            prompt = user_text
        elif self._sandbox:
            prompt = f"""You received a direct message from a user on Society AI.

User message: {user_text}

Respond helpfully. You are running inside an isolated sandbox and do not have access
to the user's local files; use the Society AI MCP tools and any cloud APIs you've
been granted to do your work."""
        else:
            prompt = f"""You received a direct message from a user on Society AI.

User message: {user_text}

Respond helpfully. You have access to the codebase in the current working directory.
If they ask about code, read files and answer. If they ask you to make changes, do so."""

        if self._sandbox:
            exit_code, output, session_id = await self._sandbox.exec_claude(prompt, session_id=existing_session)
        else:
            exit_code, output, session_id = await run_claude_code(prompt, session_id=existing_session)

        self._record_session(task_id, session_id)

        # Send response back to hub
        result_text = (output or "").strip() or "I couldn't generate a response."
        await self._send_task_complete(task_id, result_text, exit_code)

    async def _execute_agentorg_task(self, task_id: str, company_id: str, agent_task_id: str):
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

        prompt = build_prompt(company, task, sandboxed=bool(self._sandbox))
        logger.info(
            "Executing task: %s — %s",
            task.get("identifier", "unknown"), task.get("title", "unknown"),
        )

        if self._sandbox:
            exit_code, output, _ = await self._sandbox.exec_claude(prompt)
        else:
            exit_code, output, _ = await run_claude_code(prompt)
        result_text = (output or "").strip() or f"Task completed with exit code {exit_code}"
        await self._send_task_complete(task_id, result_text, exit_code)
        logger.info("Task %s completed (exit=%d)", task.get("identifier", task_id), exit_code)

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

    # -- Main loop -----------------------------------------------------------

    async def run(self):
        """Connect to hub and process messages with auto-reconnect."""
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

        reconnect_delay = 1

        while self.running:
            url = ws_url()
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
                ssl_ctx = ssl.create_default_context(cafile=certifi.where())
                async with websockets.connect(url, ping_interval=None, ssl=ssl_ctx) as ws:
                    self.ws = ws
                    self.registered = False
                    reconnect_delay = 1  # reset on successful connect

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


# -- Entry point --------------------------------------------------------------

def main():
    if not SOCIETY_AI_AUTH_TOKEN:
        print("Error: SOCIETY_AI_AUTH_TOKEN is required.", file=sys.stderr)
        print("Get your API key at https://societyai.com and set it:", file=sys.stderr)
        print("  export SOCIETY_AI_AUTH_TOKEN=sai_...", file=sys.stderr)
        print("  python bridge.py", file=sys.stderr)
        sys.exit(2)

    if not SOCIETY_AI_AUTH_TOKEN.startswith("sai_"):
        print(
            "Warning: SOCIETY_AI_AUTH_TOKEN does not start with 'sai_' — "
            "this may not be a valid Society AI API key.",
            file=sys.stderr,
        )

    logger.info("claude-code-agent bridge v%s starting (agent=%s, mode=%s)",
                __version__, AGENT_NAME, EXECUTION_MODE)

    bridge = Bridge()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # IPC server: lets the in-Claude-Code MCP server call back into the
    # bridge for search_agents / delegate_task. Started before the WS loop
    # so it survives reconnects, and stopped in the finally block below.
    ipc = bridge_ipc.IPCServer(
        handlers={
            "search_agents": bridge.ipc_search_agents,
            "delegate_task": bridge.ipc_delegate_task,
        },
        path=SOCIETY_AI_BRIDGE_SOCKET,
    )

    def handle_signal(sig, _):
        logger.info("Received signal %s", sig)
        bridge.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(ipc.start())
    except Exception as e:
        logger.error("Failed to start IPC server: %s", e)
        # IPC is best-effort — delegation/search will fail with a clear
        # error from the MCP side, but the bridge can still process
        # inbound tasks. Continue.

    try:
        loop.run_until_complete(bridge.run())
    except KeyboardInterrupt:
        bridge.stop()
    finally:
        # Give in-flight tasks a brief moment to finish their writes.
        if bridge._active_tasks:
            logger.info("Waiting for %d active tasks...", len(bridge._active_tasks))
            try:
                loop.run_until_complete(asyncio.sleep(2))
            except Exception:
                pass
        try:
            loop.run_until_complete(ipc.stop())
        except Exception as e:
            logger.warning("Error stopping IPC server: %s", e)
        try:
            loop.run_until_complete(_close_http_client())
        except Exception as e:
            logger.warning("Error during HTTP client cleanup: %s", e)
        if bridge._sandbox:
            try:
                loop.run_until_complete(bridge._sandbox.stop())
            except Exception as e:
                logger.warning("Error stopping sandbox: %s", e)
        try:
            loop.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
