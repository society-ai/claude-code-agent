"""Files that arrive with a message: parsing, safe names, downloads within the
limits, and the [Attachments] prompt block."""

import asyncio
import os
import pathlib
import stat
import sys
import tempfile
import unittest

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import attachments  # noqa: E402
from attachments import Attachment, fetch_all, parse_file_parts, prompt_block, safe_filename  # noqa: E402

AID = "0a99eb9c-8284-45b9-8a4a-27fe159bd509"
AID2 = "1b2c3d4e-0000-4000-8000-000000000002"


class Parse(unittest.TestCase):
    def test_v01_wire_shape(self):
        parts = [{"type": "text", "text": "hi"},
                 {"type": "file", "file": {"name": "photo.png", "mimeType": "image/png", "uri": "x"},
                  "metadata": {"artifact_id": AID}}]
        self.assertEqual(parse_file_parts(parts), [Attachment(AID, "photo.png", "image/png")])

    def test_v1_kind_and_bare_file(self):
        parts = [{"kind": "file", "file": {"name": "a.pdf", "mime_type": "application/pdf"},
                  "metadata": {"artifact_id": AID}},
                 {"file": {"name": "b.csv", "metadata": {"artifact_id": AID2}}}]
        got = parse_file_parts(parts)
        self.assertEqual([a.artifact_id for a in got], [AID, AID2])
        self.assertEqual(got[0].mime_type, "application/pdf")

    def test_parts_without_an_artifact_id_are_ignored(self):
        self.assertEqual(parse_file_parts([{"type": "file", "file": {"name": "x", "uri": "https://e"}}]), [])

    def test_duplicates_are_dropped(self):
        part = {"type": "file", "file": {"name": "a"}, "metadata": {"artifact_id": AID}}
        self.assertEqual(len(parse_file_parts([part, part])), 1)


class Names(unittest.TestCase):
    def test_no_path_traversal(self):
        self.assertEqual(safe_filename(AID, "../../etc/passwd"), "0a99eb9c-passwd")
        self.assertEqual(safe_filename(AID, "..\\..\\evil.exe"), "0a99eb9c-evil.exe")

    def test_no_leading_dot_or_control_chars(self):
        self.assertEqual(safe_filename(AID, ".bashrc"), "0a99eb9c-bashrc")
        self.assertEqual(safe_filename(AID, "a\x00b\nc.txt"), "0a99eb9c-a-b-c.txt")

    def test_long_names_keep_their_extension(self):
        name = safe_filename(AID, "x" * 300 + ".png")
        self.assertTrue(name.endswith(".png"))
        self.assertLessEqual(len(name), 9 + 120)

    def test_empty_name(self):
        self.assertEqual(safe_filename(AID, ""), "0a99eb9c-file")


def platform(files: dict, fail_first: set | None = None):
    """A fake platform: /api/v1/files/{id} answers from `files`, and the
    download link serves the bytes. `fail_first` ids fail once with a 500."""
    failed = set()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/v1/files/"):
            aid = path.rsplit("/", 1)[-1]
            if aid in (fail_first or set()) and aid not in failed:
                failed.add(aid)
                return httpx.Response(500)
            if aid not in files:
                return httpx.Response(404)
            body, mime = files[aid]
            return httpx.Response(200, json={"artifact_id": aid, "name": "n", "mime_type": mime,
                                             "size": len(body), "url": f"https://s3.test/{aid}"})
        if request.url.host == "s3.test":
            return httpx.Response(200, content=files[path.strip("/")][0])
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class Fetch(unittest.TestCase):
    def run_fetch(self, client, atts):
        with tempfile.TemporaryDirectory() as d:
            dest = pathlib.Path(d) / "files"
            results = asyncio.run(fetch_all(client, "https://api.test", "sai_x", atts, dest))
            snapshot = [(r.path.name if r.path else None, r.path.read_bytes() if r.path else None,
                         stat.S_IMODE(os.stat(r.path).st_mode) if r.path else None, r.error)
                        for r in results]
            leftovers = sorted(p.name for p in dest.iterdir()) if dest.exists() else []
            return results, snapshot, leftovers

    def test_downloads_into_files_never_executable(self):
        client = platform({AID: (b"PNGDATA", "image/png")})
        _, snap, left = self.run_fetch(client, [Attachment(AID, "photo.png", "image/png")])
        self.assertEqual(snap, [("0a99eb9c-photo.png", b"PNGDATA", 0o644, "")])
        self.assertEqual(left, ["0a99eb9c-photo.png"])

    def test_no_access_is_reported_not_retried(self):
        results, snap, left = self.run_fetch(platform({}), [Attachment(AID, "secret.pdf", "application/pdf")])
        self.assertEqual(snap[0][3], "not available to this agent")
        self.assertEqual(left, [])

    def test_one_retry_on_a_transient_failure(self):
        client = platform({AID: (b"ok", "text/plain")}, fail_first={AID})
        _, snap, _ = self.run_fetch(client, [Attachment(AID, "a.txt", "text/plain")])
        self.assertEqual(snap[0][1], b"ok")

    def test_over_the_file_limit_is_not_downloaded(self):
        original = attachments.MAX_FILE_BYTES
        attachments.MAX_FILE_BYTES = 4
        try:
            _, snap, left = self.run_fetch(platform({AID: (b"12345", "text/plain")}),
                                           [Attachment(AID, "big.txt", "text/plain")])
        finally:
            attachments.MAX_FILE_BYTES = original
        self.assertEqual(snap[0][3], "too large to download automatically")
        self.assertEqual(left, [])

    def test_message_budget_spans_files(self):
        original = attachments.MAX_MESSAGE_BYTES
        attachments.MAX_MESSAGE_BYTES = 6
        try:
            _, snap, _ = self.run_fetch(platform({AID: (b"1234", "text/plain"), AID2: (b"5678", "text/plain")}),
                                        [Attachment(AID, "a.txt", "text/plain"), Attachment(AID2, "b.txt", "text/plain")])
        finally:
            attachments.MAX_MESSAGE_BYTES = original
        self.assertEqual(snap[0][1], b"1234")
        self.assertEqual(snap[1][3], "too large to download automatically")


class Prompt(unittest.TestCase):
    def test_lists_every_file(self):
        ok = attachments.Fetched(Attachment(AID, "photo.png", "image/png"), path=pathlib.Path("/w/files/p.png"), size=2048)
        bad = attachments.Fetched(Attachment(AID2, "r.pdf", "application/pdf"), error="could not be fetched (X)")
        owner = prompt_block([ok, bad], can_get_file=True)
        self.assertIn("/w/files/p.png", owner)
        self.assertIn("open it with the Read tool", owner)
        self.assertIn(f'get_file("{AID2}")', owner)
        contact = prompt_block([ok, bad], can_get_file=False)
        self.assertNotIn("get_file", contact)

    def test_big_image_is_noted(self):
        big = attachments.Fetched(Attachment(AID, "huge.png", "image/png"), path=pathlib.Path("/f"), size=6 * 1024 * 1024)
        self.assertIn("over Claude's 5 MB image limit", prompt_block([big], True))

    def test_no_attachments_no_block(self):
        self.assertEqual(prompt_block([], True), "")


if __name__ == "__main__":
    unittest.main()
