"""Register the Society AI MCP server with Claude Code.

Called by setup.sh — not intended to be run directly. Safely handles paths
with special characters.

Claude Code 2.x manages MCP servers via `~/.claude.json` and the
`claude mcp add/remove/list` CLI; the older `~/.claude/settings.json`
`mcpServers` block is no longer honored. We shell out to `claude mcp add`
because that's the documented surface and it stays correct if the file
format changes again. The operation is made idempotent by removing any
existing `society-ai` entry first (best-effort; ignored on first install).

We also scrub a stale `society-ai` entry out of `~/.claude/settings.json`
left behind by earlier (<0.2.3) versions of this script — otherwise a user
re-running setup ends up with the same server configured in two places.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Optional


def _env_value_for_cli(key: str, value: str) -> str:
    """Format a single env var as one argument to the `-e` variadic option."""
    return f"{key}={value}"


def _claude_bin() -> Optional[str]:
    """Locate the Claude Code CLI binary. Returns None if not on PATH."""
    return shutil.which("claude")


def _scrub_legacy_settings_entry() -> None:
    """Remove the obsolete society-ai entry from ~/.claude/settings.json.

    Pre-0.2.3 versions of this script wrote the MCP entry into
    settings.json's `mcpServers` block. Claude Code 2.x ignores that
    location entirely, so the entry is dead weight that still contains
    the API token. Prune it.
    """
    settings_path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    if not os.path.isfile(settings_path):
        return
    try:
        with open(settings_path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return

    mcp = data.get("mcpServers")
    if not isinstance(mcp, dict) or "society-ai" not in mcp:
        return

    mcp.pop("society-ai", None)
    if not mcp:
        data.pop("mcpServers", None)

    tmp = settings_path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, settings_path)
        try:
            os.chmod(settings_path, 0o600)
        except OSError:
            pass
        print("  Removed obsolete society-ai entry from ~/.claude/settings.json")
    except OSError as e:
        print(
            f"  Warning: could not scrub legacy entry from {settings_path}: {e}",
            file=sys.stderr,
        )


def main() -> None:
    repo_dir = os.path.dirname(os.path.abspath(__file__))

    auth_token = os.environ.get("SOCIETY_AI_AUTH_TOKEN", "").strip()
    lifecycle_flag = os.environ.get("ENABLE_AGENT_LIFECYCLE", "").strip()

    # The registered entry no longer bakes the token (identity is a runtime
    # binding — see identity.py), but an empty token here still means
    # setup.sh didn't write .env correctly, so fail fast on the setup bug.
    if not auth_token:
        print(
            "  Error: SOCIETY_AI_AUTH_TOKEN is empty — setup did not produce "
            "a usable .env. Set it in .env and re-run setup.sh.",
            file=sys.stderr,
        )
        sys.exit(2)

    claude = _claude_bin()
    if claude is None:
        print(
            "  Error: `claude` CLI not found on PATH. Install Claude Code first:\n"
            "    npm install -g @anthropic-ai/claude-code",
            file=sys.stderr,
        )
        sys.exit(2)

    venv_python = os.path.join(repo_dir, "venv", "bin", "python")
    mcp_script = os.path.join(repo_dir, "mcp_server.py")

    if not os.path.isfile(venv_python):
        print(
            f"  Error: expected Python interpreter not found at {venv_python}. "
            "Did setup.sh's venv step run? Re-run ./setup.sh.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not os.path.isfile(mcp_script):
        print(
            f"  Error: MCP server script missing at {mcp_script}.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Multi-persona support: register every identity-bearing env var as
    # ${VAR:-} — expanded at MCP-server spawn time from the *Claude Code
    # process's* own environment. A session spawned by a persona's bridge
    # (whose env carries that persona's AGENT_NAME / token / socket)
    # authenticates as that persona. Interactive sessions get NO baked
    # fallback on purpose: identity.py auto-binds when the machine has
    # exactly one persona, and starts UNBOUND when it has several — the
    # user picks with "act as <name>" (switch_agent). Baking a default
    # here would silently make every session whoever ran setup.sh last,
    # which is exactly the failure mode the runtime binding removed.
    env_pairs = [
        _env_value_for_cli("SOCIETY_AI_AUTH_TOKEN", "${SOCIETY_AI_AUTH_TOKEN:-}"),
        _env_value_for_cli("AGENT_NAME", "${AGENT_NAME:-}"),
        _env_value_for_cli("COMPANY_ID", "${COMPANY_ID:-}"),
        _env_value_for_cli("AGENT_ROUTER_API_URL", "${AGENT_ROUTER_API_URL:-}"),
        _env_value_for_cli(
            "SOCIETY_AI_BRIDGE_SOCKET",
            "${SOCIETY_AI_BRIDGE_SOCKET:-}",
        ),
    ]
    if lifecycle_flag:
        env_pairs.append(_env_value_for_cli("ENABLE_AGENT_LIFECYCLE", lifecycle_flag))

    # Idempotency: removing first means re-running setup.sh after changing
    # AGENT_NAME / COMPANY_ID / API key picks up the new values. `claude mcp
    # remove` exits non-zero when the server isn't present, which is fine on
    # first install — we ignore the result.
    subprocess.run(
        [claude, "mcp", "remove", "--scope", "user", "society-ai"],
        capture_output=True,
        text=True,
        check=False,
    )

    add_cmd = [
        claude, "mcp", "add",
        "society-ai",
        "--scope", "user",
        "--transport", "stdio",
        "-e", *env_pairs,
        "--",
        venv_python,
        mcp_script,
    ]
    result = subprocess.run(add_cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # Surface a clean error rather than the raw CLI complaint. Don't echo
        # the token — env_pairs contains it.
        msg = (result.stderr or result.stdout or "").strip()
        print(f"  Error: `claude mcp add` failed: {msg}", file=sys.stderr)
        sys.exit(2)

    # `claude mcp add` prints its own confirmation line; we just add scope.
    print("  Registered society-ai MCP server at user scope")

    _scrub_legacy_settings_entry()


if __name__ == "__main__":
    main()
