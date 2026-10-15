"""Record paging, phase 2: tool output is UTF-8 on the wire, on the stdio
transport whatever the locale, and on the gateway. Non-ASCII used to be
escaped twice, so a CJK body cost seven bytes per character on the wire."""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from contextlib import ExitStack
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from khipu import activity
from khipu import gateway as gw
from khipu import mcp_server as srv
from tests.test_gateway import TOKEN, _free_port, _post
from tests.test_record_paging import _PgConn, _PgCursor

# 54,000 bytes of CJK and 8,000 of astral emoji: one default page, so the
# whole body is in one frame and the size bound is about escaping alone.
BODY = "漢" * 18_000 + "😀" * 2_000 + "末尾"
BODY_BYTES = len(BODY.encode("utf-8"))
WIRE_RATIO_MAX = 1.1


def _get_request(req_id: int = 7) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
            "params": {"name": "khipu_get", "arguments": {"id": "wide", "kind": "topic"}}}


def _body_from_frame(test: unittest.TestCase, frame: dict) -> str:
    result = frame["result"]
    test.assertFalse(result.get("isError"), result)
    page = json.loads(result["content"][0]["text"])
    test.assertTrue(page["complete"])
    test.assertEqual(page["body_total_bytes"], BODY_BYTES)
    return page["body"]


def _hub_with_wide_topic():
    return mock.patch.object(activity, "connect", return_value=_PgConn(_PgCursor({"wide": BODY})))


class EncodeFrameTest(unittest.TestCase):
    def test_non_ascii_is_literal_utf8_and_round_trips(self):
        message = {"text": "漢字 é 😀"}
        raw = srv.encode_frame(message)
        self.assertIn("漢字 é 😀".encode("utf-8"), raw)
        self.assertNotIn(b"\\u", raw)
        self.assertEqual(json.loads(raw.decode("utf-8")), message)

    def test_tool_text_keeps_non_ascii_literal(self):
        text = srv._tool_text({"body": "末尾😀"})["content"][0]["text"]
        self.assertIn("末尾😀", text)
        self.assertEqual(json.loads(text), {"body": "末尾😀"})

    def test_a_lone_surrogate_is_written_as_visible_text(self):
        raw = srv.encode_frame({"text": "a\udc80b"})
        raw.decode("utf-8")  # still valid UTF-8
        self.assertEqual(json.loads(raw), {"text": "a\\udc80b"})  # six visible chars
        self.assertNotIn(b"a\\udc80b", raw)  # no single-backslash JSON surrogate escape


class StdioWireTest(unittest.TestCase):
    def test_cjk_and_emoji_round_trip_through_an_ascii_locale_stdout(self):
        frames = (json.dumps(_get_request()) + "\n").encode("utf-8")
        stdin = io.TextIOWrapper(io.BytesIO(frames), encoding="utf-8")
        out_bytes = io.BytesIO()
        # What a C locale gives Python: a stdout that cannot encode CJK.
        stdout = io.TextIOWrapper(out_bytes, encoding="ascii")
        with ExitStack() as stack:
            stack.enter_context(_hub_with_wide_topic())
            stack.enter_context(mock.patch.object(srv, "_RELOAD_ON_CHANGE", False))
            stack.enter_context(mock.patch.object(srv, "_note_loaded_modules"))
            stack.enter_context(mock.patch.object(srv, "_drop_modules_if_code_changed"))
            stack.enter_context(mock.patch("sys.stdin", stdin))
            stack.enter_context(mock.patch("sys.stdout", stdout))
            self.assertEqual(srv.main(), 0)
        wire = out_bytes.getvalue()
        self.assertEqual(wire.count(b"\n"), 1)
        self.assertEqual(_body_from_frame(self, json.loads(wire.decode("utf-8"))), BODY)
        self.assertIn("末尾".encode("utf-8"), wire)
        self.assertLessEqual(len(wire), WIRE_RATIO_MAX * BODY_BYTES)

    def test_the_real_launcher_writes_utf8_when_the_locale_is_ascii(self):
        launcher = Path(__file__).resolve().parents[1] / "bin" / "khipu-mcp"
        name = "漢😀"
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": {}}}
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONIOENCODING="ascii", PYTHONUTF8="0")
        proc = subprocess.run([str(launcher)], input=(json.dumps(request) + "\n").encode("utf-8"),
                              capture_output=True, env=env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertIn(name.encode("utf-8"), proc.stdout)
        frame = json.loads(proc.stdout.decode("utf-8"))
        self.assertEqual(frame["error"]["message"], f"unknown tool: {name}")


class GatewayWireTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        gw.Handler.token = TOKEN
        gw.Handler.rate = gw._Rate(1000)
        gw.Handler.auth_fail_rate = gw._Rate(1000)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), gw.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.port}/mcp"
        cls._liveness_tmp = tempfile.mkdtemp(prefix="khipu-gw-liveness-")
        cls._liveness_patch = mock.patch.object(
            gw, "_liveness_path", return_value=Path(cls._liveness_tmp) / "gateway-liveness.json")
        cls._liveness_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._liveness_patch.stop()

    def test_cjk_and_emoji_round_trip_through_a_gateway_request(self):
        with _hub_with_wide_topic():
            req = urllib.request.Request(
                self.url, data=json.dumps(_get_request()).encode(), method="POST",
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
            with urllib.request.urlopen(req, timeout=10) as r:
                status, wire = r.status, r.read()
                length = int(r.headers["Content-Length"])
        self.assertEqual(status, 200)
        self.assertEqual(length, len(wire))
        self.assertEqual(_body_from_frame(self, json.loads(wire.decode("utf-8"))), BODY)
        self.assertIn("末尾".encode("utf-8"), wire)
        self.assertLessEqual(len(wire), WIRE_RATIO_MAX * BODY_BYTES)

    def test_a_batch_response_is_utf8_too(self):
        with _hub_with_wide_topic():
            status, body = _post(self.url, [_get_request(1), _get_request(2)])
        self.assertEqual(status, 200)
        self.assertEqual([_body_from_frame(self, f) for f in body], [BODY, BODY])


if __name__ == "__main__":
    unittest.main()
