"""OpenShell Sandbox Manager — runs Claude Code inside an isolated sandbox.

Provides the same interface as run_claude_code() but executes inside an
OpenShell sandbox with restricted filesystem, network, and process access.

Uses the OpenShell CLI v0.0.10+ API:
  - `openshell sandbox create --from <SANDBOX_BASE_IMAGE>` (default "claude" — community image with Claude Code)
  - `openshell sandbox upload` for file transfer
  - `openshell policy set` for network policy (hot-reloadable)
  - SSH for remote command execution

Note on filesystem isolation: the sandbox does NOT mount the host's WORK_DIR.
Secured mode is intended for cloud / API-only tasks (interacting with the
Society AI platform, calling external HTTPS APIs). If you need Claude to
read or modify your local codebase, use EXECUTION_MODE=standard instead.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from config import SANDBOX_BASE_IMAGE

logger = logging.getLogger("sandbox")

# Path to the network policy template shipped alongside this file
_POLICY_TEMPLATE = Path(__file__).parent / "network_policy.yaml"
# Path to files we need to upload into the sandbox
_MCP_SERVER = Path(__file__).parent / "mcp_server.py"
_CONFIG_PY = Path(__file__).parent / "config.py"

# Inside the sandbox, the MCP server runs from a dedicated virtual environment.
# The network policy allowlists egress to PyPI and the Society AI API only
# for binaries at these paths, so we install dependencies into this venv and
# point Claude Code's MCP server at this Python interpreter.
_SANDBOX_VENV = "/sandbox/.venv"
_SANDBOX_PY = f"{_SANDBOX_VENV}/bin/python"
_SANDBOX_PIP = f"{_SANDBOX_VENV}/bin/pip"
_SANDBOX_MCP_DIR = "/sandbox/mcp"
_SANDBOX_CLAUDE_DIR = "/sandbox/.claude"


class SandboxError(Exception):
    """Raised when the sandbox cannot be created or is unhealthy."""


class SandboxManager:
    """Manages an OpenShell sandbox for secure Claude Code execution.

    The sandbox is created once at startup and reused for all tasks.
    Claude Code's session state persists between exec calls because the
    sandbox filesystem is persistent (K3s pod).
    """

    def __init__(
        self,
        work_dir: str,
        env_vars: dict[str, str],
        sandbox_name: str = "society-ai-agent",
        timeout: int = 600,
        base_image: str | None = None,
    ):
        self._work_dir = work_dir
        self._env_vars = env_vars
        self._sandbox_name = sandbox_name
        self._timeout = timeout
        self._base_image = (base_image or SANDBOX_BASE_IMAGE or "claude").strip()
        self._ready = False
        self._openshell = shutil.which("openshell") or "openshell"
        self._oauth_token: str | None = None

    # -- Public API ----------------------------------------------------------

    async def start(self) -> None:
        """Create the sandbox and set it up for Claude Code execution.

        Raises SandboxError if any prerequisite is missing or setup fails.

        Flow:
        1. Create sandbox with base image (default permissive policy)
        2. Wait for it to become ready
        3. Install MCP server + deps in a dedicated venv (needs network for pip)
        4. Apply restrictive network policy (locks down egress)
        """
        self._check_prerequisites()
        self._resolve_oauth_token()
        await self._create_sandbox()
        await self._wait_ready()
        await self._setup_mcp_server()
        await self._apply_network_policy()  # Lock down after setup is complete
        self._ready = True
        logger.info("Sandbox '%s' is ready", self._sandbox_name)

    async def exec_claude(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> tuple[int, str, str | None]:
        """Execute Claude Code inside the sandbox.

        Same return signature as run_claude_code(): (exit_code, output_text, session_id).
        """
        if not self._ready:
            raise SandboxError("Sandbox is not ready. Call start() first.")

        # Build the claude command to run inside sandbox via SSH
        claude_cmd = "claude -p "
        claude_cmd += _shell_quote(prompt)
        claude_cmd += " --output-format json"
        if session_id:
            claude_cmd += f" --resume {_shell_quote(session_id)}"

        # Build env prefix for the SSH command
        env_prefix = ""
        for key, val in self._env_vars.items():
            if val:
                env_prefix += f"export {key}={_shell_quote(val)}; "

        # Pass Claude Code OAuth token for authentication
        if self._oauth_token:
            env_prefix += f"export CLAUDE_CODE_OAUTH_TOKEN={_shell_quote(self._oauth_token)}; "

        full_cmd = env_prefix + claude_cmd

        logger.info("Exec in sandbox (session=%s)", session_id or "new")
        exit_code, stdout = await self._ssh_exec(full_cmd, timeout=self._timeout)

        # Parse JSON output (same logic as run_claude_code)
        output_text = stdout
        returned_session_id = session_id
        try:
            data = json.loads(stdout)
            if isinstance(data, dict):
                output_text = data.get("result", stdout)
                returned_session_id = data.get("session_id", session_id)
        except (json.JSONDecodeError, TypeError):
            pass

        if exit_code != 0:
            logger.error("Claude Code FAILED in sandbox (exit=%d): %s", exit_code, (output_text or "")[:500])
        else:
            logger.info(
                "Claude Code OK in sandbox (session=%s, len=%d)",
                returned_session_id, len(output_text or ""),
            )
        return exit_code, output_text, returned_session_id

    async def stop(self) -> None:
        """Delete the sandbox."""
        if not self._ready:
            return
        logger.info("Deleting sandbox '%s'...", self._sandbox_name)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._openshell, "sandbox", "delete", self._sandbox_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=30)
            self._ready = False
            logger.info("Sandbox deleted")
        except asyncio.TimeoutError:
            logger.warning("Sandbox delete timed out after 30s")
        except Exception as e:
            logger.warning("Failed to delete sandbox: %s", e)

    async def health_check(self) -> bool:
        """Check if the sandbox is still running."""
        proc = await asyncio.create_subprocess_exec(
            self._openshell, "sandbox", "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return self._sandbox_name in stdout.decode()

    # -- Private setup methods -----------------------------------------------

    def _resolve_oauth_token(self) -> None:
        """Resolve Claude Code OAuth token from environment or macOS keychain.

        Claude Code uses Anthropic OAuth (not API keys). On macOS, the token
        is stored in the system keychain. When run via Claude Desktop, it's
        also available as CLAUDE_CODE_OAUTH_TOKEN env var.
        """
        # 1. Check environment (set by Claude Desktop or user)
        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if token:
            self._oauth_token = token
            logger.info("OAuth token found in environment")
            return

        # 2. Try macOS keychain
        if platform.system() == "Darwin":
            token = _get_oauth_from_keychain()
            if token:
                self._oauth_token = token
                logger.info("OAuth token extracted from macOS keychain")
                return

        raise SandboxError(
            "Claude Code OAuth token not found. Either:\n"
            "  1. Run from Claude Desktop (sets CLAUDE_CODE_OAUTH_TOKEN automatically), or\n"
            "  2. Log in with `claude /login` on your Mac (stores token in keychain), or\n"
            "  3. Export CLAUDE_CODE_OAUTH_TOKEN=<token> before running the bridge"
        )

    def _check_prerequisites(self) -> None:
        """Verify Docker and OpenShell CLI are available. Hard fail if not."""
        if not shutil.which("docker"):
            raise SandboxError(
                "Docker is not installed. Install Docker Desktop: https://www.docker.com/products/docker-desktop/"
            )

        # Check Docker is actually running, without invoking a shell (avoids
        # quoting/interpolation surface).
        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise SandboxError(f"Could not invoke docker: {e}")
        if result.returncode != 0:
            raise SandboxError(
                "Docker is not running. Start Docker Desktop and try again."
            )

        if not shutil.which("openshell"):
            raise SandboxError(
                "OpenShell CLI is not installed. Install it with:\n"
                "  curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh"
            )

    async def _create_sandbox(self) -> None:
        """Create the sandbox from the configured base image."""
        # Check if sandbox already exists
        if await self.health_check():
            logger.info("Sandbox '%s' already exists, reusing", self._sandbox_name)
            return

        logger.info(
            "Creating sandbox '%s' from base image '%s'...",
            self._sandbox_name, self._base_image,
        )

        # Use default policy at creation (permissive, allows pip install).
        # We apply the restrictive network policy AFTER setup is complete.
        cmd = [
            self._openshell, "sandbox", "create",
            "--name", self._sandbox_name,
            "--from", self._base_image,
            "--no-auto-providers",
            "--no-tty",
            "--", "true",  # Run `true` and exit (don't open interactive shell)
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise SandboxError("Sandbox create timed out after 600s")

        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")
            stdout_text = stdout.decode("utf-8", errors="replace")
            raise SandboxError(
                f"Failed to create sandbox from image '{self._base_image}': {error}\n{stdout_text}"
            )

        logger.info("Sandbox created")

    async def _wait_ready(self, timeout: int = 120) -> None:
        """Wait for the sandbox to reach ready state."""
        logger.info("Waiting for sandbox to be ready...")
        for _ in range(timeout):
            if await self.health_check():
                # Also verify SSH connectivity
                try:
                    exit_code, output = await self._ssh_exec("echo ready", timeout=10)
                    if exit_code == 0 and "ready" in output:
                        return
                except Exception:
                    pass
            await asyncio.sleep(1)
        raise SandboxError(f"Sandbox did not become ready within {timeout}s")

    async def _setup_mcp_server(self) -> None:
        """Upload MCP server files into sandbox and configure Claude Code to use them.

        Installs deps into a dedicated virtualenv so the egress-restricted
        network policy can be scoped tightly to that interpreter.
        """
        logger.info("Setting up MCP server inside sandbox...")

        # Create directory layout in sandbox
        await self._ssh_exec(
            f"mkdir -p {_SANDBOX_MCP_DIR} {_SANDBOX_CLAUDE_DIR}",
            timeout=10,
        )

        # Upload mcp_server.py and config.py
        await self._upload_file(str(_MCP_SERVER), f"{_SANDBOX_MCP_DIR}/")
        await self._upload_file(str(_CONFIG_PY), f"{_SANDBOX_MCP_DIR}/")

        # Create a venv at the expected location. If python3 has the venv module
        # this is fast; fall back to virtualenv if not.
        exit_code, output = await self._ssh_exec(
            f"python3 -m venv {_SANDBOX_VENV} || (pip3 install --quiet virtualenv && virtualenv {_SANDBOX_VENV})",
            timeout=60,
        )
        if exit_code != 0:
            raise SandboxError(f"Failed to create venv at {_SANDBOX_VENV}: {output[:500]}")

        # Install dependencies INTO the venv (path-scoped install — this is
        # what the network policy will allow egress for).
        exit_code, output = await self._ssh_exec(
            f"{_SANDBOX_PIP} install --quiet 'httpx>=0.27' 'mcp>=1.0' certifi",
            timeout=180,
        )
        if exit_code != 0:
            raise SandboxError(f"Failed to install MCP deps into venv: {output[:500]}")

        # Write Claude Code settings.json to register the MCP server, pointing
        # at the venv interpreter (matches the network policy binary path).
        #
        # Note: in secured mode the bridge (host) and the MCP server
        # (sandbox) live in different filesystem namespaces, so the IPC
        # socket is NOT reachable. search_agents / delegate_task will
        # return an "IPC socket not found" error from inside the sandbox;
        # this is intentional. We point SOCIETY_AI_BRIDGE_SOCKET at a path
        # under /tmp so the error message names something coherent.
        env_block = {
            "AGENT_ROUTER_API_URL": self._env_vars.get("AGENT_ROUTER_API_URL", ""),
            "SOCIETY_AI_AUTH_TOKEN": self._env_vars.get("SOCIETY_AI_AUTH_TOKEN", ""),
            "AGENT_NAME": self._env_vars.get("AGENT_NAME", ""),
            "COMPANY_ID": self._env_vars.get("COMPANY_ID", ""),
            "SOCIETY_AI_BRIDGE_SOCKET": "/tmp/society-ai-bridge.sock",
        }
        if self._env_vars.get("ENABLE_AGENT_LIFECYCLE"):
            env_block["ENABLE_AGENT_LIFECYCLE"] = self._env_vars["ENABLE_AGENT_LIFECYCLE"]
        settings = {
            "mcpServers": {
                "society-ai": {
                    "command": _SANDBOX_PY,
                    "args": [f"{_SANDBOX_MCP_DIR}/mcp_server.py"],
                    "env": env_block,
                }
            }
        }
        # Write settings to a host temp file, then upload — avoids passing
        # secrets through a heredoc shell-string interpolated in SSH.
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(settings, f, indent=2)
            tmp_settings = f.name
        try:
            os.chmod(tmp_settings, 0o600)
            await self._upload_file(tmp_settings, f"{_SANDBOX_CLAUDE_DIR}/settings.json")
        finally:
            try:
                os.unlink(tmp_settings)
            except OSError:
                pass

        logger.info("MCP server configured in sandbox (venv=%s)", _SANDBOX_VENV)

    async def _apply_network_policy(self) -> None:
        """Apply the network policy with dynamic host substitution.

        Network policies are hot-reloadable on a running sandbox.
        Filesystem and process policies are set at creation via --policy flag.
        """
        if not _POLICY_TEMPLATE.exists():
            logger.warning("Network policy template not found at %s, skipping", _POLICY_TEMPLATE)
            return

        policy_content = _POLICY_TEMPLATE.read_text()

        # Substitute the Society AI API host from AGENT_ROUTER_API_URL
        api_url = self._env_vars.get("AGENT_ROUTER_API_URL", "https://api.societyai.com")
        parsed = urlparse(api_url)
        api_host = parsed.hostname or "api.societyai.com"
        policy_content = policy_content.replace("{{SOCIETY_AI_HOST}}", api_host)

        # Write to temp file and apply via openshell policy set
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(policy_content)
            tmp_path = f.name

        try:
            proc = await asyncio.create_subprocess_exec(
                self._openshell, "policy", "set", self._sandbox_name,
                "--policy", tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                error = stderr.decode("utf-8", errors="replace")
                raise SandboxError(f"Failed to apply network policy: {error}")
            logger.info("Network policy applied (API host: %s)", api_host)
        except asyncio.TimeoutError:
            raise SandboxError("Network policy apply timed out after 30s")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # -- Helpers -------------------------------------------------------------

    def _ssh_args(self) -> list[str]:
        """Build SSH command args for connecting to the sandbox."""
        return [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            "-o", f"ProxyCommand={self._openshell} ssh-proxy --gateway-name openshell --name {self._sandbox_name}",
            f"sandbox@openshell-{self._sandbox_name}",
        ]

    async def _ssh_exec(
        self,
        command: str,
        timeout: int = 30,
    ) -> tuple[int, str]:
        """Run a command inside the sandbox via SSH."""
        args = self._ssh_args() + [command]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise SandboxError(f"SSH command timed out: {command[:100]}")

        exit_code = proc.returncode or 0
        output = stdout.decode("utf-8", errors="replace")
        if exit_code != 0 and stderr:
            err = stderr.decode("utf-8", errors="replace")
            if err.strip():
                logger.warning(
                    "SSH command failed (exit=%d): %s\n%s",
                    exit_code, command[:100], err[:500],
                )
        return exit_code, output

    async def _upload_file(self, local_path: str, dest_path: str) -> None:
        """Upload a file to the sandbox using openshell sandbox upload."""
        proc = await asyncio.create_subprocess_exec(
            self._openshell, "sandbox", "upload",
            self._sandbox_name, local_path, dest_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise SandboxError(f"Upload timed out: {local_path}")
        if proc.returncode != 0:
            error = stderr.decode("utf-8", errors="replace")
            raise SandboxError(f"Failed to upload {local_path}: {error}")


def _shell_quote(s: str) -> str:
    """Quote a string for safe shell interpolation.

    Wraps in single quotes and escapes any embedded single quotes — safe
    against shell metacharacter injection by construction.
    """
    return "'" + s.replace("'", "'\\''") + "'"


def _get_oauth_from_keychain() -> str | None:
    """Extract Claude Code OAuth token from macOS keychain.

    Returns the token string or None if not found.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "claude", "-w"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None
