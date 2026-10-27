"""Shared configuration for the Society AI Claude Code Agent.

All settings are read from environment variables. Validation runs at import
time so the bridge and MCP server fail fast on misconfiguration instead of
emitting opaque runtime errors later.
"""

from __future__ import annotations

import os
import re
import sys

__version__ = "0.25.0"

# -- Required / core ---------------------------------------------------------

# NOTE: `or` (not the getenv default) on purpose — the MCP config registers
# these as ${VAR:-} so an un-injected session gets EMPTY strings, which must
# behave exactly like unset (empty AGENT_NAME = unbound, see identity.py).
AGENT_ROUTER_API_URL: str = (
    os.getenv("AGENT_ROUTER_API_URL", "").strip() or "https://api.societyai.com"
).rstrip("/")
SOCIETY_AI_AUTH_TOKEN: str = os.getenv("SOCIETY_AI_AUTH_TOKEN", "").strip()
AGENT_NAME: str = os.getenv("AGENT_NAME", "").strip()
COMPANY_ID: str = os.getenv("COMPANY_ID", "").strip()

# IPC socket path for bridge <-> MCP server communication (delegation, search).
SOCIETY_AI_BRIDGE_SOCKET: str = (
    os.getenv("SOCIETY_AI_BRIDGE_SOCKET", "").strip()
    or os.path.join(os.path.expanduser("~"), ".cache", "society-ai", "bridge.sock")
)

# -- Session mode (v0.7 execution model) -------------------------------------
# When enabled, the bridge dispatches work into persistent interactive Claude
# Code sessions (one per work item, tmux + channel) instead of spawning
# `claude -p` per message. This bills to the interactive pool (not the SDK
# credit pool, June 15 2026), gives native multi-turn continuity, and is the
# foundation for the supervisor architecture. Default ON (matching the status
# panel's machine defaults) — session mode is what makes transcripts mirror
# to the platform; the spawn path never mirrors, so a fresh install that
# silently fell back to spawn would run invisible sessions. If session-mode
# startup fails (missing tmux/channel deps) the bridge still falls back to
# the spawn path at runtime. SESSION_MODE=false opts out explicitly.
# Requires standard (non-sandbox) mode.
SESSION_MODE: bool = os.getenv("SESSION_MODE", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

# Mirror session transcripts to Society AI (the recorded workspace). Only
# sessions the bridge itself launches are shipped — never the machine
# owner's own desktop/terminal sessions (those get a Tracker only, below).
# Kill switch: MIRROR=false.
# Only meaningful when SESSION_MODE is on.
MIRROR_SESSIONS: bool = os.getenv("MIRROR", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

# Session Trackers: a short, current Tracker for every Claude Code session on
# this machine (status, open questions, approvals, checklist, git loose ends),
# written by the owner's hidden Tracker agent with a small model. Only the
# Tracker leaves the machine, never the conversation. On unless TRACKING=false
# here, MIRROR=false, or "Track my sessions" is off in the app.
TRACKING_ENABLED: bool = MIRROR_SESSIONS and os.getenv("TRACKING", "true").strip().lower() in (
    "1", "true", "yes", "on",
)

# What gets mirrored is fixed, not a setting: the conversation only. Claude
# Code's own tool activity and thinking never leave the machine (see
# transcript_shipper). Older installs may still have MIRROR_LEVEL /
# STATUS_VERBOSITY in their env files; both are ignored.

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


def ws_url(api_url: str = "") -> str:
    """Derive WebSocket URL from an API URL (default: the process-wide
    AGENT_ROUTER_API_URL). Callers pass the agent's own api_url so it
    connects to the backend its token was minted for."""
    url = (api_url or AGENT_ROUTER_API_URL).rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://"):] + "/ws/agents"
    return "ws://" + url[len("http://"):] + "/ws/agents"
