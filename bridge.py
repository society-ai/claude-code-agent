"""Society AI Bridge — connects Claude Code to Society AI via WebSocket.

Receives task assignments and chat messages from Society AI, spawns Claude Code
CLI sessions to handle them, and reports results back.

Usage:
    python bridge.py

Env vars (see config.py):
    SOCIETY_AI_AUTH_TOKEN  — Your Society AI API key (required)
    AGENT_ROUTER_API_URL  — API URL (default: https://api.societyai.com)
    AGENT_NAME            — Agent name (default: claude-code)
    COMPANY_ID            — Default company UUID (optional)
    WORK_DIR              — Working directory for Claude Code (default: cwd)
    MAX_CONCURRENT_TASKS  — Max parallel tasks (default: 3)
"""

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
from websockets.exceptions import ConnectionClosed

from config import (
    AGENT_ROUTER_API_URL,
    SOCIETY_AI_AUTH_TOKEN,
    AGENT_NAME,
    COMPANY_ID,
    API_HEADERS,
    ws_url,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bridge")

WORK_DIR = os.getenv("WORK_DIR", os.getcwd())
HEARTBEAT_INTERVAL = 60  # seconds (hub timeout is 90s)
MAX_RECONNECT_DELAY = 60
MAX_CONCURRENT_TASKS = int(os.getenv("MAX_CONCURRENT_TASKS", "3"))
MAX_TRACKED_SESSIONS = 1000  # cap session map to prevent memory leak
TOOL_CONCURRENCY_RETRIES = 5  # retries when Claude API returns 400 for tool concurrency
TOOL_CONCURRENCY_BASE_DELAY = 3  # seconds between retries

# UUID pattern for input validation
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# -- JWT token exchange -------------------------------------------------------

async def exchange_api_key_for_jwt(api_key: str) -> str:
    """Exchange a Society AI API key (sai_...) for a short-lived WS JWT.

    POST /auth/agent-token with {"api_key": "<key>"}
    Returns JWT valid for ~15 min.
    """
    url = f"{AGENT_ROUTER_API_URL}/auth/agent-token"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json={"api_key": api_key})
        resp.raise_for_status()
        data = resp.json()
    token = data["token"]
    expires_in = data.get("expires_in", 900)
    logger.info("JWT obtained (expires in %ds)", expires_in)
    return token


# -- HTTP client for fetching task details ------------------------------------

_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30)
    return _http_client


async def fetch_task_details(company_id: str, task_id: str) -> dict | None:
    """Fetch full task details from Society AI API."""
    if not _UUID_RE.match(company_id) or not _UUID_RE.match(task_id):
        logger.error(f"Invalid UUID in fetch_task_details: company={company_id}, task={task_id}")
        return None
    url = f"{AGENT_ROUTER_API_URL}/api/v1/companies/{company_id}/tasks/{task_id}"
    try:
        resp = await _get_http_client().get(url, headers=API_HEADERS)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch task {task_id}: {e}")
        return None


async def fetch_company_details(company_id: str) -> dict | None:
    """Fetch company context from Society AI API."""
    if not _UUID_RE.match(company_id):
        logger.error(f"Invalid UUID in fetch_company_details: {company_id}")
        return None
    url = f"{AGENT_ROUTER_API_URL}/api/v1/companies/{company_id}"
    try:
        resp = await _get_http_client().get(url, headers=API_HEADERS)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Failed to fetch company {company_id}: {e}")
        return None


# -- Claude Code process spawner ----------------------------------------------

def build_prompt(company: dict, task: dict) -> str:
    """Build the prompt for a Claude Code session."""
    company_name = company.get("name", "Unknown")
    mission = company.get("mission", "")
    goals = company.get("goals", [])

    task_id = task.get("id", "")
    identifier = task.get("identifier", "")
    title = task.get("title", "")
    description = task.get("description", "")
    acceptance = task.get("acceptanceCriteria", [])
    company_id = task.get("companyId", "")

    goals_str = "\n".join(f"  - {g}" for g in goals) if goals else "  (none)"
    criteria_str = "\n".join(f"  - {c}" for c in acceptance) if acceptance else "  (none specified)"

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

Instructions:
1. First, update the task status to "in_progress" using the update_task tool.
2. Do the work in the codebase — read files, write code, run tests, etc.
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
    for attempt in range(1, TOOL_CONCURRENCY_RETRIES + 1):
        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "json",
        ]
        if session_id:
            cmd.extend(["--resume", session_id])

        logger.info(f"Spawning Claude Code in {WORK_DIR} (session={session_id or 'new'}, attempt={attempt})")
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
                logger.warning(f"Claude Code stderr: {stderr_text[:1000]}")

        # Parse JSON output to extract result and session_id
        output_text = raw_output
        returned_session_id = session_id
        try:
            data = json.loads(raw_output)
            output_text = data.get("result", raw_output)
            returned_session_id = data.get("session_id", session_id)
        except (json.JSONDecodeError, TypeError):
            pass  # Fall back to raw output

        # Retry on tool-use concurrency error
        if exit_code != 0 and "concurrency" in output_text.lower():
            if attempt < TOOL_CONCURRENCY_RETRIES:
                delay = TOOL_CONCURRENCY_BASE_DELAY * attempt
                logger.warning(f"Tool concurrency conflict, retrying in {delay}s (attempt {attempt}/{TOOL_CONCURRENCY_RETRIES})")
                await asyncio.sleep(delay)
                continue
            else:
                logger.error(f"Tool concurrency conflict persisted after {TOOL_CONCURRENCY_RETRIES} attempts")

        if exit_code != 0:
            logger.error(f"Claude Code FAILED (exit={exit_code}): {output_text[:500]}")
        else:
            logger.info(f"Claude Code OK (session={returned_session_id}, len={len(output_text)})")
        return exit_code, output_text, returned_session_id

    # Should not reach here, but just in case
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
        """Exchange API key for a short-lived WS JWT."""
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
                    logger.warning(f"Heartbeat failed: {e}")
                    break

    # -- Message handling ----------------------------------------------------

    async def handle_message(self, raw: str):
        """Route an incoming JSON-RPC message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON: {raw[:200]}")
            return

        # Response to our request (has "result" or "error")
        if "result" in msg:
            result = msg["result"]
            if isinstance(result, dict) and "registered" in result:
                if result["registered"]:
                    self.registered = True
                    logger.info(f"Registered as {result.get('agent_id', AGENT_NAME)}")
                else:
                    logger.error(f"Registration failed: {result.get('error')}")
            return

        if "error" in msg:
            logger.error(f"RPC error: {msg['error']}")
            return

        # Notification or request (has "method")
        method = msg.get("method")
        params = msg.get("params", {})
        msg_id = msg.get("id")

        if method == "connection.established":
            logger.info(f"Connected: {params.get('connection_id')}")
            await self.register()

        elif method == "heartbeat_ack":
            logger.debug("Heartbeat ack received")

        elif method == "task.execute":
            asyncio.create_task(self.handle_task_execute(msg_id, params))

        else:
            logger.debug(f"Unhandled method: {method}")

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
        metadata = params.get("metadata", {})
        company_id = metadata.get("company_id", COMPANY_ID)
        agent_task_id = metadata.get("agent_task_id")

        # Extract user message text
        message = params.get("message", {})
        parts = message.get("parts", [])
        user_text = ""
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "text":
                user_text += part.get("text", "")
            elif isinstance(part, str):
                user_text += part

        logger.info(f"Task received: {task_id} (agent_task={agent_task_id}, company={company_id})")
        logger.info(f"Message: {user_text[:200]}")

        # Limit concurrency
        if len(self._active_tasks) >= MAX_CONCURRENT_TASKS:
            logger.warning(f"At max concurrent tasks ({MAX_CONCURRENT_TASKS}), queuing...")

        # Per-task lock: serialize messages in the same conversation so
        # we don't try to --resume a Claude Code session that's still running
        if task_id not in self._task_locks:
            self._task_locks[task_id] = asyncio.Lock()
        task_lock = self._task_locks[task_id]

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
                finally:
                    self._active_tasks.discard(task_id)

    async def _execute_chat_task(self, task_id: str, user_text: str):
        """Handle a direct chat message — spawn Claude Code and return response.

        Uses --resume for follow-up messages in the same conversation (same task_id).
        """
        existing_session = self._task_sessions.get(task_id)
        is_followup = existing_session is not None
        logger.info(f"Chat task {task_id}: {user_text[:100]} (followup={is_followup})")

        if is_followup:
            prompt = user_text
        else:
            prompt = f"""You received a direct message from a user on Society AI.

User message: {user_text}

Respond helpfully. You have access to the codebase in the current working directory.
If they ask about code, read files and answer. If they ask you to make changes, do so."""

        exit_code, output, session_id = await run_claude_code(prompt, session_id=existing_session)

        # Track session for follow-ups (with bounded size)
        if session_id:
            self._task_sessions[task_id] = session_id
            # Evict oldest entries if map grows too large
            if len(self._task_sessions) > MAX_TRACKED_SESSIONS:
                oldest_keys = list(self._task_sessions.keys())[: MAX_TRACKED_SESSIONS // 2]
                for k in oldest_keys:
                    del self._task_sessions[k]

        # Send response back to hub
        result_text = output.strip() if output else "I couldn't generate a response."
        await self._send_task_complete(task_id, result_text, exit_code)

    async def _execute_agentorg_task(self, task_id: str, company_id: str, agent_task_id: str):
        """Handle an AgentOrg task — fetch context, spawn Claude Code, report results."""
        company = await fetch_company_details(company_id)
        if not company:
            company = {"name": "Unknown", "mission": "", "goals": []}

        task = await fetch_task_details(company_id, agent_task_id) if agent_task_id else None

        if not task:
            logger.warning(f"Could not fetch task details for {agent_task_id}, using trigger message")
            task = {
                "id": agent_task_id or task_id,
                "identifier": "UNKNOWN",
                "title": "Check task queue",
                "description": "A trigger was received but task details could not be fetched. Use list_tasks to find your assigned tasks.",
                "acceptanceCriteria": [],
                "companyId": company_id,
            }

        prompt = build_prompt(company, task)
        logger.info(f"Executing task: {task.get('identifier', 'unknown')} — {task.get('title', 'unknown')}")

        exit_code, output, _ = await run_claude_code(prompt)
        result_text = output.strip() if output else f"Task completed with exit code {exit_code}"
        await self._send_task_complete(task_id, result_text, exit_code)
        logger.info(f"Task {task.get('identifier', task_id)} completed (exit={exit_code})")

    async def _send_task_complete(self, task_id: str, result_text: str, exit_code: int = 0):
        """Send task.complete to the hub."""
        if len(result_text) > 4000:
            result_text = result_text[:4000] + "\n... (truncated)"
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
        reconnect_delay = 1

        while self.running:
            url = ws_url()
            logger.info(f"Connecting to {url}")

            # Get fresh JWT before each connection attempt
            try:
                await self.obtain_jwt()
            except Exception as e:
                logger.error(f"Failed to obtain JWT: {e}")
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
                        logger.warning(f"Connection closed: {e}")
                    finally:
                        heartbeat.cancel()
                        self.ws = None
                        self.registered = False

            except Exception as e:
                logger.error(f"Connection error: {e}")

            if self.running:
                logger.info(f"Reconnecting in {reconnect_delay}s...")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, MAX_RECONNECT_DELAY)

    def stop(self):
        """Signal the bridge to stop."""
        self.running = False
        logger.info("Shutting down...")


# -- Entry point --------------------------------------------------------------

def main():
    if not SOCIETY_AI_AUTH_TOKEN:
        print("Error: SOCIETY_AI_AUTH_TOKEN is required.")
        print("Get your API key at https://societyai.com and set it:")
        print("  export SOCIETY_AI_AUTH_TOKEN=sai_...")
        print("  python bridge.py")
        sys.exit(1)

    bridge = Bridge()

    loop = asyncio.new_event_loop()

    def handle_signal(sig, _):
        logger.info(f"Received signal {sig}")
        bridge.stop()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        loop.run_until_complete(bridge.run())
    except KeyboardInterrupt:
        bridge.stop()
    finally:
        if bridge._active_tasks:
            logger.info(f"Waiting for {len(bridge._active_tasks)} active tasks...")
            loop.run_until_complete(asyncio.sleep(2))
        loop.close()


if __name__ == "__main__":
    main()
