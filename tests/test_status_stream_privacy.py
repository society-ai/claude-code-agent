"""Progress rows streamed to the chat (spawn mode task.status frames) follow
MIRROR_LEVEL like the session mirror: at the default messages level they carry
the tool name and a safe target only, never command lines, MCP inputs, tool
results or thinking. full keeps the richer summaries."""

import json
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from streaming import StreamMapper  # noqa: E402

SECRET = "sk-live-SECRET123"


def tool_use(tool_id, name, inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": inp},
    ]}}


def tool_result(tool_id, text, is_error=False):
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "is_error": is_error,
         "content": [{"type": "text", "text": text}]},
    ]}}


def thinking(text):
    return {"type": "assistant", "message": {"content": [
        {"type": "thinking", "text": text},
    ]}}


def payload(parts):
    return parts[0]["data"]["payload"]


class MessagesLevel(unittest.TestCase):
    def setUp(self):
        self.m = StreamMapper("t1")  # default level is messages

    def test_bash_shows_description_never_the_command(self):
        p = payload(self.m.consume(tool_use("a", "Bash", {
            "command": f"curl -H 'Authorization: {SECRET}' https://x.io",
            "description": "Call the API",
        })))
        self.assertEqual(p["args"], "Call the API")
        self.assertNotIn(SECRET, json.dumps(p))

    def test_web_tools_show_the_host_only(self):
        p = payload(self.m.consume(tool_use("a", "WebFetch", {
            "url": f"https://api.example.com/v1?token={SECRET}", "prompt": "x",
        })))
        self.assertEqual(p["args"], "api.example.com")

    def test_file_tools_show_the_path(self):
        p = payload(self.m.consume(tool_use("a", "Read", {"file_path": "/w/src/main.py"})))
        self.assertEqual(p["args"], "/w/src/main.py")

    def test_mcp_tools_show_no_input(self):
        p = payload(self.m.consume(tool_use("a", "mcp__society-ai__post_chat_message", {
            "text": f"here is the key {SECRET}",
        })))
        self.assertEqual(p["name"], "mcp__society-ai__post_chat_message")
        self.assertEqual(p["args"], "")

    def test_results_and_errors_carry_no_output(self):
        self.m.consume(tool_use("a", "Bash", {"command": "env", "description": "Show env"}))
        ok = payload(self.m.consume(tool_result("a", f"API_KEY={SECRET}")))
        self.assertEqual(ok["status"], "success")
        self.assertNotIn("result", ok)
        self.m.consume(tool_use("b", "Read", {"file_path": "/w/.env"}))
        err = payload(self.m.consume(tool_result("b", f"denied {SECRET}", is_error=True)))
        self.assertEqual(err["status"], "error")
        self.assertNotIn("error", err)
        self.assertNotIn(SECRET, json.dumps([ok, err]))

    def test_thinking_is_not_streamed_even_when_verbose(self):
        m = StreamMapper("t1", verbosity="verbose")
        self.assertEqual(m.consume(thinking(f"The key is {SECRET}. Next.")), [])

    def test_unknown_level_falls_back_to_messages(self):
        m = StreamMapper("t1", mirror_level="bogus")
        p = payload(m.consume(tool_use("a", "Bash", {"command": SECRET})))
        self.assertEqual(p["args"], "")

    def test_reply_text_still_accumulates(self):
        self.m.consume({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Done."},
        ]}})
        self.assertEqual(self.m.final_text(), "Done.")


class FullLevel(unittest.TestCase):
    def setUp(self):
        self.m = StreamMapper("t1", mirror_level="full")

    def test_keeps_input_summaries_results_and_thinking(self):
        p = payload(self.m.consume(tool_use("a", "Bash", {"command": "pytest tests/"})))
        self.assertEqual(p["args"], "$ pytest tests/")
        r = payload(self.m.consume(tool_result("a", "3 passed")))
        self.assertEqual(r["result"], "3 passed")
        log = payload(self.m.consume(thinking("Checking the tests. Then more.")))
        self.assertEqual(log["message"], "💭 Checking the tests.")

    def test_quiet_still_drops_thinking(self):
        m = StreamMapper("t1", verbosity="quiet", mirror_level="full")
        self.assertEqual(m.consume(thinking("Anything.")), [])


if __name__ == "__main__":
    unittest.main()
