"""Claude Code's own tool activity and thinking never leave the machine: the
session recording ships the conversation only, and there is no setting that
widens it. Society AI tool calls are recorded by the platform instead."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from transcript_shipper import TranscriptShipper  # noqa: E402

SECRET = "sk-live-SECRET123"


def line(record: dict) -> bytes:
    return json.dumps(record).encode()


class Recording(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.shipper = TranscriptShipper("https://api.x", "sai_x", self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def entries(self, record):
        return self.shipper._line_entries(line(record))

    def test_the_conversation_ships(self):
        user = self.entries({"type": "user", "message": {"content": "What changed?"}})
        reply = self.entries({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Two files."},
        ]}})
        self.assertEqual(user, [{"entry_type": "user", "content": {"text": "What changed?"}}])
        self.assertEqual(reply, [{"entry_type": "assistant", "content": {"text": "Two files."}}])

    def test_tool_calls_and_thinking_never_ship(self):
        out = self.entries({"type": "assistant", "message": {"content": [
            {"type": "thinking", "text": f"The key is {SECRET}."},
            {"type": "tool_use", "id": "t1", "name": "Bash",
             "input": {"command": f"echo {SECRET}", "description": "Show the key"}},
            {"type": "tool_use", "id": "t2", "name": "Edit",
             "input": {"file_path": "/Users/me/secret-project/.env"}},
            {"type": "text", "text": "Done."},
        ]}})
        self.assertEqual(out, [{"entry_type": "assistant", "content": {"text": "Done."}}])
        self.assertNotIn(SECRET, json.dumps(out))
        self.assertNotIn("secret-project", json.dumps(out))

    def test_tool_results_never_ship(self):
        out = self.entries({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": f"API_KEY={SECRET}"},
        ]}})
        self.assertEqual(out, [])

    def test_claude_code_internals_never_ship(self):
        for rtype in ("attachment", "file-history-snapshot", "progress", "system"):
            self.assertEqual(self.entries({"type": rtype, "content": SECRET}), [])


class NoSettingWidensIt(unittest.TestCase):
    def test_old_detail_settings_are_ignored_not_fatal(self):
        env = {**os.environ, "MIRROR_LEVEL": "full", "STATUS_VERBOSITY": "verbose"}
        r = subprocess.run(
            [sys.executable, "-c", "import config; print(hasattr(config, 'MIRROR_LEVEL'))"],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "False")

    def test_the_shipper_takes_no_level(self):
        with tempfile.TemporaryDirectory() as d, self.assertRaises(TypeError):
            TranscriptShipper("https://api.x", "sai_x", d, level="full")


if __name__ == "__main__":
    unittest.main()
