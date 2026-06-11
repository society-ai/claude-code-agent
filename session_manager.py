"""SessionManager — one Claude Code session per work item.

The core of the v0.7 execution model. Instead of spawning `claude -p` per
message (SDK-credit pool, no continuity, mixed-context), the bridge launches
a persistent *interactive* Claude Code session per work item (a task or a
chat thread) in its own tmux window:

  - interactive (not -p) → bills to the interactive pool, native compaction
  - one session per work item → clean, isolated context; per-task transcript
  - channel-attached → the bridge pushes events in and gets replies out
  - --session-id <uuid> generated up front → resume the SAME session later
    for review-rework or follow-up turns, with full task context

This module owns: launching sessions (with startup-prompt automation),
the work_item -> session registry, resume, idle reaping, and concurrency.
It is pure local plumbing — no platform knowledge, no policy decisions
(those come from fetched config). The bridge wires it to the platform.

Transport for inbound events / outbound replies is the ChannelHub; this
module only arranges for each session's channel server to point at the hub
with the right session_key (== work_item_key).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("session_manager")

REPO_DIR = pathlib.Path(__file__).resolve().parent
CHANNEL_SERVER = str(REPO_DIR / "channel" / "server.mjs")

# Tools an autonomous agent session needs without per-call prompts. We use a
# broad pre-seeded allow-list rather than the bypassPermissions flag (which
# carries a one-time interactive "accept" prompt). The machine owner scopes
# real access via WORK_DIR / EXTRA_DIRS, not this list.
DEFAULT_ALLOW = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "LS",
    "WebFetch", "WebSearch", "TodoWrite", "NotebookEdit", "Task",
    "mcp__society-ai-channel__reply",
    "mcp__society-ai",  # the platform tool server (prefix match)
]


@dataclass
class PersonaPolicy:
    """Per-persona runtime policy. Defaults here; overridden by fetched
    config (platform) then local env. See policy.py."""
    name: str
    work_dir: str
    extra_dirs: list[str] = field(default_factory=list)
    remote_control: bool = True
    keep_alive: bool = False           # supervisor / warm primaries
    idle_reap_minutes: int = 15
    max_concurrent: int = 3
    permission_mode: str = "default"   # 'default' | 'acceptEdits' | 'bypassPermissions'


@dataclass
class SessionRecord:
    work_item_key: str
    persona: str
    session_id: str                    # the uuid we pass to --session-id
    tmux_name: str
    title: str
    state: str = "starting"            # starting | ready | reaped | failed
    last_active: float = field(default_factory=time.time)
    has_run_once: bool = False         # has it been launched at least once (→ resume)


class SessionManager:
    def __init__(self, hub_sock_path: str):
        self._hub_sock = hub_sock_path
        self._sessions: dict[str, SessionRecord] = {}
        self._policies: dict[str, PersonaPolicy] = {}
        self._launch_locks: dict[str, asyncio.Lock] = {}
        self._reaper_task: Optional[asyncio.Task] = None
        # process-machine one-time flags
        self._bypass_accepted = False
        # Optional async callback fired after a session is reaped (idle,
        # concurrency, or explicit). The bridge uses it to ship the final
        # transcript delta + flip the platform mirror to 'ended'.
        self.on_reap = None  # async (SessionRecord) -> None

    # -- policy registration --------------------------------------------------

    def set_policy(self, policy: PersonaPolicy) -> None:
        self._policies[policy.name] = policy

    def policy(self, persona: str) -> PersonaPolicy:
        p = self._policies.get(persona)
        if p is None:
            # Safe default: cwd = repo dir (always trusted), no extra dirs.
            p = PersonaPolicy(name=persona, work_dir=str(REPO_DIR))
            self._policies[persona] = p
        return p

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> None:
        if self._reaper_task is None:
            self._reaper_task = asyncio.ensure_future(self._reaper_loop())

    async def stop(self) -> None:
        if self._reaper_task:
            self._reaper_task.cancel()
            self._reaper_task = None
        for rec in list(self._sessions.values()):
            await self._tmux_kill(rec.tmux_name)

    def get(self, work_item_key: str) -> Optional[SessionRecord]:
        return self._sessions.get(work_item_key)

    def touch(self, work_item_key: str) -> None:
        rec = self._sessions.get(work_item_key)
        if rec:
            rec.last_active = time.time()

    async def ensure_session(
        self,
        work_item_key: str,
        persona: str,
        *,
        title: str = "",
    ) -> SessionRecord:
        """Return a live session for the work item, launching or resuming as
        needed. Concurrency-safe per work item."""
        lock = self._launch_locks.setdefault(work_item_key, asyncio.Lock())
        async with lock:
            rec = self._sessions.get(work_item_key)
            if rec and rec.state == "ready" and await self._tmux_alive(rec.tmux_name):
                rec.last_active = time.time()
                return rec

            pol = self.policy(persona)
            await self._enforce_concurrency(persona)

            if rec is None:
                rec = SessionRecord(
                    work_item_key=work_item_key,
                    persona=persona,
                    session_id=str(uuid.uuid4()),
                    tmux_name=self._tmux_name(persona, work_item_key),
                    title=title or work_item_key,
                )
                self._sessions[work_item_key] = rec
            else:
                rec.title = title or rec.title

            resume = rec.has_run_once
            await self._launch(rec, pol, resume=resume)
            return rec

    async def reap(self, work_item_key: str) -> None:
        rec = self._sessions.get(work_item_key)
        if not rec:
            return
        await self._tmux_kill(rec.tmux_name)
        rec.state = "reaped"
        if self.on_reap is not None:
            try:
                await self.on_reap(rec)
            except Exception:
                logger.exception("on_reap callback failed for %s", work_item_key)

    # -- launching ------------------------------------------------------------

    async def _launch(self, rec: SessionRecord, pol: PersonaPolicy, *, resume: bool) -> None:
        self._write_workspace_config(rec, pol)

        # Fresh launch sets the session id with --session-id; resume reopens
        # it with --resume. The two flags are mutually exclusive — passing
        # both with the same id makes the CLI exit immediately.
        if resume:
            cmd = ["claude", "--resume", rec.session_id]
        else:
            cmd = ["claude", "--session-id", rec.session_id]
        for d in pol.extra_dirs:
            cmd += ["--add-dir", d]
        if pol.permission_mode and pol.permission_mode != "default":
            cmd += ["--permission-mode", pol.permission_mode]
        if pol.remote_control:
            cmd += ["--remote-control", rec.title[:60]]
        # Dev-flag loads our bare .mcp.json channel server during the research
        # preview. A packaged plugin + --channels replaces this post-GA.
        cmd += ["--dangerously-load-development-channels", "server:society-ai-channel"]

        # Launch detached in tmux, cwd = persona work dir.
        cwd = pol.work_dir
        shell_cmd = f"cd {_shq(cwd)} && exec " + " ".join(_shq(c) for c in cmd)
        await self._tmux_kill(rec.tmux_name)  # idempotent
        proc = await asyncio.create_subprocess_exec(
            "tmux", "new-session", "-d", "-s", rec.tmux_name,
            "-x", "200", "-y", "50", shell_cmd,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            rec.state = "failed"
            logger.error("tmux launch failed for %s: %s", rec.tmux_name,
                         (err or b"").decode("utf-8", "replace")[:300])
            raise RuntimeError("tmux launch failed")

        rec.has_run_once = True
        ready = await self._clear_startup_prompts(rec.tmux_name)
        rec.state = "ready" if ready else "failed"
        rec.last_active = time.time()
        logger.info("Session %s for %s (%s) state=%s resume=%s",
                    rec.session_id[:8], rec.work_item_key, rec.persona, rec.state, resume)

    def _write_workspace_config(self, rec: SessionRecord, pol: PersonaPolicy) -> None:
        """Write .mcp.json (channel server with this session's key) + project
        settings (enable the channel, pre-seed permission allow-rules) into
        the persona's work dir."""
        wd = pathlib.Path(pol.work_dir)
        wd.mkdir(parents=True, exist_ok=True)

        mcp = {
            "mcpServers": {
                "society-ai-channel": {
                    "command": "node",
                    "args": [CHANNEL_SERVER],
                    "env": {
                        "SOCIETY_AI_CHANNEL_SOCK": self._hub_sock,
                        "SOCIETY_AI_SESSION_KEY": rec.work_item_key,
                    },
                }
            }
        }
        _merge_json(wd / ".mcp.json", mcp, list_keys=())

        claude_dir = wd / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {
            "enabledMcpjsonServers": ["society-ai-channel"],
            "permissions": {"allow": list(DEFAULT_ALLOW)},
        }
        _merge_json(claude_dir / "settings.local.json", settings,
                    list_keys=("enabledMcpjsonServers",),
                    nested_list_keys={("permissions", "allow")})

    async def _clear_startup_prompts(self, tmux_name: str, timeout_s: float = 25.0) -> bool:
        """Drive past the known first-run prompts until the input box is ready.

        Handles: workspace-trust, bypassPermissions accept (once per machine),
        dev-channel confirm, and MCP-server consent. Returns True once the
        session shows its ready prompt, False on timeout/exit.
        """
        deadline = time.time() + timeout_s
        last_sig = ""
        stable_ready = 0
        while time.time() < deadline:
            pane = await self._tmux_capture(tmux_name)
            if pane is None:
                return False  # pane gone (session exited)
            low = pane.lower()

            if "bypass permissions mode" in low and "yes, i accept" in low:
                await self._tmux_send(tmux_name, "2", enter=True)
                self._bypass_accepted = True
                await asyncio.sleep(1.0)
                continue
            if "trust" in low and ("yes, i trust" in low or "do you trust" in low):
                await self._tmux_send(tmux_name, "", enter=True)  # default = trust
                await asyncio.sleep(1.0)
                continue
            if "loading development channels" in low and "local development" in low:
                await self._tmux_send(tmux_name, "", enter=True)  # default = dev
                await asyncio.sleep(1.0)
                continue
            if "new mcp server found" in low or "use this mcp server" in low:
                await self._tmux_send(tmux_name, "", enter=True)
                await asyncio.sleep(1.0)
                continue

            # Ready heuristic: the channel registration notice or the input
            # prompt with no pending menu. Require two consecutive stable reads.
            ready_now = (
                "messages from server:society-ai-channel" in low
                or ("❯" in pane and "enter to confirm" not in low)
            )
            sig = pane[-200:]
            if ready_now and sig == last_sig:
                stable_ready += 1
                if stable_ready >= 1:
                    return True
            else:
                stable_ready = 0
            last_sig = sig
            await asyncio.sleep(0.8)
        return False

    # -- concurrency / reaping ------------------------------------------------

    async def _enforce_concurrency(self, persona: str) -> None:
        pol = self.policy(persona)
        live = [
            r for r in self._sessions.values()
            if r.persona == persona and r.state == "ready"
        ]
        if len(live) < pol.max_concurrent:
            return
        # Reap the least-recently-active over the cap.
        live.sort(key=lambda r: r.last_active)
        for r in live[: len(live) - pol.max_concurrent + 1]:
            logger.info("Concurrency cap for %s: reaping %s", persona, r.work_item_key)
            await self.reap(r.work_item_key)

    async def _reaper_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                for rec in list(self._sessions.values()):
                    if rec.state != "ready":
                        continue
                    pol = self.policy(rec.persona)
                    if pol.keep_alive:
                        continue
                    if now - rec.last_active > pol.idle_reap_minutes * 60:
                        logger.info("Idle-reaping %s (idle %.0fm)",
                                    rec.work_item_key, (now - rec.last_active) / 60)
                        await self.reap(rec.work_item_key)
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("reaper loop error")

    # -- tmux helpers ---------------------------------------------------------

    @staticmethod
    def _tmux_name(persona: str, work_item_key: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in work_item_key)
        return f"sai-{persona}-{safe}"[:200]

    async def _tmux_alive(self, name: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "has-session", "-t", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def _tmux_kill(self, name: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "kill-session", "-t", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def _tmux_capture(self, name: str) -> Optional[str]:
        proc = await asyncio.create_subprocess_exec(
            "tmux", "capture-pane", "-t", name, "-p",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return None
        return out.decode("utf-8", "replace")

    async def _tmux_send(self, name: str, keys: str, *, enter: bool = False) -> None:
        if keys:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", name, keys,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            await asyncio.sleep(0.3)
        if enter:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", name, "Enter",
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()

    async def tmux_type(self, work_item_key: str, text: str) -> bool:
        """Type a literal message into a session's input + submit. Used for
        on-demand RC enable (/remote-control) and direct nudges. Returns False
        if the session isn't alive."""
        rec = self._sessions.get(work_item_key)
        if not rec or not await self._tmux_alive(rec.tmux_name):
            return False
        await self._tmux_send(rec.tmux_name, text, enter=False)
        await self._tmux_send(rec.tmux_name, "", enter=True)
        return True


# -- helpers ------------------------------------------------------------------

def _shq(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _merge_json(path: pathlib.Path, additions: dict, *, list_keys=(), nested_list_keys=frozenset()) -> None:
    """Merge `additions` into a JSON file, preserving existing content.
    list_keys: top-level keys whose lists should union. nested_list_keys: set
    of (parent, child) tuples whose lists should union."""
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}

    out = dict(existing)
    for k, v in additions.items():
        if k in list_keys and isinstance(v, list):
            cur = out.get(k) if isinstance(out.get(k), list) else []
            out[k] = sorted(set(cur) | set(v))
        elif isinstance(v, dict):
            base = out.get(k) if isinstance(out.get(k), dict) else {}
            merged = dict(base)
            for kk, vv in v.items():
                if (k, kk) in nested_list_keys and isinstance(vv, list):
                    curl = merged.get(kk) if isinstance(merged.get(kk), list) else []
                    merged[kk] = sorted(set(curl) | set(vv))
                else:
                    merged[kk] = vv
            out[k] = merged
        else:
            out[k] = v

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    os.replace(tmp, path)
