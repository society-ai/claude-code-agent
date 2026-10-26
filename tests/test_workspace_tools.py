"""Workspace tools match the router: projects need a space, leads are agent
names, one way to create a company space, and task boards by workspace.
Spaces and projects work outside companies too, company_id="personal"
overrides COMPANY_ID, and teams take a join policy and agent level."""

import asyncio
import json
import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import mcp_server  # noqa: E402

CID = "5d86bae8-0000-4000-8000-000000000001"
SID = "5d86bae8-0000-4000-8000-000000000002"
PID = "5d86bae8-0000-4000-8000-000000000003"


def parsed(result: str) -> dict:
    return json.loads(result.split("\n", 1)[1])


class Tools(unittest.TestCase):
    def setUp(self):
        self.calls = []

        async def record(path, body=None, params=None, **kw):
            # api.get(path, params=...) and api.post/patch(path, body)
            self.calls.append({"path": path, "body": body, "params": params or kw.get("params")})
            return {"ok": True}

        ident = SimpleNamespace(api_url="https://api.test", token="t", company_id=CID, bound=True, name="jenkins")
        self.patches = [
            mock.patch.object(mcp_server, "_ident", return_value=ident),
            mock.patch.object(mcp_server.api, "get", side_effect=record),
            mock.patch.object(mcp_server.api, "post", side_effect=record),
            mock.patch.object(mcp_server.api, "patch", side_effect=record),
            mock.patch.object(mcp_server.api, "put", side_effect=record),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def run_tool(self, coro):
        return parsed(asyncio.run(coro))

    def test_project_needs_a_space(self):
        with self.assertRaises(TypeError):
            asyncio.run(mcp_server.create_project("Launch"))  # space_id is required
        self.assertTrue(self.run_tool(mcp_server.create_project("Launch", "not-a-uuid")).get("error"))
        self.run_tool(mcp_server.create_project("Launch", SID))
        self.assertEqual(self.calls[-1]["body"], {"name": "Launch", "space_id": SID})

    def test_leads_are_agent_names(self):
        self.run_tool(mcp_server.create_space("Ops", lead_agent_id="jenkins"))
        self.assertEqual(self.calls[-1]["body"]["lead_agent_id"], "jenkins")
        self.run_tool(mcp_server.update_space(SID, lead_agent_id="searchpro"))
        self.assertEqual(self.calls[-1]["body"], {"lead_agent_id": "searchpro"})
        self.run_tool(mcp_server.update_project(PID, lead_agent_id="jenkins"))
        self.assertEqual(self.calls[-1]["body"], {"lead_agent_id": "jenkins"})
        self.assertTrue(self.run_tool(mcp_server.update_space(SID, lead_agent_id="two words")).get("error"))

    def test_space_and_department_create_company_spaces(self):
        self.run_tool(mcp_server.create_space("Research", description="d"))
        self.assertEqual((self.calls[-1]["path"], self.calls[-1]["body"]),
                         (f"/api/v1/companies/{CID}/spaces", {"name": "Research", "description": "d"}))
        self.run_tool(mcp_server.create_space("Ops", dept_function="operations"))
        self.assertEqual((self.calls[-1]["path"], self.calls[-1]["body"]),
                         (f"/api/v1/companies/{CID}/spaces", {"name": "Ops", "dept_function": "operations"}))
        self.assertFalse(hasattr(mcp_server, "create_department"))  # a team is a company space

    def test_workspace_board(self):
        self.run_tool(mcp_server.list_tasks(space_id=SID))
        params = self.calls[-1]["params"]
        self.assertEqual(params["space_id"], SID)
        self.assertNotIn("company_id", params)  # the env default would filter a plain space away
        self.run_tool(mcp_server.list_tasks(project_id=PID, company_id=CID))
        self.assertEqual((self.calls[-1]["params"]["project_id"], self.calls[-1]["params"]["company_id"]), (PID, CID))

    def test_default_list_still_uses_the_company(self):
        self.run_tool(mcp_server.list_tasks())
        self.assertEqual(self.calls[-1]["params"]["company_id"], CID)


    def test_personal_overrides_the_company_default(self):
        self.run_tool(mcp_server.create_task("Write it up", company_id="personal", assigned_to_agent="jenkins"))
        self.assertNotIn("company_id", self.calls[-1]["body"])
        self.run_tool(mcp_server.list_tasks(company_id="personal"))
        self.assertNotIn("company_id", self.calls[-1]["params"])
        self.run_tool(mcp_server.create_task("Company work", assigned_to_agent="jenkins"))
        self.assertEqual(self.calls[-1]["body"]["company_id"], CID)

    def test_personal_spaces_and_projects(self):
        self.run_tool(mcp_server.create_space("Home", company_id="personal", description="d"))
        self.assertEqual((self.calls[-1]["path"], self.calls[-1]["body"]),
                         ("/api/v1/platform/spaces", {"name": "Home", "description": "d"}))
        self.run_tool(mcp_server.create_project("Garden", SID, company_id="personal"))
        self.assertEqual((self.calls[-1]["path"], self.calls[-1]["body"]),
                         ("/api/v1/platform/projects", {"name": "Garden", "spaceId": SID}))
        self.run_tool(mcp_server.list_spaces(company_id="personal"))
        self.assertEqual(self.calls[-1]["path"], "/api/v1/platform/spaces")
        self.run_tool(mcp_server.get_project(PID, company_id="personal"))
        self.assertEqual(self.calls[-1]["path"], f"/api/v1/platform/projects/{PID}")
        # Team settings belong to company teams.
        self.assertTrue(self.run_tool(mcp_server.create_space("Home", company_id="personal", join_policy="invite")).get("error"))

    def test_personal_update_keeps_the_name(self):
        async def current(path, params=None, **kw):
            self.calls.append({"path": path, "body": None, "params": params})
            return {"id": SID, "name": "Home"}

        with mock.patch.object(mcp_server.api, "get", side_effect=current):
            self.run_tool(mcp_server.update_space(SID, company_id="personal", description="new"))
        self.assertEqual((self.calls[-1]["path"], self.calls[-1]["body"]),
                         (f"/api/v1/platform/spaces/{SID}", {"name": "Home", "description": "new"}))
        self.assertTrue(self.run_tool(mcp_server.update_space(SID, company_id="personal", lead_agent_id="jenkins")).get("error"))

    def test_team_settings(self):
        self.run_tool(mcp_server.create_space("Legal", join_policy="invite", agent_level="read"))
        self.assertEqual(self.calls[-1]["body"], {"name": "Legal", "join_policy": "invite", "agent_level": "read"})
        self.run_tool(mcp_server.update_space(SID, agent_level="chat"))
        self.assertEqual(self.calls[-1]["body"], {"agent_level": "chat"})
        self.assertTrue(self.run_tool(mcp_server.create_space("X", join_policy="secret")).get("error"))
        self.assertTrue(self.run_tool(mcp_server.update_space(SID, agent_level="admin")).get("error"))

    def test_kb_listing_uses_the_knowledge_routes(self):
        self.assertFalse(hasattr(mcp_server, "search_kb"))  # search_knowledge searches
        self.run_tool(mcp_server.list_kb_items(space_id=SID, kinds=["process"]))
        self.assertEqual((self.calls[-1]["path"], self.calls[-1]["params"]),
                         ("/api/v1/kb/items", {"limit": 200, "space_id": SID, "kinds": ["process"]}))


if __name__ == "__main__":
    unittest.main()
