"""save_artifact: the file goes from disk straight to storage.

agent_router /api/v1/artifacts/upload-url gives a link signed for the file's
type and size; the file is streamed there with those headers; then
/api/v1/artifacts/complete records it. No bytes pass through the platform's
servers, so files up to the real limits (10 MB, videos 100 MB) work.
"""

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

LINK = {
    "upload_url": "https://s3.test/artifacts/u/a1.mp4?sig",
    "artifact_id": "a1",
    "file_key": "artifacts/u/2026/10/21/a1.mp4",
    "headers": {"Content-Type": "video/mp4"},
}


def parsed(result: str) -> dict:
    """Tool results start with an "[acting as ...]" line, then the JSON."""
    return json.loads(result.split("\n", 1)[1])


class SaveArtifact(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "clip.mp4")
        self.data = os.urandom(3 * 1024 * 1024 + 7)  # several chunks
        pathlib.Path(self.path).write_bytes(self.data)
        self.puts: list[httpx.Request] = []
        self.storage_status = 200

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "s3.test":
                request.read()
                self.puts.append(request)
                return httpx.Response(self.storage_status)
            return httpx.Response(404)

        self.post = mock.AsyncMock(side_effect=self._post)
        ident = SimpleNamespace(api_url="https://api.test", token="sai_agent", company_id="", bound=True, name="tester")
        self.patches = [
            mock.patch.object(mcp_server, "_ident", return_value=ident),
            mock.patch.object(mcp_server.api, "client",
                              return_value=httpx.AsyncClient(transport=httpx.MockTransport(handler))),
            mock.patch.object(mcp_server.api, "post", self.post),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._tmp.cleanup()

    async def _post(self, path, body):
        if path == "/api/v1/artifacts/upload-url":
            return LINK
        if path == "/api/v1/artifacts/complete":
            return {"artifact_id": "a1", "size": len(self.data)}
        raise AssertionError(path)

    def test_streams_the_file_to_the_signed_link_then_completes(self):
        out = parsed(asyncio.run(mcp_server.save_artifact(self.path, "video/mp4", "clip.mp4", title="Demo")))
        self.assertEqual(out, {"artifact_id": "a1", "size": len(self.data)})

        (path1, asked), (path2, done) = [c.args for c in self.post.await_args_list]
        self.assertEqual(path1, "/api/v1/artifacts/upload-url")
        self.assertEqual(asked["size"], len(self.data))
        self.assertEqual(asked["mime_type"], "video/mp4")
        self.assertEqual(asked["source_agent"], "tester")
        self.assertNotIn("bytes_base64", asked)

        [put] = self.puts
        self.assertEqual(put.method, "PUT")
        self.assertEqual(put.headers["content-type"], "video/mp4")
        self.assertEqual(put.headers["content-length"], str(len(self.data)))
        self.assertEqual(put.content, self.data)

        self.assertEqual(path2, "/api/v1/artifacts/complete")
        self.assertEqual((done["artifact_id"], done["file_key"]), ("a1", LINK["file_key"]))
        self.assertEqual(done["title"], "Demo")

    def test_a_refused_link_is_reported_and_nothing_is_uploaded(self):
        self.post.side_effect = None
        self.post.return_value = {"error": "HTTP 422 from POST /api/v1/artifacts/upload-url",
                                  "body": "File is too large: 101.0 MB. The limit is 100.0 MB per file."}
        out = parsed(asyncio.run(mcp_server.save_artifact(self.path, "video/mp4", "clip.mp4")))
        self.assertIn("101.0 MB", out["body"])
        self.assertEqual(self.puts, [])
        self.assertEqual(self.post.await_count, 1)

    def test_a_storage_refusal_stops_before_completing(self):
        self.storage_status = 403
        out = parsed(asyncio.run(mcp_server.save_artifact(self.path, "video/mp4", "clip.mp4")))
        self.assertIn("Storage refused the upload of clip.mp4", out["message"])
        self.assertEqual([c.args[0] for c in self.post.await_args_list], ["/api/v1/artifacts/upload-url"])

    def test_an_empty_file_is_refused_locally(self):
        empty = os.path.join(self._tmp.name, "empty.txt")
        pathlib.Path(empty).write_bytes(b"")
        out = parsed(asyncio.run(mcp_server.save_artifact(empty, "text/plain", "empty.txt")))
        self.assertIn("is empty", out["message"])
        self.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
