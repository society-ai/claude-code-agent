"""Per-persona runtime policy resolution.

THE IP BOUNDARY, ENFORCED IN CODE: this bridge contains NO runtime policy of
its own beyond conservative defaults. The real policy (which agents run
always-on, RC visibility, concurrency, reap timing, permission posture) is
*platform-owned content*, fetched from Society AI at connect time and per
change. The bridge is transport: it applies whatever the platform sends.

Resolution order (last wins):
  1. Built-in defaults (conservative, here)
  2. Platform-fetched config  (GET /api/v1/claude-code/policy)
  3. Local env override       (.env / .env.<persona> — machine-owner sovereignty)

Local always beats platform for resources the machine owner controls (file
scope, whether something runs at all) — same principle as WORK_DIR.

The platform endpoint does not exist yet (Phase 5 ships the UI + route);
fetch failures fall back to defaults silently, so the bridge works today.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from session_manager import PersonaPolicy

logger = logging.getLogger("policy")

_POLICY_PATH = "/api/v1/claude-code/policy"


def _env(persona: str, key: str) -> Optional[str]:
    """Read KEY from the persona's environment. The bridge sources the
    persona's .env into its own environment per launch, so os.environ holds
    the active persona's values."""
    v = os.environ.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _as_bool(s: Optional[str], default: bool) -> bool:
    if s is None:
        return default
    return s.lower() in ("1", "true", "yes", "on")


def _as_int(s: Optional[str], default: int) -> int:
    try:
        return int(s) if s is not None else default
    except ValueError:
        return default


def default_policy(persona: str, work_dir: str, extra_dirs: list[str]) -> PersonaPolicy:
    """Layer 1 — conservative built-in defaults."""
    return PersonaPolicy(
        name=persona,
        work_dir=work_dir,
        extra_dirs=extra_dirs,
        remote_control=True,     # default-on: a visible workforce
        keep_alive=False,        # ephemeral task sessions by default
        idle_reap_minutes=15,
        max_concurrent=3,
        permission_mode="bypassPermissions",  # self-hosted autonomous agent
    )


async def fetch_platform_policy(
    api_url: str, token: str, persona: str, timeout: float = 5.0
) -> Optional[dict]:
    """Layer 2 — platform-owned policy. Returns the raw dict or None when the
    endpoint is absent/unreachable (which is the case until Phase 5)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.get(
                f"{api_url.rstrip('/')}{_POLICY_PATH}",
                params={"agent_name": persona},
                headers={"Authorization": f"Bearer {token}"},
            )
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("policy fetch unavailable (expected pre-Phase-5): %s", e)
    return None


def apply_platform(policy: PersonaPolicy, fetched: dict) -> None:
    """Overlay platform-fetched fields onto the policy (Layer 2)."""
    if "remote_control" in fetched:
        policy.remote_control = bool(fetched["remote_control"])
    if "keep_alive" in fetched:
        policy.keep_alive = bool(fetched["keep_alive"])
    if "idle_reap_minutes" in fetched:
        policy.idle_reap_minutes = int(fetched["idle_reap_minutes"])
    if "max_concurrent" in fetched:
        policy.max_concurrent = int(fetched["max_concurrent"])
    if isinstance(fetched.get("permission_mode"), str):
        policy.permission_mode = fetched["permission_mode"]


def apply_local_env(policy: PersonaPolicy, persona: str) -> None:
    """Overlay local env (Layer 3 — machine-owner sovereignty, wins)."""
    policy.remote_control = _as_bool(_env(persona, "REMOTE_CONTROL"), policy.remote_control)
    policy.keep_alive = _as_bool(_env(persona, "KEEP_ALIVE"), policy.keep_alive)
    policy.idle_reap_minutes = _as_int(_env(persona, "IDLE_REAP_MINUTES"), policy.idle_reap_minutes)
    policy.max_concurrent = _as_int(_env(persona, "MAX_CONCURRENT"), policy.max_concurrent)
    pm = _env(persona, "PERMISSION_MODE")
    if pm:
        policy.permission_mode = pm


async def resolve_policy(
    persona: str,
    work_dir: str,
    extra_dirs: list[str],
    api_url: str,
    token: str,
) -> PersonaPolicy:
    """Full three-layer resolution. Used by the bridge per persona on connect."""
    policy = default_policy(persona, work_dir, extra_dirs)
    fetched = await fetch_platform_policy(api_url, token, persona)
    if fetched:
        apply_platform(policy, fetched)
    apply_local_env(policy, persona)
    return policy
