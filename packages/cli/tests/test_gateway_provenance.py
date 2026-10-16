"""Gateway provenance, phase 1: who owns a row is stamped by the server.

Hermetic: no database, no real ioreg, no real machine id. Cursors and
connections are fakes in the style of tests/test_capture.py. Scope:
docs/plans/2026-10-11-gateway-provenance.md, phase 1.
"""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from khipu import capture as cap
from khipu import db as khipu_db
from khipu import gateway as gw
from khipu import machine_identity as mi
from khipu import migrate
from khipu import mirror
from khipu import request_context as rc
from tests.test_capture import _EpisodesFakeCursor, _FakeConn

FAKE_UUID = "11111111-2222-3333-4444-555555555555"
FAKE_MACHINE_ID = "abcdef0123456789abcdef0123456789"


def _expected(raw: str) -> str:
    return "local:" + hashlib.sha256(raw.encode()).hexdigest()[:12]


class _ResetIdentityCache(unittest.TestCase):
    def setUp(self):
        mi._CACHED = None
        self.addCleanup(setattr, mi, "_CACHED", None)


class MachineIdentityTest(_ResetIdentityCache):
    def _ioreg(self, stdout: str):
        return mock.patch.object(
            mi.subprocess, "run", return_value=mock.Mock(stdout=stdout))

    def test_macos_ioreg_output_is_parsed_and_hashed_to_local_12_hex(self):
        out = ('+-o Root  <class IORegistryEntry>\n'
               f'    "IOPlatformUUID" = "{FAKE_UUID}"\n'
               '    "IOPlatformSerialNumber" = "X"\n')
        with mock.patch.object(mi.sys, "platform", "darwin"), self._ioreg(out):
            got = mi.local_origin()
        self.assertEqual(got, _expected(FAKE_UUID))
        self.assertTrue(mi.is_local_origin(got))
        self.assertNotIn(FAKE_UUID, got)

    def test_linux_machine_id_file_is_hashed_to_local_12_hex(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "machine-id"
            f.write_text(FAKE_MACHINE_ID + "\n", encoding="utf-8")
            with mock.patch.object(mi.sys, "platform", "linux"), \
                 mock.patch.object(mi, "LINUX_MACHINE_ID", f):
                got = mi.local_origin()
        self.assertEqual(got, _expected(FAKE_MACHINE_ID))

    def test_macos_without_a_readable_uuid_raises_and_never_invents_one(self):
        with mock.patch.object(mi.sys, "platform", "darwin"), self._ioreg("nothing here"):
            with self.assertRaises(mi.MachineIdentityError):
                mi.local_origin()
        with mock.patch.object(mi.sys, "platform", "darwin"), \
             mock.patch.object(mi.subprocess, "run", side_effect=OSError("no ioreg")):
            with self.assertRaises(mi.MachineIdentityError):
                mi.local_origin()

    def test_linux_without_a_readable_machine_id_raises(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "absent"
            empty = Path(d) / "empty"
            empty.write_text("\n", encoding="utf-8")
            for f in (missing, empty):
                with mock.patch.object(mi.sys, "platform", "linux"), \
                     mock.patch.object(mi, "LINUX_MACHINE_ID", f):
                    with self.assertRaises(mi.MachineIdentityError):
                        mi.local_origin()

    def test_linux_placeholder_or_malformed_machine_id_raises(self):
        bad = ("uninitialized", "ABCDEF0123456789ABCDEF0123456789", "abcdef01",
               FAKE_MACHINE_ID + "0", "abcdef01-2345-6789-abcd-ef0123456789",
               "ghijkl0123456789abcdef0123456789")
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "machine-id"
            for value in bad:
                f.write_text(value + "\n", encoding="utf-8")
                with self.subTest(value=value), \
                     mock.patch.object(mi.sys, "platform", "linux"), \
                     mock.patch.object(mi, "LINUX_MACHINE_ID", f):
                    with self.assertRaises(mi.MachineIdentityError):
                        mi.local_origin()
        self.assertIsNone(mi._CACHED)

    def test_origin_is_cached_per_process_and_a_failure_is_not_cached(self):
        with mock.patch.object(mi.sys, "platform", "darwin"), \
             self._ioreg(f'"IOPlatformUUID" = "{FAKE_UUID}"') as run:
            first = mi.local_origin()
            second = mi.local_origin()
        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 1)
        mi._CACHED = None
        with mock.patch.object(mi.sys, "platform", "darwin"), self._ioreg(""):
            with self.assertRaises(mi.MachineIdentityError):
                mi.local_origin()
        self.assertIsNone(mi._CACHED)


class RequestContextTest(_ResetIdentityCache):
    def test_outside_a_request_the_label_is_none(self):
        self.assertIsNone(rc.gateway_label())

    def test_label_is_visible_inside_and_restored_after(self):
        with rc.gateway_request("alpha"):
            self.assertEqual(rc.gateway_label(), "alpha")
        self.assertIsNone(rc.gateway_label())

    def test_empty_label_is_refused(self):
        with self.assertRaises(ValueError):
            with rc.gateway_request(""):
                pass

    def test_two_concurrent_requests_never_see_each_others_label(self):
        barrier = threading.Barrier(2, timeout=10)
        seen: dict[str, list[str | None]] = {}
        errors: list[BaseException] = []

        def worker(label: str):
            try:
                with rc.gateway_request(label):
                    barrier.wait()  # both threads are inside a request now
                    before = rc.gateway_label()
                    barrier.wait()
                    seen[label] = [before, rc.gateway_label()]
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in ("alpha", "beta")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(errors, [])
        self.assertEqual(seen, {"alpha": ["alpha", "alpha"], "beta": ["beta", "beta"]})
        self.assertIsNone(rc.gateway_label())

    def test_request_origin_is_gateway_label_inside_a_request(self):
        with mock.patch("khipu.machine_identity.local_origin", return_value="local:aaaaaaaaaaaa"):
            with rc.gateway_request("default"):
                self.assertEqual(rc.request_origin(), "gateway:default")

    def test_request_origin_is_none_in_a_gateway_process_outside_a_request(self):
        with mock.patch.dict("os.environ", {rc.GATEWAY_ACTIVE_ENV: "1"}), \
             mock.patch("khipu.machine_identity.local_origin",
                        side_effect=AssertionError("must not claim the container id")):
            self.assertIsNone(rc.request_origin())

    def test_request_origin_is_the_local_origin_otherwise(self):
        env = {k: v for k, v in __import__("os").environ.items() if k != rc.GATEWAY_ACTIVE_ENV}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch("khipu.machine_identity.local_origin", return_value="local:abcabcabcabc"):
            self.assertEqual(rc.request_origin(), "local:abcabcabcabc")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _call(url: str, token: str, rid: int) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": "khipu_status", "arguments": {}}}).encode()
    req = urllib.request.Request(
        url + "/mcp", data=body, method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


class GatewayLabelReachesHandlerTest(unittest.TestCase):
    TOKENS = {"alpha": "alpha-token-0123456789abcdef-aaaa", "beta": "beta-token-0123456789abcdef-bbbbb"}

    @classmethod
    def setUpClass(cls):
        cls._saved = (gw.Handler.token, gw.Handler.tokens, gw.Handler.rate,
                      gw.Handler.auth_fail_rate, gw.Handler.token_rate)
        gw.Handler.token = "primary-token-0123456789abcdef-pp"
        gw.Handler.tokens = dict(cls.TOKENS)
        gw.Handler.rate = gw._Rate(1000)
        gw.Handler.auth_fail_rate = gw._Rate(1000)
        gw.Handler.token_rate = gw._Rate(1000)
        port = _free_port()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", port), gw.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{port}"
        cls._tmp = tempfile.mkdtemp(prefix="khipu-gw-prov-")
        cls._patch = mock.patch.object(
            gw, "_liveness_path", return_value=Path(cls._tmp) / "liveness.json")
        cls._patch.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._patch.stop()
        (gw.Handler.token, gw.Handler.tokens, gw.Handler.rate,
         gw.Handler.auth_fail_rate, gw.Handler.token_rate) = cls._saved

    def _recording_handler(self, record: list, barrier: threading.Barrier | None = None):
        def fake(m):
            if barrier is not None:
                barrier.wait()  # both requests are inside the handler at once
            record.append(rc.gateway_label())
            return {"jsonrpc": "2.0", "id": m.get("id"),
                    "result": {"content": [{"type": "text", "text": rc.gateway_label() or ""}]}}
        return fake

    def test_tools_call_reaches_handle_message_with_the_tokens_label(self):
        for label, tok in self.TOKENS.items():
            record: list = []
            with mock.patch.object(gw, "handle_message", side_effect=self._recording_handler(record)):
                body = _call(self.url, tok, 1)
            self.assertEqual(record, [label])
            self.assertEqual(body["result"]["content"][0]["text"], label)
        self.assertIsNone(rc.gateway_label())

    def test_primary_token_is_labelled_default(self):
        record: list = []
        with mock.patch.object(gw, "handle_message", side_effect=self._recording_handler(record)):
            _call(self.url, gw.Handler.token, 1)
        self.assertEqual(record, ["default"])

    def test_concurrent_requests_from_two_tokens_each_record_their_own_label(self):
        barrier = threading.Barrier(2, timeout=10)
        record: list = []
        bodies: dict[str, dict] = {}
        errors: list[BaseException] = []

        def go(label: str):
            try:
                bodies[label] = _call(self.url, self.TOKENS[label], 1)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with mock.patch.object(gw, "handle_message",
                               side_effect=self._recording_handler(record, barrier)):
            threads = [threading.Thread(target=go, args=(n,)) for n in self.TOKENS]
            for t in threads:
                t.start()
            for t in threads:
                t.join(20)
        self.assertEqual(errors, [])
        self.assertEqual(sorted(record), ["alpha", "beta"])
        for label, body in bodies.items():
            self.assertEqual(body["result"]["content"][0]["text"], label)


class _OriginFakeCursor(_EpisodesFakeCursor):
    """The write_pg fake, plus an optional episodes.origin column and a record
    of every episodes INSERT (columns and params)."""

    def __init__(self, *, with_origin: bool = True):
        super().__init__()
        self.with_origin = with_origin
        self.inserts: list[dict] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("INSERT INTO episodes"):
            names = [c.strip() for c in
                     s.split("INSERT INTO episodes (", 1)[1].split(")", 1)[0].split(",")]
            self.inserts.append(dict(zip(names, params)))
        super().execute(sql, params)
        if self.with_origin and s.startswith("SELECT column_name FROM information_schema.columns") \
                and params == ("episodes",):
            self._result = list(self._result) + [("origin",)]


class _WritePgBase(_ResetIdentityCache):
    LOCAL = "local:0123456789ab"

    def setUp(self):
        super().setUp()
        khipu_db._TABLE_COLUMNS_CACHE.clear()
        self.addCleanup(khipu_db._TABLE_COLUMNS_CACHE.clear)
        for target, kw in (
            ("khipu.topic_graph.persist_capture_graph",
             {"return_value": {"nodes_minted": 0, "edges_minted": 0}}),
            ("khipu.config.path_setting", {"return_value": None}),
            ("khipu.embed.embed_one", {"side_effect": RuntimeError("no key in test")}),
            ("khipu.hygiene.classify_topics", {"return_value": ([], [], False)}),
        ):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)
        env = {k: v for k, v in __import__("os").environ.items()
               if k != rc.GATEWAY_ACTIVE_ENV}
        p = mock.patch.dict("os.environ", env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def _write(self, cur, payload=None, *, local=LOCAL):
        payload = payload or {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                              "summary": "a thing happened"}
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
             mock.patch("khipu.machine_identity.local_origin", return_value=local):
            return cap.write_pg(payload)


class WritePgOriginTest(_WritePgBase):
    def test_gateway_capture_lands_gateway_default(self):
        cur = _OriginFakeCursor()
        with rc.gateway_request("default"):
            stats = self._write(cur)
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(cur.inserts[0]["origin"], "gateway:default")

    def test_local_capture_lands_the_local_origin(self):
        cur = _OriginFakeCursor()
        stats = self._write(cur)
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(cur.inserts[0]["origin"], self.LOCAL)

    def test_payload_origin_is_overwritten_in_the_column_and_absent_from_raw(self):
        cur = _OriginFakeCursor()
        payload = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                   "summary": "spoof attempt", "origin": "gateway:evil"}
        with rc.gateway_request("default"):
            stats = self._write(cur, payload)
        self.assertEqual(cur.inserts[0]["origin"], "gateway:default")
        raw = json.loads(cur.inserts[0]["raw"])
        self.assertNotIn("origin", raw)
        self.assertNotIn("origin", cur.episodes[stats["episode_id"]]["raw"])
        self.assertNotIn("gateway:evil", json.dumps(cur.inserts[0]))

    def test_spoofed_origin_is_dropped_even_when_the_hub_has_no_column(self):
        cur = _OriginFakeCursor(with_origin=False)
        payload = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                   "summary": "spoof, old hub", "origin": "gateway:evil"}
        self._write(cur, payload)
        self.assertNotIn("origin", cur.inserts[0])
        self.assertNotIn("gateway:evil", json.dumps(cur.inserts[0]))

    def test_hub_without_the_origin_column_inserts_no_origin_column(self):
        for gateway in (True, False):
            khipu_db._TABLE_COLUMNS_CACHE.clear()
            cur = _OriginFakeCursor(with_origin=False)
            payload = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                       "summary": f"old hub gateway={gateway}"}
            if gateway:
                with rc.gateway_request("default"):
                    stats = self._write(cur, payload)
            else:
                stats = self._write(cur, payload)
            self.assertTrue(stats["episode_inserted"])
            self.assertNotIn("origin", cur.inserts[0])

    def test_unreadable_identity_leaves_origin_unset_and_the_capture_succeeds(self):
        cur = _OriginFakeCursor()
        with mock.patch("khipu.machine_identity.local_origin",
                        side_effect=mi.MachineIdentityError("unreadable")):
            with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)):
                stats = cap.write_pg({"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                                      "summary": "no identity here"})
        self.assertTrue(stats["episode_inserted"])
        self.assertNotIn("origin", cur.inserts[0])

    def test_gateway_process_outside_a_request_leaves_origin_unset(self):
        cur = _OriginFakeCursor()
        with mock.patch.dict("os.environ", {rc.GATEWAY_ACTIVE_ENV: "1"}):
            stats = self._write(cur)
        self.assertTrue(stats["episode_inserted"])
        self.assertNotIn("origin", cur.inserts[0])


class _ReplayCursor(_OriginFakeCursor):
    """The origin fake with one episode already stored, plus the two SQL
    shapes a replay of it issues: the origin claim and the row read-back."""

    TS = "2026-09-03T00:00:00Z"
    SUMMARY = "an episode a file sync wrote first"

    def __init__(self, *, with_origin: bool = True):
        super().__init__(with_origin=with_origin)
        self.episodes[1] = {
            "ts": self.TS, "session_id": "s1", "summary": self.SUMMARY,
            "topics": [], "decisions": [], "preferences": [], "raw": {},
        }
        self._existing_keys.add((self.TS, hashlib.md5(self.SUMMARY.encode()).hexdigest()))
        self.next_id = 2
        self.claims: list[tuple] = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("UPDATE episodes SET origin"):
            self.claims.append((s, params))
            return
        if s.startswith("SELECT to_jsonb(e) FROM episodes e"):
            self._result = [({"session_id": "s1", "raw": {}},)]
            return
        super().execute(sql, params)


class WritePgReplayClaimTest(_WritePgBase):
    """Item 4: a replayed capture claims an unowned row only for this machine."""

    def _replay(self, cur, **kw):
        payload = {"ts": cur.TS, "session_id": "s1", "summary": cur.SUMMARY}
        return self._write(cur, payload, **kw)

    def test_local_replay_claims_an_unowned_row_with_a_null_guard(self):
        cur = _ReplayCursor()
        stats = self._replay(cur)
        self.assertFalse(stats["episode_inserted"])
        self.assertEqual(len(cur.claims), 1)
        sql, params = cur.claims[0]
        self.assertEqual(params, (self.LOCAL, 1))
        self.assertIn("origin IS NULL", sql)

    def test_gateway_replay_never_claims_an_unowned_row(self):
        cur = _ReplayCursor()
        with rc.gateway_request("default"):
            stats = self._replay(cur)
        self.assertFalse(stats["episode_inserted"])
        self.assertEqual(cur.claims, [])

    def test_replay_with_no_origin_to_stamp_claims_nothing(self):
        cur = _ReplayCursor()
        with mock.patch.dict("os.environ", {rc.GATEWAY_ACTIVE_ENV: "1"}):
            self._replay(cur)
        self.assertEqual(cur.claims, [])
        khipu_db._TABLE_COLUMNS_CACHE.clear()
        cur = _ReplayCursor(with_origin=False)
        self._replay(cur)
        self.assertEqual(cur.claims, [])

    def test_a_fresh_insert_does_not_issue_a_claim(self):
        cur = _ReplayCursor()
        stats = self._write(cur)
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(cur.claims, [])


class StampOriginTest(_ResetIdentityCache):
    """Item 3: _stamp_origin re-reads the column list once when it says no."""

    LOCAL = "local:0123456789ab"

    def setUp(self):
        super().setUp()
        khipu_db._TABLE_COLUMNS_CACHE.clear()
        self.addCleanup(khipu_db._TABLE_COLUMNS_CACHE.clear)
        env = {k: v for k, v in os.environ.items() if k != rc.GATEWAY_ACTIVE_ENV}
        p = mock.patch.dict("os.environ", env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("khipu.machine_identity.local_origin", return_value=self.LOCAL)
        p.start()
        self.addCleanup(p.stop)

    @staticmethod
    def _column_reads(cur) -> int:
        return sum(1 for s, _ in cur.calls
                   if s.startswith("SELECT column_name FROM information_schema.columns"))

    def test_column_added_after_a_stale_cache_is_seen_without_a_restart(self):
        cur = _OriginFakeCursor(with_origin=False)
        khipu_db.table_columns(cur, "episodes")  # the long-lived process cached it early
        self.assertNotIn("origin", khipu_db._TABLE_COLUMNS_CACHE["episodes"])
        cur.with_origin = True  # `khipu migrate` ran meanwhile
        self.assertEqual(cap._stamp_origin(cur), self.LOCAL)
        self.assertIn("origin", khipu_db._TABLE_COLUMNS_CACHE["episodes"])

    def test_still_no_column_re_reads_once_and_returns_none(self):
        cur = _OriginFakeCursor(with_origin=False)
        khipu_db.table_columns(cur, "episodes")
        before = self._column_reads(cur)
        self.assertIsNone(cap._stamp_origin(cur))
        self.assertEqual(self._column_reads(cur) - before, 1)

    def test_present_column_does_not_re_read(self):
        cur = _OriginFakeCursor(with_origin=True)
        khipu_db.table_columns(cur, "episodes")
        before = self._column_reads(cur)
        self.assertEqual(cap._stamp_origin(cur), self.LOCAL)
        self.assertEqual(self._column_reads(cur), before)


class UpsertEpisodeOriginTest(unittest.TestCase):
    """_upsert_episode is the lowest door: a payload's own origin never gets in."""

    def setUp(self):
        khipu_db._TABLE_COLUMNS_CACHE.clear()
        self.addCleanup(khipu_db._TABLE_COLUMNS_CACHE.clear)

    def test_payload_origin_reaches_neither_raw_nor_an_origin_column(self):
        for with_origin in (True, False):
            khipu_db._TABLE_COLUMNS_CACHE.clear()
            cur = _OriginFakeCursor(with_origin=with_origin)
            payload = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                       "summary": f"direct call {with_origin}", "origin": "gateway:evil"}
            with self.subTest(with_origin=with_origin):
                self.assertTrue(mirror._upsert_episode(cur, payload))
                self.assertNotIn("origin", cur.inserts[0])
                self.assertNotIn("origin", json.loads(cur.inserts[0]["raw"]))
                self.assertNotIn("gateway:evil", json.dumps(cur.inserts[0]))
                self.assertEqual(payload["origin"], "gateway:evil")  # caller's dict untouched

    def test_an_explicit_origin_argument_is_the_only_way_in(self):
        cur = _OriginFakeCursor(with_origin=True)
        payload = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                   "summary": "stamped by the caller", "origin": "gateway:evil"}
        mirror._upsert_episode(cur, payload, origin="local:0123456789ab")
        self.assertEqual(cur.inserts[0]["origin"], "local:0123456789ab")
        self.assertNotIn("origin", json.loads(cur.inserts[0]["raw"]))


class MirrorOriginTest(_ResetIdentityCache):
    """Item 5: the live mirror write is stamped; the file replays are not."""

    LOCAL = "local:0123456789ab"

    def setUp(self):
        super().setUp()
        khipu_db._TABLE_COLUMNS_CACHE.clear()
        self.addCleanup(khipu_db._TABLE_COLUMNS_CACHE.clear)
        env = {k: v for k, v in os.environ.items() if k != rc.GATEWAY_ACTIVE_ENV}
        p = mock.patch.dict("os.environ", env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        for target, kw in (
            ("khipu.machine_identity.local_origin", {"return_value": self.LOCAL}),
            ("khipu.mirror.persist_capture_graph", {"return_value": {}}),
        ):
            p = mock.patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def _mirror(self, cur, payload):
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)):
            return mirror.mirror_episode(payload)

    def test_mirror_episode_stamps_this_machine(self):
        cur = _OriginFakeCursor()
        self.assertTrue(self._mirror(cur, {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                                           "summary": "live mirror"}))
        self.assertEqual(cur.inserts[0]["origin"], self.LOCAL)

    def test_mirror_episode_stamps_the_gateway_label_and_drops_a_spoof(self):
        cur = _OriginFakeCursor()
        with rc.gateway_request("default"):
            self._mirror(cur, {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                               "summary": "gateway mirror", "origin": "local:ffffffffffff"})
        self.assertEqual(cur.inserts[0]["origin"], "gateway:default")
        self.assertNotIn("origin", json.loads(cur.inserts[0]["raw"]))

    def test_mirror_episode_on_a_hub_without_the_column_inserts_no_origin(self):
        cur = _OriginFakeCursor(with_origin=False)
        self.assertTrue(self._mirror(cur, {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                                           "summary": "old hub mirror"}))
        self.assertNotIn("origin", cur.inserts[0])

    def test_mirror_episode_with_unreadable_identity_still_mirrors_unset(self):
        cur = _OriginFakeCursor()
        with mock.patch("khipu.machine_identity.local_origin",
                        side_effect=mi.MachineIdentityError("unreadable")):
            ok = self._mirror(cur, {"ts": "2026-09-03T00:00:00Z", "session_id": "s1",
                                    "summary": "no identity mirror"})
        self.assertTrue(ok)
        self.assertNotIn("origin", cur.inserts[0])

    def test_sync_recent_episodes_passes_no_origin(self):
        obj = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1", "summary": "from a file"}
        key = (obj["ts"], hashlib.md5(obj["summary"].encode()).hexdigest())
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "episodes.jsonl").write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with mock.patch("khipu.db.connect", return_value=_FakeConn(_OriginFakeCursor())), \
                 mock.patch("khipu.drift.episodes_missing_in_pg", return_value=[key]), \
                 mock.patch("khipu.embed.embed_on_capture", return_value=False), \
                 mock.patch.object(mirror, "_upsert_episode", return_value=True) as up:
                stats = mirror.sync_recent_episodes(Path(d))
        self.assertEqual(stats["inserted"], 1)
        up.assert_called_once()
        self.assertIsNone(up.call_args.kwargs.get("origin"))
        self.assertEqual(len(up.call_args.args), 2)  # cursor and payload only

    def test_reconcile_passes_no_origin(self):
        obj = {"ts": "2026-09-03T00:00:00Z", "session_id": "s1", "summary": "from a file"}
        key = (obj["ts"], hashlib.md5(obj["summary"].encode()).hexdigest())
        with tempfile.TemporaryDirectory() as d:
            ep = Path(d) / "episodes.jsonl"
            ep.write_text(json.dumps(obj) + "\n", encoding="utf-8")
            with mock.patch("khipu.db.connect", return_value=_FakeConn(_OriginFakeCursor())), \
                 mock.patch("khipu.drift.episode_files", return_value=[ep]), \
                 mock.patch("khipu.drift.episodes_missing_in_pg", return_value=[key]), \
                 mock.patch.object(mirror, "_upsert_episode", return_value=True) as up:
                stats = mirror.reconcile_memory_root(Path(d))
        self.assertEqual(stats["episodes_new"], 1)
        up.assert_called_once()
        self.assertIsNone(up.call_args.kwargs.get("origin"))
        self.assertEqual(len(up.call_args.args), 2)


class _MigCursor:
    def __init__(self, log: list):
        self.log = log
        self._rows: list = []

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.log.append((s, params))
        if s.startswith("SELECT to_regclass"):
            self._rows = [(True,)]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _MigConn:
    def __init__(self):
        self.log: list = []
        self.commits_at: list[int] = []

    def cursor(self):
        return _MigCursor(self.log)

    def commit(self):
        self.commits_at.append(len(self.log))


class MigrateOriginTest(_ResetIdentityCache):
    ORIGIN = "local:0123456789ab"

    def _dir(self, files: dict[str, str]) -> Path:
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, sql in files.items():
            (Path(d.name) / name).write_text(sql, encoding="utf-8")
        return Path(d.name)

    def _index(self, conn, fragment: str) -> int:
        for i, (s, _p) in enumerate(conn.log):
            if fragment in s:
                return i
        self.fail(f"{fragment!r} never executed")

    def test_file_naming_the_setting_gets_set_config_before_it_runs(self):
        d = self._dir({"0001_plain.sql": "SELECT 1;",
                       "0002_origin.sql": "SELECT current_setting('khipu.local_origin', true);"})
        conn = _MigConn()
        with mock.patch("khipu.machine_identity.local_origin", return_value=self.ORIGIN) as lo:
            result = migrate.run(conn=conn, directory=d, claim_local_rows=True)
        self.assertEqual(result["ran"], ["0001_plain", "0002_origin"])
        lo.assert_called_once()
        cfg = [(s, p) for s, p in conn.log if s.startswith("SELECT set_config")]
        self.assertEqual(len(cfg), 1)
        self.assertEqual(cfg[0][1], ("khipu.local_origin", self.ORIGIN))
        self.assertIn(", true)", cfg[0][0])  # transaction-local
        self.assertLess(self._index(conn, "SELECT set_config"),
                        self._index(conn, "current_setting('khipu.local_origin'"))

    def test_file_not_naming_the_setting_never_asks_for_the_identity(self):
        d = self._dir({"0001_plain.sql": "SELECT 1;", "0002_other.sql": "SELECT 2;"})
        conn = _MigConn()
        with mock.patch("khipu.machine_identity.local_origin",
                        side_effect=AssertionError("must not be called")) as lo:
            result = migrate.run(conn=conn, directory=d)
        self.assertEqual(result["ran"], ["0001_plain", "0002_other"])
        lo.assert_not_called()
        self.assertFalse([1 for s, _ in conn.log if "set_config" in s])

    def test_unreadable_identity_aborts_before_that_file_and_keeps_earlier_ones(self):
        d = self._dir({"0001_plain.sql": "SELECT 1;",
                       "0002_origin.sql": "SELECT current_setting('khipu.local_origin', true);",
                       "0003_after.sql": "SELECT 3;"})
        conn = _MigConn()
        with mock.patch("khipu.machine_identity.local_origin",
                        side_effect=mi.MachineIdentityError("unreadable")):
            with self.assertRaises(mi.MachineIdentityError):
                migrate.run(conn=conn, directory=d, claim_local_rows=True)
        executed = " ".join(s for s, _ in conn.log)
        self.assertIn("SELECT 1;", executed)
        self.assertNotIn("current_setting('khipu.local_origin'", executed)
        self.assertNotIn("SELECT 3;", executed)
        self.assertEqual(len(conn.commits_at), 1)  # only 0001 was committed
        self.assertTrue([1 for s, p in conn.log
                         if s.startswith("INSERT INTO schema_migrations") and p[0] == "0001_plain"])
        self.assertFalse([1 for s, p in conn.log
                          if s.startswith("INSERT INTO schema_migrations") and p[0] != "0001_plain"])


    def test_default_run_applies_the_none_value_and_never_reads_the_identity(self):
        d = self._dir({"0001_plain.sql": "SELECT 1;",
                       "0002_origin.sql": "SELECT current_setting('khipu.local_origin', true);"})
        for kwargs in ({}, {"claim_local_rows": False}):
            conn = _MigConn()
            with self.subTest(kwargs=kwargs), mock.patch(
                    "khipu.machine_identity.local_origin",
                    side_effect=AssertionError("must not be called")) as lo:
                result = migrate.run(conn=conn, directory=d, **kwargs)
            self.assertEqual(result["ran"], ["0001_plain", "0002_origin"])
            lo.assert_not_called()
            cfg = [(s, p) for s, p in conn.log if s.startswith("SELECT set_config")]
            self.assertEqual([p for _s, p in cfg], [("khipu.local_origin", "none")])
            self.assertIn(", true)", cfg[0][0])
            self.assertLess(self._index(conn, "SELECT set_config"),
                            self._index(conn, "current_setting('khipu.local_origin'"))

    def test_default_run_still_applies_when_this_machine_has_no_identity(self):
        d = self._dir({"0002_origin.sql": "SELECT current_setting('khipu.local_origin', true);"})
        conn = _MigConn()
        with mock.patch("khipu.machine_identity.local_origin",
                        side_effect=mi.MachineIdentityError("unreadable")):
            result = migrate.run(conn=conn, directory=d)
        self.assertEqual(result["ran"], ["0002_origin"])

    def test_dry_run_sets_nothing_and_asks_for_nothing(self):
        d = self._dir({"0002_origin.sql": "SELECT current_setting('khipu.local_origin', true);"})
        conn = _MigConn()
        with mock.patch("khipu.machine_identity.local_origin",
                        side_effect=AssertionError("must not be called")):
            result = migrate.run(conn=conn, directory=d, dry_run=True, claim_local_rows=True)
        self.assertEqual(result["ran"], [])
        self.assertFalse([1 for s, _ in conn.log if "set_config" in s])


class MigrateCallerClaimTest(unittest.TestCase):
    """Item 1: which callers may claim the local-harness rows."""

    RESULT = {"pending": [], "ran": [], "applied": []}

    def test_khipu_migrate_claims(self):
        from khipu import cli

        with mock.patch("khipu.migrate.run", return_value=dict(self.RESULT)) as run, \
             mock.patch("builtins.print"):
            cli.cmd_migrate(argparse.Namespace(dry_run=False))
        self.assertIs(run.call_args.kwargs["claim_local_rows"], True)

    def test_a_database_this_machine_just_created_claims(self):
        from khipu import components_postgres as cp

        with mock.patch("khipu.migrate.run", return_value=dict(self.RESULT)) as run:
            cp._run_migrations()
        self.assertIs(run.call_args.kwargs["claim_local_rows"], True)

    def test_setup_schema_stage_does_not_claim(self):
        from khipu import setup

        with mock.patch("khipu.migrate.run", return_value=dict(self.RESULT)) as run:
            setup._stage_schema(object())
        self.assertFalse(run.call_args.kwargs.get("claim_local_rows", False))

    def test_join_import_does_not_claim(self):
        from khipu import cli

        with tempfile.TemporaryDirectory() as d:
            kit = Path(d) / "kit.bin"
            kit.write_bytes(b"x")
            args = argparse.Namespace(join_cmd="import", file=str(kit), passphrase=None,
                                      skip_migrate_check=False)
            with mock.patch("khipu.join.read_passphrase", return_value="p"), \
                 mock.patch("khipu.join.import_kit", return_value={"expected": {}}), \
                 mock.patch("khipu.join.verify_live_counts", return_value={"ok": True}), \
                 mock.patch("khipu.migrate.run", return_value=dict(self.RESULT)) as run, \
                 mock.patch("builtins.print"):
                self.assertEqual(cli.cmd_join(args), 0)
        run.assert_called_once()
        self.assertFalse(run.call_args.kwargs.get("claim_local_rows", False))


if __name__ == "__main__":
    unittest.main()
