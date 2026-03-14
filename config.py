"""Shared configuration for the Society AI Claude Code Agent."""

import os


AGENT_ROUTER_API_URL = os.getenv("AGENT_ROUTER_API_URL", "https://api.societyai.com")
SOCIETY_AI_AUTH_TOKEN = os.getenv("SOCIETY_AI_AUTH_TOKEN", "")
AGENT_NAME = os.getenv("AGENT_NAME", "claude-code")
COMPANY_ID = os.getenv("COMPANY_ID", "")

# Derived
API_HEADERS = {
    "Authorization": f"Bearer {SOCIETY_AI_AUTH_TOKEN}",
    "Content-Type": "application/json",
}


def ws_url() -> str:
    """Derive WebSocket URL from API URL."""
    url = AGENT_ROUTER_API_URL
    if url.startswith("https://"):
        return url.replace("https://", "wss://") + "/ws/agents"
    return url.replace("http://", "ws://") + "/ws/agents"
