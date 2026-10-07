"""The session-start hook lists other agents' open work only in the machine
owner's own sessions, never in a session the bridge launched for one agent."""

import io
import json
import os
import pathlib
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import hook_session_start as hook  # noqa: E402

PERSONAS = [
    {"name": "jenkins", "display_name": "", "api_url": "https://api.x", "token": "t1"},
    {"name": "agent-nben2doc", "display_name": "Johnny", "api_url": "https://api.x", "token": "t2"},
]
TASKS = [{"identifier": "TASK-SECRET-1", "status": "backlog", "title": "Rotate the DB password"}]


def run_hook(dispatched: bool) -> str:
    env = {"SOCIETY_AI_DISPATCHED": "1"} if dispatched else {}
    acting = {"name": "agent-nben2doc", "api_url": "https://api.x", "bound": True}
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False), \
         mock.patch.object(hook, "discover_personas", return_value=PERSONAS), \
         mock.patch.object(hook, "resolve_mcp_identity", return_value=acting), \
         mock.patch.object(hook, "_fetch", side_effect=lambda url, tok, name: (TASKS, []) if name == "jenkins" else ([], [])), \
         mock.patch.object(sys, "stdin", io.StringIO(json.dumps({"cwd": "/tmp"}))), \
         redirect_stdout(out):
        if not dispatched:
            os.environ.pop("SOCIETY_AI_DISPATCHED", None)
        hook.main()
    return out.getvalue()


class OtherAgentsWork(unittest.TestCase):
    def test_owner_terminal_session_sees_other_agents_work(self):
        self.assertIn("TASK-SECRET-1", run_hook(dispatched=False))

    def test_bridge_session_never_sees_other_agents_work(self):
        self.assertNotIn("TASK-SECRET-1", run_hook(dispatched=True))


if __name__ == "__main__":
    unittest.main()
