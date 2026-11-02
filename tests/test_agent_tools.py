"""The Lead agent's tools and the message tool.

- An agent sees only the tools its permissions allow (the platform's
  /api/v1/agents/me says them: its preset with its owner's changes); other
  tools are refused locally.
- update_agent edits instructions and skills (router) and a connected
  agent's profile and card skills (factory); nothing hosted-era is left.
- send_message replaces delegate_task: any reachable agent, "chat" by default.
- respond_to_inbox(response="dismiss") dismisses.
- Every agent reads session Trackers (the platform scopes them: the Lead all,
  others their own); filters are checked before any call.
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


# The platform's presets (agent_router/iam/catalog.py), as /api/v1/agents/me reports them.
REGULAR = [
    "network.use", "inbox.use", "tasks.own", "tasks.contacts", "chats.own", "files.use", "home.profile",
    "home.post", "sessions.own", "schedules.self", "workflows.view", "workflows.use", "knowledge.view",
    "knowledge.submit", "companies.view", "companies.manage", "skills.view",
]
LEAD = REGULAR + [
    "tasks.owner_agents", "tasks.board", "files.owner", "home.view", "home.brief", "sessions.all",
    "schedules.owner_agents", "workflows.personal", "workspaces.view", "workspaces.manage", "knowledge.owner",
    "companies.create", "agents.view", "agents.setup",
]
LEAD_TOOLS = {"list_owner_agents", "update_agent", "get_feed", "publish_brief", "create_company"}


class ToolsFollowPermissions(unittest.TestCase):
    def setUp(self):
        mcp_server._permissions.clear()

    def _names(self, permissions):
        mcp_server._permissions.clear()
        me = {"agent_name": "jenkins", "agent_role": "regular", "permissions": permissions}
        with mock.patch.object(mcp_server, "_ident", return_value=ident()), \
             mock.patch.object(mcp_server.api, "get", new=mock.AsyncMock(return_value=me)):
            return {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}

    def test_lead_sees_lead_tools(self):
        self.assertTrue(LEAD_TOOLS <= self._names(LEAD))

    def test_regular_agent_does_not(self):
        names = self._names(REGULAR)
        self.assertFalse(LEAD_TOOLS & names)
        self.assertIn("list_tasks", names)
        self.assertIn("send_message", names)
        # Curating knowledge is a permission its owner (or the Librarian template) adds.
        self.assertNotIn("write_kb_item", names)

    def test_the_owners_choices_add_and_remove(self):
        names = self._names(REGULAR + ["agents.setup"])
        self.assertIn("update_agent", names)
        names = self._names([p for p in REGULAR if p != "tasks.own"])
        self.assertNotIn("create_task", names)

    def test_unknown_permissions_show_everything_and_are_not_cached(self):
        with mock.patch.object(mcp_server, "_ident", return_value=ident()), \
             mock.patch.object(mcp_server.api, "get", new=mock.AsyncMock(return_value={"error": True})):
            names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
        self.assertTrue(LEAD_TOOLS <= names)  # the platform decides
        self.assertNotIn("jenkins", mcp_server._permissions)

    def test_calling_a_tool_it_may_not_use_is_refused(self):
        mcp_server._permissions["searchpro"] = frozenset(REGULAR)
        from mcp.server.fastmcp.exceptions import ToolError

        with mock.patch.object(mcp_server, "_ident", return_value=ident("searchpro")):
            with self.assertRaises(ToolError):
                asyncio.run(mcp_server.mcp.call_tool("get_feed", {}))

    def test_every_tool_names_its_permission(self):
        names = {t.name for t in asyncio.run(mcp_server.FastMCP.list_tools(mcp_server.mcp))}
        unlisted = names - set(mcp_server.TOOL_PERMISSIONS) - {"switch_agent"}
        self.assertEqual(unlisted, set(), "add these to TOOL_PERMISSIONS")
        self.assertEqual(set(mcp_server.TOOL_PERMISSIONS) - names, set(), "stale TOOL_PERMISSIONS entries")

    def test_removed_tools_are_gone(self):
        names = self._names(LEAD)
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


class SessionTrackers(unittest.TestCase):
    def _call(self, name, **kw):
        get = mock.AsyncMock(return_value={"trackers": []})
        with mock.patch.object(mcp_server.api, "get", new=get):
            out = asyncio.run(getattr(mcp_server, name)(**kw))
        return out, get

    def test_every_agent_has_them(self):
        names = ToolsFollowPermissions()._names(REGULAR)
        self.assertIn("list_session_trackers", names)
        self.assertIn("get_session_tracker", names)

    def test_list_passes_the_filters(self):
        _, get = self._call(
            "list_session_trackers", show="needs_you", agent="jenkins", started_in="claude_code",
            loose_ends=True, query="push", limit=500,
        )
        get.assert_awaited_once_with("/api/v1/session-trackers", params={
            "show": "needs_you", "limit": 100, "agent": "jenkins", "started_in": "claude_code",
            "loose_ends": "true", "q": "push",
        })

    def test_bad_filters_never_call(self):
        for kw in ({"show": "everything"}, {"status": "sleeping"}, {"started_in": "mars"}):
            out, get = self._call("list_session_trackers", **kw)
            self.assertTrue(parsed(out)["error"])
            get.assert_not_awaited()

    def test_get_one(self):
        _, get = self._call("get_session_tracker", tracker_id=ITEM)
        get.assert_awaited_once_with(f"/api/v1/session-trackers/{ITEM}")
        out, get = self._call("get_session_tracker", tracker_id="TRK-1")
        self.assertTrue(parsed(out)["error"])
        get.assert_not_awaited()
