"""The reply is the whole turn: the bridge waits for the turn's final entry,
so a turn that used a tool is not cut off at its opening line."""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("SESSION_MODE", "false")

import bridge  # noqa: E402

EVENT = "task-1"


def entry(kind, **kw):
    if kind == "event":
        return {"type": "user", "message": {"role": "user", "content":
                f'<channel source="society-ai-channel" event_id="{EVENT}" kind="chat">hi</channel>'}}
    if kind == "text":
        return {"type": "assistant", "message": {"content": [{"type": "text", "text": kw["text"]}],
                                                  "stop_reason": kw["stop"]}}
    if kind == "tool":
        return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}],
                                                  "stop_reason": "tool_use"}}


class TurnReply(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cwd = self._tmp.name
        self.sid = "sid-1"
        from transcript_shipper import transcript_path
        self.path = transcript_path(self.cwd, self.sid)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.path.parent, ignore_errors=True)
        self._tmp.cleanup()

    def write(self, *entries):
        self.path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    def test_partial_turn_is_not_final(self):
        self.write(entry("event"), entry("text", text="Let me look.", stop="tool_use"), entry("tool"))
        text, final = bridge.Bridge._turn_reply(self.cwd, self.sid, EVENT)
        self.assertEqual((text, final), ("Let me look.", False))

    def test_complete_turn(self):
        self.write(entry("event"), entry("text", text="Let me look.", stop="tool_use"), entry("tool"),
                   entry("text", text="It is red.", stop="end_turn"))
        text, final = bridge.Bridge._turn_reply(self.cwd, self.sid, EVENT)
        self.assertEqual((text, final), ("Let me look.\n\nIt is red.", True))

    def test_close_waits_for_the_late_final_block(self):
        self.write(entry("event"), entry("text", text="Let me look.", stop="tool_use"), entry("tool"))
        with tempfile.TemporaryDirectory() as d:
            ctx = bridge.AgentContext(name="a", token="t", work_dir=d, extra_dirs=[], company_id="",
                                      api_url="https://x", socket=f"{d}/b.sock", state_dir=d)
            b = bridge.Bridge(ctx)

            async def run():
                fut = asyncio.get_event_loop().create_future()
                b._pending_session_tasks = {EVENT: fut}   # session-mode state
                b._session_awaiting = {self.sid: {"task_id": EVENT, "cwd": self.cwd}}

                async def flush_late():
                    await asyncio.sleep(0.8)  # the CLI writes the final block after the hook
                    self.write(entry("event"), entry("text", text="Let me look.", stop="tool_use"),
                               entry("tool"), entry("text", text="It is red.", stop="end_turn"))
                asyncio.ensure_future(flush_late())
                await b._close_turn_from_transcript(self.sid)
                return fut.result()

            self.assertEqual(asyncio.run(run()), "Let me look.\n\nIt is red.")

    def test_never_final_still_answers_after_the_wait(self):
        self.write(entry("event"), entry("text", text="Partial.", stop="refusal"))
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bridge, "TURN_FINAL_WAIT_S", 0.5):
            ctx = bridge.AgentContext(name="a", token="t", work_dir=d, extra_dirs=[], company_id="",
                                      api_url="https://x", socket=f"{d}/b.sock", state_dir=d)
            b = bridge.Bridge(ctx)

            async def run():
                fut = asyncio.get_event_loop().create_future()
                b._pending_session_tasks = {EVENT: fut}   # session-mode state
                b._session_awaiting = {self.sid: {"task_id": EVENT, "cwd": self.cwd}}
                await b._close_turn_from_transcript(self.sid)
                return fut.result()

            self.assertEqual(asyncio.run(run()), "Partial.")


if __name__ == "__main__":
    unittest.main()
