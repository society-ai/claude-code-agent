#!/usr/bin/env python3
"""One-time setup for OpenShell secured mode.

Verifies prerequisites (Docker, OpenShell CLI) and bootstraps the gateway.

Usage:
    python setup_sandbox.py

The sandbox image is read from SANDBOX_BASE_IMAGE (default: "claude") so
this verification uses the same image the bridge will actually use at
runtime — if the configured image is wrong, you find out here.
"""

import os
import shutil
import subprocess
import sys


SANDBOX_BASE_IMAGE = os.environ.get("SANDBOX_BASE_IMAGE", "claude").strip() or "claude"


def run(cmd: list[str], check: bool = True, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=timeout)


def check_docker() -> bool:
    """Verify Docker is installed and running."""
    print("[1/4] Checking Docker...")
    if not shutil.which("docker"):
        print("  ERROR: Docker is not installed.", file=sys.stderr)
        print("  Install Docker Desktop: https://www.docker.com/products/docker-desktop/", file=sys.stderr)
        return False

    try:
        result = run(["docker", "info"], check=False, timeout=10)
    except subprocess.TimeoutExpired:
        print("  ERROR: `docker info` timed out — daemon may be unresponsive.", file=sys.stderr)
        return False
    if result.returncode != 0:
        print("  ERROR: Docker is not running.", file=sys.stderr)
        print("  Start Docker Desktop and try again.", file=sys.stderr)
        return False

    print("  OK: Docker is running")
    return True


def check_openshell() -> bool:
    """Verify OpenShell CLI is installed."""
    print("[2/4] Checking OpenShell CLI...")
    if not shutil.which("openshell"):
        print("  OpenShell CLI not found. Installing...")
        try:
            result = run(
                ["bash", "-c", "curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh"],
                check=False,
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            print("  ERROR: OpenShell install timed out", file=sys.stderr)
            return False
        if result.returncode != 0:
            print(f"  ERROR: Failed to install OpenShell: {result.stderr[:500]}", file=sys.stderr)
            print(
                "  Install manually: "
                "curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh",
                file=sys.stderr,
            )
            return False

    # Verify it works
    try:
        result = run(["openshell", "--version"], check=False, timeout=10)
    except subprocess.TimeoutExpired:
        print("  ERROR: openshell --version timed out", file=sys.stderr)
        return False
    if result.returncode != 0:
        print("  ERROR: openshell command not working after install", file=sys.stderr)
        return False

    version = result.stdout.strip()
    print(f"  OK: OpenShell {version}")
    return True


def bootstrap_gateway() -> bool:
    """Start the OpenShell gateway if not already running."""
    print("[3/4] Checking OpenShell gateway...")

    # Check if gateway is already running
    try:
        result = run(["openshell", "gateway", "list"], check=False, timeout=15)
    except subprocess.TimeoutExpired:
        print("  ERROR: `openshell gateway list` timed out", file=sys.stderr)
        return False
    if result.returncode == 0 and result.stdout.strip():
        print("  OK: Gateway is running")
        return True

    print("  Starting gateway (this may take a minute on first run)...")
    try:
        result = run(["openshell", "gateway", "start"], check=False, timeout=300)
    except subprocess.TimeoutExpired:
        print("  ERROR: `openshell gateway start` timed out", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"  ERROR: Failed to start gateway: {result.stderr[:500]}", file=sys.stderr)
        return False

    print("  OK: Gateway started")
    return True


def verify_sandbox() -> bool:
    """Quick sanity check — create a test sandbox, run echo, delete it."""
    print(f"[4/4] Verifying sandbox creation (image='{SANDBOX_BASE_IMAGE}')...")

    test_name = "society-ai-test"

    # Clean up any leftover test sandbox
    run(["openshell", "sandbox", "delete", test_name], check=False, timeout=30)

    # Create
    try:
        result = run(
            ["openshell", "sandbox", "create", "--name", test_name, "--from", SANDBOX_BASE_IMAGE],
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print(f"  ERROR: Sandbox create timed out for image '{SANDBOX_BASE_IMAGE}'", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"  ERROR: Failed to create test sandbox: {result.stderr[:500]}", file=sys.stderr)
        return False

    # Test exec
    try:
        result = run(
            ["openshell", "sandbox", "exec", test_name, "--", "echo", "hello"],
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        run(["openshell", "sandbox", "delete", test_name], check=False, timeout=30)
        print("  ERROR: Sandbox exec timed out", file=sys.stderr)
        return False
    ok = result.returncode == 0 and "hello" in result.stdout

    # Clean up
    run(["openshell", "sandbox", "delete", test_name], check=False, timeout=30)

    if ok:
        print("  OK: Sandbox creation and execution verified")
    else:
        print("  ERROR: Sandbox test failed", file=sys.stderr)
    return ok


def main():
    print("=" * 60)
    print("Society AI Claude Code Agent — OpenShell Setup")
    print(f"Base image: {SANDBOX_BASE_IMAGE} (override with SANDBOX_BASE_IMAGE env)")
    print("=" * 60)
    print()

    steps = [check_docker, check_openshell, bootstrap_gateway, verify_sandbox]
    for step in steps:
        if not step():
            print()
            print("Setup FAILED. Fix the issue above and try again.", file=sys.stderr)
            sys.exit(1)
        print()

    print("=" * 60)
    print("Setup complete! Run the agent in secured mode:")
    print()
    print("  EXECUTION_MODE=secured \\")
    print("  SOCIETY_AI_AUTH_TOKEN=sai_... \\")
    print("  WORK_DIR=/path/to/project \\")
    print("  python bridge.py")
    print()
    print("=" * 60)


if __name__ == "__main__":
    main()
