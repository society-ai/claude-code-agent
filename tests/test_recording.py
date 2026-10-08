"""Contact conversations are not recorded to Society AI unless the owner turns
MIRROR_CONTACTS on, and the owner's sidebar name is worked out sensibly."""

import asyncio
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("SESSION_MODE", "false")

import bridge  # noqa: E402
from policy import apply_local_env, default_policy  # noqa: E402
from transcript_shipper import TranscriptShipper  # noqa: E402


class ContactRecording(unittest.TestCase):
    def test_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MIRROR_CONTACTS", None)
            pol = default_policy("a", "/w", [])
            apply_local_env(pol, "a")
        self.assertFalse(pol.mirror_contacts)

    def test_owner_can_turn_it_on(self):
        with mock.patch.dict(os.environ, {"MIRROR_CONTACTS": "true"}):
            pol = default_policy("a", "/w", [])
            apply_local_env(pol, "a")
        self.assertTrue(pol.mirror_contacts)

    def test_previously_registered_contact_sessions_are_forgotten(self):
        with tempfile.TemporaryDirectory() as d:
            shipper = TranscriptShipper("https://api.x", "sai_x", d)
            shipper.register("owner-s", cwd=d, work_item_kind="chat", work_item_id="chat-1")
            shipper.register("contact-s", cwd=d, work_item_kind="chat",
                             work_item_id="contact:agent:sai:cv_1")
            self.assertEqual(shipper.forget_contact_sessions(), 1)
            self.assertTrue(shipper.is_registered("owner-s"))
            self.assertFalse(shipper.is_registered("contact-s"))
            # and it stays forgotten across a restart
            again = TranscriptShipper("https://api.x", "sai_x", d)
            self.assertFalse(again.is_registered("contact-s"))


class FakeResp:
    def __init__(self, data, status=200):
        self.status_code, self._data = status, data

    def json(self):
        return self._data


class FakeClient:
    def __init__(self, resp):
        self.resp = resp

    async def get(self, url, headers=None):
        return self.resp


class OwnerLabel(unittest.TestCase):
    def label(self, resp, env=None):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, env or {}):
            if not env:
                os.environ.pop("OWNER_NAME", None)
            ctx = bridge.AgentContext(name="a", token="sai_x", work_dir=d, extra_dirs=[], company_id="",
                                      api_url="https://api.x", socket=f"{d}/b.sock", state_dir=d)
            b = bridge.Bridge(ctx)
            original = bridge._get_http_client
            bridge._get_http_client = lambda: FakeClient(resp)
            try:
                return asyncio.run(b._resolve_owner_label())
            finally:
                bridge._get_http_client = original

    def test_configured_name_wins(self):
        self.assertEqual(self.label(FakeResp({"name": "X"}), {"OWNER_NAME": "Lior"}), "Lior")

    def test_profile_first_name(self):
        self.assertEqual(self.label(FakeResp({"name": "Lior Davidovitch", "email": "l@x.com"})), "Lior")

    def test_email_first_part(self):
        self.assertEqual(self.label(FakeResp({"name": None, "email": "lior@publc.com"})), "Lior")

    def test_fallback(self):
        self.assertEqual(self.label(FakeResp({}, status=500)), "Owner")


if __name__ == "__main__":
    unittest.main()
