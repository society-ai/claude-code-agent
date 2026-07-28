"""Shared helpers for the Claude Code hooks (SessionStart / Stop / statusLine).

Persona discovery: every persona on this machine is an env file in the
repo dir — `.env` (the default persona) plus `.env.<name>` for each
additional one (written by `setup.sh --persona`). The hooks iterate all
of them so a machine running two agents sees both in the session
snapshot, the stop reminder, and the status line. No registry file to
keep in sync — the env files ARE the registry.
"""

from __future__ import annotations

import os
import pathlib
import re

REPO_DIR = pathlib.Path(__file__).resolve().parent
DEFAULT_API_URL = "https://api.societyai.com"

# .env.<persona> — same name shape the WS hub accepts for agents.
_PERSONA_ENV_RE = re.compile(r"^\.env\.([a-z0-9][a-z0-9._-]{0,62})$")
_EXCLUDED = {".env.example"}
# setup.sh keeps timestamped backups (.env.bak-<epoch>, .env.<p>.bak-<epoch>)
# before overwriting a config. They match the persona regex (dots are legal
# in agent names) but are NOT personas — listing them invents duplicate
# agents with possibly-stale tokens. Same fix as status.sh (fd0b3ce).
_BACKUP_RE = re.compile(r"\.bak-\d+$")


def parse_env_file(path: pathlib.Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


_VAR_DEFAULT_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*))?\}$")


def _expand(value: str) -> str:
    """Expand a `${VAR:-default}` MCP-config value the way Claude Code does,
    against THIS process's environment. Hooks are children of the same
    `claude` process that spawned the MCP server, so they inherit the same
    env — the expansion here matches what the server actually received."""
    m = _VAR_DEFAULT_RE.match(value or "")
    if not m:
        return value or ""
    var, default = m.group(1), m.group(2) or ""
    return os.environ.get(var) or default


def resolve_mcp_identity(cwd: str | None = None) -> dict[str, str] | None:
    """Resolve the identity the society-ai MCP server was spawned with.

    Checks the project-scope .mcp.json in `cwd` first (it shadows the
    user-scope entry of the same name), then ~/.claude.json. Returns
    {name, api_url, company_id} or None if no entry is found. The token is
    deliberately not returned — display surfaces don't need it.
    """
    import json

    def _entry(path: pathlib.Path) -> dict | None:
        try:
            with path.open() as f:
                data = json.load(f)
        except Exception:
            return None
        servers = data.get("mcpServers") or {}
        entry = servers.get("society-ai")
        return entry if isinstance(entry, dict) else None

    entry = None
    if cwd:
        entry = _entry(pathlib.Path(cwd) / ".mcp.json")
    if entry is None:
        entry = _entry(pathlib.Path(os.path.expanduser("~")) / ".claude.json")
    if entry is None:
        return None

    env = entry.get("env") or {}
    name = _expand(env.get("AGENT_NAME", "")) or "claude-code"
    api_url = (
        _expand(env.get("AGENT_ROUTER_API_URL", "")) or DEFAULT_API_URL
    ).rstrip("/")
    company_id = _expand(env.get("COMPANY_ID", ""))
    return {"name": name, "api_url": api_url, "company_id": company_id}


def discover_personas() -> list[dict[str, str]]:
    """Return [{name, token, api_url}] for every configured persona.

    The default persona (.env) comes first. Personas without a token or
    AGENT_NAME are skipped — a half-written env file shouldn't break
    every hook on the machine.
    """
    personas: list[dict[str, str]] = []

    candidates: list[pathlib.Path] = []
    default_env = REPO_DIR / ".env"
    if default_env.is_file():
        candidates.append(default_env)
    for p in sorted(REPO_DIR.glob(".env.*")):
        if p.name in _EXCLUDED or not p.is_file():
            continue
        if _BACKUP_RE.search(p.name):
            continue
        if _PERSONA_ENV_RE.match(p.name):
            candidates.append(p)

    seen: set[str] = set()
    for path in candidates:
        env = parse_env_file(path)
        token = (env.get("SOCIETY_AI_AUTH_TOKEN") or "").strip()
        name = (env.get("AGENT_NAME") or "").strip()
        if not token or not name or name in seen:
            continue
        seen.add(name)
        api_url = (
            env.get("AGENT_ROUTER_API_URL") or os.environ.get("AGENT_ROUTER_API_URL") or DEFAULT_API_URL
        ).strip().rstrip("/")
        personas.append({
            "name": name,
            "token": token,
            "api_url": api_url,
            "company_id": (env.get("COMPANY_ID") or "").strip(),
            "bridge_socket": (env.get("SOCIETY_AI_BRIDGE_SOCKET") or "").strip(),
            "env_file": str(path),
        })

    return personas
