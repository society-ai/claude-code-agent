"""Configure Claude Code settings.json with the Society AI MCP server.

Called by setup.sh — not intended to be run directly.
Safely handles paths with special characters.
"""

import json
import os
import sys


def main():
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    settings_dir = os.path.join(os.path.expanduser("~"), ".claude")
    settings_file = os.path.join(settings_dir, "settings.json")

    # Read env vars (passed safely by the caller)
    auth_token = os.environ.get("SOCIETY_AI_AUTH_TOKEN", "").strip()
    agent_name = os.environ.get("AGENT_NAME", "claude-code").strip()
    company_id = os.environ.get("COMPANY_ID", "").strip()
    api_url = os.environ.get("AGENT_ROUTER_API_URL", "https://api.societyai.com").strip()
    bridge_socket = os.environ.get(
        "SOCIETY_AI_BRIDGE_SOCKET",
        os.path.join(os.path.expanduser("~"), ".cache", "society-ai", "bridge.sock"),
    ).strip()
    service_key = os.environ.get("SOCIETY_AI_SERVICE_KEY", "").strip()
    lifecycle_flag = os.environ.get("ENABLE_AGENT_LIFECYCLE", "").strip()

    if not auth_token:
        print(
            "  Error: SOCIETY_AI_AUTH_TOKEN is empty — refusing to write an MCP entry "
            "that would 401 on every call. Set it in .env and re-run setup.sh.",
            file=sys.stderr,
        )
        sys.exit(2)

    env_block = {
        "SOCIETY_AI_AUTH_TOKEN": auth_token,
        "AGENT_NAME": agent_name,
        "COMPANY_ID": company_id,
        "AGENT_ROUTER_API_URL": api_url,
        "SOCIETY_AI_BRIDGE_SOCKET": bridge_socket,
    }
    # Optional values — only include if explicitly set so we don't pollute
    # settings.json with empty strings that look like accidental configuration.
    if service_key:
        env_block["SOCIETY_AI_SERVICE_KEY"] = service_key
    if lifecycle_flag:
        env_block["ENABLE_AGENT_LIFECYCLE"] = lifecycle_flag

    mcp_entry = {
        "command": os.path.join(repo_dir, "venv", "bin", "python"),
        "args": [os.path.join(repo_dir, "mcp_server.py")],
        "env": env_block,
    }

    # Load existing settings or start fresh
    data = {}
    existed = os.path.isfile(settings_file)
    if existed:
        with open(settings_file) as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                print(f"  Warning: {settings_file} has invalid JSON, backing up and recreating")
                os.rename(settings_file, settings_file + ".bak")
                existed = False
                data = {}

    if "mcpServers" not in data or not isinstance(data.get("mcpServers"), dict):
        data["mcpServers"] = {}

    # Always overwrite the society-ai entry so re-running setup.sh after
    # changing AGENT_NAME / COMPANY_ID / API_URL takes effect.
    data["mcpServers"]["society-ai"] = mcp_entry

    os.makedirs(settings_dir, exist_ok=True)
    # Write atomically: temp file + rename so we don't half-write on Ctrl-C.
    # Set 0600 on the temp file BEFORE the rename so the final file is never
    # briefly world-readable with the API token inside it.
    tmp_file = settings_file + ".tmp"
    with open(tmp_file, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(tmp_file, 0o600)
    except OSError:
        pass  # Best-effort; Windows / unusual filesystems may not support chmod.
    os.replace(tmp_file, settings_file)
    # Belt-and-braces: ensure the final file is 0600 even if the OS lost the
    # mode across rename or the file already existed with looser permissions.
    try:
        os.chmod(settings_file, 0o600)
    except OSError:
        pass

    if existed:
        print(f"  Updated society-ai MCP in {settings_file}")
    else:
        print(f"  Created {settings_file} with society-ai MCP")


if __name__ == "__main__":
    main()
