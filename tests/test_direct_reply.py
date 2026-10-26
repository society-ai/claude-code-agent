"""Replying into a session started in Claude Code (direct_reply.py).

- A chat page maps to its direct session through the Tracker runner's state.
- The relay runs in the session's own permission mode and reports SENT,
  NOT_FOUND or AMBIGUOUS; anything but SENT is an error the person can act on.
- Delivery is confirmed by the message id in the transcript; the reply is the
  agent's text after it, up to the next prompt.
- The Tracker reads a relayed message as the owner's, another session's as a peer's.
"""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import direct_reply as dr  # noqa: E402
import session_tracker as st  # noqa: E402


def line(r):
    return json.dumps(r) + "\n"


PEER = {
    "type": "user", "isMeta": True, "permissionMode": "auto",
    "origin": {"kind": "peer", "name": "Society AI", "msg_id": "m-1", "body": "Ship it?"},
    "message": {"content": "Another Claude session sent a message: ..."},
}


class TestDirectReply(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = pathlib.Path(self.tmp.name)
        self.p = mock.patch.object(st, "BASE_DIR", self.base / "tracker")
        self.p.start()
        (self.base / "tracker").mkdir()
        self.t = self.base / "s.jsonl"

    def tearDown(self):
        self.p.stop()
        self.tmp.cleanup()

    def test_chat_maps_to_its_direct_session_only(self):
        (self.base / "tracker" / "state.json").write_text(json.dumps({"sessions": {
            "s1": {"chat_id": "c1", "origin": "direct", "cwd": "/repo"},
            "s2": {"chat_id": "c2", "origin": "society_ai", "cwd": "/x"},
        }}))
        found = dr.direct_session_for_chat("c1")
        self.assertEqual((found.session_id, found.cwd), ("s1", "/repo"))
        self.assertIsNone(dr.direct_session_for_chat("c2"))
        self.assertIsNone(dr.direct_session_for_chat(""))

    def test_permission_mode_is_the_latest(self):
        self.t.write_text(
            line({"type": "user", "permissionMode": "default", "message": {"content": "a"}})
            + line({"type": "user", "permissionMode": "bypassPermissions", "message": {"content": "b"}})
        )
        self.assertEqual(dr.permission_mode(self.t), "bypassPermissions")
        self.t.write_text(line({"type": "user", "permissionMode": "default", "message": {"content": "a"}}))
        self.assertEqual(dr.permission_mode(self.t), "manual")

    def test_delivery_and_reply(self):
        before = line({"type": "user", "message": {"content": "earlier"}})
        self.t.write_text(
            before
            + line(PEER)
            + line({"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "x"}]}})
            + line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Yes, pushing."}]}})
            + line({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})
            + line({"type": "assistant", "message": {"content": [{"type": "text", "text": "Pushed."}]}})
            + line({"type": "user", "message": {"content": "next prompt"}})
            + line({"type": "assistant", "message": {"content": [{"type": "text", "text": "not ours"}]}})
        )
        after = dr.find_delivered(self.t, 0, "m-1")
        self.assertEqual(after, len(before) + len(line(PEER)))
        self.assertIsNone(dr.find_delivered(self.t, 0, "m-2"))
        self.assertEqual(dr.reply_after(self.t, after), "Yes, pushing.\n\nPushed.")

    def test_relay_outcomes(self):
        def result(text):
            return mock.AsyncMock(return_value=(0, json.dumps({"result": text}), ""))

        with mock.patch.object(dr, "_run", result("SENT 4498a0ab-54bf-4c02-9848-35017e21f297")) as run:
            self.assertEqual(asyncio.run(dr.relay("My session", "hi", "auto")), "4498a0ab-54bf-4c02-9848-35017e21f297")
            cmd = run.await_args.args[0]
            self.assertIn("ListAgents,SendMessage", cmd)
            self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "auto")
            self.assertIn("<message>hi</message>", cmd[-1])
        for text, words in (("AMBIGUOUS", "Several"), ("NOT_FOUND", "not reachable"), ("ERROR boom", "boom")):
            with mock.patch.object(dr, "_run", result(text)):
                with self.assertRaises(dr.DirectReplyError) as e:
                    asyncio.run(dr.relay("My session", "hi", "auto"))
                self.assertIn(words, str(e.exception))


class TestClosedSession(unittest.TestCase):
    SESSION = dr.DirectSession("sid-1234-5678", "/repo", "c1")

    def run_ask(self, out, running=None):
        with mock.patch.object(dr.st, "running_session", return_value=running), \
                mock.patch.object(dr, "_run", mock.AsyncMock(return_value=(0, json.dumps(out), ""))) as run:
            return asyncio.run(dr.ask_closed_session(self.SESSION, "hi", "auto", 60)), run

    def test_one_turn_in_the_same_session(self):
        reply, run = self.run_ask({"result": "4", "session_id": "sid-1234-5678"})
        self.assertEqual(reply, "4")
        cmd = run.await_args.args[0]
        self.assertEqual(cmd[:4], ["claude", "-p", "--resume", "sid-1234-5678"])
        self.assertNotIn("--bg", cmd)
        self.assertEqual(run.await_args.kwargs["cwd"], "/repo")

    def test_runs_without_the_bridges_agent_settings(self):
        with mock.patch.dict(os.environ, {"AGENT_NAME": "bridge-agent", "SOCIETY_AI_AUTH_TOKEN": "sai_x",
                                          "SOCIETY_AI_TRACKER": "1", "HOME_TEST": "kept"}):
            _, run = self.run_ask({"result": "4", "session_id": "sid-1234-5678"})
        env = run.await_args.kwargs["env"]
        self.assertNotIn("AGENT_NAME", env)
        self.assertNotIn("SOCIETY_AI_AUTH_TOKEN", env)
        self.assertNotIn("SOCIETY_AI_TRACKER", env)
        self.assertEqual(env["HOME_TEST"], "kept")
        self.assertEqual(env["SOCIETY_AI_DISPATCHED"], "1")

    def test_never_answers_from_a_copy(self):
        with self.assertRaises(dr.DirectReplyError):
            self.run_ask({"result": "4", "session_id": "other-session"})

    def test_refuses_when_it_opened_meanwhile(self):
        with self.assertRaises(dr.DirectReplyError):
            self.run_ask({"result": "4"}, running={"pid": 1})


class TestTrackerReadsRelayedMessages(unittest.TestCase):
    def test_relayed_is_the_owner_other_sessions_are_peers(self):
        other = {**PEER, "origin": {"kind": "peer", "name": "Builder", "body": "status?"}}
        seg = st.extract_segment([PEER, other])
        self.assertEqual(seg.turns, [("USER", "Ship it?"), ("PEER Builder", "status?")])
        self.assertEqual(seg.user_turns, 1)


if __name__ == "__main__":
    unittest.main()
