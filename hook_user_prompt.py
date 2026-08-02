#!/usr/bin/env python3
"""UserPromptSubmit hook — mirror a locally-typed message right away.

The transcript shipper otherwise runs only on Stop, i.e. at the END of a
turn. For a message the owner types directly into a bridge-launched desktop
session, that means the platform (and the web app watching the chat) does
not see the message until the agent finishes answering — 30 seconds on a
good day, minutes on a long turn. This hook tells the bridge "a prompt just
landed in this session" the moment it is submitted, and the bridge ships
the transcript delta on a short delay (the prompt entry is flushed to the
transcript file as the turn starts, not synchronously with this hook).

Sessions the bridge did not launch are ignored bridge-side (the shipper
only ships registered sessions), so this hook is safe to run globally.

Must stay FAST: UserPromptSubmit blocks the turn from starting until every
hook returns. One short-timeout IPC send per bridge socket, no waiting on
the ship itself.
"""

from __future__ import annotations

import sys

from hook_stop import _notify_mirrors, _read_hook_input


def main() -> int:
    _notify_mirrors(_read_hook_input(), event="prompt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
