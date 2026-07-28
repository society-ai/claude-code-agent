"""Shared configuration for the Society AI Claude Code Agent.

All settings are read from environment variables. Validation runs at import
time so the bridge and MCP server fail fast on misconfiguration instead of
emitting opaque runtime errors later.
"""

from __future__ import annotations

import os
import re
import sys

__version__ = "0.12.0"

# -- Required / core ---------------------------------------------------------

AGENT_ROUTER_API_URL: str = os.getenv("AGENT_ROUTER_API_URL", "https://api.societyai.com").rstrip("/")
SOCIETY_AI_AUTH_TOKEN: str = os.getenv("SOCIETY_AI_AUTH_TOKEN", "").strip()
AGENT_NAME: str = os.getenv("AGENT_NAME", "claude-code").strip()
COMPANY_ID: str = os.getenv("COMPANY_ID", "").strip()

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

# -- Streaming + visibility --------------------------------------------------
# Controls how much intermediate progress the bridge surfaces to the Society
# AI chat UI while Claude Code is working. Values are validated below.
#   quiet   = no thinking markers, no text deltas; tool calls + results only
#   normal  = tool calls, tool results, and a "Thinking: <first sentence>"
#             marker per thinking block. Default.
#   verbose = everything in normal + assistant text streamed as partial
#             text_delta DataParts before the final task.complete

_VALID_STATUS_VERBOSITY = {"quiet", "normal", "verbose"}
STATUS_VERBOSITY: str = os.getenv("STATUS_VERBOSITY", "normal").strip().lower()

# -- Session mode (v0.7 execution model) -------------------------------------
# When enabled, the bridge dispatches work into persistent interactive Claude
# Code sessions (one per work item, tmux + channel) instead of spawning
# `claude -p` per message. This bills to the interactive pool (not the SDK
# credit pool, June 15 2026), gives native multi-turn continuity, and is the
# foundation for the supervisor architecture. Opt-in while it matures; the
# spawn path remains the fallback. Requires standard (non-sandbox) mode.
SESSION_MODE: bool = os.getenv("SESSION_MODE", "false").strip().lower() in (
    "1", "true", "yes", "on",
)

# Mirror session transcripts to Society AI (the recorded workspace). Only
# sessions the bridge itself launches are shipped — never the machine
# owner's own desktop/terminal sessions. Kill switch: MIRROR=false.
# Only meaningful when SESSION_MODE is on.
MIRROR_SESSIONS: bool = os.getenv("MIRROR", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

# What gets mirrored. SECURITY BOUNDARY — local-only setting, the platform
# can never raise it remotely:
#   messages = conversation only: user/dispatch text, assistant text, and
#              activity stubs (tool name + safe target like a file path —
#              never command lines, inputs, or outputs). Default.
#   full     = trimmed raw transcript records, including tool inputs and
#              results. Debug opt-in for your own agents only.
_VALID_MIRROR_LEVELS = {"messages", "full"}
MIRROR_LEVEL: str = os.getenv("MIRROR_LEVEL", "messages").strip().lower()
if MIRROR_LEVEL not in _VALID_MIRROR_LEVELS:
    print(
        f"Error: MIRROR_LEVEL must be one of {sorted(_VALID_MIRROR_LEVELS)} "
        f"(got {MIRROR_LEVEL!r})",
        file=sys.stderr,
    )
    sys.exit(2)

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

if STATUS_VERBOSITY not in _VALID_STATUS_VERBOSITY:
    print(
        f"Error: STATUS_VERBOSITY must be one of {sorted(_VALID_STATUS_VERBOSITY)} "
        f"(got {STATUS_VERBOSITY!r})",
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

# -- Extra directories the spawned Claude Code is allowed to read/write -------
# Claude Code 2.x sandboxes file access to the cwd it was launched in. The
# bridge launches it in WORK_DIR; anything outside WORK_DIR is off-limits.
# EXTRA_DIRS adds more directories via `claude -p --add-dir <path>` so the
# agent can work across multiple projects without you having to point WORK_DIR
# at your whole home. Each entry must be an absolute path. Missing directories
# are warned about and skipped — we don't fail startup, since a user might
# legitimately have one of them mounted only sometimes.

EXTRA_DIRS_RAW: str = os.getenv("EXTRA_DIRS", "").strip()
EXTRA_DIRS: list[str] = []
if EXTRA_DIRS_RAW:
    for _candidate in EXTRA_DIRS_RAW.split(","):
        _candidate = _candidate.strip()
        if not _candidate:
            continue
        if not os.path.isabs(_candidate):
            print(
                f"Warning: EXTRA_DIRS entry must be an absolute path, skipping {_candidate!r}",
                file=sys.stderr,
            )
            continue
        if not os.path.isdir(_candidate):
            print(
                f"Warning: EXTRA_DIRS entry {_candidate!r} is not an existing directory, skipping",
                file=sys.stderr,
            )
            continue
        EXTRA_DIRS.append(_candidate)

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
