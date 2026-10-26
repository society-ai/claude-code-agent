"""Session Tracker runner (session_tracker.py).

- Only the conversation is read: user prompts and agent text; tool calls,
  tool results, thinking, sub-agents and system reminders never are.
- The checklist is replayed from TaskCreate / TaskUpdate (and TodoWrite).
- The model sees short item ids; the patch carries the real ids and drops
  unknown ones.
- Turns of one session coalesce into one job; the stop event wins.
- A full job: registers a new session (bridge vs direct origin), sends facts
  and the model's patch, keeps the cursor only on success, redoes the model
  part once on 409, and forgets a deleted session on 404.
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

import session_tracker as st  # noqa: E402


def rec(kind, content, ts="2026-10-27T10:00:00Z", **extra):
    return {"type": kind, "timestamp": ts, "message": {"content": content}, **extra}


TRANSCRIPT = [
    rec("user", "Design the tracker please"),
    rec("assistant", [{"type": "thinking", "thinking": "secret"}, {"type": "text", "text": "Sure, plan:"}]),
    rec("assistant", [{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "Write schema"}}]),
    rec("user", [{"type": "tool_result", "content": "Task #1 created successfully"}]),
    rec("assistant", [{"type": "tool_use", "name": "Bash", "input": {"command": "rm -rf /tmp/x"}}]),
    rec("assistant", [{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "Write routes"}}]),
    rec("assistant", [{"type": "tool_use", "name": "TaskUpdate", "input": {"taskId": "1", "status": "completed"}}]),
    rec("user", "<system-reminder>ignore me</system-reminder>"),
    rec("assistant", [{"type": "text", "text": "Sub-agent text"}], isSidechain=True),
    rec("assistant", [{"type": "text", "text": "Schema done. Want me to commit?"}], ts="2026-10-27T10:05:00Z"),
]


def write_transcript(path: pathlib.Path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


class TestReading(unittest.TestCase):
    def test_conversation_only(self):
        seg = st.extract_segment(TRANSCRIPT)
        text = st.render_turns(seg.turns)
        self.assertIn("USER: Design the tracker please", text)
        self.assertIn("Want me to commit?", text)
        for leaked in ("secret", "rm -rf", "Task #1", "ignore me", "Sub-agent"):
            self.assertNotIn(leaked, text)
        self.assertEqual(seg.user_turns, 1)
        self.assertEqual(seg.last_agent_at, "2026-10-27T10:05:00Z")
        self.assertEqual(seg.first_prompt, "Design the tracker please")

    def test_agent_texts_of_one_turn_merge(self):
        seg = st.extract_segment(TRANSCRIPT)
        self.assertEqual([who for who, _ in seg.turns], ["USER", "AGENT"])

    def test_checklist_replay(self):
        tasks = st.apply_tasks(TRANSCRIPT, {})
        self.assertEqual(tasks, {
            "1": {"text": "Write schema", "state": "done"},
            "2": {"text": "Write routes", "state": "todo"},
        })
        more = [rec("assistant", [{"type": "tool_use", "name": "TaskCreate", "input": {"subject": "Tests"}}])]
        self.assertIn("3", st.apply_tasks(more, dict(tasks)))

    def test_read_records_stops_at_last_complete_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "t.jsonl"
            p.write_text(json.dumps(rec("user", "a")) + "\n" + '{"type": "user", "mess')
            records, end = st.read_records(p, 0)
            self.assertEqual(len(records), 1)
            self.assertEqual(end, len(json.dumps(rec("user", "a"))) + 1)

    def test_newest_turns_fit_the_cap(self):
        turns = [("USER", "x" * 100), ("AGENT", "y" * 100), ("USER", "z" * 100)]
        out = st.render_turns(turns, cap=250)
        self.assertNotIn("x", out)
        self.assertTrue(out.endswith("z" * 100))


class TestPatch(unittest.TestCase):
    ITEMS = [
        {"id": "uuid-q", "kind": "question", "state": "open", "text": "Backfill?", "blocking": True},
        {"id": "uuid-a", "kind": "approval", "state": "pending", "text": "Push", "action": "git push"},
        {"id": "uuid-s", "kind": "step", "state": "todo", "text": "hook step"},
    ]

    def test_short_ids_and_block(self):
        block, short = st.render_tracker({"version": 4, "title": "T", "nextStep": "N"}, self.ITEMS)
        self.assertEqual(short, {"q1": "uuid-q", "a1": "uuid-a"})
        self.assertIn("CURRENT TRACKER (version 4):", block)
        self.assertIn("next_step: N", block)
        self.assertIn("- q1 [question, open, blocking] Backfill?", block)
        self.assertNotIn("hook step", block)

    def test_patch_maps_ids_and_drops_unknown(self):
        out = {
            "set": {"status": "waiting_on_you", "state": " "},
            "add_items": [{"kind": "decision", "text": "No backfill"}],
            "update_items": [{"id": "q1", "state": "answered"}, {"id": "q9", "state": "answered"}],
        }
        patch = st.build_patch(out, {"q1": "uuid-q"}, 4)
        self.assertEqual(patch["base_version"], 4)
        self.assertEqual(patch["set"], {"status": "waiting_on_you"})
        self.assertEqual(patch["update_items"], [{"id": "uuid-q", "state": "answered"}])

    def test_no_change(self):
        self.assertIsNone(st.build_patch({"no_change": True}, {}, 1))
        self.assertIsNone(st.build_patch({"update_items": [{"id": "zz"}]}, {}, 1))


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = pathlib.Path(self.tmp.name)
        self.patches = [
            mock.patch.object(st, "BASE_DIR", base / "tracker"),
            mock.patch.object(st, "CLAUDE_SESSIONS_DIR", base / "claude-sessions"),
            mock.patch.object(st, "SOCIETY_CACHE", base / "cache"),
        ]
        for p in self.patches:
            p.start()
        (base / "tracker" / "runs").mkdir(parents=True)
        (base / "claude-sessions").mkdir()
        (base / "cache").mkdir()
        self.transcript = base / "s1.jsonl"
        write_transcript(self.transcript, TRANSCRIPT)
        self.runner = st.SessionTrackerRunner("jenkins", "sai_persona", "https://api.test", default_agent="jenkins")
        self.runner._lock_fd = 99
        self.runner._config = {"enabled": True, "model": "haiku", "prompt": "p", "output_schema": {}}
        self.calls = []
        self.responses = []

        async def fake_call(method, path, body=None):
            self.calls.append((method, path, body))
            return self.responses.pop(0)

        self.runner._call = fake_call
        self.runner._run_model = mock.AsyncMock(return_value={
            "set": {"status": "waiting_on_you"},
            "add_items": [{"kind": "approval", "text": "Commit the schema"}],
        })

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def job(self, event="stop"):
        return {"event": event, "cwd": "", "transcript_path": str(self.transcript), "due": 0}

    def run_job(self, event="stop"):
        asyncio.run(self.runner._process("s1", self.job(event)))

    def test_events_coalesce_and_stop_wins(self):
        self.runner.notify({"session_id": "s1", "event": "stop", "cwd": "/r"})
        self.runner.notify({"session_id": "s1", "event": "prompt", "cwd": "/r"})
        self.assertEqual(self.runner._pending["s1"]["event"], "stop")
        self.runner.notify({"session_id": "s2", "cwd": str(st.BASE_DIR / "runs")})
        self.assertNotIn("s2", self.runner._pending)

    def test_new_direct_session_registers_then_patches(self):
        tracker = {"id": "t1", "version": 0}
        self.responses = [
            (201, {"tracker": tracker}),           # register
            (200, {"tracker": tracker, "items": []}),  # get (model input)
            (200, {"tracker": {**tracker, "version": 1}}),  # patch
        ]
        self.run_job()
        method, path, body = self.calls[0]
        self.assertEqual(path, "/api/v1/session-trackers/register")
        self.assertEqual(body["origin"], "direct")
        self.assertEqual(body["agent_name"], "jenkins")
        self.assertEqual(body["title"], "Design the tracker please")
        _, ppath, patch = self.calls[2]
        self.assertEqual(ppath, "/api/v1/session-trackers/t1")
        self.assertEqual(patch["base_version"], 0)
        self.assertEqual(patch["set"], {"status": "waiting_on_you"})
        self.assertEqual(patch["facts"]["steps"][0], {"text": "Write schema", "state": "done"})
        self.assertFalse(patch["facts"]["running"])
        state = self.runner._state["sessions"]["s1"]
        self.assertEqual(state["tracker_id"], "t1")
        self.assertEqual(state["cursor"], self.transcript.stat().st_size)

    def test_bridge_session_is_society_ai_under_its_persona(self):
        (st.SOCIETY_CACHE / "searchpro").mkdir()
        (st.SOCIETY_CACHE / "searchpro" / "sessions.json").write_text(
            json.dumps({"sessions": [{"session_id": "s1", "persona": "searchpro"}]})
        )
        self.responses = [(201, {"tracker": {"id": "t1", "version": 0}}),
                          (200, {"tracker": {"id": "t1", "version": 0}, "items": []}),
                          (200, {})]
        self.run_job()
        self.assertEqual(self.calls[0][2]["origin"], "society_ai")
        self.assertEqual(self.calls[0][2]["agent_name"], "searchpro")

    def test_prompt_event_sends_facts_without_the_model(self):
        self.runner._state["sessions"]["s1"] = {"tracker_id": "t1", "cursor": 0, "tasks": {}}
        self.responses = [(200, {})]
        self.run_job("prompt")
        self.runner._run_model.assert_not_awaited()
        patch = self.calls[0][2]
        self.assertTrue(patch["no_change"])
        self.assertTrue(patch["facts"]["running"])

    def test_conflict_redoes_the_model_once_and_failure_keeps_the_cursor(self):
        self.runner._state["sessions"]["s1"] = {"tracker_id": "t1", "cursor": 0, "tasks": {}}
        self.responses = [
            (200, {"tracker": {"id": "t1", "version": 2}, "items": []}),
            (409, {}),
            (200, {"tracker": {"id": "t1", "version": 3}, "items": []}),
            (500, {}),
        ]
        self.run_job()
        self.assertEqual(self.runner._run_model.await_count, 2)
        self.assertEqual(self.calls[3][2]["base_version"], 3)
        self.assertEqual(self.runner._state["sessions"]["s1"]["cursor"], 0)

    def test_deleted_session_is_forgotten(self):
        self.runner._state["sessions"]["s1"] = {"tracker_id": "t1", "cursor": 0, "tasks": {}}
        self.responses = [(200, {"tracker": {"id": "t1", "version": 0}, "items": []}), (404, {})]
        self.run_job()
        self.assertNotIn("s1", self.runner._state["sessions"])

    def test_another_users_session_is_skipped_after_403(self):
        self.responses = [(403, {})]
        self.run_job()
        self.assertTrue(self.runner._state["sessions"]["s1"]["skip"])
        self.calls.clear()
        self.run_job()
        self.assertEqual(self.calls, [])


class TestHookGuard(unittest.TestCase):
    def test_tracker_runs_never_notify(self):
        import hook_stop
        with mock.patch.dict(os.environ, {"SOCIETY_AI_TRACKER": "1"}), \
                mock.patch.object(hook_stop, "_bridge_sockets") as socks:
            hook_stop._notify_mirrors({"session_id": "s1"})
        socks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
