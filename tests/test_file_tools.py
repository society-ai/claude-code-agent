"""get_file and list_files: the agent opens platform files with its own token."""

import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import mcp_server  # noqa: E402

AID = "0a99eb9c-8284-45b9-8a4a-27fe159bd509"


def parsed(result: str) -> dict:
    """Tool results start with an "[acting as ...]" line, then the JSON."""
    return json.loads(result.split("\n", 1)[1])


def platform():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/v1/files/{AID}":
            assert request.headers["authorization"] == "Bearer sai_agent"
            return httpx.Response(200, json={"artifact_id": AID, "name": "plan.md", "mime_type": "text/markdown",
                                             "size": 5, "url": "https://s3.test/x", "url_expires_at": "t"})
        if request.url.host == "s3.test":
            return httpx.Response(200, content=b"# hi\n")
        return httpx.Response(404)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Tools(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        ident = SimpleNamespace(api_url="https://api.test", token="sai_agent", company_id="", bound=True, name="tester")
        self.patches = [mock.patch.object(mcp_server, "_ident", return_value=ident),
                        mock.patch.object(mcp_server.api, "client", return_value=platform()),
                        mock.patch.dict(os.environ, {"SOCIETY_AI_WORK_DIR": self._tmp.name})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    def test_get_file_saves_into_the_work_folder(self):
        out = parsed(asyncio.run(mcp_server.get_file(AID)))
        self.assertEqual(out["path"], os.path.join(self._tmp.name, "files", "0a99eb9c-plan.md"))
        self.assertEqual(pathlib.Path(out["path"]).read_bytes(), b"# hi\n")

    def test_get_file_without_access(self):
        out = parsed(asyncio.run(mcp_server.get_file("1b2c3d4e-0000-4000-8000-000000000002")))
        self.assertTrue(out.get("error"))

    def test_list_files_needs_exactly_one_scope(self):
        out = parsed(asyncio.run(mcp_server.list_files()))
        self.assertTrue(out.get("error"))
        both = parsed(asyncio.run(mcp_server.list_files(company_id=AID, space_id=AID)))
        self.assertTrue(both.get("error"))

    def test_list_files_passes_the_scope(self):
        seen = {}

        async def fake_get(path, params=None, **kw):
            seen.update(path=path, params=params)
            return {"files": [], "next_cursor": None}

        with mock.patch.object(mcp_server.api, "get", side_effect=fake_get):
            asyncio.run(mcp_server.list_files(space_id=AID, limit=500))
        self.assertEqual(seen, {"path": "/api/v1/files", "params": {"space_id": AID, "limit": 200}})


if __name__ == "__main__":
    unittest.main()
