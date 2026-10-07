"""The bridge drops a connection the hub never accepts, so it reconnects
instead of sitting connected but unreachable."""

import asyncio
import os
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
os.environ.setdefault("SESSION_MODE", "false")

import bridge  # noqa: E402


class FakeWS:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class RegistrationWatchdog(unittest.TestCase):
    def make_bridge(self, state_dir):
        ctx = bridge.AgentContext(
            name="t", token="sai_x", work_dir=state_dir, extra_dirs=[], company_id="",
            api_url="http://127.0.0.1:9", socket=f"{state_dir}/b.sock", state_dir=state_dir,
        )
        return bridge.Bridge(ctx)

    def run_watchdog(self, registered):
        with tempfile.TemporaryDirectory() as d:
            b = self.make_bridge(d)
            ws = FakeWS()
            b.ws = ws
            b._conn_registered = registered
            original = bridge.REGISTRATION_TIMEOUT_S
            bridge.REGISTRATION_TIMEOUT_S = 0.01
            try:
                asyncio.run(b._registration_watchdog(ws))
            finally:
                bridge.REGISTRATION_TIMEOUT_S = original
            return ws.closed

    def test_unanswered_registration_closes_the_connection(self):
        self.assertTrue(self.run_watchdog(registered=False))

    def test_accepted_registration_keeps_it(self):
        self.assertFalse(self.run_watchdog(registered=True))


if __name__ == "__main__":
    unittest.main()
