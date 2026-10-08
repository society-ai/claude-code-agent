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
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from contacts import (  # noqa: E402
    OWNER,
    Sender,
    clamp,
    classify_sender,
    contact_work_item_key,
    owner_id_from_jwt,
    strip_private_blocks,
)
import folders  # noqa: E402
import session_manager  # noqa: E402
from session_manager import (  # noqa: E402
    CONTACT_STRIPPED_ENV,
    PersonaPolicy,
    SessionManager,
    SessionRecord,
)

OWNER_ID = "11111111-1111-1111-1111-111111111111"
OTHER_ID = "22222222-2222-2222-2222-222222222222"


class TempHome(unittest.TestCase):
    """Points the Society AI folder (SOCIETY_AI_HOME) and the session
    registry at a temp dir, so tests never touch the real home folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.home = self.tmp / "Society AI"
        self._env = mock.patch.dict(os.environ, {"SOCIETY_AI_HOME": str(self.home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def manager(self):
        return SessionManager(str(self.tmp / "state" / "channels.sock"))


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

    def test_missing_permission_is_chat_even_for_owner_user_id(self):
        frame = {"from": {"kind": "agent", "name": "x"}, "conversation_id": "c1"}
        s = classify_sender(frame, {"user_id": OWNER_ID}, OWNER_ID)
        self.assertFalse(s.is_owner)
        self.assertEqual(s.permission, "chat")

    def test_owner_user_id_alone_is_not_the_owner(self):
        for kind in ("supervisor", "agent", "user", ""):
            frame = {"from": {"kind": kind, "name": "jenkins"}}
            s = classify_sender(frame, {"user_id": OWNER_ID}, OWNER_ID)
            self.assertFalse(s.is_owner, kind)
            self.assertEqual(s.permission, "chat", kind)

    def test_no_frame_is_a_contact(self):
        self.assertFalse(classify_sender(None, {"user_id": OWNER_ID}, OWNER_ID).is_owner)
        self.assertFalse(classify_sender(None, {"user_id": OTHER_ID}, OWNER_ID).is_owner)

    def test_sender_group_names(self):
        sai = classify_sender({"from": {"kind": "agent", "name": "sai", "permission": "chat"}},
                              {"user_id": OTHER_ID}, OWNER_ID)
        self.assertEqual(sai.group, "Sai")
        mixed = classify_sender({"from": {"kind": "agent", "name": "SearchPro", "permission": "chat"}},
                                {"user_id": OTHER_ID}, OWNER_ID)
        self.assertEqual(mixed.group, "SearchPro")
        person = classify_sender({"from": {"kind": "user", "id": OTHER_ID, "name": "Dana K.", "permission": "chat"}},
                                 {"user_id": OTHER_ID}, OWNER_ID)
        self.assertEqual(person.group, "Dana K.")

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


class LaunchFlags(TempHome):
    def setUp(self):
        super().setUp()
        self.mgr = self.manager()
        self.pol = PersonaPolicy(name="agent-x1", display_name="Johnny", work_dir="/work/main",
                                 extra_dirs=["/work/extra"], permission_mode="bypassPermissions",
                                 owner_label="Lior",
                                 session_env={"SOCIETY_AI_AUTH_TOKEN": "sai_x", "AGENT_NAME": "agent-x1"})
        self.mgr.set_policy(self.pol)
        self.owner_dir = str(self.home / ".sessions" / "Lior" / "Johnny")
        self.contacts = str(self.home / ".sessions" / "Sai" / "Johnny")

    def rec(self, permission):
        return SessionRecord(work_item_key="k", persona="agent-x1", session_id="sid",
                             tmux_name="t", title="nova: hi", permission=permission,
                             sender_group="" if permission is None else "Sai")

    def add_dirs(self, cmd):
        return [cmd[i + 1] for i, c in enumerate(cmd) if c == "--add-dir"]

    def test_owner(self):
        cwd, cmd, env, unset = self.mgr._owner_command(self.rec(None), self.pol, False)
        self.assertEqual(cwd, self.owner_dir)
        self.assertIn("- `/work/main`", (pathlib.Path(cwd) / "CLAUDE.md").read_text())
        self.assertEqual(self.add_dirs(cmd), ["/work/main", "/work/extra"])
        self.assertEqual(env["CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"], "1")
        self.assertEqual(env["SOCIETY_AI_AUTH_TOKEN"], "sai_x")
        self.assertEqual(unset, ())

    def test_chat(self):
        cwd, cmd, env, unset = self.mgr._contact_command(self.rec("chat"), self.pol, False)
        self.assertEqual(cwd, self.contacts)
        self.assertIn("--restricted", cmd)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
        self.assertNotIn("bypassPermissions", cmd)
        self.assertEqual(self.add_dirs(cmd), [])
        self.assertNotIn("SOCIETY_AI_AUTH_TOKEN", env)
        self.assertEqual(set(unset), set(CONTACT_STRIPPED_ENV))
        hooks = json.loads(cmd[cmd.index("--settings") + 1])["hooks"]
        self.assertIn("--mirror-only", hooks["Stop"][0]["hooks"][0]["command"])
        self.assertNotIn("SessionStart", hooks)

    def test_read(self):
        cwd, cmd, _, unset = self.mgr._contact_command(self.rec("read"), self.pol, False)
        self.assertEqual(cwd, self.contacts)
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read,Grep,Glob")
        self.assertEqual(self.add_dirs(cmd), ["/work/main", "/work/extra"])
        self.assertTrue(unset)

    def test_act(self):
        cwd, cmd, env, unset = self.mgr._contact_command(self.rec("act"), self.pol, True)
        self.assertEqual(cwd, self.contacts)
        self.assertNotIn("--restricted", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertEqual(cmd[1:3], ["--resume", "sid"])
        self.assertEqual(env["SOCIETY_AI_AUTH_TOKEN"], "sai_x")
        self.assertEqual(unset, ())

    def test_remote_control_shows_contact_sessions(self):
        _, cmd, _, _ = self.mgr._contact_command(self.rec("chat"), self.pol, False)
        self.assertEqual(cmd[cmd.index("--remote-control") + 1], "nova: hi")

    def test_session_cwd(self):
        self.assertEqual(self.mgr.session_cwd(self.rec(None)), self.owner_dir)
        self.assertEqual(self.mgr.session_cwd(self.rec("read")), self.contacts)
        moved = self.rec(None)
        moved.cwd = "/old/work/dir"
        self.assertEqual(self.mgr.session_cwd(moved), "/old/work/dir")


class Folders(TempHome):
    def git_origin(self, path):
        import subprocess
        return subprocess.run(["git", "remote", "get-url", "origin"], cwd=path,
                              capture_output=True, text=True).stdout.strip()

    def test_agent_workspace_is_a_plain_folder_named_after_the_agent(self):
        path = folders.agent_workspace("agent-x1", "Johnny")
        self.assertEqual(path, self.home / "Johnny")
        self.assertEqual((path / ".society-ai-agent").read_text().strip(), "agent-x1")
        self.assertFalse((path / ".git").exists())

    def test_canonical_name_without_display_name(self):
        self.assertEqual(folders.agent_workspace("jenkins").name, "jenkins")

    def test_same_display_name_never_shares_a_workspace(self):
        first = folders.agent_workspace("a1", "Johnny")
        second = folders.agent_workspace("a2", "Johnny")
        self.assertEqual(second.name, "Johnny (a2)")
        self.assertNotEqual(first, second)

    def test_session_dir_groups_by_sender(self):
        path = folders.session_dir("Sai", "agent-x1", "Johnny")
        self.assertEqual(path, self.home / ".sessions" / "Sai" / "Johnny")
        group = self.home / ".sessions" / "Sai"
        self.assertEqual(self.git_origin(group), "https://societyai.com/agents/Society-AI-Sai.git")
        self.assertEqual((group / ".gitignore").read_text().splitlines()[-1], "*")

    def test_one_group_per_sender_across_agents(self):
        folders.session_dir("Lior", "a1", "Johnny")
        folders.session_dir("Lior", "a2", "searchpro")
        self.assertEqual(sorted(p.name for p in (self.home / ".sessions" / "Lior").iterdir()
                                if not p.name.startswith(".")), ["Johnny", "searchpro"])

    def test_remote_the_bridge_set_earlier_is_renamed(self):
        import subprocess
        group = self.home / ".sessions" / "Sai"
        group.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=group, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://societyai.com/agents/Sai.git"],
                       cwd=group, check=True)
        folders.session_dir("Sai", "agent-x1")
        self.assertEqual(self.git_origin(group), "https://societyai.com/agents/Society-AI-Sai.git")

    def test_existing_origin_is_left_alone(self):
        import subprocess
        group = self.home / ".sessions" / "Mine"
        group.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=group, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://example.com/mine.git"], cwd=group, check=True)
        folders.session_dir("Mine", "agent-x1")
        self.assertEqual(self.git_origin(group), "https://example.com/mine.git")

    def test_home_is_configurable(self):
        self.assertEqual(folders.society_ai_home(), self.home)


class RememberedSessions(TempHome):
    def test_registry_survives_a_restart(self):
        mgr = self.manager()
        rec = SessionRecord(work_item_key="contact:agent:nova:c1", persona="agent-x1", session_id="sid-1",
                            tmux_name="t1", title="nova: hi", has_run_once=True, state="ready",
                            permission="read", contact_label="nova", cwd=str(self.tmp),
                            sender_group="Nova")
        mgr._sessions[rec.work_item_key] = rec
        mgr.alias("chat-123", rec.work_item_key)  # saves
        again = self.manager()
        got = again.get("chat-123")
        self.assertIsNotNone(got)
        self.assertEqual((got.session_id, got.permission, got.contact_label, got.cwd,
                          got.has_run_once, got.sender_group),
                         ("sid-1", "read", "nova", str(self.tmp), True, "Nova"))
        self.assertEqual(got.state, "closed")

    def test_session_whose_folder_is_gone_is_dropped(self):
        mgr = self.manager()
        mgr._sessions["k"] = SessionRecord(work_item_key="k", persona="p", session_id="s", tmux_name="t",
                                           title="x", has_run_once=True, cwd=str(self.tmp / "gone"))
        mgr._save_registry()
        self.assertIsNone(self.manager().get("k"))

    def test_never_launched_sessions_are_not_saved(self):
        mgr = self.manager()
        mgr._sessions["k"] = SessionRecord(work_item_key="k", persona="p", session_id="s",
                                           tmux_name="t", title="x")
        mgr._save_registry()
        self.assertIsNone(self.manager().get("k"))


class WorkspaceConfig(TempHome):
    def test_mcp_json_is_static_and_keyed_by_env(self):
        with tempfile.TemporaryDirectory() as d:
            self.manager()._write_workspace_config(d)
            env = json.loads((pathlib.Path(d) / ".mcp.json").read_text())["mcpServers"]["society-ai-channel"]["env"]
            self.assertEqual(env["SOCIETY_AI_SESSION_KEY"], "${SOCIETY_AI_SESSION_KEY}")
            self.assertEqual(env["SOCIETY_AI_CHANNEL_SOCK"], "${SOCIETY_AI_CHANNEL_SOCK}")


if __name__ == "__main__":
    unittest.main()
