"""deploy_agent sends the router's schema: hosting (not platform), and an
access role only when the caller chose one, so ceo/coo default to admin."""

import asyncio
import json
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("SESSION_MODE", "false")

import mcp_server  # noqa: E402

COMPANY = "14ddcd04-6920-447d-8c25-26fb9446dac6"


def _deploy(**kw):
    sent = {}

    async def fake_post(path, body):
        sent["path"], sent["body"] = path, body
        return {"ok": True}

    with mock.patch.object(mcp_server, "ENABLE_AGENT_LIFECYCLE", True), \
         mock.patch.object(mcp_server.api, "post", side_effect=fake_post):
        out = asyncio.run(mcp_server.deploy_agent(role_position="ceo", company_id=COMPANY, **kw))
    return sent, json.loads(out.split("\n", 1)[1])  # drop the "[acting as ...]" label line


class DeployAgentPayload(unittest.TestCase):
    def test_sends_hosting_not_platform(self):
        sent, _ = _deploy(hosting="gce")
        self.assertEqual(sent["path"], f"/api/v1/companies/{COMPANY}/agents")
        self.assertEqual(sent["body"]["hosting"], "gce")
        self.assertNotIn("platform", sent["body"])

    def test_access_role_only_when_chosen(self):
        sent, _ = _deploy()
        self.assertNotIn("access_role", sent["body"]["org_chart"])
        sent, _ = _deploy(access_role="viewer")
        self.assertEqual(sent["body"]["org_chart"]["access_role"], "viewer")

    def test_rejects_unknown_hosting_and_visibility(self):
        for kw in ({"hosting": "cloudflare"}, {"visibility": "shared"}):
            sent, out = _deploy(**kw)
            self.assertTrue(out.get("error"), kw)
            self.assertNotIn("body", sent)

    def test_model_and_keys_pass_through(self):
        sent, _ = _deploy(model="anthropic/claude-sonnet-4-6", api_keys={"anthropic": "k"})
        self.assertEqual(sent["body"]["model"], "anthropic/claude-sonnet-4-6")
        self.assertEqual(sent["body"]["api_keys"], {"anthropic": "k"})


if __name__ == "__main__":
    unittest.main()
