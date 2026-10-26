"""The Lead agent's tools and the message tool.

- Lead-only tools are listed only for the owner's Lead agent (the platform's
  /api/v1/agents/me says agent_role), and refused locally for anyone else.
- update_agent edits instructions and skills (router) and a connected
  agent's profile and card skills (factory); nothing hosted-era is left.
- send_message replaces delegate_task: any reachable agent, "chat" by default.
- respond_to_inbox(response="dismiss") dismisses.
"""

import asyncio
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import mcp_server  # noqa: E402

ITEM = "5d86bae8-0000-4000-8000-000000000009"


def parsed(result: str) -> dict:
    return json.loads(result.split("\n", 1)[1])


def ident(name="jenkins", bound=True):
    return SimpleNamespace(api_url="https://api.test", token="t", company_id="", bound=bound, name=name, display_name="")


class LeadOnlyTools(unittest.TestCase):
    def setUp(self):
        mcp_server._roles.clear()

    def _names(self, role):
        me = {"agent_name": "jenkins", "agent_role": role, "permissions": []}
        with mock.patch.object(mcp_server, "_ident", return_value=ident()), \
             mock.patch.object(mcp_server.api, "get", new=mock.AsyncMock(return_value=me)):
            return {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}

    def test_lead_sees_lead_tools(self):
        names = self._names("lead")
        self.assertTrue(mcp_server.LEAD_ONLY_TOOLS <= names)

    def test_regular_agent_does_not(self):
        names = self._names("regular")
        self.assertFalse(mcp_server.LEAD_ONLY_TOOLS & names)
        self.assertIn("list_tasks", names)
        self.assertIn("send_message", names)

    def test_unknown_role_hides_them_and_is_not_cached(self):
        with mock.patch.object(mcp_server, "_ident", return_value=ident()), \
             mock.patch.object(mcp_server.api, "get", new=mock.AsyncMock(return_value={"error": True})):
            names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
        self.assertFalse(mcp_server.LEAD_ONLY_TOOLS & names)
        self.assertNotIn("jenkins", mcp_server._roles)

    def test_regular_agent_calling_a_lead_tool_is_refused(self):
        mcp_server._roles["searchpro"] = "regular"
        from mcp.server.fastmcp.exceptions import ToolError

        with mock.patch.object(mcp_server, "_ident", return_value=ident("searchpro")):
            with self.assertRaises(ToolError):
                asyncio.run(mcp_server.mcp.call_tool("get_feed", {}))

    def test_removed_tools_are_gone(self):
        names = self._names("lead")
        for gone in ("deploy_agent", "delegate_task", "get_my_tasks", "dismiss_inbox",
                     "list_departments", "create_department"):
            self.assertNotIn(gone, names)


class UpdateAgent(unittest.TestCase):
    def _run(self, **kw):
        calls = []

        async def record(path, body=None, **_):
            calls.append((path, body))
            return {"ok": True}

        with mock.patch.object(mcp_server, "_ident", return_value=ident()), \
             mock.patch.object(mcp_server.api, "patch", side_effect=record), \
             mock.patch.object(mcp_server.api, "put", side_effect=record):
            out = parsed(asyncio.run(mcp_server.update_agent(agent_name="searchpro", **kw)))
        return calls, out

    def test_instructions_and_skills_go_to_the_router(self):
        calls, _ = self._run(system="Answer briefly.", skills=["orchestrator-wake"])
        self.assertEqual(calls, [("/api/v1/agents/searchpro/system", {"system": "Answer briefly.", "skills": ["orchestrator-wake"]})])

    def test_profile_and_card_skills_go_to_the_factory(self):
        skill = {"id": "research", "name": "Research", "description": "d", "pricing_model": "per_task", "price_usd": 0.5}
        calls, _ = self._run(display_name="Search Pro", category="research", card_skills=[skill])
        self.assertEqual(calls, [("/api/v1/factory/self-hosted/agents/searchpro",
                                  {"display_name": "Search Pro", "category": "research", "skills": [skill]})])

    def test_nothing_to_update(self):
        calls, out = self._run()
        self.assertEqual(calls, [])
        self.assertTrue(out.get("error"))


class SendMessage(unittest.TestCase):
    def _send(self, **kw):
        seen = {}

        async def fake_ipc(method, params, timeout=60.0):
            seen["method"], seen["params"] = method, params
            return {"reply": "hi"}

        with mock.patch.object(mcp_server, "_ident", return_value=ident()), \
             mock.patch.object(mcp_server, "_ipc_call", side_effect=fake_ipc):
            out = parsed(asyncio.run(mcp_server.send_message(**kw)))
        return seen, out

    def test_chat_by_default(self):
        seen, _ = self._send(to="@research-bot", message="hello")
        self.assertEqual(seen["method"], "send_message")
        self.assertEqual(seen["params"]["agent_name"], "research-bot")
        self.assertEqual(seen["params"]["skill_id"], "chat")

    def test_conversation_id_continues_the_session(self):
        cid = "5d86bae8-0000-4000-8000-00000000000a"
        seen, _ = self._send(to="research-bot", message="and?", conversation_id=cid, skill_id="research")
        self.assertEqual(seen["params"]["session_id"], cid)
        self.assertEqual(seen["params"]["skill_id"], "research")

    def test_bad_skill_id_is_refused(self):
        seen, out = self._send(to="research-bot", message="x", skill_id="not a slug!")
        self.assertTrue(out.get("error"))
        self.assertNotIn("method", seen)


class DismissThroughRespond(unittest.TestCase):
    def test_dismiss(self):
        calls = []

        async def record(path, body=None, **_):
            calls.append((path, body))
            return {"ok": True}

        with mock.patch.object(mcp_server, "_ident", return_value=ident()), \
             mock.patch.object(mcp_server.api, "post", side_effect=record):
            asyncio.run(mcp_server.respond_to_inbox(item_id=ITEM, response="dismiss"))
        self.assertEqual(calls, [(f"/api/v1/inbox/{ITEM}/dismiss", {"dismissed_by_agent": "jenkins"})])


if __name__ == "__main__":
    unittest.main()
