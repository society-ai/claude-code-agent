"""Workspace tools match the router: projects need a space, leads are agent
names, one way to create a company space, and task boards by workspace."""

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
        self.run_tool(mcp_server.create_department("Ops", lead_agent_id="jenkins"))
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
        self.run_tool(mcp_server.create_department("Ops"))
        self.assertEqual(self.calls[-1]["path"], f"/api/v1/companies/{CID}/spaces")

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


if __name__ == "__main__":
    unittest.main()
