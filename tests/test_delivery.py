"""Delivery ticks: which chat a dispatch belongs to, and the report the
bridge sends so the ticks under the owner's message move."""

import asyncio
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("SESSION_MODE", "false")

import bridge  # noqa: E402


class ChatId(unittest.TestCase):
    def test_top_level(self):
        self.assertEqual(bridge._owner_chat_id({"chat_id": "c-1"}), "c-1")

    def test_nested_context(self):
        self.assertEqual(bridge._owner_chat_id({"context": {"chat_id": "c-2"}}), "c-2")

    def test_none(self):
        self.assertIsNone(bridge._owner_chat_id({}))
        self.assertIsNone(bridge._owner_chat_id({"chat_id": "  "}))


class FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeClient:
    def __init__(self, status_code=200, fail=False):
        self.calls, self.status_code, self.fail = [], status_code, fail

    async def post(self, url, json=None, headers=None):
        if self.fail:
            raise OSError("network down")
        self.calls.append((url, json, headers))
        return FakeResponse(self.status_code)


class Report(unittest.TestCase):
    def run_report(self, client, state="received"):
        with tempfile.TemporaryDirectory() as d:
            ctx = bridge.AgentContext(
                name="jenkins", token="sai_test", work_dir=d, extra_dirs=[], company_id="",
                api_url="https://api.example.com", socket=f"{d}/b.sock", state_dir=d,
            )
            b = bridge.Bridge(ctx)
            original = bridge._get_http_client
            bridge._get_http_client = lambda: client
            try:
                asyncio.run(b._report_delivery("chat-9", state))
            finally:
                bridge._get_http_client = original

    def test_posts_the_state_with_the_agent_token(self):
        client = FakeClient()
        self.run_report(client)
        url, body, headers = client.calls[0]
        self.assertEqual(url, "https://api.example.com/api/v1/chats/chat-9/delivery")
        self.assertEqual(body, {"state": "received"})
        self.assertEqual(headers["Authorization"], "Bearer sai_test")

    def test_rejection_and_network_errors_never_raise(self):
        self.run_report(FakeClient(status_code=409))
        self.run_report(FakeClient(fail=True))


if __name__ == "__main__":
    unittest.main()
