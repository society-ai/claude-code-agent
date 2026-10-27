"""Which Society AI account each agent on this machine belongs to.

setup.sh uses it to warn when a new agent belongs to a different account than
the agents already here. That works, but everything on one macOS user is
shared whatever the account: one Claude Code login and its credits, one home
folder, and one default agent for the sessions started in Claude Code itself.

    venv/bin/python accounts.py .env.<persona>

prints the agents on this machine that belong to another account, one per
line, and nothing when it cannot tell (offline, a rejected key).
"""

from __future__ import annotations

import pathlib
import ssl
import sys
from typing import Optional

import certifi
import httpx

from hook_common import DEFAULT_API_URL, discover_personas, parse_env_file


def token_owner(token: str, api_url: str) -> Optional[str]:
    """The account an agent token belongs to (the agent-token exchange says)."""
    try:
        r = httpx.post(
            f"{api_url.rstrip('/')}/auth/agent-token", json={"api_key": token}, timeout=10.0,
            verify=ssl.create_default_context(cafile=certifi.where()),
        )
        if r.status_code != 200:
            return None
        return r.json().get("agent_id") or None
    except (httpx.HTTPError, ValueError):
        return None


def other_account_agents(env_file: pathlib.Path) -> list[str]:
    env = parse_env_file(env_file)
    name, token = env.get("AGENT_NAME", ""), env.get("SOCIETY_AI_AUTH_TOKEN", "")
    api_url = (env.get("AGENT_ROUTER_API_URL") or DEFAULT_API_URL).rstrip("/")
    owner = token_owner(token, api_url) if name and token else None
    if owner is None:
        return []
    others = []
    for p in discover_personas():
        if p["name"] == name or p["api_url"].rstrip("/") != api_url:
            continue
        other = token_owner(p["token"], api_url)
        if other and other != owner:
            others.append(p["name"])
    return others


if __name__ == "__main__":
    print("\n".join(other_account_agents(pathlib.Path(sys.argv[1]))))
