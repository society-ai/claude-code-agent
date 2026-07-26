"""Society AI — local status & control panel for the Claude Code bridge.

A localhost-only web panel for the machine owner to see and control their
self-hosted Society AI agents. It is NOT a copy of the platform (tasks/inbox
live on societyai.com) — it owns only what exists on THIS machine.

Config is split (the way people actually use it):
  - Per agent (.env / .env.<persona>): identity + where it may work
    (WORK_DIR, EXTRA_DIRS). These genuinely differ per agent.
  - Machine-wide (.env.defaults): mirroring, security mode, limits,
    verbosity — one shared Settings that applies to every agent. The
    launcher sources .env.defaults before each persona's file.

Security: binds 127.0.0.1 only; every mutation needs a token (0600 file) and
a loopback Origin/Host, so a website you visit can't drive your agents.

Run:  ./status.sh
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

REPO_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_LABEL = "io.societyai.claude-code-bridge"
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "society-ai")
TOKEN_FILE = os.path.join(CACHE_DIR, "status-token")
SERVICE_SH = os.path.join(REPO_DIR, "service.sh")
DEFAULTS_FILE = os.path.join(REPO_DIR, ".env.defaults")
DEFAULT_ENV = os.path.join(REPO_DIR, ".env")
DEFAULT_PORT = int(os.environ.get("STATUS_PORT", "8787"))

# ---------------------------------------------------------------------------
# Config schemas. Field: key, label, type, plus optional enum/help/advanced/
# readonly/default. Types: bool | int | enum | path | paths | str | secret.
# ---------------------------------------------------------------------------

# Per-agent — identity + where the agent may work.
AGENT_SCHEMA = [
    {"key": "AGENT_NAME", "label": "Name", "type": "str", "readonly": True},
    {"key": "WORK_DIR", "label": "Works in", "type": "path",
     "help": "The agent's main folder — where it starts and works by default."},
    {"key": "EXTRA_DIRS", "label": "Also has access to", "type": "paths",
     "help": "Extra folders it may read and write, beyond its main folder."},
    {"key": "COMPANY_ID", "label": "Default company", "type": "str", "advanced": True,
     "help": "Optional company UUID used when a dispatch omits one."},
    {"key": "AGENT_ROUTER_API_URL", "label": "API URL", "type": "str", "advanced": True,
     "default": "https://api.societyai.com"},
    {"key": "SOCIETY_AI_AUTH_TOKEN", "label": "Auth token", "type": "secret", "advanced": True,
     "help": "The agent's credential. Leave blank to keep the current one."},
]

# Machine-wide — applies to every agent on this machine.
MACHINE_SCHEMA = [
    {"key": "SESSION_MODE", "label": "Persistent sessions", "type": "bool", "default": True,
     "help": "Keep a session per work item (the recorded workspace)."},
    {"key": "MIRROR", "label": "Record sessions to Society AI", "type": "bool", "default": True,
     "help": "Ship session transcripts to the platform so you can review them."},
    {"key": "MIRROR_LEVEL", "label": "Recording detail", "type": "enum",
     "enum": ["messages", "full"], "default": "messages",
     "help": "messages = conversation + safe activity (recommended). full = includes tool inputs/outputs (local debug only)."},
    {"key": "EXECUTION_MODE", "label": "Where agents run", "type": "enum",
     "enum": ["standard", "secured"], "default": "standard",
     "help": "standard = directly on this machine, with the folder access you grant each agent. secured = an isolated sandbox that can't see any of your files — only Society AI tools and limited network (also turns off the per-agent folders above). Use it for work you don't want touching your machine."},
    {"key": "STATUS_VERBOSITY", "label": "Progress detail in chat", "type": "enum",
     "enum": ["quiet", "normal", "verbose"], "default": "normal",
     "help": "How much an agent narrates while it works — quiet shows just the result, verbose streams its thinking and every step."},
    {"key": "IDLE_REAP_MINUTES", "label": "End idle sessions after (min)", "type": "int", "default": 15,
     "help": "An inactive session is wrapped up and recorded after this many minutes, freeing resources."},
    {"key": "MAX_CONCURRENT", "label": "Max sessions at once", "type": "int", "default": 3,
     "help": "How many work sessions a single agent runs in parallel before new ones queue."},
    {"key": "PERMISSION_MODE", "label": "Permission mode", "type": "enum", "advanced": True,
     "enum": ["default", "acceptEdits", "bypassPermissions"], "default": "bypassPermissions"},
    {"key": "ENABLE_AGENT_LIFECYCLE", "label": "Allow deploy/delete tools", "type": "bool",
     "advanced": True, "default": False,
     "help": "Lets agents deploy/update/delete OTHER agents. Off by default."},
    {"key": "REMOTE_CONTROL", "label": "Controllable from platform", "type": "bool",
     "advanced": True, "default": True},
    {"key": "KEEP_ALIVE", "label": "Keep sessions warm", "type": "bool", "advanced": True, "default": False},
    {"key": "SANDBOX_NAME", "label": "Sandbox name", "type": "str", "advanced": True, "default": "society-ai-agent"},
    {"key": "SANDBOX_BASE_IMAGE", "label": "Sandbox image", "type": "str", "advanced": True, "default": "claude"},
    {"key": "SANDBOX_TIMEOUT", "label": "Sandbox timeout (s)", "type": "int", "advanced": True, "default": 600},
]

AGENT_BY_KEY = {f["key"]: f for f in AGENT_SCHEMA}
MACHINE_BY_KEY = {f["key"]: f for f in MACHINE_SCHEMA}
ALL_BY_KEY = {**AGENT_BY_KEY, **MACHINE_BY_KEY}
SECRET_KEYS = {f["key"] for f in AGENT_SCHEMA if f["type"] == "secret"}


# ---------------------------------------------------------------------------
# .env read / write / strip — order- and comment-preserving, shell-safe.
# ---------------------------------------------------------------------------

_ENV_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$""")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v.split(" #", 1)[0].strip()


def read_env(path: str) -> dict:
    out: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.lstrip().startswith("#"):
                    continue
                m = _ENV_LINE.match(line)
                if m:
                    out[m.group(1)] = _unquote(m.group(2))
    except FileNotFoundError:
        pass
    return out


def _shell_quote(value: str) -> str:
    if value == "" or re.fullmatch(r"[A-Za-z0-9_/.:,@%+=-]+", value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`")
    return f'"{escaped}"'


def write_env(path: str, updates: dict) -> None:
    """Update KEY=value in place (preserving comments/order); append unknowns."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    remaining = dict(updates)
    out = []
    for line in lines:
        m = _ENV_LINE.match(line)
        if m and m.group(1) in remaining:
            k = m.group(1)
            out.append(f"{k}={_shell_quote(str(remaining.pop(k)))}\n")
        else:
            out.append(line)
    if remaining:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        for k, v in remaining.items():
            out.append(f"{k}={_shell_quote(str(v))}\n")
    _atomic_write(path, "".join(out))


def strip_env_keys(path: str, keys: set) -> None:
    """Remove KEY=... lines for the given keys (used to lift machine settings
    out of persona files so the shared .env.defaults wins)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return
    out = [ln for ln in lines if not (_ENV_LINE.match(ln) and _ENV_LINE.match(ln).group(1) in keys)]
    if len(out) != len(lines):
        _atomic_write(path, "".join(out))


def _atomic_write(path: str, content: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Validation.
# ---------------------------------------------------------------------------

def validate(updates: dict, allowed: dict) -> tuple[dict, dict]:
    clean: dict[str, str] = {}
    errors: dict[str, str] = {}
    for key, raw in updates.items():
        field = allowed.get(key)
        if field is None or field.get("readonly"):
            continue
        t = field["type"]
        try:
            if t == "secret":
                if raw is None or str(raw).strip() == "":
                    continue
                clean[key] = str(raw).strip()
            elif t == "bool":
                clean[key] = "true" if (raw is True or str(raw).lower() in ("1", "true", "yes", "on")) else "false"
            elif t == "int":
                n = int(str(raw).strip())
                if n < 0:
                    raise ValueError("must be 0 or more")
                clean[key] = str(n)
            elif t == "enum":
                v = str(raw).strip()
                if v not in field["enum"]:
                    raise ValueError(f"must be one of {field['enum']}")
                clean[key] = v
            elif t == "path":
                v = str(raw).strip()
                if v:
                    if not os.path.isabs(v):
                        raise ValueError("must be an absolute path")
                    if not os.path.isdir(v):
                        raise ValueError("folder does not exist")
                clean[key] = v
            elif t == "paths":
                parts = [p.strip() for p in str(raw).split(",") if p.strip()]
                for p in parts:
                    if not os.path.isabs(p):
                        raise ValueError(f"{p}: must be an absolute path")
                    if not os.path.isdir(p):
                        raise ValueError(f"{p}: does not exist")
                clean[key] = ",".join(parts)
            else:
                clean[key] = str(raw).strip()
        except ValueError as e:
            errors[key] = str(e)
    return clean, errors


# ---------------------------------------------------------------------------
# Persona discovery + live IPC.
# ---------------------------------------------------------------------------

def discover_personas() -> list[dict]:
    personas = []
    for entry in sorted(os.listdir(REPO_DIR)):
        if entry == ".env":
            persona_arg, pid = "", "default"
        elif entry.startswith(".env.") and entry not in (".env.example", ".env.defaults"):
            persona_arg = entry[len(".env."):]
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,62}", persona_arg):
                continue
            # setup.sh keeps timestamped backups (.env.bak-<epoch>,
            # .env.<persona>.bak-<epoch>) before overwriting a config. They
            # are not agents; listing them invents personas that don't exist.
            if re.search(r"\.?bak-\d+$", persona_arg):
                continue
            pid = persona_arg
        else:
            continue
        env = read_env(os.path.join(REPO_DIR, entry))
        label = BASE_LABEL if not persona_arg else f"{BASE_LABEL}.{persona_arg}"
        sock = env.get("SOCIETY_AI_BRIDGE_SOCKET") or (
            os.path.join(CACHE_DIR, "bridge.sock") if not persona_arg
            else os.path.join(CACHE_DIR, persona_arg, "bridge.sock")
        )
        plist = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", f"{label}.plist")
        personas.append({
            "id": pid, "persona_arg": persona_arg, "env_file": entry,
            "display": env.get("AGENT_NAME") or pid, "label": label,
            "socket": sock, "installed": os.path.isfile(plist), "env": env,
        })
    return personas


def ipc_call(sock_path: str, method: str, params: dict | None = None, timeout: float = 3.0) -> dict:
    if not os.path.exists(sock_path):
        return {"error": True, "down": True, "message": "socket not present"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(sock_path)
            s.sendall((json.dumps({"id": "status", "method": method, "params": params or {}}) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf.decode().splitlines()[0])
        return resp.get("result", resp) if isinstance(resp, dict) else {"error": True, "message": "bad response"}
    except (ConnectionRefusedError, FileNotFoundError):
        return {"error": True, "down": True, "message": "bridge not running"}
    except Exception as e:  # noqa: BLE001
        return {"error": True, "message": f"{type(e).__name__}: {e}"}


def machine_config() -> dict:
    """Effective machine-wide settings: .env.defaults wins, else the default
    persona's .env (legacy location), else the schema default."""
    defaults = read_env(DEFAULTS_FILE)
    legacy = read_env(DEFAULT_ENV)
    cfg = {}
    for f in MACHINE_SCHEMA:
        if f["key"] in defaults:
            cfg[f["key"]] = defaults[f["key"]]
        elif f["key"] in legacy:
            cfg[f["key"]] = legacy[f["key"]]
        else:
            cfg[f["key"]] = f.get("default", "")
    return cfg


def build_status() -> dict:
    agents = []
    for p in discover_personas():
        live = ipc_call(p["socket"], "status")
        running = not live.get("down", False)
        if running and live.get("error"):
            live = {"version": "?", "registered": False, "ws_connected": None, "sessions": []}
        cfg = {}
        for f in AGENT_SCHEMA:
            if f["key"] in SECRET_KEYS:
                cfg[f["key"]] = "set" if p["env"].get(f["key"]) else ""
            else:
                cfg[f["key"]] = p["env"].get(f["key"], f.get("default", ""))
        agents.append({
            "id": p["id"], "persona_arg": p["persona_arg"], "display": p["display"],
            "label": p["label"], "installed": p["installed"], "running": running,
            "live": live if running else None, "config": cfg,
        })
    return {
        "agent_schema": AGENT_SCHEMA, "machine_schema": MACHINE_SCHEMA,
        "agents": agents, "machine": machine_config(), "repo": REPO_DIR,
    }


def run_service(cmd: str, persona_arg: str) -> dict:
    args = ["bash", SERVICE_SH, cmd] + ([persona_arg] if persona_arg else [])
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=60, cwd=REPO_DIR)
        return {"ok": r.returncode == 0, "code": r.returncode, "output": (r.stdout + r.stderr).strip()[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": f"service.sh {cmd} timed out"}


def find_persona(persona_id: str) -> dict | None:
    for p in discover_personas():
        if p["id"] == persona_id:
            return p
    return None


# ---------------------------------------------------------------------------
# HTTP — localhost only, token + Origin gated on mutations.
# ---------------------------------------------------------------------------

def load_token() -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            tok = f.read().strip()
            if tok:
                return tok
    except FileNotFoundError:
        pass
    tok = secrets.token_urlsafe(24)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok)
    return tok


TOKEN = load_token()


class Handler(BaseHTTPRequestHandler):
    server_version = "SocietyAIStatus/2.0"

    def log_message(self, *_):
        pass

    def _host_is_local(self) -> bool:
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost", "::1", "")

    def _origin_is_local(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).hostname in ("127.0.0.1", "localhost", "::1")

    def _authed(self, qs: dict) -> bool:
        supplied = self.headers.get("X-Status-Token") or (qs.get("t", [""])[0])
        return bool(supplied) and secrets.compare_digest(supplied, TOKEN)

    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if not self._host_is_local():
            return self._send(403, b"forbidden", "text/plain")
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        if parsed.path == "/api/status":
            if not self._authed(qs):
                return self._json(401, {"error": "unauthorized"})
            return self._json(200, build_status())
        return self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._host_is_local() or not self._origin_is_local():
            return self._json(403, {"error": "forbidden"})
        parsed = urlparse(self.path)
        if not self._authed(parse_qs(parsed.query)):
            return self._json(401, {"error": "unauthorized"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})

        if parsed.path == "/api/pick-folder":
            # Open the native macOS folder chooser (we run locally, so we can)
            # and hand the absolute path back — no path-typing for the user.
            script = ('POSIX path of (choose folder with prompt '
                      '"Choose a folder for this agent" default location (path to home folder))')
            try:
                r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
            except subprocess.TimeoutExpired:
                return self._json(200, {"canceled": True})
            if r.returncode == 0:
                return self._json(200, {"path": r.stdout.strip().rstrip("/")})
            if "-128" in r.stderr or "User canceled" in r.stderr:
                return self._json(200, {"canceled": True})
            return self._json(200, {"error": r.stderr.strip()[:200] or "could not open folder picker"})

        if parsed.path == "/api/machine/config":
            clean, errors = validate(body.get("updates", {}), MACHINE_BY_KEY)
            if errors:
                return self._json(400, {"error": "validation", "fields": errors})
            if clean:
                write_env(DEFAULTS_FILE, clean)
                # Lift these keys out of every persona file so the shared
                # default is the single source of truth.
                for p in discover_personas():
                    strip_env_keys(os.path.join(REPO_DIR, p["env_file"]), set(clean.keys()))
            return self._json(200, {"ok": True, "written": sorted(clean), "restart_required": bool(clean)})

        m = re.fullmatch(r"/api/persona/([a-z0-9._-]+)/(config|start|stop|restart|reap)", parsed.path)
        if not m:
            return self._json(404, {"error": "not found"})
        persona_id, action = m.group(1), m.group(2)
        p = find_persona(persona_id)
        if p is None:
            return self._json(404, {"error": f"no persona {persona_id}"})
        if action == "config":
            clean, errors = validate(body.get("updates", {}), AGENT_BY_KEY)
            if errors:
                return self._json(400, {"error": "validation", "fields": errors})
            if clean:
                write_env(os.path.join(REPO_DIR, p["env_file"]), clean)
            return self._json(200, {"ok": True, "written": sorted(clean), "restart_required": bool(clean)})
        if action in ("start", "stop", "restart"):
            return self._json(200, run_service(action, p["persona_arg"]))
        if action == "reap":
            return self._json(200, ipc_call(p["socket"], "reap_session", {"work_item_key": str(body.get("work_item_key") or "")}))
        return self._json(404, {"error": "not found"})


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", DEFAULT_PORT), Handler)
    url = f"http://127.0.0.1:{DEFAULT_PORT}/?t={TOKEN}"
    print(f"Society AI status panel → {url}")
    print(f"(token at {TOKEN_FILE}; localhost-only)")
    if "--open" in sys.argv:
        subprocess.Popen(["open", url])
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Society AI — Local Agents</title>
<style>
:root{--bg:#f5f6f8;--card:#fff;--bd:#e7e9ee;--bd2:#eef0f4;--fg:#161a22;--mut:#727a89;--acc:#4f5bd5;--acc-w:#eef0fd;--ok:#16a34a;--off:#b6bcc8;--warn:#b7791f;--warn-bg:#fdf6e9;--err:#d93a4a;--chip:#f0f2f6}
@media(prefers-color-scheme:dark){:root{--bg:#0e1014;--card:#171a21;--bd:#262a33;--bd2:#21252d;--fg:#e7e9ee;--mut:#8c95a6;--acc:#7c87ff;--acc-w:#1d2138;--ok:#3ecf8e;--off:#4a505d;--warn:#e6b450;--warn-bg:#2a2410;--err:#f0616d;--chip:#21252e}}
*{box-sizing:border-box}
html{overflow-x:hidden}
body{margin:0;background:var(--bg);color:var(--fg);font:14.5px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;-webkit-font-smoothing:antialiased;overflow-x:hidden}
body.drawer-open{overflow:hidden}
.top{display:flex;align-items:center;gap:12px;max-width:760px;margin:0 auto;padding:26px 22px 6px}
.top h1{font-size:18px;font-weight:650;margin:0;letter-spacing:-.01em}
.top .gear{margin-left:auto;background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:8px 13px;font-size:13.5px;cursor:pointer;color:var(--fg);display:flex;gap:7px;align-items:center}
.top .gear:hover{border-color:var(--acc)}
.sub{max-width:760px;margin:0 auto;padding:0 22px;color:var(--mut);font-size:12.5px}
.wrap{max-width:760px;margin:0 auto;padding:14px 22px 80px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:16px;margin:14px 0;box-shadow:0 1px 2px rgba(20,26,40,.04),0 4px 16px rgba(20,26,40,.03);overflow:hidden}
.ph{display:flex;align-items:center;gap:13px;padding:18px 20px}
.dot{width:9px;height:9px;border-radius:50%;flex:none}
.dot.on{background:var(--ok)}.dot.off{background:var(--off)}
.nm{font-weight:640;font-size:16px;letter-spacing:-.01em}
.meta{color:var(--mut);font-size:12.5px;margin-top:1px}
.ph .right{margin-left:auto;display:flex;align-items:center;gap:14px}
/* toggle */
.tog{position:relative;display:inline-block;width:42px;height:24px;flex:none;cursor:pointer;vertical-align:middle}
.tog input{opacity:0;width:0;height:0;position:absolute}
.tog .tk{position:absolute;inset:0;background:var(--off);border-radius:999px;transition:.18s}
.tog .tk:before{content:"";position:absolute;width:18px;height:18px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.18s;box-shadow:0 1px 2px rgba(0,0,0,.25)}
.tog input:checked + .tk{background:var(--acc)}
.tog input:checked + .tk:before{transform:translateX(18px)}
.tog input:disabled + .tk{opacity:.55;cursor:default}
.body{padding:4px 20px 18px;border-top:1px solid var(--bd2)}
.lbl{font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin:14px 0 7px}
.path{display:flex;align-items:center;gap:8px;margin-bottom:7px}
.path input{flex:1;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:9px;padding:8px 11px;font-size:13.5px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.path input:focus{outline:none;border-color:var(--acc)}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{display:inline-flex;align-items:center;gap:7px;background:var(--chip);border:1px solid var(--bd);border-radius:8px;padding:5px 6px 5px 11px;font-size:12.5px;font-family:ui-monospace,Menlo,monospace;max-width:100%}
.chip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chip b{cursor:pointer;color:var(--mut);font-weight:600;padding:0 2px}
.chip b:hover{color:var(--err)}
.chips.dirs .chip:first-child{border-color:var(--acc)}
.chips.dirs .chip:first-child::before{content:"main";font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;background:var(--acc);color:#fff;padding:2px 5px;border-radius:5px}
.addbtn{background:none;border:1px dashed var(--bd);border-radius:8px;padding:6px 11px;font-size:12.5px;color:var(--mut);cursor:pointer}
.addbtn:hover{border-color:var(--acc);color:var(--acc)}
.savebar{display:flex;align-items:center;gap:10px;margin-top:14px;min-height:24px}
.btn{background:var(--card);color:var(--fg);border:1px solid var(--bd);border-radius:9px;padding:7px 14px;font-size:13.5px;cursor:pointer}
.btn:hover{border-color:var(--acc)}
.btn.p{background:var(--acc);border-color:var(--acc);color:#fff;font-weight:600}
.btn.p:hover{filter:brightness(1.07)}
.btn.sm{padding:5px 11px;font-size:12.5px}
.btn:disabled{opacity:.45;cursor:default}
.muted{color:var(--mut);font-size:12.5px}
.hint{font-size:11.5px;color:var(--mut);margin:5px 0 2px}
.warn{color:var(--warn)}
.err{color:var(--err);font-size:12px;margin-top:5px}
.adv summary{cursor:pointer;color:var(--mut);font-size:12.5px;margin-top:14px;list-style:none}
.adv summary::-webkit-details-marker{display:none}
.adv summary:before{content:"▸ ";font-size:10px}
.adv[open] summary:before{content:"▾ "}
.field{margin:10px 0}
.field label{display:block;font-size:12.5px;color:var(--mut);margin-bottom:4px}
.field input,.field select{width:100%;background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:9px;padding:8px 11px;font-size:13.5px}
.field input:focus,.field select:focus{outline:none;border-color:var(--acc)}
.field .h{font-size:11.5px;color:var(--mut);margin-top:4px}
.sess{margin-top:10px}
.sess .row{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--bd2);font-size:13px}
.sess .row .t{flex:1}.sess .row .k{color:var(--mut);font-size:11.5px;font-family:ui-monospace,Menlo,monospace}
/* settings drawer */
.scrim{position:fixed;inset:0;background:rgba(10,12,18,.42);opacity:0;pointer-events:none;transition:.18s;z-index:5}
.scrim.show{opacity:1;pointer-events:auto}
.drawer{position:fixed;top:0;right:0;height:100%;width:440px;max-width:100vw;background:var(--card);border-left:1px solid var(--bd);transform:translateX(105%);transition:transform .22s;z-index:6;overflow-y:auto;overflow-x:hidden}
.drawer.show{transform:none}
.dh{display:flex;align-items:center;gap:10px;padding:20px 22px;border-bottom:1px solid var(--bd)}
.dh h2{font-size:16px;margin:0;font-weight:640}
.dh .x{margin-left:auto;background:none;border:none;font-size:22px;color:var(--mut);cursor:pointer;line-height:1}
.dbody{padding:8px 22px 30px}
.set{display:flex;align-items:center;gap:12px;padding:13px 0;border-bottom:1px solid var(--bd2)}
.set .txt{flex:1}.set .nme{font-size:14px}.set .h{color:var(--mut);font-size:12px;margin-top:2px}
.set .ctrl{flex:none}
.set select,.set input[type=number]{background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:8px;padding:6px 9px;font-size:13px}
.seg{display:inline-flex;border:1px solid var(--bd);border-radius:9px;overflow:hidden}
.seg button{background:var(--card);border:none;padding:6px 12px;font-size:12.5px;cursor:pointer;color:var(--mut);border-left:1px solid var(--bd)}
.seg button:first-child{border-left:none}
.seg button.on{background:var(--acc);color:#fff}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:11px 18px;font-size:13.5px;box-shadow:0 6px 24px rgba(0,0,0,.14);opacity:0;transition:.2s;z-index:9}
.toast.show{opacity:1}
</style></head>
<body>
<div class="top"><h1>Society AI · Local Agents</h1>
  <button class="gear" onclick="openSettings()">⚙ Machine settings</button></div>
<div class="sub" id="sub"></div>
<div class="wrap" id="root"><p class="muted">Loading…</p></div>
<div class="scrim" id="scrim" onclick="closeSettings()"></div>
<div class="drawer" id="drawer">
  <div class="dh"><h2>Machine settings</h2><button class="x" onclick="closeSettings()">×</button></div>
  <div class="dbody"><p class="muted" style="margin:6px 0 16px">These control how <b>every</b> agent on this machine behaves — what gets recorded, where agents run, and their limits. They're shared across all your agents (per-agent things like folders live on each agent's card). Changes apply after you restart agents.</p>
   <div id="mset"></div>
   <div class="savebar" style="margin-top:18px"><button class="btn p" onclick="saveMachine()">Save</button>
     <button class="btn" onclick="restartAll()">Save &amp; restart all</button></div>
  </div>
</div>
<div class="toast" id="toast"></div>
<script>
const TOKEN="__TOKEN__";
const H={"X-Status-Token":TOKEN,"Content-Type":"application/json"};
let DATA=null;
function toast(m,bad){const t=document.getElementById('toast');t.textContent=m;t.style.borderColor=bad?'var(--err)':'var(--bd)';t.classList.add('show');clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove('show'),2600);}
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:H,body:body?JSON.stringify(body):undefined});const j=await r.json().catch(()=>({}));if(!r.ok)throw j;return j;}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
async function load(){try{DATA=await api('/api/status?t='+TOKEN);render();}catch(e){document.getElementById('root').innerHTML='<p class="muted">Could not reach the panel. Is it still running?</p>';}}

function render(){
 document.getElementById('sub').textContent=DATA.repo;
 const root=document.getElementById('root');root.innerHTML='';
 DATA.agents.forEach(a=>{
  const live=a.live||{}, sess=live.sessions||[];
  // One folder list: WORK_DIR (the "main" folder) + EXTRA_DIRS, in order.
  const dirs=[a.config.WORK_DIR, ...(a.config.EXTRA_DIRS?a.config.EXTRA_DIRS.split(','):[])].map(s=>(s||'').trim()).filter(Boolean);
  const card=document.createElement('div');card.className='card';card.dataset.id=a.id;
  card.innerHTML=`
   <div class="ph">
     <div class="dot ${a.running?'on':'off'}"></div>
     <div><div class="nm">${esc(a.display)}</div>
       <div class="meta">${a.running?('online · v'+esc(live.version||'?')+(live.ws_connected===false?' · connecting':'')):(a.installed?'offline':'not installed')}${a.running&&sess.length?' · '+sess.length+' session'+(sess.length>1?'s':''):''}</div></div>
     <div class="right">
       ${a.running?`<button class="btn sm" onclick="act('${a.id}','restart')">Restart</button>`:''}
       <label class="tog" title="${a.running?'Online — click to take offline':'Offline — click to bring online'}">
         <input type="checkbox" ${a.running?'checked':''} ${!a.installed?'disabled':''} onchange="toggleAgent('${a.id}',this.checked)"><span class="tk"></span></label>
     </div>
   </div>
   <div class="body">
     <div class="lbl">Folders this agent can use</div>
     <div class="chips dirs" data-dirs>${dirs.length?dirs.map(chip).join(''):'<span class="muted">none — the agent uses its default folder</span>'}</div>
     <button class="addbtn" style="margin-top:8px" onclick="addDir('${a.id}')">+ add folder</button>
     <div class="hint">The agent can read and write in these. The first is its <b>main</b> folder, where it starts.</div>
     <div class="err" data-err></div>
     <div class="savebar"><button class="btn p sm" data-save disabled onclick="saveDirs('${a.id}')">Save directories</button>
       <span class="muted" data-hint></span></div>
     ${sess.length?`<details class="adv" style="margin-top:6px"><summary>${sess.length} live session${sess.length>1?'s':''}</summary><div class="sess">${sess.map(s=>`<div class="row"><div class="t">${esc(s.title||s.work_item_key)}<div class="k">${esc(s.kind)} · ${esc(s.state)} · idle ${s.idle_seconds}s</div></div><button class="btn sm" onclick="reap('${a.id}','${esc(s.work_item_key)}')">End</button></div>`).join('')}</div></details>`:''}
     <details class="adv"><summary>Advanced (identity)</summary><div data-adv>${advFields(a)}</div></details>
   </div>`;
  root.appendChild(card);
 });
}
function chip(d){return `<span class="chip"><span title="${esc(d)}">${esc(d)}</span><b onclick="this.closest('.chip').remove();dirtyEv(event)">×</b></span>`;}
async function pickFolder(){try{const r=await api('/api/pick-folder',{});if(r.canceled)return null;if(r.error){toast(r.error,true);return null;}return r.path;}catch(e){toast('Could not open the folder picker',true);return null;}}
function advFields(a){return DATA.agent_schema.filter(f=>f.advanced).map(f=>{
  const v=a.config[f.key]||'';
  let inp;
  if(f.type==='secret')inp=`<input type="password" data-k="${f.key}" placeholder="${v==='set'?'•••••• (unchanged)':'(not set)'}">`;
  else inp=`<input data-k="${f.key}" value="${esc(v)}" placeholder="${esc(f.default||'')}">`;
  return `<div class="field"><label>${esc(f.label)}</label>${inp}${f.help?`<div class="h">${esc(f.help)}</div>`:''}</div>`;
 }).join('')+`<div class="savebar"><button class="btn sm" onclick="saveAdv('${a.id}')">Save identity</button></div>`;}

function card(id){return document.querySelector('.card[data-id="'+id+'"]');}
function dirty(id){const c=card(id);c.querySelector('[data-save]').disabled=false;}
function dirtyEv(e){const c=e.target.closest('.card');if(c)c.querySelector('[data-save]').disabled=false;}
async function addDir(id){const path=await pickFolder();if(!path)return;const c=card(id);const box=c.querySelector('[data-dirs]');if(box.querySelector('.muted'))box.innerHTML='';box.insertAdjacentHTML('beforeend',chip(path));dirty(id);}
function collectDirs(id){const c=card(id);const list=[...c.querySelectorAll('[data-dirs] .chip span[title]')].map(s=>s.getAttribute('title'));return {WORK_DIR:list[0]||'',EXTRA_DIRS:list.slice(1).join(',')};}
async function saveDirs(id){const c=card(id);c.querySelector('[data-err]').textContent='';try{const r=await api('/api/persona/'+id+'/config',{updates:collectDirs(id)});toast(r.written.length?'Saved — restart to apply':'No changes');c.querySelector('[data-save]').disabled=true;if(r.restart_required)c.querySelector('[data-hint]').innerHTML='Saved. <a href="#" onclick="act(\''+id+'\',\'restart\');return false">Restart now</a>';}catch(e){if(e&&e.fields){c.querySelector('[data-err]').textContent=Object.values(e.fields).join('; ');}else toast('Save failed',true);}}
async function saveAdv(id){const c=card(id);const u={};c.querySelectorAll('[data-adv] [data-k]').forEach(el=>{if(el.type==='password'){if(el.value)u[el.dataset.k]=el.value;}else u[el.dataset.k]=el.value;});try{const r=await api('/api/persona/'+id+'/config',{updates:u});toast(r.written.length?'Saved — restart to apply':'No changes');}catch(e){toast('Save failed',true);}}

async function toggleAgent(id,on){try{const r=await api('/api/persona/'+id+'/'+(on?'start':'stop'),{});toast(on?'Connecting…':'Disconnecting…',!r.ok);setTimeout(load,1400);}catch(e){toast('Failed',true);load();}}
async function act(id,verb){try{const r=await api('/api/persona/'+id+'/'+verb,{});toast(verb+(r.ok?' ok':' failed'),!r.ok);setTimeout(load,1400);}catch(e){toast(verb+' failed',true);}}
async function reap(id,key){if(!confirm('End session '+key+'? It will be stopped and recorded.'))return;try{const r=await api('/api/persona/'+id+'/reap',{work_item_key:key});toast(r.reaped?'Ended':'Failed',!r.reaped);setTimeout(load,1000);}catch(e){toast('Failed',true);}}

/* machine settings drawer */
function openSettings(){renderMachine();document.getElementById('scrim').classList.add('show');document.getElementById('drawer').classList.add('show');document.body.classList.add('drawer-open');}
function closeSettings(){document.getElementById('scrim').classList.remove('show');document.getElementById('drawer').classList.remove('show');document.body.classList.remove('drawer-open');}
function renderMachine(){
 const host=document.getElementById('mset');host.innerHTML='';
 const basic=DATA.machine_schema.filter(f=>!f.advanced), adv=DATA.machine_schema.filter(f=>f.advanced);
 host.innerHTML=basic.map(f=>machineRow(f)).join('')+
   `<details class="adv"><summary>Advanced</summary>${adv.map(f=>machineRow(f)).join('')}</details>`;
}
function machineRow(f){
 const v=DATA.machine[f.key];let ctrl;
 if(f.type==='bool'){const on=String(v)==='true';ctrl=`<label class="tog"><input type="checkbox" data-m="${f.key}" ${on?'checked':''}><span class="tk"></span></label>`;}
 else if(f.type==='enum'){ctrl=`<div class="seg" data-m="${f.key}" data-val="${esc(v)}">`+f.enum.map(o=>`<button class="${o===v?'on':''}" onclick="seg(this,'${o}')">${o}</button>`).join('')+`</div>`;}
 else if(f.type==='int'){ctrl=`<input type="number" data-m="${f.key}" value="${esc(v)}" min="0" style="width:78px">`;}
 else ctrl=`<input data-m="${f.key}" value="${esc(v)}" style="width:150px">`;
 const warn=(f.key==='MIRROR_LEVEL')?'<div class="h warn" data-fullwarn style="display:'+(String(v)==='full'?'block':'none')+'">⚠ full records tool inputs/outputs — local debug only</div>':'';
 return `<div class="set"><div class="txt"><div class="nme">${esc(f.label)}</div>${f.help?`<div class="h">${esc(f.help)}</div>`:''}${warn}</div><div class="ctrl">${ctrl}</div></div>`;
}
function seg(btn,val){const seg=btn.closest('.seg');seg.dataset.val=val;[...seg.children].forEach(b=>b.classList.toggle('on',b===btn));const fw=seg.closest('.set').querySelector('[data-fullwarn]');if(fw)fw.style.display=(val==='full')?'block':'none';}
function collectMachine(){const u={};document.querySelectorAll('#mset [data-m]').forEach(el=>{const k=el.getAttribute('data-m');if(el.classList.contains('seg'))u[k]=el.dataset.val;else if(el.type==='checkbox')u[k]=el.checked;else u[k]=el.value;});return u;}
async function saveMachine(restart){try{const r=await api('/api/machine/config',{updates:collectMachine()});toast(r.written.length?'Settings saved':'No changes');if(restart)await restartAll(true);}catch(e){if(e&&e.fields)toast(Object.values(e.fields)[0],true);else toast('Save failed',true);throw e;}}
async function restartAll(skipSave){try{if(!skipSave)await saveMachine(true);for(const a of DATA.agents){if(a.installed)await api('/api/persona/'+a.id+'/restart',{});}toast('Restarting all agents…');closeSettings();setTimeout(load,1600);}catch(e){}}

// Pause the auto-refresh while you're interacting, so a poll never collapses
// an open section, steals focus, or wipes unsaved edits.
function isBusy(){
 if(document.getElementById('drawer').classList.contains('show'))return true;
 if(document.querySelector('.card details[open]'))return true;
 const ae=document.activeElement;
 if(ae&&ae.closest&&ae.closest('.card')&&(ae.tagName==='INPUT'||ae.tagName==='SELECT'))return true;
 if(document.querySelector('.card [data-save]:not([disabled])'))return true;
 return false;
}
load();setInterval(()=>{if(!isBusy())load();},5000);
</script></body></html>"""


if __name__ == "__main__":
    main()
