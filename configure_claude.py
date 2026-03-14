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
    auth_token = os.environ.get("SOCIETY_AI_AUTH_TOKEN", "")
    agent_name = os.environ.get("AGENT_NAME", "claude-code")
    company_id = os.environ.get("COMPANY_ID", "")

    mcp_entry = {
        "command": os.path.join(repo_dir, "venv", "bin", "python"),
        "args": [os.path.join(repo_dir, "mcp_server.py")],
        "env": {
            "SOCIETY_AI_AUTH_TOKEN": auth_token,
            "AGENT_NAME": agent_name,
            "COMPANY_ID": company_id,
        },
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

    if "society-ai" in data.get("mcpServers", {}):
        print("  society-ai MCP already configured in settings.json")
        return

    # Add society-ai MCP server
    if "mcpServers" not in data:
        data["mcpServers"] = {}
    data["mcpServers"]["society-ai"] = mcp_entry

    os.makedirs(settings_dir, exist_ok=True)
    with open(settings_file, "w") as f:
        json.dump(data, f, indent=2)

    if existed:
        print(f"  Added society-ai MCP to {settings_file}")
    else:
        print(f"  Created {settings_file} with society-ai MCP")


if __name__ == "__main__":
    main()
