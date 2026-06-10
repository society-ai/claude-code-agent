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
        if _PERSONA_ENV_RE.match(p.name):
            candidates.append(p)

    for path in candidates:
        env = parse_env_file(path)
        token = (env.get("SOCIETY_AI_AUTH_TOKEN") or "").strip()
        name = (env.get("AGENT_NAME") or "").strip()
        if not token or not name:
            continue
        api_url = (
            env.get("AGENT_ROUTER_API_URL") or os.environ.get("AGENT_ROUTER_API_URL") or DEFAULT_API_URL
        ).strip().rstrip("/")
        personas.append({"name": name, "token": token, "api_url": api_url})

    return personas
