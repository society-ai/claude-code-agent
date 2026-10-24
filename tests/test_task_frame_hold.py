"""task.* frames wait for a registered connection instead of being lost.

The hub refuses every method but agent.register and heartbeat until the
connection has registered, and between connections there is no socket. A
task that finishes in either window must still deliver its reply, once the
bridge is registered again."""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("SESSION_MODE", "false")

import bridge  # noqa: E402


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    def methods(self) -> list[str]:
        return [m.get("method") for m in self.sent]


REGISTERED_ACK = json.dumps(
    {"jsonrpc": "2.0", "id": "msg-1", "result": {"registered": True, "agent_id": "t"}}
)


class TaskFrameHold(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        d = self._dir.name
        ctx = bridge.AgentContext(
            name="t", token="sai_x", work_dir=d, extra_dirs=[], company_id="",
            api_url="http://127.0.0.1:9", socket=f"{d}/b.sock", state_dir=d,
        )
        self.b = bridge.Bridge(ctx)

    def tearDown(self):
        self._dir.cleanup()

    def complete(self):
        return self.b.send_rpc("task.complete", {"task_id": "task-1", "final": True})

    def test_task_frame_before_registration_goes_out_after_the_ack(self):
        async def run():
            ws = FakeWS()
            self.b.ws = ws
            held = asyncio.create_task(self.complete())
            await asyncio.sleep(0.01)
            self.assertEqual(ws.sent, [])
            await self.b.handle_message(REGISTERED_ACK)
            await asyncio.wait_for(held, 1)
            return ws

        ws = asyncio.run(run())
        self.assertEqual(ws.methods(), ["task.complete"])

    def test_task_frame_while_disconnected_goes_out_on_the_next_connection(self):
        async def run():
            self.b.ws = None
            held = asyncio.create_task(self.complete())
            await asyncio.sleep(0.01)
            ws = FakeWS()
            self.b.ws = ws
            await self.b.handle_message(REGISTERED_ACK)
            await asyncio.wait_for(held, 1)
            return ws

        ws = asyncio.run(run())
        self.assertEqual(ws.methods(), ["task.complete"])

    def test_registered_connection_sends_task_frames_at_once(self):
        async def run():
            ws = FakeWS()
            self.b.ws = ws
            await self.b.handle_message(REGISTERED_ACK)
            await asyncio.wait_for(self.complete(), 1)
            return ws

        self.assertEqual(asyncio.run(run()).methods(), ["task.complete"])

    def test_register_and_heartbeat_are_never_held(self):
        async def run():
            ws = FakeWS()
            self.b.ws = ws
            await asyncio.wait_for(self.b.send_rpc("agent.register", {"agent_name": "t"}), 1)
            await asyncio.wait_for(self.b.send_rpc("heartbeat", {}), 1)
            return ws

        self.assertEqual(asyncio.run(run()).methods(), ["agent.register", "heartbeat"])

    def test_task_frame_is_dropped_when_no_registration_comes(self):
        original = bridge.TASK_FRAME_HOLD_S
        bridge.TASK_FRAME_HOLD_S = 0.01
        try:
            async def run():
                ws = FakeWS()
                self.b.ws = ws
                await asyncio.wait_for(self.complete(), 1)
                return ws

            self.assertEqual(asyncio.run(run()).sent, [])
        finally:
            bridge.TASK_FRAME_HOLD_S = original


if __name__ == "__main__":
    unittest.main()
