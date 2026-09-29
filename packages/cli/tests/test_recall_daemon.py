"""The warm recall service: protocol, staleness, socket ownership, and the
warm state it keeps (the replica's matrix, the embedding connection, the key).

Nothing here touches the network, a real replica or the real home directory:
sockets live in short temporary directories (AF_UNIX paths are limited to about
100 bytes), ``hook_main`` is stubbed, and the embedding transport is driven
through fake connections.
"""
from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import socket
import sqlite3
import stat
import struct
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from khipu import __version__, embed, recall_daemon
from khipu import hub_snapshot as hs
from khipu.recall_daemon import KeepAliveTransport, RecallService


def _short_dir() -> Path:
    d = Path(tempfile.mkdtemp(prefix="krd-"))
    if len(str(d / "recall.sock")) >= 90:
        shutil.rmtree(d, ignore_errors=True)
        d = Path(tempfile.mkdtemp(prefix="krd-", dir="/tmp"))
    return d


def _ask(path: Path, payload: bytes, *, timeout: float = 5.0) -> dict:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(str(path))
        s.sendall(payload)
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return json.loads(buf.decode("utf-8"))
    finally:
        s.close()


def _req(path: Path, obj: dict, **kw) -> dict:
    return _ask(path, json.dumps(obj).encode("utf-8") + b"\n", **kw)


class _Running:
    """A bound service running its accept loop on a thread."""

    def __init__(self, **kw) -> None:
        self.dir = _short_dir()
        self.path = self.dir / "recall.sock"
        self.svc = RecallService(self.path, **kw)
        assert self.svc.bind()
        self.exit: list[int] = []
        self.thread = threading.Thread(target=lambda: self.exit.append(self.svc.run()), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.svc.stop()
        self.thread.join(5)
        self.svc.close()
        shutil.rmtree(self.dir, ignore_errors=True)


class _ServiceCase(unittest.TestCase):
    def running(self, **kw) -> _Running:
        r = _Running(**kw)
        self.addCleanup(r.stop)
        return r


class ProtocolTest(_ServiceCase):
    def test_recall_round_trip_answers_what_hook_main_returns(self) -> None:
        answer = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "x"}}
        r = self.running()
        with mock.patch("khipu.recall_prompt.hook_main", return_value=answer) as hook:
            out = _req(r.path, {"op": "recall", "raw": '{"prompt": "hi"}', "shape": "cursor"})
            default = _req(r.path, {"op": "recall", "raw": "{}"})
        self.assertEqual(out, answer)
        self.assertEqual(default, answer)
        self.assertEqual(hook.call_args_list[0].args, ('{"prompt": "hi"}',))
        self.assertEqual(hook.call_args_list[0].kwargs, {"shape": "cursor"})
        self.assertEqual(hook.call_args_list[1].kwargs, {"shape": "claude"})

    def test_ping_reports_the_process_and_counts_recall_requests(self) -> None:
        r = self.running()
        first = _req(r.path, {"op": "ping"})
        with mock.patch("khipu.recall_prompt.hook_main", return_value={}):
            _req(r.path, {"op": "recall", "raw": "{}"})
        second = _req(r.path, {"op": "ping"})
        self.assertIs(first["ok"], True)
        self.assertEqual(first["version"], __version__)
        self.assertEqual(first["pid"], os.getpid())
        self.assertEqual(first["code_stamp"], r.svc.stamp)
        self.assertTrue(first["started"])
        self.assertEqual((first["served"], second["served"]), (0, 1))

    def test_malformed_and_unknown_requests_answer_an_error(self) -> None:
        r = self.running()
        cases = [
            b"this is not json\n",
            b"[1, 2]\n",
            b"\xff\xfe\n",
            json.dumps({"op": "nope"}).encode() + b"\n",
            json.dumps({"op": "recall", "raw": 5}).encode() + b"\n",
            json.dumps({"op": "recall", "raw": "{}", "shape": "vscode"}).encode() + b"\n",
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                self.assertIn("error", _ask(r.path, payload))

    def test_a_handler_that_raises_answers_an_empty_object_and_the_next_request_works(self) -> None:
        r = self.running()
        good = {"additional_context": "ok"}
        with mock.patch("khipu.recall_prompt.hook_main", side_effect=[RuntimeError("boom"), good]):
            first = _req(r.path, {"op": "recall", "raw": "{}", "shape": "cursor"})
            second = _req(r.path, {"op": "recall", "raw": "{}", "shape": "cursor"})
        self.assertEqual(first, {})
        self.assertEqual(second, good)
        self.assertTrue(r.thread.is_alive())

    def test_a_non_object_from_the_handler_answers_an_empty_object(self) -> None:
        r = self.running()
        with mock.patch("khipu.recall_prompt.hook_main", return_value="not a dict"):
            self.assertEqual(_req(r.path, {"op": "recall", "raw": "{}"}), {})

    def test_two_requests_are_served_at_the_same_time(self) -> None:
        r = self.running()
        both_inside = threading.Barrier(2, timeout=5)

        def hook(raw, *, shape):
            both_inside.wait()  # only passes when a second request is in the handler too
            return {"echo": raw}

        answers: dict[str, dict] = {}

        def call(tag: str) -> None:
            answers[tag] = _req(r.path, {"op": "recall", "raw": tag})

        with mock.patch("khipu.recall_prompt.hook_main", side_effect=hook):
            threads = [threading.Thread(target=call, args=(t,)) for t in ("a", "b")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(answers, {"a": {"echo": "a"}, "b": {"echo": "b"}})

    def test_several_requests_on_one_connection(self) -> None:
        r = self.running()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        self.addCleanup(s.close)
        s.connect(str(r.path))
        s.sendall(b'{"op": "ping"}\n{"op": "ping"}\n')
        buf = b""
        while buf.count(b"\n") < 2:
            buf += s.recv(65536)
        self.assertEqual([json.loads(x)["ok"] for x in buf.splitlines()], [True, True])

    def test_a_client_that_hangs_up_without_a_request_is_harmless(self) -> None:
        r = self.running()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(str(r.path))
        s.close()
        self.assertIs(_req(r.path, {"op": "ping"})["ok"], True)


class StalenessTest(_ServiceCase):
    def test_a_changed_stamp_answers_the_request_then_stops(self) -> None:
        stamp = ["a"]
        r = self.running(stamp_fn=lambda: stamp[0], recheck_s=0.0, idle_check=False)
        stamp[0] = "b"
        with mock.patch("khipu.recall_prompt.hook_main", return_value={"ok": 1}):
            out = _req(r.path, {"op": "recall", "raw": "{}"})
        self.assertEqual(out, {"ok": 1})  # the current request is answered, on the code it started with
        r.thread.join(5)
        self.assertFalse(r.thread.is_alive())
        self.assertEqual(r.exit, [0])
        self.assertFalse(r.path.exists())  # a client now gets "no connection" and falls back
        with self.assertRaises(OSError):
            _req(r.path, {"op": "ping"})

    def test_it_never_serves_a_second_request_on_stale_code(self) -> None:
        stamp = ["a"]
        svc = RecallService(_short_dir() / "recall.sock", stamp_fn=lambda: stamp[0], recheck_s=0.0)
        self.addCleanup(shutil.rmtree, svc.path.parent, ignore_errors=True)
        stamp[0] = "b"
        with mock.patch.object(svc, "_wake"), \
                mock.patch("khipu.recall_prompt.hook_main", return_value={"n": 1}) as hook:
            first = svc._answer(b'{"op": "recall", "raw": "{}"}')
            second = svc._answer(b'{"op": "recall", "raw": "{}"}')
        self.assertEqual(first, {"n": 1})
        self.assertEqual(second, {"error": "restarting"})
        self.assertEqual(hook.call_count, 1)

    def test_an_unchanged_stamp_keeps_serving(self) -> None:
        r = self.running(stamp_fn=lambda: "same", recheck_s=0.0)
        for _ in range(3):
            self.assertIs(_req(r.path, {"op": "ping"})["ok"], True)
        self.assertTrue(r.thread.is_alive())

    def test_the_disk_is_looked_at_most_once_per_interval(self) -> None:
        now = [100.0]
        stamp = ["a"]
        r = self.running(stamp_fn=lambda: stamp[0], recheck_s=5.0, clock=lambda: now[0], idle_check=False)
        stamp[0] = "b"
        now[0] = 102.0  # inside the interval: the change is not noticed yet
        self.assertIs(_req(r.path, {"op": "ping"})["ok"], True)
        self.assertTrue(r.thread.is_alive())
        now[0] = 106.0
        self.assertIs(_req(r.path, {"op": "ping"})["ok"], True)  # noticed here, and answered
        r.thread.join(5)
        self.assertFalse(r.thread.is_alive())

    def test_an_idle_service_notices_new_code_without_a_request(self) -> None:
        stamp = ["a"]
        r = self.running(stamp_fn=lambda: stamp[0], recheck_s=0.0)
        stamp[0] = "b"
        r.thread.join(5)
        self.assertFalse(r.thread.is_alive())
        self.assertEqual(r.exit, [0])

    def test_the_stamp_follows_the_package_files(self) -> None:
        d = Path(tempfile.mkdtemp(prefix="krd-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / "sub").mkdir()
        a, b = d / "a.py", d / "sub" / "b.py"
        a.write_text("x = 1\n")
        b.write_text("y = 2\n")
        os.utime(a, ns=(10**9, 10**9))
        os.utime(b, ns=(2 * 10**9, 2 * 10**9))
        with mock.patch.object(recall_daemon, "__file__", str(d / "recall_daemon.py")):
            base = recall_daemon.code_stamp()
            os.utime(a, ns=(5 * 10**9, 5 * 10**9))
            edited = recall_daemon.code_stamp()
            b.unlink()
            deleted = recall_daemon.code_stamp()
            (d / "__pycache__").mkdir()
            (d / "__pycache__" / "c.py").write_text("z = 3\n")
            with_cache = recall_daemon.code_stamp()
        self.assertEqual(len({base, edited, deleted}), 3)
        self.assertEqual(deleted, with_cache)  # bytecode directories are not the package's code


class SocketOwnershipTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _short_dir()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = self.dir / "recall.sock"

    def test_the_socket_is_private_to_the_user(self) -> None:
        svc = RecallService(self.path)
        self.assertTrue(svc.bind())
        self.addCleanup(svc.close)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertTrue(stat.S_ISSOCK(os.stat(self.path).st_mode))

    def test_a_stale_socket_file_from_a_dead_process_is_replaced(self) -> None:
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(str(self.path))
        dead.close()  # the file stays behind, nobody listens on it
        self.assertTrue(self.path.exists())
        svc = RecallService(self.path)
        self.assertTrue(svc.bind())
        self.addCleanup(svc.close)
        thread = threading.Thread(target=svc.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(svc.stop)
        self.assertIs(_req(self.path, {"op": "ping"})["ok"], True)

    def test_a_live_instance_makes_a_second_one_exit_zero(self) -> None:
        first = RecallService(self.path)
        self.assertTrue(first.bind())
        thread = threading.Thread(target=first.run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(first.stop)
        second = RecallService(self.path)
        self.assertFalse(second.bind())
        self.assertEqual(recall_daemon.serve(self.path, install_signals=False, keep_alive=False), 0)
        self.assertIs(_req(self.path, {"op": "ping"})["ok"], True)  # the first is untouched

    def test_a_file_that_is_not_a_socket_is_never_deleted(self) -> None:
        self.path.write_text("precious")
        with self.assertRaises(RuntimeError):
            RecallService(self.path).bind()
        self.assertEqual(self.path.read_text(), "precious")

    def test_a_directory_others_can_write_to_is_refused(self) -> None:
        os.chmod(self.dir, 0o777)
        with self.assertRaises(RuntimeError):
            RecallService(self.path).bind()

    def test_a_path_too_long_for_the_os_is_refused(self) -> None:
        long_path = self.dir / ("x" * 120) / "recall.sock"
        with self.assertRaises(RuntimeError):
            RecallService(long_path).bind()

    def test_close_leaves_a_successors_socket_alone(self) -> None:
        old = RecallService(self.path)
        self.assertTrue(old.bind())
        os.unlink(self.path)  # a successor replaced it
        new = RecallService(self.path)
        self.assertTrue(new.bind())
        self.addCleanup(new.close)
        old.close()
        self.assertTrue(self.path.exists())


class DaemonHealthTest(_ServiceCase):
    def test_not_running_is_ok(self) -> None:
        d = _short_dir()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        self.assertEqual(recall_daemon.daemon_health(d / "recall.sock"), {"ok": True, "running": False})

    def test_running_on_the_code_on_disk_is_ok(self) -> None:
        r = self.running()
        out = recall_daemon.daemon_health(r.path)
        self.assertTrue(out["ok"])
        self.assertTrue(out["running"])
        self.assertEqual((out["pid"], out["version"], out["served"]), (os.getpid(), __version__, 0))

    def test_different_code_is_ok_for_a_minute_and_then_not(self) -> None:
        r = self.running(stamp_fn=lambda: "0:0")  # what the service started on is not what is on disk
        newest, _count = recall_daemon.code_stamp_parts()
        soon = recall_daemon.daemon_health(r.path, now_ns=newest + 10 * 10**9)
        late = recall_daemon.daemon_health(r.path, now_ns=newest + 61 * 10**9)
        self.assertTrue(soon["ok"])
        self.assertFalse(late["ok"])
        self.assertTrue(late["running"])
        self.assertIn("different code", late["reason"])


# ---- the matrix cache ------------------------------------------------------------


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _replica(path: Path, rows: list[tuple[str, list[float]]]) -> None:
    con = sqlite3.connect(str(path))
    hs._create_schema(con)
    con.execute(
        "INSERT INTO embedding_profiles (id, provider, model, dim, is_active) "
        "VALUES ('p1', 'gemini', 'x', 3, 1)"
    )
    for ref, vec in rows:
        con.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, embedding) "
            "VALUES ('p1', 'episode', ?, 0, ?, ?)",
            (ref, f"text of {ref}", _pack(vec)),
        )
    con.commit()
    con.close()


class MatrixCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="krd-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.snap = self.dir / "hub_snapshot.sqlite"
        _replica(self.snap, [("near", [1.0, 0.0, 0.0]), ("far", [0.0, 1.0, 0.0])])
        patch = mock.patch.object(hs, "snapshot_path", return_value=self.snap)
        patch.start()
        self.addCleanup(patch.stop)
        opens = mock.patch.object(hs, "open_snapshot", wraps=hs.open_snapshot)
        self.opens = opens.start()
        self.addCleanup(opens.stop)

    def _query(self) -> list[dict]:
        return hs.cosine_candidates_snapshot([1.0, 0.0, 0.0], "p1", limit=10)

    def test_an_unchanged_replica_is_loaded_once(self) -> None:
        first = self._query()
        opens_after_first = self.opens.call_count
        second = self._query()
        self.assertEqual(first, second)
        # A miss opens the replica twice (rows, then the winners' text); a hit only once.
        self.assertEqual(opens_after_first, 2)
        self.assertEqual(self.opens.call_count - opens_after_first, 1)

    def test_a_changed_replica_misses_and_reloads(self) -> None:
        before = self._query()
        self.assertEqual({r["id"] for r in before}, {"near", "far"})
        con = sqlite3.connect(str(self.snap))
        con.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, embedding) "
            "VALUES ('p1', 'episode', 'added', 0, 'text of added', ?)",
            (_pack([0.6, 0.8, 0.0]),),
        )
        con.commit()
        con.close()
        st = os.stat(self.snap)
        os.utime(self.snap, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
        opens_before = self.opens.call_count
        after = self._query()
        self.assertEqual({r["id"] for r in after}, {"near", "far", "added"})
        self.assertEqual(self.opens.call_count - opens_before, 2)

    def test_the_cache_holds_one_entry_and_is_keyed_by_the_profile(self) -> None:
        self._query()
        self.assertEqual(hs.cosine_candidates_snapshot([1.0, 0.0, 0.0], "no-such-profile", limit=5), [])
        self.assertEqual(len(hs._MATRIX_CACHE["rows"]), 0)  # the second profile replaced the first
        self.assertEqual({r["id"] for r in self._query()}, {"near", "far"})

    def test_concurrent_callers_share_one_load(self) -> None:
        results: list[list[dict]] = []
        threads = [threading.Thread(target=lambda: results.append(self._query())) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(results), 4)
        self.assertTrue(all(r == results[0] for r in results))
        # One load (1 open) plus one text fetch per caller (4 opens).
        self.assertEqual(self.opens.call_count, 5)


# ---- the embedding connection ------------------------------------------------------


class _Resp:
    def __init__(self, status: int, body: bytes, *, will_close: bool = False) -> None:
        self.status, self.reason, self._body = status, "reason", body
        self.msg = http.client.HTTPMessage()
        self.will_close = will_close

    def read(self) -> bytes:
        return self._body


class _Conn:
    """One fake HTTPS connection; ``script`` says what each request does."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list[tuple] = []
        self.closed = False
        self.sock = None
        self.timeout = None

    def request(self, method, path, body=None, headers=None) -> None:
        self.requests.append((method, path, body, headers))
        step = self.script[0]
        if isinstance(step, BaseException):
            self.script.pop(0)
            raise step

    def getresponse(self) -> _Resp:
        return self.script.pop(0)

    def close(self) -> None:
        self.closed = True


class _Pool:
    def __init__(self, *conns: _Conn) -> None:
        self.queue = list(conns)
        self.connects: list[tuple] = []

    def __call__(self, host: str, port: int, timeout: float) -> _Conn:
        self.connects.append((host, port, timeout))
        return self.queue.pop(0)


URL = "https://embed.example.test/v1/models/m:batchEmbedContents"
HEADERS = {"Content-Type": "application/json", "x-goog-api-key": "k"}


class KeepAliveTransportTest(unittest.TestCase):
    def test_a_connection_is_reused_across_requests(self) -> None:
        conn = _Conn([_Resp(200, b"one"), _Resp(200, b"two")])
        pool = _Pool(conn)
        t = KeepAliveTransport(connect=pool)
        self.assertEqual(t.send(URL, b"a", HEADERS, 3.0), b"one")
        self.assertEqual(t.send(URL, b"b", HEADERS, 3.0), b"two")
        self.assertEqual(pool.connects, [("embed.example.test", 443, 3.0)])
        self.assertEqual([r[2] for r in conn.requests], [b"a", b"b"])
        self.assertEqual(conn.requests[0][:2], ("POST", "/v1/models/m:batchEmbedContents"))
        self.assertEqual(conn.requests[0][3], HEADERS)
        self.assertFalse(conn.closed)

    def test_a_connection_the_server_closed_is_retried_once_on_a_fresh_one(self) -> None:
        old = _Conn([_Resp(200, b"one"), http.client.RemoteDisconnected("closed")])
        fresh = _Conn([_Resp(200, b"two")])
        pool = _Pool(old, fresh)
        t = KeepAliveTransport(connect=pool)
        t.send(URL, b"a", HEADERS, 3.0)
        self.assertEqual(t.send(URL, b"b", HEADERS, 3.0), b"two")
        self.assertEqual(len(pool.connects), 2)
        self.assertTrue(old.closed)
        self.assertEqual([r[2] for r in fresh.requests], [b"b"])

    def test_a_fresh_connection_that_fails_is_not_retried(self) -> None:
        pool = _Pool(_Conn([ConnectionResetError("reset")]), _Conn([_Resp(200, b"never")]))
        t = KeepAliveTransport(connect=pool)
        with self.assertRaises(OSError):
            t.send(URL, b"a", HEADERS, 3.0)
        self.assertEqual(len(pool.connects), 1)

    def test_the_retry_happens_once_only(self) -> None:
        first = _Conn([_Resp(200, b"one"), BrokenPipeError("gone")])
        second = _Conn([ConnectionResetError("still gone")])
        third = _Conn([_Resp(200, b"never")])
        pool = _Pool(first, second, third)
        t = KeepAliveTransport(connect=pool)
        t.send(URL, b"a", HEADERS, 3.0)
        with self.assertRaises(ConnectionResetError):
            t.send(URL, b"b", HEADERS, 3.0)
        self.assertEqual(len(pool.connects), 2)

    def test_a_timeout_on_a_reused_connection_is_not_retried(self) -> None:
        first = _Conn([_Resp(200, b"one"), TimeoutError("slow")])
        pool = _Pool(first, _Conn([_Resp(200, b"never")]))
        t = KeepAliveTransport(connect=pool)
        t.send(URL, b"a", HEADERS, 3.0)
        with self.assertRaises(TimeoutError):
            t.send(URL, b"b", HEADERS, 3.0)
        self.assertEqual(len(pool.connects), 1)

    def test_a_protocol_error_surfaces_as_url_error(self) -> None:
        t = KeepAliveTransport(connect=_Pool(_Conn([http.client.BadStatusLine("junk")])))
        with self.assertRaises(urllib.error.URLError):
            t.send(URL, b"a", HEADERS, 3.0)

    def test_a_non_2xx_status_raises_http_error_with_a_readable_body(self) -> None:
        t = KeepAliveTransport(connect=_Pool(_Conn([_Resp(503, b'{"error": "busy"}')])))
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            t.send(URL, b"a", HEADERS, 3.0)
        self.assertEqual(ctx.exception.code, 503)
        self.assertEqual(ctx.exception.read(), b'{"error": "busy"}')

    def test_a_connection_the_server_will_close_is_not_kept(self) -> None:
        first = _Conn([_Resp(200, b"one", will_close=True)])
        second = _Conn([_Resp(200, b"two")])
        pool = _Pool(first, second)
        t = KeepAliveTransport(connect=pool)
        t.send(URL, b"a", HEADERS, 3.0)
        t.send(URL, b"b", HEADERS, 3.0)
        self.assertTrue(first.closed)
        self.assertEqual(len(pool.connects), 2)

    def test_concurrent_requests_each_get_their_own_connection(self) -> None:
        a, b = _Conn([_Resp(200, b"a")]), _Conn([_Resp(200, b"b")])
        pool = _Pool(a, b)
        t = KeepAliveTransport(connect=pool)
        inside = threading.Barrier(2, timeout=5)
        real_request = _Conn.request

        def request(self, *args, **kw):
            inside.wait()
            return real_request(self, *args, **kw)

        with mock.patch.object(_Conn, "request", request):
            out: list[bytes] = []
            threads = [threading.Thread(target=lambda: out.append(t.send(URL, b"x", HEADERS, 3.0)))
                       for _ in range(2)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(10)
        self.assertEqual(sorted(out), [b"a", b"b"])
        self.assertEqual(len(pool.connects), 2)


def _embedding_body(vectors: list[list[float]]) -> bytes:
    return json.dumps({"embeddings": [{"values": v} for v in vectors]}).encode("utf-8")


def _unit(i: int) -> list[float]:
    v = [0.0] * embed.DIM
    v[i] = 1.0
    return v


class EmbedTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        for patch in (
            mock.patch.object(embed, "_gemini_key", return_value="k"),
            mock.patch.object(embed, "_budget_take"),
            mock.patch.object(embed.time, "sleep"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _via_urllib(self, body: bytes, status: int = 200) -> list[list[float]]:
        class _Ok:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *a):
                return False

            def read(self_inner):
                return body

        def fake_urlopen(req, timeout=None):
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body))
            return _Ok()

        with mock.patch.object(embed.urllib.request, "urlopen", fake_urlopen):
            return embed.embed_batch(["a", "b"], retries=0)

    def test_the_keep_alive_path_returns_what_the_urllib_path_returns(self) -> None:
        body = _embedding_body([_unit(0), _unit(1)])
        expected = self._via_urllib(body)
        transport = KeepAliveTransport(connect=_Pool(_Conn([_Resp(200, body)])))
        got = embed.embed_batch(["a", "b"], retries=0, transport=transport.send)
        self.assertEqual(got, expected)
        self.assertEqual(len(got), 2)

    def test_an_error_status_reads_the_same_on_both_paths(self) -> None:
        body = b'{"error": {"message": "API key not valid"}}'
        with self.assertRaises(RuntimeError) as via_urllib:
            self._via_urllib(body, status=400)
        transport = KeepAliveTransport(connect=_Pool(_Conn([_Resp(400, body)])))
        with self.assertRaises(RuntimeError) as via_keepalive:
            embed.embed_batch(["a", "b"], retries=0, transport=transport.send)
        self.assertEqual(str(via_urllib.exception), str(via_keepalive.exception))
        self.assertIn("embed HTTP 400", str(via_keepalive.exception))

    def test_the_retry_ladder_is_the_same_on_the_keep_alive_path(self) -> None:
        body = _embedding_body([_unit(0)])
        conn = _Conn([_Resp(503, b"busy"), _Resp(200, body)])
        transport = KeepAliveTransport(connect=_Pool(conn))
        got = embed.embed_batch(["a"], retries=1, transport=transport.send)
        self.assertEqual(got, [_unit(0)])
        self.assertEqual(len(conn.requests), 2)

    def test_a_dead_network_is_a_network_error_after_the_retries(self) -> None:
        def down(*_a):
            raise ConnectionRefusedError("no route")

        transport = KeepAliveTransport(connect=down)
        with self.assertRaises(RuntimeError) as ctx:
            embed.embed_batch(["a"], retries=1, transport=transport.send)
        self.assertIn("network error", str(ctx.exception))

    def test_the_installed_transport_is_the_default_until_it_is_removed(self) -> None:
        body = _embedding_body([_unit(2)])
        calls: list[str] = []

        def transport(url, data, headers, timeout):
            calls.append(url)
            return body

        embed.set_transport(transport)
        self.addCleanup(embed.set_transport, None)
        self.assertEqual(embed.embed_batch(["a"], retries=0), [_unit(2)])
        self.assertEqual(len(calls), 1)
        embed.set_transport(None)
        self.assertIs(embed._transport, embed._urllib_transport)


class KeyCacheTest(unittest.TestCase):
    def test_the_key_is_resolved_once_and_dropped_when_the_api_rejects_it(self) -> None:
        with mock.patch("khipu.keychain.resolve_gemini_key", side_effect=["old", "rotated"]) as resolve:
            self.assertEqual(embed._gemini_key(), "old")
            self.assertEqual(embed._gemini_key(), "old")
            self.assertEqual(resolve.call_count, 1)
            embed._note_auth_failure(500)  # a server error says nothing about the key
            self.assertEqual(embed._gemini_key(), "old")
            embed._note_auth_failure(400)
            self.assertEqual(embed._gemini_key(), "rotated")
            self.assertEqual(resolve.call_count, 2)

    def test_a_rejected_request_drops_the_key_it_used(self) -> None:
        embed._KEY_CACHE[:] = ["stale"]
        transport = KeepAliveTransport(connect=_Pool(_Conn([_Resp(403, b"denied")])))
        with mock.patch.object(embed, "_budget_take"), self.assertRaises(RuntimeError):
            embed.embed_batch(["a"], retries=0, transport=transport.send)
        self.assertEqual(embed._KEY_CACHE, [])


class ProjectCacheTest(unittest.TestCase):
    def test_a_project_is_looked_up_again_after_the_ttl(self) -> None:
        from khipu import recall_prompt

        answers = [{"project": None}, {"project": "acme/widget"}]
        with mock.patch("khipu.identity.resolve_repo_root", side_effect=answers) as resolve:
            self.assertIsNone(recall_prompt._project_for_cwd("/work/x"))
            self.assertIsNone(recall_prompt._project_for_cwd("/work/x"))
            self.assertEqual(resolve.call_count, 1)
            recall_prompt._PROJECT_FOR_CWD["/work/x"] = (None, time.monotonic() - 301.0)
            self.assertEqual(recall_prompt._project_for_cwd("/work/x"), "acme/widget")
            self.assertEqual(resolve.call_count, 2)


if __name__ == "__main__":
    unittest.main()
