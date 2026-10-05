"""Contact permissions: sender classification, levels, session keys and the
launch flags each level gets. Run: ./venv/bin/python -m unittest discover tests
"""

import base64
import json
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from contacts import (  # noqa: E402
    OWNER,
    Sender,
    clamp,
    classify_sender,
    contact_work_item_key,
    contacts_dir,
    owner_id_from_jwt,
    strip_private_blocks,
)
from session_manager import (  # noqa: E402
    CONTACT_STRIPPED_ENV,
    PersonaPolicy,
    SessionManager,
    SessionRecord,
)

OWNER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"


class ClassifySender(unittest.TestCase):
    def test_owner_needs_label_and_matching_user_id(self):
        frame = {"from": {"kind": "owner"}, "conversation_id": "c1"}
        self.assertEqual(classify_sender(frame, {"user_id": OWNER_ID}, OWNER_ID), OWNER)

    def test_owner_label_from_someone_else_is_a_contact_at_chat(self):
        frame = {"from": {"kind": "owner"}, "conversation_id": "c1"}
        s = classify_sender(frame, {"user_id": OTHER_ID}, OWNER_ID)
        self.assertFalse(s.is_owner)
        self.assertEqual(s.permission, "chat")

    def test_unknown_owner_id_trusts_nobody(self):
        frame = {"from": {"kind": "owner"}}
        self.assertFalse(classify_sender(frame, {"user_id": OWNER_ID}, None).is_owner)

    def test_agent_contact_carries_its_level(self):
        frame = {"from": {"kind": "agent", "name": "nova", "owner_name": "Dana", "permission": "read"}}
        s = classify_sender(frame, {"user_id": OTHER_ID}, OWNER_ID)
        self.assertEqual((s.is_owner, s.key, s.label, s.permission),
                         (False, "agent:nova", "nova (Dana)", "read"))

    def test_owners_own_agent_is_a_contact_on_the_new_router(self):
        frame = {"from": {"kind": "agent", "name": "jenkins", "permission": "act"}}
        s = classify_sender(frame, {"user_id": OWNER_ID}, OWNER_ID)
        self.assertFalse(s.is_owner)
        self.assertEqual(s.permission, "act")

    def test_unknown_level_falls_back_to_chat(self):
        frame = {"from": {"kind": "agent", "name": "x", "permission": "admin"}}
        self.assertEqual(classify_sender(frame, {"user_id": OTHER_ID}, OWNER_ID).permission, "chat")

    def test_new_router_missing_permission_is_chat_even_for_owner_user_id(self):
        frame = {"from": {"kind": "agent", "name": "x"}, "conversation_id": "c1"}
        s = classify_sender(frame, {"user_id": OWNER_ID}, OWNER_ID)
        self.assertFalse(s.is_owner)
        self.assertEqual(s.permission, "chat")

    def test_legacy_router_owner_user_id_is_owner(self):
        for kind in ("supervisor", "agent", "user"):
            frame = {"from": {"kind": kind, "name": "jenkins"}}
            self.assertTrue(classify_sender(frame, {"user_id": OWNER_ID}, OWNER_ID).is_owner, kind)

    def test_legacy_router_anyone_else_is_chat(self):
        frame = {"from": {"kind": "agent", "name": "stranger"}}
        s = classify_sender(frame, {"user_id": OTHER_ID}, OWNER_ID)
        self.assertEqual((s.is_owner, s.permission), (False, "chat"))

    def test_no_frame_at_all(self):
        self.assertTrue(classify_sender(None, {"user_id": OWNER_ID}, OWNER_ID).is_owner)
        self.assertFalse(classify_sender(None, {"user_id": OTHER_ID}, OWNER_ID).is_owner)

    def test_person_contact(self):
        frame = {"from": {"kind": "user", "id": OTHER_ID, "name": "Sam", "permission": "chat"}}
        s = classify_sender(frame, {"user_id": OTHER_ID}, OWNER_ID)
        self.assertEqual((s.key, s.label), (f"user:{OTHER_ID}", "Sam"))


class Levels(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(clamp("act", "read"), "read")
        self.assertEqual(clamp("chat", "act"), "chat")
        self.assertEqual(clamp(None, None), "chat")
        self.assertEqual(clamp("read", "bogus"), "read")


class SessionKeys(unittest.TestCase):
    sender = Sender(is_owner=False, key="agent:nova", label="nova", permission="chat")

    def test_sender_session_id_is_namespaced_not_ignored(self):
        key = contact_work_item_key(self.sender, {}, {}, {"sessionId": "owner-chat-123"}, "t1")
        self.assertEqual(key, "contact:agent:nova:owner-chat-123")

    def test_router_conversation_id_wins(self):
        key = contact_work_item_key(self.sender, {"conversation_id": "conv9"}, {},
                                    {"sessionId": "s"}, "t1")
        self.assertEqual(key, "contact:agent:nova:conv9")

    def test_task_work_keys_by_task(self):
        key = contact_work_item_key(self.sender, {"conversation_id": "c"},
                                    {"agent_task_id": "T-1"}, {}, "t1")
        self.assertEqual(key, "contact:agent:nova:task:T-1")


class PrivateContext(unittest.TestCase):
    def test_activity_and_scope_blocks_are_dropped(self):
        md = {"blocks": [{"kind": "identity", "text": "a"}, {"kind": "activity", "text": "b"},
                         {"kind": "scope", "text": "c"}, {"kind": "contact", "text": "d"}]}
        kinds = [b["kind"] for b in strip_private_blocks(md)["blocks"]]
        self.assertEqual(kinds, ["identity", "contact"])


class OwnerId(unittest.TestCase):
    def test_reads_sub(self):
        payload = base64.urlsafe_b64encode(json.dumps({"sub": OWNER_ID}).encode()).decode().rstrip("=")
        self.assertEqual(owner_id_from_jwt(f"h.{payload}.s"), OWNER_ID)

    def test_garbage(self):
        self.assertIsNone(owner_id_from_jwt("nope"))
        self.assertIsNone(owner_id_from_jwt(None))


class LaunchFlags(unittest.TestCase):
    def setUp(self):
        self.mgr = SessionManager("/tmp/hub.sock")
        self.pol = PersonaPolicy(name="johnny", work_dir="/work/main", extra_dirs=["/work/extra"],
                                 permission_mode="bypassPermissions",
                                 session_env={"SOCIETY_AI_AUTH_TOKEN": "sai_x", "AGENT_NAME": "johnny"})

    def rec(self, permission):
        return SessionRecord(work_item_key="k", persona="johnny", session_id="sid",
                             tmux_name="t", title="nova: hi", permission=permission)

    def test_chat(self):
        cwd, cmd, env, unset = self.mgr._contact_command(self.rec("chat"), self.pol, False)
        self.assertEqual(cwd, contacts_dir("johnny"))
        self.assertIn("--restricted", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("bypassPermissions", cmd)
        self.assertNotIn("--add-dir", cmd)
        self.assertNotIn("SOCIETY_AI_AUTH_TOKEN", env)
        self.assertEqual(set(unset), set(CONTACT_STRIPPED_ENV))
        hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]
        self.assertIn("--mirror-only", hooks["Stop"][0]["hooks"][0]["command"])
        self.assertNotIn("SessionStart", hooks)

    def test_read(self):
        _, cmd, _, unset = self.mgr._contact_command(self.rec("read"), self.pol, False)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual([cmd[i + 1] for i, c in enumerate(cmd) if c == "--add-dir"],
                         ["/work/main", "/work/extra"])
        self.assertTrue(unset)

    def test_act(self):
        cwd, cmd, env, unset = self.mgr._contact_command(self.rec("act"), self.pol, True)
        self.assertEqual(cwd, contacts_dir("johnny"))
        self.assertNotIn("--restricted", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(cmd[1:3], ["--resume", "sid"])
        self.assertEqual(env["SOCIETY_AI_AUTH_TOKEN"], "sai_x")
        self.assertEqual(unset, ())

    def test_remote_control_shows_contact_sessions(self):
        _, cmd, _, _ = self.mgr._contact_command(self.rec("chat"), self.pol, False)
        self.assertEqual(cmd[cmd.index("--remote-control") + 1], "nova: hi")

    def test_session_cwd(self):
        self.mgr.set_policy(self.pol)
        self.assertEqual(self.mgr.session_cwd(self.rec(None)), "/work/main")
        self.assertEqual(self.mgr.session_cwd(self.rec("read")), contacts_dir("johnny"))


class WorkspaceConfig(unittest.TestCase):
    def test_mcp_json_is_static_and_keyed_by_env(self):
        with tempfile.TemporaryDirectory() as d:
            SessionManager("/tmp/hub.sock")._write_workspace_config(d)
            env = json.loads((pathlib.Path(d) / ".mcp.json").read_text())["mcpServers"]["society-ai-channel"]["env"]
            self.assertEqual(env["SOCIETY_AI_SESSION_KEY"], "${SOCIETY_AI_SESSION_KEY}")
            self.assertEqual(env["SOCIETY_AI_CHANNEL_SOCK"], "${SOCIETY_AI_CHANNEL_SOCK}")


if __name__ == "__main__":
    unittest.main()
