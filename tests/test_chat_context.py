"""Work done in a session names the app Chat it happens in, for the System of
Record: X-Society-Chat on HTTP calls to the router, chat_id on the hub's
agent.send_task. Nothing is sent when the chat is unknown. Calls the
society-ai MCP server makes for a tool also name the tool (X-Society-Tool);
the bridge's own housekeeping calls never do."""

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

import api  # noqa: E402
import attachments  # noqa: E402
import bridge  # noqa: E402
import chat_context  # noqa: E402
import identity  # noqa: E402
from session_manager import SessionManager, SessionRecord  # noqa: E402
from transcript_shipper import TranscriptShipper  # noqa: E402

CHAT = "3a8c79ae-45b6-4cbd-865d-fadb5e5620df"
OTHER_CHAT = "e1940c41-5b11-4921-905f-abbda611776c"


def run(coro):
    return asyncio.run(coro)


class Headers(unittest.TestCase):
    def test_only_uuids_become_a_header(self):
        self.assertEqual(chat_context.chat_headers(CHAT), {"X-Society-Chat": CHAT})
        self.assertEqual(chat_context.chat_headers(CHAT.upper()), {"X-Society-Chat": CHAT})
        for bad in ("", None, "chat-9", f"{CHAT}\r\nX-Evil: 1"):
            self.assertEqual(chat_context.chat_headers(bad), {})


class CurrentChat(unittest.TestCase):
    def setUp(self):
        chat_context._cache.clear()
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("SOCIETY_AI_CHAT_ID", None)
        os.environ.pop("SOCIETY_AI_SESSION_KEY", None)

    def tearDown(self):
        self._env.stop()

    def test_no_session_means_no_chat_and_no_ipc(self):
        with mock.patch.object(chat_context.bridge_ipc, "call") as call:
            self.assertEqual(run(chat_context.current_chat_id()), "")
        call.assert_not_called()

    def test_a_spawned_run_gets_it_directly(self):
        os.environ["SOCIETY_AI_CHAT_ID"] = CHAT
        self.assertEqual(run(chat_context.current_chat_id()), CHAT)

    def test_a_session_asks_the_bridge_and_caches_briefly(self):
        os.environ["SOCIETY_AI_SESSION_KEY"] = "work-1"

        async def call(method, params, timeout=60.0):
            self.assertEqual((method, params), ("session_chat", {"session_key": "work-1"}))
            return {"chat_id": CHAT}

        with mock.patch.object(chat_context.bridge_ipc, "call", side_effect=call) as m:
            self.assertEqual(run(chat_context.current_chat_id()), CHAT)
            self.assertEqual(run(chat_context.current_chat_id()), CHAT)
        self.assertEqual(m.call_count, 1)

    def test_a_bridge_that_cannot_answer_means_no_chat(self):
        os.environ["SOCIETY_AI_SESSION_KEY"] = "work-2"

        async def call(*a, **kw):
            raise chat_context.bridge_ipc.IPCClientError("no bridge")

        with mock.patch.object(chat_context.bridge_ipc, "call", side_effect=call):
            self.assertEqual(run(chat_context.current_chat_id()), "")


class FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.content = json.dumps(self._body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class ApiRequests(unittest.TestCase):
    def test_router_calls_carry_the_chat(self):
        sent = {}

        class Client:
            async def request(self, method, url, headers=None, **kw):
                sent["headers"] = headers
                return FakeResp()

        ident = identity.Identity(name="jenkins", token="sai_x", api_url="https://api.x")

        async def chat():
            return CHAT

        with mock.patch.object(api.identity, "current", return_value=ident), \
             mock.patch.object(api, "client", return_value=Client()), \
             mock.patch.object(api.chat_context, "current_chat_id", side_effect=chat):
            run(api.get("/api/v1/tasks"))
        self.assertEqual(sent["headers"]["X-Society-Chat"], CHAT)
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sai_x")

    def test_no_chat_no_header(self):
        sent = {}

        class Client:
            async def request(self, method, url, headers=None, **kw):
                sent["headers"] = headers
                return FakeResp()

        ident = identity.Identity(name="jenkins", token="sai_x", api_url="https://api.x")

        async def chat():
            return ""

        with mock.patch.object(api.identity, "current", return_value=ident), \
             mock.patch.object(api, "client", return_value=Client()), \
             mock.patch.object(api.chat_context, "current_chat_id", side_effect=chat):
            run(api.get("/api/v1/tasks"))
        self.assertNotIn("X-Society-Chat", sent["headers"])


class SessionChat(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.sock = str(pathlib.Path(self._tmp.name) / "channels.sock")

    def tearDown(self):
        self._tmp.cleanup()

    def manager_with(self, key="work-1"):
        mgr = SessionManager(self.sock)
        mgr._sessions[key] = SessionRecord(
            work_item_key=key, persona="jenkins", session_id="s-1", tmux_name="t", title="x",
            has_run_once=True,
        )
        return mgr

    def test_the_owner_chat_wins_and_the_recording_only_fills_a_gap(self):
        mgr = self.manager_with()
        mgr.set_chat("work-1", OTHER_CHAT, only_if_empty=True)
        self.assertEqual(mgr.get("work-1").chat_id, OTHER_CHAT)
        mgr.set_chat("work-1", CHAT)
        mgr.set_chat("work-1", OTHER_CHAT, only_if_empty=True)
        self.assertEqual(mgr.get("work-1").chat_id, CHAT)

    def test_it_survives_a_bridge_restart(self):
        mgr = self.manager_with()
        mgr.set_chat("work-1", CHAT)
        self.assertEqual(SessionManager(self.sock).get("work-1").chat_id, CHAT)

    def test_the_bridge_answers_its_sessions(self):
        with tempfile.TemporaryDirectory() as d:
            ctx = bridge.AgentContext(
                name="jenkins", token="sai_x", work_dir=d, extra_dirs=[], company_id="",
                api_url="https://api.x", socket=f"{d}/b.sock", state_dir=d,
            )
            b = bridge.Bridge(ctx)
            b._session_mgr = self.manager_with()
            b._session_mgr.set_chat("work-1", CHAT)
            self.assertEqual(run(b.ipc_session_chat({"session_key": "work-1"})), {"chat_id": CHAT})
            self.assertEqual(run(b.ipc_session_chat({"session_key": "nope"})), {"chat_id": ""})
            b._session_mgr = None
            self.assertEqual(run(b.ipc_session_chat({"session_key": "work-1"})), {"chat_id": ""})


class HubSend(unittest.TestCase):
    def send(self, params):
        with tempfile.TemporaryDirectory() as d:
            ctx = bridge.AgentContext(
                name="jenkins", token="sai_x", work_dir=d, extra_dirs=[], company_id="",
                api_url="https://api.x", socket=f"{d}/b.sock", state_dir=d,
            )
            b = bridge.Bridge(ctx)
            b.ws, b.registered = object(), True
            sent = {}

            async def send_rpc(method, rpc_params, msg_id=None):
                sent["method"], sent["params"] = method, rpc_params
                b._pending_requests[msg_id].set_result({"error": True, "message": "stop here"})

            b.send_rpc = send_rpc
            run(b.ipc_send_message({"agent_name": "nova", "message": "hi", "skill_id": "chat", **params}))
            return sent

    def test_send_task_carries_the_chat_and_tool(self):
        sent = self.send({"chat_id": CHAT, "tool": "send_message"})
        self.assertEqual(sent["method"], "agent.send_task")
        self.assertEqual(sent["params"]["chat_id"], CHAT)
        self.assertEqual(sent["params"]["tool"], "send_message")

    def test_no_or_bad_context_is_left_out(self):
        params = self.send({})["params"]
        self.assertNotIn("chat_id", params)
        self.assertNotIn("tool", params)
        params = self.send({"chat_id": "not-a-chat", "tool": "bad name"})["params"]
        self.assertNotIn("chat_id", params)
        self.assertNotIn("tool", params)

    def test_the_send_message_tool_passes_its_name(self):
        import mcp_server

        captured = {}

        async def ipc(method, params, timeout=60.0):
            captured.update(params)
            return {"ok": True}

        async def chat():
            return CHAT

        with mock.patch.object(mcp_server, "_ipc_call", side_effect=ipc), \
             mock.patch.object(mcp_server.chat_context, "current_chat_id", side_effect=chat):
            run(mcp_server.mcp.call_tool("send_message", {"to": "nova", "message": "hi"}))
        self.assertEqual(captured["tool"], "send_message")
        self.assertEqual(captured["chat_id"], CHAT)


class Recording(unittest.TestCase):
    def test_mirror_batches_name_the_chat(self):
        posts = []

        class Client:
            async def post(self, url, json=None, headers=None):
                posts.append(headers)
                return FakeResp(200, {"chatId": OTHER_CHAT})

        with tempfile.TemporaryDirectory() as d:
            shipper = TranscriptShipper("https://api.x", "sai_x", d)
            shipper._client = Client()
            shipper.register("s-1", cwd=d, chat_id=CHAT)
            meta = shipper._state["s-1"]
            run(shipper._post_batch("s-1", meta, [], status=None))
            # Once the platform says which chat the session projects into,
            # that is the one named.
            run(shipper._post_batch("s-1", meta, [], status=None))
        self.assertEqual(posts[0], {"X-Society-Chat": CHAT})
        self.assertEqual(posts[1], {"X-Society-Chat": OTHER_CHAT})


class Files(unittest.TestCase):
    def test_file_lookups_name_the_chat(self):
        seen = {}

        class Client:
            async def get(self, url, headers=None):
                seen["headers"] = headers
                return FakeResp(200, {"url": "https://s3/x", "size": 1})

        run(attachments.file_info(Client(), "https://api.x", "sai_x", "a1",
                                  chat_context.chat_headers(CHAT)))
        self.assertEqual(seen["headers"]["X-Society-Chat"], CHAT)
        run(attachments.file_info(Client(), "https://api.x", "sai_x", "a1"))
        self.assertNotIn("X-Society-Chat", seen["headers"])


class ToolName(unittest.TestCase):
    def test_a_tool_run_names_itself_and_nothing_else_does(self):
        import mcp_server

        server = mcp_server.SocietyMCP("test")

        @server.tool()
        async def list_tasks() -> str:
            return json.dumps(chat_context.tool_headers())

        out = run(server.call_tool("list_tasks", {}))
        blocks = out[0] if isinstance(out, tuple) else out
        self.assertEqual(json.loads(blocks[0].text), {"X-Society-Tool": "list_tasks"})
        self.assertEqual(chat_context.tool_headers(), {})

    def test_router_calls_inside_a_tool_carry_its_name(self):
        sent = {}

        class Client:
            async def request(self, method, url, headers=None, **kw):
                sent["headers"] = headers
                return FakeResp()

        ident = identity.Identity(name="jenkins", token="sai_x", api_url="https://api.x")

        async def chat():
            return ""

        async def as_tool():
            token = chat_context.current_tool.set("update_task")
            try:
                await api.patch("/api/v1/tasks/x", {})
            finally:
                chat_context.current_tool.reset(token)

        with mock.patch.object(api.identity, "current", return_value=ident), \
             mock.patch.object(api, "client", return_value=Client()), \
             mock.patch.object(api.chat_context, "current_chat_id", side_effect=chat):
            run(as_tool())
        self.assertEqual(sent["headers"]["X-Society-Tool"], "update_task")

    def test_housekeeping_never_names_a_tool(self):
        posts = []

        class Client:
            async def post(self, url, json=None, headers=None):
                posts.append(headers)
                return FakeResp(200, {})

        async def inside_a_tool():
            chat_context.current_tool.set("list_tasks")
            with tempfile.TemporaryDirectory() as d:
                shipper = TranscriptShipper("https://api.x", "sai_x", d)
                shipper._client = Client()
                shipper.register("s-1", cwd=d, chat_id=CHAT)
                await shipper._post_batch("s-1", shipper._state["s-1"], [], status=None)

        run(inside_a_tool())
        self.assertNotIn("X-Society-Tool", posts[0])

    def test_odd_names_are_not_sent(self):
        async def check():
            chat_context.current_tool.set("bad name\r\n")
            return chat_context.tool_headers()

        self.assertEqual(run(check()), {})


if __name__ == "__main__":
    unittest.main()
