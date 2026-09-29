"""The recall client and the launcher that calls it.

The client is run as a real subprocess (``python3 -S -E -B``, the way the
launcher starts it) against a tiny fake service on a Unix socket. The launcher
is run as a real ``sh`` script with a stand-in interpreter on ``KHIPU_PYTHON``,
so what is under test is the launcher's own decision: the client's answer, or
today's one-shot path.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

BIN = Path(__file__).resolve().parents[1] / "bin"
CLIENT = BIN / "khipu-recall-client.py"
LAUNCHER = BIN / "khipu-prompt-recall"


def _short_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="krc-"))
    if len(str(d / "recall.sock")) >= 90:
        shutil.rmtree(d, ignore_errors=True)
        d = Path(tempfile.mkdtemp(prefix="krc-", dir="/tmp"))
    return d


class _FakeService:
    """Answers each connection's first line with ``reply(line)`` after
    ``delay`` seconds. ``reply`` returns bytes to send, or None to hang up."""

    def __init__(self, home: Path, reply, *, delay: float = 0.0) -> None:
        self.received: list[bytes] = []
        self._reply, self._delay = reply, delay
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(home / "recall.sock"))
        self.sock.listen(8)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
            line = buf.split(b"\n", 1)[0]
            self.received.append(line)
            time.sleep(self._delay)
            out = self._reply(line)
            if out is not None:
                conn.sendall(out)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self) -> None:
        self.sock.close()


class ClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = _short_dir()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def _run(self, payload: str = '{"prompt": "hello"}', *args: str) -> tuple[subprocess.CompletedProcess, float]:
        env = {"PATH": os.environ.get("PATH", ""), "KHIPU_CAPTURE_HOME": str(self.home)}
        started = time.monotonic()
        done = subprocess.run(
            [sys.executable, "-S", "-E", "-B", str(CLIENT), *args],
            input=payload.encode("utf-8"), capture_output=True, env=env, timeout=30,
        )
        return done, time.monotonic() - started

    def _serve(self, reply, **kw) -> _FakeService:
        svc = _FakeService(self.home, reply, **kw)
        self.addCleanup(svc.close)
        return svc

    def test_an_answer_is_printed_and_the_request_carries_the_payload(self) -> None:
        answer = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "ctx"}}
        svc = self._serve(lambda _line: json.dumps(answer).encode() + b"\n")
        done, _ = self._run('{"prompt": "hello"}')
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads(done.stdout), answer)
        self.assertEqual(
            json.loads(svc.received[0]),
            {"op": "recall", "raw": '{"prompt": "hello"}', "shape": "claude"},
        )

    def test_the_cursor_shape_is_passed_on(self) -> None:
        svc = self._serve(lambda _line: b"{}\n")
        self._run("{}", "cursor")
        self.assertEqual(json.loads(svc.received[0])["shape"], "cursor")

    def test_the_payload_reaches_the_service_verbatim(self) -> None:
        svc = self._serve(lambda _line: b"{}\n")
        payload = '{"prompt": "café \\"quoted\\" \\n newline", "cwd": "/work/x"}'
        self._run(payload)
        self.assertEqual(json.loads(svc.received[0])["raw"], payload)

    def test_no_socket_means_exit_3_and_no_output(self) -> None:
        done, _ = self._run()
        self.assertEqual((done.returncode, done.stdout), (3, b""))

    def test_a_socket_nobody_listens_on_means_exit_3_and_no_output(self) -> None:
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(self.home / "recall.sock"))
        dead.close()
        done, _ = self._run()
        self.assertEqual((done.returncode, done.stdout), (3, b""))

    def test_a_slow_service_gets_an_empty_object_and_no_second_attempt(self) -> None:
        svc = self._serve(lambda _line: b'{"late": true}\n', delay=6.0)
        done, elapsed = self._run()
        self.assertEqual(done.returncode, 0)
        self.assertEqual(json.loads(done.stdout), {})
        self.assertLess(elapsed, 5.5)  # gave up at 1.6 s, not at the service's 6 s
        self.assertEqual(len(svc.received), 1)

    def test_an_answer_that_is_not_a_usable_object_prints_an_empty_object(self) -> None:
        for reply in (b"\n", None, b"garbage\n", b"[1, 2]\n", b'"text"\n', b'{"error": "restarting"}\n'):
            with self.subTest(reply=reply):
                home = _short_dir()
                self.addCleanup(shutil.rmtree, home, ignore_errors=True)
                self.home = home
                svc = _FakeService(home, lambda _line, r=reply: r)
                self.addCleanup(svc.close)
                done, _ = self._run()
                self.assertEqual(done.returncode, 0)
                self.assertEqual(json.loads(done.stdout), {})


class SocketPathTest(unittest.TestCase):
    """The client cannot import khipu, so it repeats khipu_home's resolution;
    this is what keeps the two from drifting apart."""

    @classmethod
    def setUpClass(cls) -> None:
        spec = importlib.util.spec_from_file_location("khipu_recall_client_under_test", CLIENT)
        cls.client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.client)

    def test_it_matches_the_services_socket_path(self) -> None:
        from khipu import recall_daemon

        cases = [
            {"KHIPU_CAPTURE_HOME": "/srv/capture-home"},
            {"KHIPU_AEGIS_HOME": "/srv/aegis-home"},
            {"KHIPU_CAPTURE_HOME": "/srv/capture-home", "KHIPU_AEGIS_HOME": "/srv/aegis-home"},
            {},
        ]
        for extra in cases:
            with self.subTest(env=extra):
                clean = {k: v for k, v in os.environ.items()
                         if k not in ("KHIPU_CAPTURE_HOME", "KHIPU_AEGIS_HOME")}
                with mock.patch.dict(os.environ, {**clean, **extra}, clear=True):
                    self.assertEqual(self.client.socket_path(), str(recall_daemon.socket_path()))

    def test_the_client_imports_only_the_standard_library(self) -> None:
        lines = CLIENT.read_text(encoding="utf-8").splitlines()
        imports = {ln.split()[1].split(".")[0] for ln in lines if ln.startswith("import ")}
        self.assertLessEqual(imports, {"json", "os", "socket", "sys", "time"})
        self.assertEqual([ln for ln in lines if ln.startswith("from ")], [])


class LauncherTest(unittest.TestCase):
    """Drive the real launcher script; a stand-in interpreter plays both the
    client (its argument list names the client file) and today's one-shot
    path (its argument list is ``-c ...``)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="krl-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bindir = self.tmp / "packages" / "cli" / "bin"
        bindir.mkdir(parents=True)
        self.launcher = bindir / "khipu-prompt-recall"
        shutil.copy(LAUNCHER, self.launcher)
        (bindir / "khipu-recall-client.py").write_text("# stand-in: the fake interpreter answers\n")
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.log = self.tmp / "log"
        self.fake_py = self.tmp / "fakepy"
        self.fake_py.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            "  *khipu-recall-client.py*)\n"
            '    printf \'%s\\n\' "$*" > "$FAKE_LOG.client_args"\n'
            '    cat > "$FAKE_LOG.client_stdin"\n'
            '    printf \'%s\' "$FAKE_CLIENT_OUT"\n'
            '    exit "${FAKE_CLIENT_EXIT:-0}" ;;\n'
            "  *)\n"
            '    cat > "$FAKE_LOG.fallback_stdin"\n'
            "    echo FALLBACK\n"
            "    exit 0 ;;\n"
            "esac\n"
        )
        self.fake_py.chmod(0o755)

    def _run(self, payload: str, *, client_exit: int = 0, client_out: str = "", extra_env=None, args=()):
        env = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(self.home),
            "KHIPU_PYTHON": str(self.fake_py), "KHIPU_ROOT": str(self.tmp),
            "FAKE_LOG": str(self.log), "FAKE_CLIENT_EXIT": str(client_exit),
            "FAKE_CLIENT_OUT": client_out, **(extra_env or {}),
        }
        return subprocess.run(
            ["/bin/sh", str(self.launcher), *args],
            input=payload, capture_output=True, text=True, env=env, timeout=30,
        )

    def _seen(self, name: str) -> str | None:
        p = Path(f"{self.log}.{name}")
        return p.read_text() if p.exists() else None

    def test_a_client_answer_is_printed_and_the_one_shot_path_never_runs(self) -> None:
        done = self._run('{"prompt": "hi"}', client_exit=0, client_out='{"a": 1}')
        self.assertEqual((done.returncode, done.stdout), (0, '{"a": 1}\n'))
        self.assertEqual(self._seen("client_stdin"), '{"prompt": "hi"}')
        self.assertIsNone(self._seen("fallback_stdin"))

    def test_a_client_that_exits_zero_without_output_prints_an_empty_object_and_does_not_fall_back(self) -> None:
        done = self._run("{}", client_exit=0, client_out="")
        self.assertEqual((done.returncode, done.stdout), (0, "{}\n"))
        self.assertIsNone(self._seen("fallback_stdin"))

    def test_exit_3_runs_the_one_shot_path_with_the_same_payload(self) -> None:
        payload = '{"prompt": "hello there", "cwd": "/work/x"}'
        done = self._run(payload, client_exit=3, client_out="")
        self.assertEqual((done.returncode, done.stdout), (0, "FALLBACK\n"))
        self.assertEqual(self._seen("fallback_stdin"), payload)
        self.assertEqual(self._seen("client_stdin"), payload)

    def test_a_broken_client_also_falls_back(self) -> None:
        done = self._run("{}", client_exit=1, client_out="half an answ")
        self.assertEqual(done.stdout, "FALLBACK\n")

    def test_the_cursor_flag_reaches_the_client_as_the_shape(self) -> None:
        self._run("{}", client_exit=0, client_out="{}", args=("--cursor",))
        self.assertIn("cursor", self._seen("client_args"))
        self._run("{}", client_exit=0, client_out="{}")
        self.assertIn("claude", self._seen("client_args"))

    def test_the_client_runs_without_site_or_environment(self) -> None:
        self._run("{}", client_exit=0, client_out="{}")
        self.assertEqual(self._seen("client_args").split()[:3], ["-S", "-E", "-B"])

    def test_the_daemon_can_be_switched_off(self) -> None:
        done = self._run("{}", client_exit=0, client_out='{"a": 1}', extra_env={"KHIPU_RECALL_DAEMON": "0"})
        self.assertEqual(done.stdout, "FALLBACK\n")
        self.assertIsNone(self._seen("client_stdin"))

    def test_aegis_is_refused_before_anything_else_runs(self) -> None:
        done = self._run("{}", client_exit=0, client_out='{"a": 1}', extra_env={"KHIPU_HARNESS": "aegis"})
        self.assertEqual((done.returncode, done.stdout), (0, "{}\n"))
        self.assertIsNone(self._seen("client_stdin"))
        self.assertIsNone(self._seen("fallback_stdin"))

    def test_the_launcher_keeps_its_bytecode_cache_outside_the_bundle(self) -> None:
        text = LAUNCHER.read_text(encoding="utf-8")
        self.assertIn('PYTHONPYCACHEPREFIX="${HOME}/Library/Caches/Khipu/pycache"', text)
        self.assertLess(text.index("KHIPU_HARNESS"), text.index("khipu-recall-client.py"))


if __name__ == "__main__":
    unittest.main()
