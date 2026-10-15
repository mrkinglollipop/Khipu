"""Every frame Khipu writes (stdio MCP, the gateway, hook stdout, the CLI JSON
the desktop parses) is strict JSON any client parses: Foundation and
serde_json reject a lone surrogate escape at any nesting level, tool text
included, and a reader splitting on Unicode line breaks cuts a frame at a
literal U+2028 or U+2029. Every Khipu reader of these frames decodes UTF-8
explicitly and splits on LF only."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import ExitStack, redirect_stdout
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from khipu import gateway as gw
from khipu import integrations
from khipu import mcp_server as srv
from tests.test_gateway import TOKEN, _free_port

CLI_ROOT = Path(__file__).resolve().parents[1]
# Lone surrogates of both halves, a reversed pair, the two Unicode line
# separators, NUL, astral emoji and CJK.
AWKWARD = "a\udc80b\ud800c\udfff\ud800d e f\x00g😀h漢字i"
_SURROGATE = re.compile("[\ud800-\udfff]")
# A surrogate written as a JSON escape: \uD800-\uDFFF after an even run of
# backslashes. Strict output never escapes a LONE one; a well-formed escaped
# pair (ensure_ascii=True astral text such as an emoji) is legitimate JSON.
_SURROGATE_ESCAPE = re.compile(r"(?<!\\)(?:\\\\)*\\u[dD][89a-fA-F][0-9a-fA-F]{2}")
_ESCAPED_PAIR = re.compile(
    r"(?<!\\)((?:\\\\)*)\\u[dD][89abAB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}")


def _wire():
    from khipu import wire
    return wire


def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from _walk_strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_strings(v)


def assert_strict_json_text(test: unittest.TestCase, text: str, *, depth: int = 0):
    """``text`` is strict JSON at this level and, for any string in it that
    is itself JSON (tool text), at every level below."""
    test.assertNotRegex(_ESCAPED_PAIR.sub(r"\1", text), _SURROGATE_ESCAPE, "a lone surrogate escape")
    test.assertIsNone(_SURROGATE.search(text), "a raw surrogate code unit")
    test.assertNotIn(" ", text)
    test.assertNotIn(" ", text)
    value = json.loads(text)
    for s in _walk_strings(value):
        test.assertIsNone(_SURROGATE.search(s), f"surrogate in decoded string {s!r} at depth {depth}")
        if depth < 3 and s[:1] in "{[":
            try:
                json.loads(s)
            except ValueError:
                continue
            assert_strict_json_text(test, s, depth=depth + 1)
    return value


def assert_strict_frame(test: unittest.TestCase, raw: bytes):
    """One newline-free frame of strict UTF-8 JSON, one line to every reader."""
    text = raw.decode("utf-8")
    test.assertNotIn("\n", text)
    test.assertEqual(len(text.splitlines()), 1)
    return assert_strict_json_text(test, text)


def _awkward_result(req_id=7) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": srv._tool_text({"body": AWKWARD, AWKWARD: [AWKWARD]}, is_error=False)}


def _fake_launcher(directory: str, frames: bytes) -> str:
    out = Path(directory) / "frames.bin"
    out.write_bytes(frames)
    script = Path(directory) / "launcher"
    script.write_text(f"#!/bin/sh\ncat >/dev/null\ncat '{out}'\n", encoding="utf-8")
    script.chmod(0o755)
    return str(script)


def _status_frames(summary: str, *, literal: bool) -> bytes:
    """Two frames as a launcher prints them: initialize and a khipu_status
    result. ``literal`` writes non-ASCII and line separators raw, as any
    UTF-8 writer may."""
    status = {"counts": {"episodes": 3}, "recent_captures": [{"summary": summary}]}
    frames = [
        {"jsonrpc": "2.0", "id": 1, "result": {"instructions": "Khipu memory \u2014 \u8a18\u61b6"}},
        {"jsonrpc": "2.0", "id": 2,
         "result": {"content": [{"type": "text", "text": json.dumps(status, ensure_ascii=not literal)}],
                    "isError": False}},
    ]
    return b"".join(json.dumps(f, ensure_ascii=not literal).encode("utf-8") + b"\n" for f in frames)


def _ascii_locale_env() -> dict:
    env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    env.pop("PYTHONIOENCODING", None)
    env["PYTHONPATH"] = os.pathsep.join([str(CLI_ROOT), env.get("PYTHONPATH", "")])
    return env


class FindingOneNestedSurrogateTest(unittest.TestCase):
    """Codex finding 1: a lone surrogate in tool text survived as a raw
    surrogate inside content[].text once the outer frame was decoded."""

    def test_lone_surrogate_in_tool_text_is_strict_at_every_level(self):
        raw = srv.encode_frame(_awkward_result())
        frame = assert_strict_frame(self, raw)
        body = json.loads(frame["result"]["content"][0]["text"])["body"]
        self.assertEqual(body, "a\\udc80b\\ud800c\\udfff\\ud800d e f\x00g😀h漢字i")

    def test_a_tool_error_carrying_a_surrogate_is_strict_through_dispatch(self):
        # Adapt: patch whichever handler khipu_get dispatches to so it raises
        # (or returns an error payload) whose message holds "\udc80".
        request = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "khipu_get", "arguments": {"id": "x"}}}
        with mock.patch.dict(srv.TOOL_FUNCS, {"khipu_get": mock.Mock(side_effect=RuntimeError("bad \udc80 row"))}):
            response = srv.handle_message(request)
        frame = assert_strict_frame(self, srv.encode_frame(response))
        self.assertIn("\\udc80", json.dumps(frame, ensure_ascii=False))


class FindingTwoAsciiLocaleProbeTest(unittest.TestCase):
    """Codex finding 2: the stdio verifier decoded with the locale, so an
    ASCII locale failed on the UTF-8 in the initialize instructions."""

    def test_probe_mcp_reads_utf8_under_an_ascii_locale(self):
        with tempfile.TemporaryDirectory(prefix="khipu-probe-") as td:
            launcher = _fake_launcher(td, _status_frames("plain", literal=True))
            code = ("import json, sys\nfrom khipu.integrations import _probe_mcp\n"
                    "sys.stdout.write(json.dumps(_probe_mcp(sys.argv[1])))\n")
            p = subprocess.run([sys.executable, "-c", code, launcher], capture_output=True,
                               env=_ascii_locale_env(), timeout=60, cwd=td)
        self.assertEqual(p.returncode, 0, p.stderr.decode("utf-8", "replace"))
        out = json.loads(p.stdout.decode("ascii"))
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["episodes"], 3)


class FindingThreeLineSeparatorProbeTest(unittest.TestCase):
    """Codex finding 3: str.splitlines cut a frame at a literal U+2028."""

    def test_probe_mcp_does_not_split_a_frame_at_a_unicode_line_separator(self):
        with tempfile.TemporaryDirectory(prefix="khipu-probe-") as td:
            launcher = _fake_launcher(td, _status_frames("a b c\x85d", literal=True))
            try:
                out = integrations._probe_mcp(launcher)
            except ValueError as e:
                self.fail(f"probe raised {e!r}")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["episodes"], 3)


class EveryWriterAndReaderTest(unittest.TestCase):
    """AWKWARD through every writer, and every writer's output through the
    readers."""

    def test_encode_frame_and_tool_text(self):
        assert_strict_frame(self, srv.encode_frame(_awkward_result()))
        text = srv._tool_text({"x": AWKWARD})["content"][0]["text"]
        assert_strict_json_text(self, text)

    def test_wire_dumps_both_ascii_modes_and_default(self):
        wire = _wire()

        class Odd:
            def __str__(self):
                return AWKWARD

        for ensure_ascii in (False, True):
            raw = wire.encode({"v": AWKWARD, "o": Odd()}, ensure_ascii=ensure_ascii, default=str)
            frame = assert_strict_frame(self, raw)
            self.assertEqual(frame["v"], frame["o"])
            if ensure_ascii:
                raw.decode("ascii")

    def test_a_valid_surrogate_pair_becomes_its_character(self):
        self.assertEqual(_wire().clean_text("x😀y"), "x😀y")

    def test_stdio_server_under_an_ascii_locale_stdout(self):
        request = (json.dumps({"jsonrpc": "2.0", "id": 7, "method": "ping"}) + "\n").encode("utf-8")
        out_bytes = io.BytesIO()
        stdout = io.TextIOWrapper(out_bytes, encoding="ascii")
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(srv, "_RELOAD_ON_CHANGE", False))
            stack.enter_context(mock.patch.object(srv, "_note_loaded_modules"))
            stack.enter_context(mock.patch.object(srv, "_drop_modules_if_code_changed"))
            stack.enter_context(mock.patch.object(srv, "handle_message", return_value=_awkward_result()))
            stack.enter_context(mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(request), encoding="utf-8")))
            stack.enter_context(mock.patch("sys.stdout", stdout))
            self.assertEqual(srv.main(), 0)
        frames = [ln for ln in out_bytes.getvalue().split(b"\n") if ln.strip()]
        self.assertEqual(len(frames), 1)
        assert_strict_frame(self, frames[0])

    def test_gateway_single_and_batch(self):
        port = _free_port()
        gw.Handler.token = TOKEN
        gw.Handler.rate = gw._Rate(1000)
        gw.Handler.auth_fail_rate = gw._Rate(1000)
        httpd = ThreadingHTTPServer(("127.0.0.1", port), gw.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory(prefix="khipu-gw-liveness-", ignore_cleanup_errors=True) as td, \
                    mock.patch.object(gw, "_liveness_path", return_value=Path(td) / "l.json"), \
                    mock.patch.object(gw, "handle_message", side_effect=lambda m, *a, **k: _awkward_result(m["id"])):
                for body in ({"jsonrpc": "2.0", "id": 1, "method": "ping"},
                             [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                              {"jsonrpc": "2.0", "id": 2, "method": "ping"}]):
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}/mcp", data=json.dumps(body).encode(), method="POST",
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
                    with urllib.request.urlopen(req, timeout=10) as r:
                        assert_strict_frame(self, r.read())
        finally:
            httpd.shutdown()
            httpd.server_close()

    def _hook_stdout(self, run) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            run()
        return buf.getvalue()

    def _assert_hook_output(self, text: str):
        lines = text.split("\n")
        self.assertEqual(lines[1:], [""])
        text.encode("ascii")
        assert_strict_frame(self, lines[0].encode("utf-8"))

    def test_session_start_hook(self):
        from khipu import recall_rule

        routing = mock.Mock()
        routing.begin.return_value = routing
        routing.text.return_value = ""
        with mock.patch.object(recall_rule, "_session_start_payload", return_value={"cwd": "/tmp/x"}), \
                mock.patch.object(recall_rule, "is_helper_session", return_value=False), \
                mock.patch.object(recall_rule, "_RoutingAtStart", return_value=routing), \
                mock.patch.object(recall_rule, "session_start_context", return_value=AWKWARD), \
                mock.patch.object(recall_rule, "fit_start_context", side_effect=lambda c, r: (c, False)):
            out = self._hook_stdout(lambda: recall_rule.session_start_main(shape="claude"))
        self._assert_hook_output(out)

    def test_prompt_recall_hook(self):
        from khipu import recall_prompt

        answer = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": AWKWARD}}
        with mock.patch.object(recall_prompt, "hook_main", return_value=answer), \
                mock.patch.object(recall_prompt, "finish_delivery"), \
                mock.patch("sys.stdin", io.StringIO("{}")):
            out = self._hook_stdout(lambda: recall_prompt.prompt_recall_main(shape="claude"))
        self._assert_hook_output(out)

    def test_delegation_hook(self):
        from khipu import routing_delegation

        with mock.patch.object(routing_delegation, "hook_main", return_value=({"x": AWKWARD}, None, None)), \
                mock.patch("sys.stdin", io.StringIO("{}")):
            out = self._hook_stdout(routing_delegation.main)
        self._assert_hook_output(out)

    def test_capture_hook_probe_output(self):
        from khipu import session_capture

        with mock.patch.object(session_capture, "hook_main", return_value={"x": AWKWARD}), \
                mock.patch.dict(os.environ, {"KHIPU_CAPTURE_PROBE": "1"}), \
                mock.patch("sys.stdin", io.StringIO("{}")):
            out = self._hook_stdout(lambda: session_capture.main(["--harness", "claude_code"]))
        self._assert_hook_output(out)

    def test_cli_status_and_doctor_json(self):
        # status, doctor and doctor --quick all print through cli._strict_json.
        from khipu import cli

        text = cli._strict_json({"recent_captures": [{"summary": AWKWARD}]}, indent=2, default=str)
        text.encode("ascii")
        assert_strict_json_text(self, text)

    def test_recall_daemon_answer_through_the_client(self):
        wire = _wire()
        spec = importlib.util.spec_from_file_location("khipu_recall_client", CLI_ROOT / "bin" / "khipu-recall-client.py")
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        # The daemon writes its answer line with wire.encode(..., ensure_ascii=True).
        line = wire.encode({"ok": True, "output": {"additionalContext": AWKWARD}}, ensure_ascii=True)
        out = client.as_hook_output(line)
        self.assertIsNotNone(out)
        # What the client prints: plain json.dumps of the daemon's answer.
        assert_strict_frame(self, json.dumps(out).encode("utf-8"))

    def test_readers_split_on_lf_only_and_decode_utf8(self):
        wire = _wire()
        frames = [wire.encode({"n": i, "v": AWKWARD}) for i in range(3)]
        raw = b"\n".join(frames) + b"\n\n"
        self.assertEqual(len(wire.split_frames(raw)), 3)
        literal = (json.dumps({"v": "a b c\x85d"}, ensure_ascii=False) + "\n").encode("utf-8")
        self.assertEqual(json.loads(wire.split_frames(literal * 2)[1])["v"], "a b c\x85d")

    def test_hook_stdin_is_utf8_under_an_ascii_locale(self):
        payload = json.dumps({"prompt": "漢字 😀  "}, ensure_ascii=False).encode("utf-8")
        with mock.patch("sys.stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="ascii")):
            self.assertEqual(json.loads(_wire().read_stdin())["prompt"], "漢字 😀  ")

    def test_probe_mcp_reads_every_awkward_point_from_a_real_writer(self):
        wire = _wire()
        status = {"counts": {"episodes": 1}, "recent_captures": [{"summary": AWKWARD}]}
        frames = (wire.encode({"jsonrpc": "2.0", "id": 1, "result": {"instructions": AWKWARD}}) + b"\n"
                  + wire.encode({"jsonrpc": "2.0", "id": 2, "result": srv._tool_text(status)}) + b"\n")
        with tempfile.TemporaryDirectory(prefix="khipu-probe-") as td:
            out = integrations._probe_mcp(_fake_launcher(td, frames))
        self.assertTrue(out["ok"], out)


if __name__ == "__main__":
    unittest.main()


class CriticFollowUpTest(unittest.TestCase):
    """Four spots the critics found after the first round."""

    def test_warm_daemon_answer_is_strict_through_the_client(self):
        import socket
        from khipu import recall_daemon, recall_prompt
        ctx = "prior work: caf\udce9   end   " + AWKWARD

        def stub(raw, shape="claude", **kw):
            return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                           "additionalContext": ctx}}

        svc = recall_daemon.RecallService.__new__(recall_daemon.RecallService)
        svc._lock = threading.Lock()
        svc.served = 0
        svc.routing_forkserver = None
        svc._admit = lambda **k: False
        spec = importlib.util.spec_from_file_location(
            "khipu_recall_client", CLI_ROOT / "bin" / "khipu-recall-client.py")
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        server, peer = socket.socketpair()
        peer.settimeout(10)
        with mock.patch.object(recall_prompt, "hook_main", stub):
            t = threading.Thread(target=svc._serve_conn, args=(server,), daemon=True)
            t.start()
            peer.sendall(json.dumps({"op": "recall", "raw": "{}", "shape": "claude"}).encode() + b"\n")
            buf = b""
            while b"\n" not in buf:
                chunk = peer.recv(65536)
                self.assertTrue(chunk, "daemon closed without answering")
                buf += chunk
            peer.close()
            t.join(10)
        line = buf.split(b"\n", 1)[0]
        assert_strict_frame(self, line)
        out = client.as_hook_output(line)
        self.assertIsNotNone(out)
        stdout = json.dumps(out)  # what the client writes as hook stdout
        assert_strict_frame(self, stdout.encode("utf-8"))

    def test_nan_and_infinity_become_null(self):
        wire = _wire()
        for ensure_ascii in (False, True):
            text = wire.dumps({"a": float("nan"), "b": [float("inf"), float("-inf")], "c": 1.5},
                              ensure_ascii=ensure_ascii)
            self.assertEqual(json.loads(text), {"a": None, "b": [None, None], "c": 1.5})
            self.assertNotRegex(text, r"NaN|Infinity")

    def test_promote_run_timeout_with_a_truncated_utf8_tail_does_not_raise(self):
        from khipu import promote
        code = ("import sys,time\nsys.stdout.buffer.write(b'ok \\xe2\\x80')\n"
                "sys.stdout.buffer.flush()\ntime.sleep(30)\n")
        r = promote._run([sys.executable, "-c", code], timeout=1)
        self.assertEqual(r.returncode, 124)
        self.assertTrue(r.stdout.startswith("ok "), r.stdout)

    def test_capture_v2_call_does_not_raise_under_an_ascii_locale(self):
        with tempfile.TemporaryDirectory(prefix="khipu-capv2-") as td:
            sink = Path(td) / "stdin.bin"
            stub = Path(td) / "capture_v2.py"
            stub.write_text(
                "import sys\nopen(sys.argv[1] if len(sys.argv) > 1 else %r, 'wb')"
                ".write(sys.stdin.buffer.read())\nsys.stdout.write('ok\\n')\n"
                % str(sink), encoding="utf-8")
            code = (
                "import sys\nfrom pathlib import Path\nfrom unittest import mock\n"
                "from khipu import capture\n"
                "with mock.patch.object(capture, '_capture_v2', lambda: Path(sys.argv[1])):\n"
                "    rc = capture.run_capture_v2({'summary': 'caf\\u00e9 \\u8a18\\u61b6 \\udce9'},"
                " suppress_mirror=True)\n"
                "sys.exit(rc)\n")
            home = Path(td) / "home"
            home.mkdir()
            env = _ascii_locale_env()
            env.update(HOME=str(home), KHIPU_KEYCHAIN="0", KHIPU_TEST="1")
            env.pop("KHIPU_ISOLATED_DIR", None)
            p = subprocess.run([sys.executable, "-c", code, str(stub)], capture_output=True,
                               env=env, timeout=60, cwd=td)
            err = p.stderr.decode("utf-8", "replace")
            self.assertEqual(p.returncode, 0, err)
            assert_strict_json_text(self, sink.read_bytes().decode("utf-8"))


class LeftoverWritersTest(unittest.TestCase):
    """Writers the first rounds missed: the recall client relaying a legacy
    daemon's reply, and session_capture's --status, --drain and dry-run
    diagnostics."""

    def test_recall_client_relays_a_legacy_reply_strictly(self):
        # An older service answers with the bare hook object, its text
        # carrying a lone surrogate escape; the client ran under
        # python3 -S -E -B and re-encoded it with plain json.dumps.
        import socket
        legacy = json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                                    "additionalContext": AWKWARD}}).encode() + b"\n"
        with tempfile.TemporaryDirectory(prefix="kh-", dir="/tmp") as td:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(os.path.join(td, "recall.sock"))
            listener.listen(1)
            listener.settimeout(20)

            def serve():
                conn, _ = listener.accept()
                with conn:
                    buf = b""
                    while b"\n" not in buf:
                        chunk = conn.recv(65536)
                        if not chunk:
                            return
                        buf += chunk
                    conn.sendall(legacy)

            t = threading.Thread(target=serve, daemon=True)
            t.start()
            env = {"PATH": os.environ.get("PATH", ""), "HOME": td, "KHIPU_CAPTURE_HOME": td}
            p = subprocess.run([sys.executable, "-S", "-E", "-B", str(CLI_ROOT / "bin" / "khipu-recall-client.py")],
                               input=b'{"prompt": "x", "session_id": "s"}', capture_output=True,
                               env=env, timeout=30)
            t.join(10)
            listener.close()
        self.assertEqual(p.returncode, 0, p.stderr)
        assert_strict_frame(self, p.stdout.rstrip(b"\n"))
        self.assertIn("hookSpecificOutput", json.loads(p.stdout))

    def _main_stdout(self, argv, **patches):
        from khipu import session_capture as sc
        buf = io.StringIO()
        with ExitStack() as stack:
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(sc, name, value))
            stack.enter_context(redirect_stdout(buf))
            self.assertEqual(sc.main(argv), 0)
        return buf.getvalue()

    def test_session_capture_status_is_strict(self):
        out = self._main_stdout(["--status"], liveness_all=lambda: {"warnings": [{"harness": AWKWARD}]})
        assert_strict_json_text(self, out)

    def test_session_capture_drain_report_is_strict(self):
        out = self._main_stdout(["--drain"], drain=lambda **kw: {"errors": [AWKWARD]})
        assert_strict_json_text(self, out)
