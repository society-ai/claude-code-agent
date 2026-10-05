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
import sys
import uuid
from dataclasses import dataclass, field
from typing import Optional

from contacts import contacts_dir

logger = logging.getLogger("session_manager")

REPO_DIR = pathlib.Path(__file__).resolve().parent
CHANNEL_SERVER = str(REPO_DIR / "channel" / "server.mjs")
CHANNEL_SERVER_NAME = "society-ai-channel"

# Env a contact session at chat/read must not inherit: the agent's platform
# credential and identity. Those sessions get no Society AI tools, and nothing
# in them should be able to act as the agent.
CONTACT_STRIPPED_ENV = (
    "SOCIETY_AI_AUTH_TOKEN", "AGENT_NAME", "COMPANY_ID", "SOCIETY_AI_BRIDGE_SOCKET",
)

# Built-in tools a contact session gets per level. `act` gets everything.
CONTACT_TOOLS = {"chat": "", "read": "Read,Grep,Glob"}

# How long to hold out for the channel banner before accepting a bare input
# prompt as "ready". Channel load takes ~1-3s on a warm machine; this leaves
# room for a slow MCP handshake without stalling a session whose channel is
# genuinely absent.
PROMPT_FALLBACK_S = 12.0

# Tools an autonomous agent session needs without per-call prompts. We use a
# broad pre-seeded allow-list rather than the bypassPermissions flag (which
# carries a one-time interactive "accept" prompt). The machine owner scopes
# real access via WORK_DIR / EXTRA_DIRS, not this list.
DEFAULT_ALLOW = [
    "Bash", "Read", "Edit", "Write", "Glob", "Grep", "LS",
    "WebFetch", "WebSearch", "TodoWrite", "NotebookEdit", "Task",
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
    # Per-agent environment injected into each spawned `claude` session so it
    # acts as THIS agent (token, name, IPC socket): the session's society-ai
    # MCP reads these from its environment, so they must be the agent's, not
    # whatever the user-scope MCP config falls back to.
    session_env: dict = field(default_factory=dict)
    # Machine owner's cap on what this agent may do for ANY contact
    # (chat | read | act). Local only: the platform can never raise it.
    contact_permission_ceiling: str = "act"


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
    background: bool = False           # automation (wakes/schedules): no RC row
    fresh_launch: bool = False         # last ensure_session() created a brand-new
                                       # Claude session (no prior context) — the
                                       # bridge sends the platform protocol then
    failure: str = ""                  # why the last launch did not reach ready,
                                       # in words the person who sent the message
                                       # can act on
    permission: Optional[str] = None   # None = an owner session; otherwise the
                                       # contact level it was launched with
                                       # (chat | read | act)
    contact_label: str = ""            # who the contact is, for titles and logs


class SessionManager:
    def __init__(self, hub_sock_path: str):
        self._hub_sock = hub_sock_path
        self._sessions: dict[str, SessionRecord] = {}
        self._aliases: dict[str, str] = {}  # alias key -> canonical work_item_key
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

    def resolve(self, work_item_key: str) -> str:
        """Resolve an aliased key (e.g. the chat a task session projects
        into) to its canonical work-item key."""
        return self._aliases.get(work_item_key, work_item_key)

    def alias(self, alias_key: str, canonical_key: str) -> None:
        """Route a second work-item key into an existing session. Used so
        replies typed in the session's platform chat continue the SAME
        Claude Code session instead of opening a new one."""
        if alias_key and alias_key != canonical_key:
            self._aliases[alias_key] = canonical_key

    def get(self, work_item_key: str) -> Optional[SessionRecord]:
        return self._sessions.get(self.resolve(work_item_key))

    def snapshot(self) -> list[dict]:
        """Read-only view of live sessions for the local status panel.

        Returns one dict per tracked session (newest activity first). No
        transcript content — only the operational shape (what's running,
        how old, what kind). Aliases are reported alongside their canonical
        session so the panel can show 'chat X → work item Y'.
        """
        now = time.time()
        alias_by_canonical: dict[str, list[str]] = {}
        for alias_key, canonical in self._aliases.items():
            alias_by_canonical.setdefault(canonical, []).append(alias_key)

        rows = []
        for rec in self._sessions.values():
            rows.append({
                "work_item_key": rec.work_item_key,
                "persona": rec.persona,
                "title": rec.title,
                "kind": "background" if rec.background else "interactive",
                "state": rec.state,
                "tmux": rec.tmux_name,
                "idle_seconds": int(now - rec.last_active),
                "aliases": alias_by_canonical.get(rec.work_item_key, []),
            })
        rows.sort(key=lambda r: r["idle_seconds"])
        return rows

    def touch(self, work_item_key: str) -> None:
        rec = self._sessions.get(self.resolve(work_item_key))
        if rec:
            rec.last_active = time.time()

    async def ensure_session(
        self,
        work_item_key: str,
        persona: str,
        *,
        title: str = "",
        background: bool = False,
        permission: Optional[str] = None,
        contact_label: str = "",
    ) -> SessionRecord:
        """Return a live session for the work item, launching or resuming as
        needed. Concurrency-safe per work item.

        `permission` is None for the owner and the contact level otherwise.
        A live session launched at a different level (the owner changed the
        contact's permissions) is restarted with --resume, so the
        conversation keeps its history under the new limits."""
        work_item_key = self.resolve(work_item_key)
        lock = self._launch_locks.setdefault(work_item_key, asyncio.Lock())
        async with lock:
            rec = self._sessions.get(work_item_key)
            if rec and rec.state == "ready" and await self._tmux_alive(rec.tmux_name):
                if rec.permission == permission:
                    rec.last_active = time.time()
                    rec.fresh_launch = False
                    return rec
                logger.info("Permissions for %s changed (%s -> %s); relaunching",
                            work_item_key[:40], rec.permission, permission)
                await self._tmux_kill(rec.tmux_name)

            pol = self.policy(persona)
            await self._enforce_concurrency(persona)

            if rec is None:
                rec = SessionRecord(
                    work_item_key=work_item_key,
                    persona=persona,
                    session_id=str(uuid.uuid4()),
                    tmux_name=self._tmux_name(persona, work_item_key),
                    title=title or work_item_key,
                    background=background,
                )
                self._sessions[work_item_key] = rec
            else:
                rec.title = title or rec.title
            rec.permission = permission
            rec.contact_label = contact_label

            resume = rec.has_run_once
            rec.fresh_launch = not resume
            await self._launch(rec, pol, resume=resume)
            return rec

    async def reap(self, work_item_key: str) -> None:
        rec = self._sessions.get(self.resolve(work_item_key))
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
        if rec.permission is None:
            cwd, cmd, env_set, env_unset = self._owner_command(rec, pol, resume)
        else:
            cwd, cmd, env_set, env_unset = self._contact_command(rec, pol, resume)
        if rec.permission in CONTACT_TOOLS:
            # Strict sessions load MCP servers only from --mcp-config, and the
            # dev channel is found there.
            cmd += ["--mcp-config", json.dumps(self._channel_mcp_config(rec))]
        else:
            # Without --strict-mcp-config the dev channel is only found among
            # file-configured servers, so it lives in the folder's .mcp.json.
            # That file is the same for every session in the folder: each
            # launch passes its own key and hub socket in its environment
            # (two sessions starting together used to overwrite each other's
            # key in the file).
            self._write_workspace_config(cwd)
            env_set = {
                **env_set,
                "SOCIETY_AI_SESSION_KEY": rec.work_item_key,
                "SOCIETY_AI_CHANNEL_SOCK": self._hub_sock,
            }
        # Dev-flag loads the channel server during the research preview. A
        # packaged plugin + --channels replaces this post-GA.
        cmd += ["--dangerously-load-development-channels", f"server:{CHANNEL_SERVER_NAME}"]

        # Launch detached in tmux. Per-agent env is injected as inline
        # assignments on the exec so the spawned claude (and its society-ai
        # MCP) act as THIS agent, not whichever persona the user-scope MCP
        # config falls back to. Contact sessions below act drop that env.
        env_prefix = "".join(f"-u {k} " for k in env_unset)
        env_prefix += "".join(f"{k}={_shq(str(v))} " for k, v in env_set.items() if v)
        os.makedirs(cwd, exist_ok=True)
        shell_cmd = (
            f"cd {_shq(cwd)} && exec env {env_prefix}"
            + " ".join(_shq(c) for c in cmd)
        )
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
        rec.failure = ""
        if not ready:
            rec.failure = await self._explain_failed_launch(rec, pol)
            logger.error("Session %s for %s did not start: %s",
                         rec.session_id[:8], rec.work_item_key, rec.failure)
            # A boot that outlasted even the generous gate may still finish
            # later; left alone it would sit as an orphan holding this
            # session's key (and a Remote Control row). Kill it — the next
            # dispatch relaunches with --resume and loses nothing.
            await self._tmux_kill(rec.tmux_name)
        rec.last_active = time.time()
        logger.info("Session %s for %s (%s) state=%s resume=%s",
                    rec.session_id[:8], rec.work_item_key, rec.persona, rec.state, resume)

    async def _explain_failed_launch(self, rec: SessionRecord, pol: PersonaPolicy) -> str:
        """Say why a launch never reached the ready prompt. The pane is gone
        by the time a fast exit is noticed, taking its error with it, so the
        CLI is re-run on its own to recover the reason. A broken install
        (half-finished update, binary not executable) is the common case,
        and it fails the same way on every launch until someone fixes it."""
        alive = await self._tmux_alive(rec.tmux_name)
        if alive:
            pane = (await self._tmux_capture(rec.tmux_name)) or ""
            tail = [ln.strip() for ln in pane.splitlines() if ln.strip()][-5:]
            logger.error("Pane of %s at startup timeout:\n%s", rec.tmux_name, "\n".join(tail))
        problem = await self._claude_cli_problem(pol)
        if problem:
            return (
                f"Claude Code is not working on this machine ({problem}). "
                "Reinstall Claude Code, check that `claude --version` works "
                "in a terminal, then send the message again."
            )
        if alive:
            return ("Claude Code did not finish starting within 2 minutes. "
                    "The bridge log has the last screen it showed.")
        return ("Claude Code exited right after starting. Run `claude` in "
                f"{pol.work_dir} to see the error.")

    async def _claude_cli_problem(self, pol: PersonaPolicy) -> str:
        """Run `claude --version` the way a session runs `claude`: same PATH,
        same cwd. Returns what went wrong, or "" if the CLI runs."""
        env = dict(os.environ)
        env.update({k: str(v) for k, v in (pol.session_env or {}).items() if v})
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude", "--version", cwd=pol.work_dir, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            return "the `claude` command is not on the bridge's PATH"
        except OSError as e:
            return f"`claude` could not be run: {e.strerror or e}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            return "`claude --version` did not answer within 30 seconds"
        if proc.returncode == 0:
            return ""
        if proc.returncode < 0:
            return f"`claude` was killed by signal {-proc.returncode}, usually a corrupt binary"
        lines = [ln.strip() for ln in (err or out).decode("utf-8", "replace").splitlines() if ln.strip()]
        detail = f": {lines[0]}" if lines else ""
        return f"`claude --version` failed with exit code {proc.returncode}{detail}"

    def session_cwd(self, rec: SessionRecord) -> str:
        """The folder a session runs in, which is also where Claude Code
        keeps its transcript."""
        if rec.permission is None:
            return self.policy(rec.persona).work_dir
        return contacts_dir(rec.persona)

    def _session_flags(self, rec: SessionRecord, pol: PersonaPolicy, resume: bool) -> list[str]:
        # Fresh launch sets the session id with --session-id; resume reopens
        # it with --resume. The two flags are mutually exclusive — passing
        # both with the same id makes the CLI exit immediately.
        cmd = ["claude", "--resume" if resume else "--session-id", rec.session_id]
        # Background automation (wakes, schedules) never claims a Remote
        # Control sidebar row — only user-, contact- and task-originated
        # sessions do.
        if pol.remote_control and not rec.background:
            cmd += ["--remote-control", rec.title[:60]]
        return cmd

    def _owner_command(self, rec: SessionRecord, pol: PersonaPolicy, resume: bool):
        cmd = self._session_flags(rec, pol, resume)
        for d in pol.extra_dirs:
            cmd += ["--add-dir", d]
        if pol.permission_mode and pol.permission_mode != "default":
            cmd += ["--permission-mode", pol.permission_mode]
        # Marks the session as platform-driven. Hooks use it to drop output
        # meant for a human at this terminal: whatever the session writes is
        # now the response the platform sends back, so local-only furniture
        # (the identity banner) would end up in the web app.
        env = dict(pol.session_env or {})
        env["SOCIETY_AI_DISPATCHED"] = "1"
        return pol.work_dir, cmd, env, ()

    def _contact_command(self, rec: SessionRecord, pol: PersonaPolicy, resume: bool):
        """A session working on a contact's request. All of an agent's
        contact sessions share one empty folder, so they group under a
        single directory in the Claude Code sidebar.

        chat / read: --restricted (no command-running tools, user/project
        settings and memory ignored, file tools confined to the working
        folders), an explicit built-in tool list, only the channel MCP server
        (no Society AI tools), dontAsk so nothing can stall on a prompt, and
        no agent credential in the environment. The bridge's reply path
        hooks come in through --settings, because --restricted ignores the
        user settings they normally live in.

        act: the owner's toolset, working in the agent's folders."""
        cmd = self._session_flags(rec, pol, resume)
        work_dirs = [pol.work_dir, *pol.extra_dirs]
        if rec.permission == "act":
            for d in work_dirs:
                cmd += ["--add-dir", d]
            if pol.permission_mode and pol.permission_mode != "default":
                cmd += ["--permission-mode", pol.permission_mode]
            env = dict(pol.session_env or {})
            env["SOCIETY_AI_DISPATCHED"] = "1"
            return contacts_dir(pol.name), cmd, env, ()

        cmd += [
            "--restricted",
            "--tools", CONTACT_TOOLS[rec.permission],
            "--strict-mcp-config",
            "--permission-mode", "dontAsk",
            "--settings", json.dumps(self._reply_hook_settings()),
        ]
        if rec.permission == "read":
            for d in work_dirs:
                cmd += ["--add-dir", d]
        return contacts_dir(pol.name), cmd, {"SOCIETY_AI_DISPATCHED": "1"}, CONTACT_STRIPPED_ENV

    @staticmethod
    def _reply_hook_settings() -> dict:
        """Only the hooks the reply path needs: Stop (a turn ended: ship the
        transcript and hand the reply back) and UserPromptSubmit (mirror the
        prompt early). Mirror-only: the owner's task reminders and identity
        banner never reach a contact session."""
        py = _shq(sys.executable)

        def hook(script: str, *args: str) -> list:
            command = " ".join([py, _shq(str(REPO_DIR / script)), *args])
            return [{"hooks": [{"type": "command", "command": command, "timeout": 5}]}]

        return {"hooks": {
            "Stop": hook("hook_stop.py", "--mirror-only"),
            "UserPromptSubmit": hook("hook_user_prompt.py"),
        }}

    def _channel_mcp_config(self, rec: SessionRecord) -> dict:
        return {
            "mcpServers": {
                CHANNEL_SERVER_NAME: {
                    "command": "node",
                    "args": [CHANNEL_SERVER],
                    "env": {
                        "SOCIETY_AI_CHANNEL_SOCK": self._hub_sock,
                        "SOCIETY_AI_SESSION_KEY": rec.work_item_key,
                    },
                }
            }
        }

    def _write_workspace_config(self, cwd: str) -> None:
        """Register the channel server in the folder's .mcp.json (key and hub
        socket expanded from each session's environment), enable it without
        a prompt, and pre-seed permission allow-rules."""
        wd = pathlib.Path(cwd)
        wd.mkdir(parents=True, exist_ok=True)
        mcp = {
            "mcpServers": {
                CHANNEL_SERVER_NAME: {
                    "command": "node",
                    "args": [CHANNEL_SERVER],
                    "env": {
                        "SOCIETY_AI_CHANNEL_SOCK": "${SOCIETY_AI_CHANNEL_SOCK}",
                        "SOCIETY_AI_SESSION_KEY": "${SOCIETY_AI_SESSION_KEY}",
                    },
                }
            }
        }
        _merge_json(wd / ".mcp.json", mcp, list_keys=())

        claude_dir = wd / ".claude"
        claude_dir.mkdir(exist_ok=True)
        settings = {
            "enabledMcpjsonServers": [CHANNEL_SERVER_NAME],
            "permissions": {"allow": list(DEFAULT_ALLOW)},
        }
        _merge_json(claude_dir / "settings.local.json", settings,
                    list_keys=("enabledMcpjsonServers",),
                    nested_list_keys={("permissions", "allow")})

    async def _clear_startup_prompts(self, tmux_name: str, timeout_s: float = 120.0) -> bool:
        """Drive past the known first-run prompts until the input box is ready.

        Handles: workspace-trust, bypassPermissions accept (once per machine),
        dev-channel confirm, and MCP-server consent. Returns True once the
        session shows its ready prompt, False on timeout/exit.

        The budget is sized for the CLI's WORST boot, not its usual one. A
        healthy boot is ~3s, but the first boots after the CLI reinstalls
        itself run tens of seconds to minutes rebuilding caches — and during
        that window a startup prompt can be VISIBLE while the input loop is
        not yet processing keys, so the answers this loop sends are simply
        dropped until the process wakes up. Re-sending every pass is what
        eventually lands one. A 25s budget failed exactly this way after the
        2026-08-02 self-reinstall: prompt on screen, keys ignored, gate
        expired, dispatch dead.
        """
        started = time.time()
        deadline = started + timeout_s
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
                # Normally the highlighted default is "trust"; in --restricted
                # mode it is "No, exit". Move off it before confirming.
                cursor = next((ln for ln in pane.splitlines() if "❯" in ln), "").lower()
                if "no" in cursor and "trust" not in cursor:
                    await self._tmux_send(tmux_name, "Down")
                    await asyncio.sleep(0.3)
                await self._tmux_send(tmux_name, "", enter=True)
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

            # Ready heuristic. The channel banner is the only signal that
            # says anything about the channel actually being loaded, so it
            # is the one we want. The bare input prompt is NOT equivalent:
            # the TUI draws it while MCP servers are still connecting, and
            # treating it as ready is how a dispatch ends up pushed into a
            # session that cannot receive it yet. Keep it only as a late
            # fallback so a session whose channel never loads still starts
            # (the bridge's delivery ack is what makes that safe).
            banner = "messages from server:society-ai-channel" in low
            prompt_only = "❯" in pane and "enter to confirm" not in low
            ready_now = banner or (
                prompt_only and time.time() - started > PROMPT_FALLBACK_S
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

    def touch_by_session_id(self, session_id: str) -> None:
        """Refresh the idle clock for whichever work item this Claude session
        belongs to. Called from the hook-notification path: locally-typed
        turns never pass through a platform dispatch, and before this the
        reaper counted them as idle time — it once killed a session fifteen
        seconds after the owner sent a message in it."""
        for rec in self._sessions.values():
            if rec.session_id == session_id:
                rec.last_active = time.time()
                return

    def _transcript_recently_active(self, rec: SessionRecord, window_s: float) -> bool:
        """Is the session's transcript still being written to? The idle clock
        only sees dispatches and hook notifications; a single turn longer
        than the idle window has neither, and reaping it kills work in
        flight. The transcript file grows for the whole turn, so its mtime
        is the honest liveness signal of last resort."""
        try:
            from transcript_shipper import transcript_path

            mtime = os.path.getmtime(
                transcript_path(self.session_cwd(rec), rec.session_id)
            )
        except (OSError, Exception):
            return False
        if time.time() - mtime < window_s:
            rec.last_active = max(rec.last_active, mtime)
            return True
        return False

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
                    if now - rec.last_active > pol.idle_reap_minutes * 60 and \
                            not self._transcript_recently_active(
                                rec, pol.idle_reap_minutes * 60):
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
        rec = self._sessions.get(self.resolve(work_item_key))
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

