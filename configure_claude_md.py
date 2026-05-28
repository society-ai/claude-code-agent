"""Install the Society AI section into ~/.claude/CLAUDE.md.

Called by setup.sh — not intended to be run directly.

Behavior:
- Creates ~/.claude/CLAUDE.md if it doesn't exist.
- Wraps the fragment in BEGIN/END marker comments so re-running setup.sh
  replaces the block in place instead of appending a duplicate.
- Preserves anything else the user has in their CLAUDE.md.
- Writes atomically (tmpfile + os.replace) and chmod 0600 on the result
  since CLAUDE.md content sometimes leaks identifiers (company IDs etc.)
  via the install script's environment.
"""

from __future__ import annotations

import os
import sys

BEGIN_MARKER = "<!-- BEGIN: society-ai-claude-code-agent -->"
END_MARKER = "<!-- END: society-ai-claude-code-agent -->"


def main() -> None:
    if len(sys.argv) < 2:
        print(
            "  Error: configure_claude_md.py requires the fragment path "
            "as its argument.",
            file=sys.stderr,
        )
        sys.exit(2)

    fragment_path = sys.argv[1]
    if not os.path.isfile(fragment_path):
        print(
            f"  Warning: fragment {fragment_path} not found; "
            "skipping CLAUDE.md install.",
            file=sys.stderr,
        )
        # Not a fatal error — the bridge and MCP still work without CLAUDE.md,
        # the user just won't get the operating-manual context.
        return

    with open(fragment_path) as f:
        fragment_body = f.read().strip()

    target_path = os.path.join(os.path.expanduser("~"), ".claude", "CLAUDE.md")
    os.makedirs(os.path.dirname(target_path), exist_ok=True)

    existing = ""
    existed = os.path.isfile(target_path)
    if existed:
        with open(target_path) as f:
            existing = f.read()

    block = f"{BEGIN_MARKER}\n\n{fragment_body}\n\n{END_MARKER}\n"

    if BEGIN_MARKER in existing and END_MARKER in existing:
        # In-place replacement: keep everything outside the markers untouched.
        head, _, rest = existing.partition(BEGIN_MARKER)
        _, _, tail = rest.partition(END_MARKER)
        head = head.rstrip()
        tail = tail.lstrip("\n")
        new_content = head
        if head:
            new_content += "\n\n"
        new_content += block
        if tail:
            new_content += "\n" + tail
        action = "Updated"
    else:
        # Append at end, with a blank-line gap from any prior content.
        if existing:
            sep = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
            new_content = existing + sep + block
        else:
            new_content = block
        action = "Added"

    tmp_path = target_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(new_content)
    try:
        os.chmod(tmp_path, 0o600)
    except OSError:
        pass
    os.replace(tmp_path, target_path)
    try:
        os.chmod(target_path, 0o600)
    except OSError:
        pass

    print(f"  {action} Society AI section in {target_path}")


if __name__ == "__main__":
    main()
