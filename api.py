"""Shared HTTP client for the Society AI Claude Code Agent.

Used by both bridge.py (for fetching task/company context) and mcp_server.py
(for the MCP tool surface). Centralizing the client here means TLS context,
timeouts, headers, and error wrapping behave identically on both sides.

All helpers return either parsed JSON on success or a structured `_error()`
dict on failure. Nothing in this module raises HTTP errors to the caller —
LLM tools that show stack traces are unhelpful for autonomous recovery.
"""

from __future__ import annotations

import ssl
from typing import Any

import certifi
import httpx

import identity
from config import __version__

DEFAULT_TIMEOUT = 30

# Module-level shared client. Lazy-instantiated; not auto-closed (caller
# closes via `close()` during shutdown).
_client: httpx.AsyncClient | None = None


def _build_client() -> httpx.AsyncClient:
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return httpx.AsyncClient(timeout=DEFAULT_TIMEOUT, verify=ssl_ctx)


def client() -> httpx.AsyncClient:
    """Get (or create) the shared httpx.AsyncClient."""
    global _client
    if _client is None or _client.is_closed:
        _client = _build_client()
    return _client


async def close() -> None:
    """Close the shared client. Call during shutdown."""
    global _client
    if _client and not _client.is_closed:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None


def error(message: str, status: int | None = None, body: str | None = None) -> dict[str, Any]:
    """Build a structured error result."""
    err: dict[str, Any] = {"error": True, "message": message}
    if status is not None:
        err["status"] = status
    if body is not None:
        err["body"] = body[:1000]
    return err


async def request(
    method: str,
    path: str,
    params: dict | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    base_url: str | None = None,
) -> Any:
    """Perform an HTTP request. Returns parsed JSON or an error dict.

    Args:
        method: HTTP method (GET, POST, PATCH, DELETE).
        path: URL path (joined with base_url or AGENT_ROUTER_API_URL).
        params: Optional query params.
        body: Optional JSON body.
        headers: Override headers (default: API_HEADERS).
        timeout: Override timeout in seconds.
        base_url: Override base URL (e.g. for ai-chatbot service routes).

    Base URL and auth headers resolve from the CURRENT session identity on
    every call (identity.current()), not from import-time constants — a
    switch_agent rebind takes effect immediately.
    """
    ident = identity.current()
    if not ident.bound:
        names = ", ".join(
            (p.get("display_name") or p["name"]) for p in identity.personas()
        ) or "(none configured)"
        return error(
            "No Society AI agent is bound to this session. Ask the user "
            f"which agent to act as (available: {names}), then call "
            "switch_agent with that name."
        )
    base = (base_url or ident.api_url).rstrip("/")
    url = f"{base}{path}"
    hdrs = headers if headers is not None else ident.headers()
    try:
        c = client()
        if timeout is not None:
            resp = await c.request(method, url, headers=hdrs, params=params, json=body, timeout=timeout)
        else:
            resp = await c.request(method, url, headers=hdrs, params=params, json=body)
    except httpx.HTTPError as e:
        return error(f"Network error calling {method} {path}: {e}")
    except Exception as e:  # defensive
        return error(f"Unexpected error calling {method} {path}: {type(e).__name__}: {e}")

    if resp.status_code >= 400:
        return error(
            f"HTTP {resp.status_code} from {method} {path}",
            status=resp.status_code,
            body=resp.text,
        )

    if resp.status_code == 204 or not resp.content:
        return {}

    try:
        return resp.json()
    except ValueError:
        return error(
            f"Non-JSON response from {method} {path}",
            status=resp.status_code,
            body=resp.text,
        )


async def get(path: str, params: dict | None = None, **kw: Any) -> Any:
    return await request("GET", path, params=params, **kw)


async def post(path: str, body: Any = None, **kw: Any) -> Any:
    return await request("POST", path, body=body, **kw)


async def patch(path: str, body: Any = None, **kw: Any) -> Any:
    return await request("PATCH", path, body=body, **kw)


async def delete(path: str, **kw: Any) -> Any:
    return await request("DELETE", path, **kw)


__all__ = [
    "client",
    "close",
    "error",
    "request",
    "get",
    "post",
    "patch",
    "delete",
    "__version__",
]
