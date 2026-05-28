"""Shared configuration for the Society AI Claude Code Agent.

All settings are read from environment variables. Validation runs at import
time so the bridge and MCP server fail fast on misconfiguration instead of
emitting opaque runtime errors later.
"""

from __future__ import annotations

import os
import re
import sys

__version__ = "0.2.0"

# -- Required / core ---------------------------------------------------------

AGENT_ROUTER_API_URL: str = os.getenv("AGENT_ROUTER_API_URL", "https://api.societyai.com").rstrip("/")
SOCIETY_AI_AUTH_TOKEN: str = os.getenv("SOCIETY_AI_AUTH_TOKEN", "").strip()
AGENT_NAME: str = os.getenv("AGENT_NAME", "claude-code").strip()
COMPANY_ID: str = os.getenv("COMPANY_ID", "").strip()

# Optional service-auth key for routes that only accept the platform's
# internal service token (e.g. `POST /api/internal/artifacts/ingest`). When
# present, `save_artifact` will upload via that route; when absent, the tool
# returns a clear error explaining the limitation rather than silently failing.
SOCIETY_AI_SERVICE_KEY: str = os.getenv("SOCIETY_AI_SERVICE_KEY", "").strip()

# IPC socket path for bridge <-> MCP server communication (delegation, search).
SOCIETY_AI_BRIDGE_SOCKET: str = os.getenv(
    "SOCIETY_AI_BRIDGE_SOCKET",
    os.path.join(os.path.expanduser("~"), ".cache", "society-ai", "bridge.sock"),
).strip()

# Gate for high-power agent lifecycle tools (deploy/update/restart/delete).
# Disabled by default — an LLM accidentally deploying or deleting agents is
# a costly mistake to recover from. Set to "true" to enable.
ENABLE_AGENT_LIFECYCLE: bool = os.getenv("ENABLE_AGENT_LIFECYCLE", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

# -- Execution mode ----------------------------------------------------------

_VALID_EXECUTION_MODES = {"standard", "secured"}
EXECUTION_MODE: str = os.getenv("EXECUTION_MODE", "standard").strip().lower()
SANDBOX_NAME: str = os.getenv("SANDBOX_NAME", "society-ai-agent").strip()
SANDBOX_BASE_IMAGE: str = os.getenv("SANDBOX_BASE_IMAGE", "claude").strip()

try:
    SANDBOX_TIMEOUT: int = int(os.getenv("SANDBOX_TIMEOUT", "600"))
    if SANDBOX_TIMEOUT <= 0:
        raise ValueError
except ValueError:
    print(
        f"Error: SANDBOX_TIMEOUT must be a positive integer (got {os.getenv('SANDBOX_TIMEOUT')!r})",
        file=sys.stderr,
    )
    sys.exit(2)

# -- Validation --------------------------------------------------------------

# Agent name: lowercase letters, digits, hyphens, underscores, dots.
# Mirrors the constraints the WS hub places on agent IDs (used in URLs and
# routing keys). Keep this stricter rather than looser.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

if EXECUTION_MODE not in _VALID_EXECUTION_MODES:
    print(
        f"Error: EXECUTION_MODE must be one of {sorted(_VALID_EXECUTION_MODES)} "
        f"(got {EXECUTION_MODE!r})",
        file=sys.stderr,
    )
    sys.exit(2)

if AGENT_NAME and not _AGENT_NAME_RE.match(AGENT_NAME):
    print(
        f"Error: AGENT_NAME must be lowercase alphanumerics with -, _, or . "
        f"(got {AGENT_NAME!r}). Example: claude-code-laptop",
        file=sys.stderr,
    )
    sys.exit(2)

if not AGENT_ROUTER_API_URL.startswith(("http://", "https://")):
    print(
        f"Error: AGENT_ROUTER_API_URL must start with http:// or https:// (got {AGENT_ROUTER_API_URL!r})",
        file=sys.stderr,
    )
    sys.exit(2)

# -- Derived -----------------------------------------------------------------

API_HEADERS = {
    "Authorization": f"Bearer {SOCIETY_AI_AUTH_TOKEN}",
    "Content-Type": "application/json",
    "User-Agent": f"claude-code-agent/{__version__}",
}


def ws_url() -> str:
    """Derive WebSocket URL from API URL."""
    url = AGENT_ROUTER_API_URL
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):] + "/ws/agents"
    return "ws://" + url[len("http://"):] + "/ws/agents"
