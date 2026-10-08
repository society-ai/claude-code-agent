"""Where agents keep their files and where their sessions start, on this machine.

    <Society AI folder>/              SOCIETY_AI_HOME, default ~/Society AI
      <agent>/                        the agent's own work folder: the default
                                      WORK_DIR, so a new agent starts with an
                                      empty folder it owns rather than this
                                      repository
      .sessions/                      hidden; where sessions start
        <sender>/                     one git repo per sender: the Claude Code
                                      sidebar groups Remote Control sessions by
                                      the repo's remote name, so its placeholder
                                      origin "Society-AI-<sender>" groups every
                                      conversation from that sender together
          <agent>/                    sessions of that agent with that sender

The remotes are labels only: nothing is ever pushed and the addresses do not
need to exist. Everything here stays on this computer.
"""

from __future__ import annotations

import logging
import os
import pathlib
import re
import subprocess

logger = logging.getLogger("folders")

GROUP_PREFIX = "Society-AI-"
REMOTE_BASE = "https://societyai.com/agents/"
AGENT_MARKER = ".society-ai-agent"
SESSIONS_DIRNAME = ".sessions"


def society_ai_home() -> pathlib.Path:
    """The Society AI folder: SOCIETY_AI_HOME (set in ./status.sh), else
    ~/Society AI."""
    configured = os.getenv("SOCIETY_AI_HOME", "").strip()
    return pathlib.Path(configured).expanduser() if configured else pathlib.Path.home() / "Society AI"


def folder_name(text: str, fallback: str) -> str:
    """A name safe to use as a folder on macOS, Linux and Windows."""
    cleaned = re.sub(r'[\x00-\x1f/\\:*?"<>|]+', "-", text or "").strip(" .-")
    return cleaned or fallback


def group_slug(text: str, fallback: str) -> str:
    """A name safe to use as the last segment of a remote URL."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-") or fallback


def agent_workspace(persona: str, display_name: str = "") -> pathlib.Path:
    """<Society AI folder>/<display name>: the agent's own work folder.
    Created on first use. A marker file records which agent owns it, so two
    agents with the same display name never share a folder."""
    home = society_ai_home()
    name = folder_name(display_name or persona, persona)
    path = home / name
    marker = path / AGENT_MARKER
    if marker.exists() and marker.read_text().strip() != persona:
        path = home / f"{name} ({persona})"
        marker = path / AGENT_MARKER
    path.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text(persona + "\n")
    return path


def session_dir(sender: str, persona: str, display_name: str = "") -> pathlib.Path:
    """<Society AI folder>/.sessions/<sender>/<agent>: where a session of this
    agent with this sender starts. The sender folder is set up as the git
    repo that names the sidebar group "Society-AI-<sender>"."""
    sender_name = folder_name(sender, "Unknown")
    group_root = society_ai_home() / SESSIONS_DIRNAME / sender_name
    path = group_root / folder_name(display_name or persona, persona)
    path.mkdir(parents=True, exist_ok=True)
    _ensure_group_repo(group_root, GROUP_PREFIX + group_slug(sender_name, "Unknown"))
    return path


def _ensure_group_repo(path: pathlib.Path, repo_name: str) -> None:
    """Make `path` a git repo whose origin is named `repo_name`, with a
    .gitignore that keeps it empty (so its git status says nothing
    misleading). A remote the bridge set earlier (under REMOTE_BASE) is
    renamed to the current scheme; any other origin is never touched."""
    want = f"{REMOTE_BASE}{repo_name}.git"
    try:
        gitignore = path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("# Society AI session folders: nothing here is tracked.\n*\n")
        if not (path / ".git").exists():
            subprocess.run(["git", "init", "-q"], cwd=path, check=True, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        got = subprocess.run(["git", "remote", "get-url", "origin"], cwd=path,
                             timeout=10, capture_output=True, text=True)
        current = got.stdout.strip() if got.returncode == 0 else None
        if current is None:
            subprocess.run(["git", "remote", "add", "origin", want], cwd=path, check=True,
                           timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif current != want and current.startswith(REMOTE_BASE):
            subprocess.run(["git", "remote", "set-url", "origin", want], cwd=path, check=True,
                           timeout=10, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("Could not set up %s as a sidebar group (%s); its sessions show under Other", path, e)


SESSION_FOLDER_GUIDE = """\
# This folder is not a project

This is where your Society AI sessions start, so they group in the Claude Code
sidebar by who you are talking to. The git repository above it only names that
group; nothing here is real work, and its remote does not exist.

Do not create files, commit or push here.

## Your work folders

{folders}

Do your work in those folders. You have full access to them, and their own
CLAUDE.md instructions are loaded.

(Written by the Society AI bridge; edits here are overwritten. Change the
folders with ./status.sh in the claude-code-agent folder.)
"""


def write_session_guide(path: pathlib.Path, work_dirs: list[str]) -> None:
    """Tell a session started here where its real work is. Without it, a
    session starting in an empty placeholder repo takes it for its project."""
    listing = "\n".join(f"- `{d}`" for d in work_dirs if d) or "- (none configured: ask your owner)"
    try:
        (path / "CLAUDE.md").write_text(SESSION_FOLDER_GUIDE.format(folders=listing))
    except OSError as e:
        logger.warning("Could not write the folder guide in %s: %s", path, e)
