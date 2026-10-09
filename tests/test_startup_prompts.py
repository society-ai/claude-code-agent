"""The folder-trust prompt defaults to "No, exit": the bridge must never press
Enter while the cursor is there, even when a key it sends is dropped."""

import asyncio
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from session_manager import SessionManager  # noqa: E402

TRUST_NO = (" Quick safety check: Is this a project you created or one you trust?\n"
            " ❯ No, exit\n   Yes, I trust this folder\n Enter to confirm")
TRUST_YES = (" Quick safety check: Is this a project you created or one you trust?\n"
             "   No, exit\n ❯ Yes, I trust this folder\n Enter to confirm")
READY = "▎ Channels (experimental) messages from server:society-ai-channel inject\n❯ "


class FakeTUI:
    """A TUI that drops the first `drop` keys (not accepting input yet)."""

    def __init__(self, drop: int):
        self.drop, self.screen, self.events = drop, TRUST_NO, []

    async def capture(self, name):
        return self.screen

    async def send(self, name, keys, *, enter=False):
        self.events.append((keys, enter, self.screen == TRUST_NO))
        if self.drop > 0:
            self.drop -= 1
            return
        if keys == "Down" and self.screen == TRUST_NO:
            self.screen = TRUST_YES
        elif enter and self.screen == TRUST_YES:
            self.screen = READY
        elif enter and self.screen == TRUST_NO:
            self.screen = None  # "No, exit": the session is gone


class TrustPrompt(unittest.TestCase):
    def run_prompts(self, drop):
        mgr = SessionManager("/tmp/never-used.sock")
        tui = FakeTUI(drop)
        mgr._tmux_capture, mgr._tmux_send = tui.capture, tui.send
        ready = asyncio.run(mgr._clear_startup_prompts("t", timeout_s=15))
        return ready, tui

    def test_dropped_key_never_exits(self):
        ready, tui = self.run_prompts(drop=1)
        self.assertTrue(ready)
        self.assertFalse(any(enter and on_no for _, enter, on_no in tui.events),
                         f"Enter pressed on 'No, exit': {tui.events}")

    def test_normal_path(self):
        ready, tui = self.run_prompts(drop=0)
        self.assertTrue(ready)


if __name__ == "__main__":
    unittest.main()
