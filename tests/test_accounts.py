"""accounts.py: which agents on this machine belong to another account."""

import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import accounts  # noqa: E402

OWNERS = {"sai_jenkins": "u1", "sai_lead": "u1", "sai_searchpro": "u2"}
PERSONAS = [
    {"name": "jenkins", "token": "sai_jenkins", "api_url": "https://api.test"},
    {"name": "lead", "token": "sai_lead", "api_url": "https://api.test/"},
    {"name": "searchpro", "token": "sai_searchpro", "api_url": "https://api.test"},
    {"name": "local", "token": "sai_local", "api_url": "http://localhost:8000"},
]


class TestOtherAccountAgents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [
            mock.patch.object(accounts, "discover_personas", return_value=PERSONAS),
            mock.patch.object(accounts, "token_owner", side_effect=lambda t, _u: OWNERS.get(t)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def env(self, name, token):
        path = pathlib.Path(self.tmp.name) / f".env.{name}"
        path.write_text(f"SOCIETY_AI_AUTH_TOKEN={token}\nAGENT_NAME={name}\nAGENT_ROUTER_API_URL=https://api.test\n")
        return path

    def test_lists_the_other_accounts_agents_on_the_same_platform(self):
        self.assertEqual(accounts.other_account_agents(self.env("searchpro", "sai_searchpro")),
                         ["jenkins", "lead"])
        self.assertEqual(accounts.other_account_agents(self.env("lead", "sai_lead")), ["searchpro"])

    def test_says_nothing_when_it_cannot_tell(self):
        self.assertEqual(accounts.other_account_agents(self.env("new", "sai_unknown")), [])


if __name__ == "__main__":
    unittest.main()
