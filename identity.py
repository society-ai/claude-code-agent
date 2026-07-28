"""Mutable per-session agent identity.

Historically the agent identity (AGENT_NAME / SOCIETY_AI_AUTH_TOKEN /
AGENT_ROUTER_API_URL) was baked into module constants at import time, so a
session was permanently whoever the MCP server process was spawned as —
usually the `${VAR:-default}` fallback in the user-scope MCP config, i.e.
whichever persona last ran setup.sh. This module makes identity a runtime
binding instead:

- The initial binding still comes from the environment (same precedence as
  before), so harness-dispatched sessions with injected env are unchanged.
- `bind(<name>)` rebinds the session to another persona configured on this
  machine (an `.env.<name>` file — see hook_common.discover_personas),
  swapping name, token, API URL, company, and bridge socket together.
- Every bind is mirrored to ~/.cache/society-ai/session-binding/<ppid>.json
  so the status-line hook (a sibling child of the same `claude` process,
  hence the shared PPID) can display the session's true identity.

State is held in this process's memory only. The MCP server is one-per-
session, so two concurrent sessions can be two different agents without
fighting over a shared pointer file.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from dataclasses import dataclass, replace

import config
from hook_common import discover_personas

BINDING_DIR = (
    pathlib.Path(os.path.expanduser("~")) / ".cache" / "society-ai" / "session-binding"
)


@dataclass(frozen=True)
class Identity:
    name: str
    token: str
    api_url: str
    company_id: str = ""
    bridge_socket: str = ""
    display_name: str = ""  # user-given name, e.g. "kilo"; "" if unknown
    # "env" (spawn-time), "auto" (single persona on the machine), "switch"
    # (rebound in-session), or "unbound" (no identity yet — several
    # personas exist and none was injected; tools refuse until bound).
    source: str = "env"

    @property
    def bound(self) -> bool:
        return bool(self.token)

    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": f"claude-code-agent/{config.__version__}",
        }


def _display_name_for(name: str) -> str:
    """Best-effort display-name lookup from the persona env files."""
    try:
        for p in discover_personas():
            if p["name"] == name:
                return p.get("display_name", "")
    except Exception:
        pass
    return ""


def _initial_identity() -> Identity:
    """Spawn-time binding. Injected env wins (harness dispatch, old-style
    baked configs). With no token in the env: exactly one persona on the
    machine auto-binds (the common single-agent install stays zero-config);
    several personas start UNBOUND — the user picks with switch_agent.
    Keep this rule in sync with hook_session_start's banner logic."""
    if config.SOCIETY_AI_AUTH_TOKEN:
        return Identity(
            name=config.AGENT_NAME,
            token=config.SOCIETY_AI_AUTH_TOKEN,
            api_url=config.AGENT_ROUTER_API_URL,
            company_id=config.COMPANY_ID,
            bridge_socket=config.SOCIETY_AI_BRIDGE_SOCKET,
            display_name=_display_name_for(config.AGENT_NAME),
            source="env",
        )
    try:
        available = discover_personas()
    except Exception:
        available = []
    if len(available) == 1:
        p = available[0]
        if p.get("bridge_socket"):
            os.environ["SOCIETY_AI_BRIDGE_SOCKET"] = p["bridge_socket"]
        return Identity(
            name=p["name"],
            token=p["token"],
            api_url=p["api_url"],
            company_id=p.get("company_id", ""),
            bridge_socket=p.get("bridge_socket", ""),
            display_name=p.get("display_name", ""),
            source="auto",
        )
    return Identity(
        name="",
        token="",
        api_url=config.AGENT_ROUTER_API_URL,
        source="unbound",
    )


_current = _initial_identity()


def current() -> Identity:
    return _current


def personas() -> list[dict[str, str]]:
    """All personas configured on this machine (delegates to hook_common)."""
    return discover_personas()


def bind(agent_name: str) -> Identity:
    """Rebind this session to the named persona — by canonical name
    (agent-8u6qy3ba) or by the user-given display name (case-insensitive).
    Raises ValueError if the persona isn't configured on this machine."""
    global _current
    agent_name = (agent_name or "").strip()
    all_personas = personas()
    matches = [p for p in all_personas if p["name"] == agent_name]
    if not matches:
        wanted = agent_name.lower()
        matches = [
            p for p in all_personas
            if p.get("display_name", "").lower() == wanted
        ]
    if not matches:
        available = ", ".join(
            p["name"] + (f' ("{p["display_name"]}")' if p.get("display_name") else "")
            for p in all_personas
        ) or "(none)"
        raise ValueError(
            f"No persona named {agent_name!r} on this machine. Available: {available}"
        )
    p = matches[0]
    _current = Identity(
        name=p["name"],
        token=p["token"],
        api_url=p["api_url"],
        company_id=p.get("company_id", ""),
        bridge_socket=p.get("bridge_socket", ""),
        display_name=p.get("display_name", ""),
        source="switch",
    )
    # bridge_ipc resolves its socket from env at call time — repoint
    # delegation/search at this persona's bridge (clear if it has none).
    if _current.bridge_socket:
        os.environ["SOCIETY_AI_BRIDGE_SOCKET"] = _current.bridge_socket
    else:
        os.environ.pop("SOCIETY_AI_BRIDGE_SOCKET", None)
    write_binding()
    return _current


# -- Status-line binding file -------------------------------------------------


def _binding_path() -> pathlib.Path:
    return BINDING_DIR / f"{os.getppid()}.json"


def write_binding() -> None:
    """Mirror the current binding for the status-line hook. Best-effort."""
    try:
        BINDING_DIR.mkdir(parents=True, exist_ok=True)
        _reap_dead()
        payload = {
            "agent": _current.name,
            "display_name": _current.display_name,
            "api_url": _current.api_url,
            "source": _current.source,
            "pid": os.getpid(),
            "at": time.time(),
        }
        tmp = _binding_path().with_suffix(".tmp")
        with tmp.open("w") as f:
            json.dump(payload, f)
        tmp.replace(_binding_path())
    except Exception:
        pass


def _reap_dead() -> None:
    """Drop binding files whose claude process (the filename PID) is gone."""
    try:
        for f in BINDING_DIR.glob("*.json"):
            try:
                pid = int(f.stem)
            except ValueError:
                continue
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                f.unlink(missing_ok=True)
            except PermissionError:
                pass  # alive, not ours
    except Exception:
        pass


def refresh_display_name(fetched: str) -> Identity:
    """Adopt a display name fetched from the platform (/api/v1/agents/me)
    for the CURRENT identity, and persist it to the persona's env file so
    the hooks (which read env files, not the API) pick it up too. Called
    by switch_agent after a successful bind; best-effort."""
    global _current
    # Same sanitization as setup.sh: the value lands in a shell-sourced
    # env file, so strip quoting/expansion characters and cap the length.
    fetched = (fetched or "").translate(str.maketrans("", "", '"`$\\\n\r'))[:80].strip()
    if not fetched or fetched == _current.display_name or not _current.name:
        return _current
    _current = replace(_current, display_name=fetched)
    write_binding()
    try:
        for p in discover_personas():
            if p["name"] == _current.name and p.get("env_file"):
                _set_env_line(p["env_file"], "DISPLAY_NAME", f'"{fetched}"')
                break
    except Exception:
        pass
    return _current


def _set_env_line(path: str, key: str, value: str) -> None:
    """Insert or replace one KEY=value line in an env file, preserving the
    rest byte-for-byte and the 0600 mode."""
    import re

    with open(path) as f:
        content = f.read()
    line = f"{key}={value}"
    if re.search(rf"^{key}=", content, flags=re.M):
        content = re.sub(rf"^{key}=.*$", lambda m: line, content, count=1, flags=re.M)
    else:
        content = content.rstrip("\n") + "\n" + line + "\n"
    with open(path, "w") as f:
        f.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_binding(ppid: int) -> dict | None:
    """Read the binding for a given claude PID (used by the status line)."""
    try:
        with (BINDING_DIR / f"{ppid}.json").open() as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None
