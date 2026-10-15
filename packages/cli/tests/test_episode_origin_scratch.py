"""Migration 0029 (episodes.origin) and the server-stamped origin on capture,
against a real, separately provisioned scratch Postgres. Same guard and
environment as test_episode_sources_scratch.py. Migration tests run inside one
transaction that is rolled back, so the scratch database is left as found."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import psycopg
import pytest

from tests import connect_scratch, validate_scratch_dsn

SCRATCH_DSN = os.environ.get("KHIPU_SCRATCH_DSN")

if SCRATCH_DSN and os.environ.get("KHIPU_LIVE_TESTS") == "1":
    validate_scratch_dsn(SCRATCH_DSN)

pytestmark = pytest.mark.skipif(
    not SCRATCH_DSN or os.environ.get("KHIPU_LIVE_TESTS") != "1",
    reason="requires KHIPU_LIVE_TESTS=1 and a provisioned nonce-scoped KHIPU_SCRATCH_DSN",
)

VERSION = "0029_episode_origin"
LOCAL = "local:aaaaaaaaaaaa"


def _direct_connect():
    return connect_scratch(SCRATCH_DSN)


def _migration_path() -> Path:
    from khipu import migrate

    for version, path in migrate.available():
        if version == VERSION:
            return path
    raise AssertionError(f"{VERSION}.sql not found under ops/migrations")


def _migration_sql() -> str:
    return _migration_path().read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_column_cache():
    from khipu import db as _db

    _db._TABLE_COLUMNS_CACHE.clear()
    yield
    _db._TABLE_COLUMNS_CACHE.clear()


class MigrationScratchTest(unittest.TestCase):
    def setUp(self):
        self.conn = _direct_connect()
        self.cur = self.conn.cursor()
        # A hub as it stood before 0029, and empty, for this transaction only.
        self.cur.execute("DROP INDEX IF EXISTS idx_episodes_origin")
        self.cur.execute("ALTER TABLE episodes DROP COLUMN IF EXISTS origin")
        self.cur.execute("DELETE FROM schema_migrations WHERE version = %s", (VERSION,))
        self.cur.execute("TRUNCATE episodes CASCADE")
        self._n = 0

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _set_origin(self, value=LOCAL):
        self.cur.execute("SELECT set_config('khipu.local_origin', %s, true)", (value,))

    def _apply(self, value=LOCAL):
        self._set_origin(value)
        self.cur.execute(_migration_sql())

    def _episode(self, session_id, *, origin=None) -> int:
        self._n += 1
        has_origin = origin is not None
        cols = "ts, session_id, summary" + (", origin" if has_origin else "")
        marks = "now(), %s, %s" + (", %s" if has_origin else "")
        params = [session_id, f"seed {self._n}"] + ([origin] if has_origin else [])
        self.cur.execute(f"INSERT INTO episodes ({cols}) VALUES ({marks}) RETURNING id", params)
        return int(self.cur.fetchone()[0])

    def _origin_of(self, eid):
        self.cur.execute("SELECT origin FROM episodes WHERE id = %s", (eid,))
        return self.cur.fetchone()[0]

    def _has_column(self):
        self.cur.execute("SELECT count(*) FROM information_schema.columns "
                         "WHERE table_schema='public' AND table_name='episodes' "
                         "AND column_name='origin'")
        return self.cur.fetchone()[0] == 1

    def _recorded(self):
        self.cur.execute("SELECT count(*) FROM schema_migrations WHERE version = %s", (VERSION,))
        return self.cur.fetchone()[0]

    def test_refuses_when_identity_unset_or_malformed(self):
        for bad in (None, "", "local:abc", "local:AAAAAAAAAAAA", "local:aaaaaaaaaaaaa",
                    "gateway:default", "aaaaaaaaaaaa", "local:aaaaaaaaaaaa\n",
                    "NONE", "none ", " none"):
            with self.subTest(value=bad):
                self.cur.execute("SAVEPOINT s")
                if bad is not None:
                    self._set_origin(bad)
                with self.assertRaises(psycopg.errors.RaiseException):
                    self.cur.execute(_migration_sql())
                self.cur.execute("ROLLBACK TO SAVEPOINT s")
                self.assertFalse(self._has_column())
                self.assertEqual(self._recorded(), 0)
                self.cur.execute("SELECT to_regclass('public.idx_episodes_origin')")
                self.assertIsNone(self.cur.fetchone()[0])

    def test_backfill_by_prefix_and_idempotent(self):
        gw = [self._episode("grokbot:x"), self._episode("grok_bot:x")]
        local = [self._episode(p) for p in
                 ("claude_code:s1", "cursor:s2", "codex:s3", "aegis:s4")]
        untouched = [self._episode(s) for s in (
            "plainsession", None, "", "unknown:zzz", "grokbotx:1", "grok%bot:1",
            "grok_botx:1", "claude_codex:1", ":leading", "Claude_Code:1", "gateway:default")]
        # Rows that already carry an origin (the column exists, as on a hub
        # where an earlier partial run added it).
        self.cur.execute("ALTER TABLE episodes ADD COLUMN IF NOT EXISTS origin TEXT")
        keep_a = self._episode("claude_code:keep", origin="gateway:other")
        keep_b = self._episode("grokbot:keep", origin="local:bbbbbbbbbbbb")

        self._apply()

        for eid in gw:
            self.assertEqual(self._origin_of(eid), "gateway:default")
        for eid in local:
            self.assertEqual(self._origin_of(eid), LOCAL)
        for eid in untouched:
            self.assertIsNone(self._origin_of(eid), eid)
        self.assertEqual(self._origin_of(keep_a), "gateway:other")
        self.assertEqual(self._origin_of(keep_b), "local:bbbbbbbbbbbb")
        self.assertEqual(self._recorded(), 1)

        self.cur.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
                         "AND indexname='idx_episodes_origin'")
        (indexdef,) = self.cur.fetchone()
        self.assertIn("origin IS NOT NULL", indexdef)

        self.cur.execute("SELECT id, origin FROM episodes ORDER BY id")
        before = self.cur.fetchall()
        # A second run, even with a different identity, changes no existing row.
        self._apply("local:cccccccccccc")
        self.cur.execute("SELECT id, origin FROM episodes ORDER BY id")
        self.assertEqual(self.cur.fetchall(), before)
        self.assertEqual(self._recorded(), 1)

    def test_runner_supplies_identity_from_local_origin(self):
        from khipu import migrate

        tmp = Path(tempfile.mkdtemp(prefix="khipu-mig-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(_migration_path(), tmp / f"{VERSION}.sql")
        eid = self._episode("codex:runner")
        gid = self._episode("grokbot:runner")
        # The runner commits per file; a stand-in connection keeps it inside
        # this test's transaction so the final rollback undoes it.
        real = self.conn
        proxy = mock.MagicMock(wraps=real)
        proxy.cursor = real.cursor
        proxy.commit = lambda: None
        with mock.patch("khipu.machine_identity.local_origin", return_value=LOCAL), \
                mock.patch.object(migrate, "plan", return_value={
                    "applied": [], "pending": [VERSION], "unknown_applied": [],
                    "_pending_paths": [(VERSION, tmp / f"{VERSION}.sql")]}):
            result = migrate.run(conn=proxy, directory=tmp, claim_local_rows=True)
        self.assertEqual(result["ran"], [VERSION])
        self.assertEqual(self._origin_of(eid), LOCAL)
        self.assertEqual(self._origin_of(gid), "gateway:default")
        self.assertEqual(self._recorded(), 1)

    def test_runner_stops_when_identity_unreadable(self):
        from khipu import migrate
        from khipu.machine_identity import MachineIdentityError

        tmp = Path(tempfile.mkdtemp(prefix="khipu-mig-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(_migration_path(), tmp / f"{VERSION}.sql")
        proxy = mock.MagicMock(wraps=self.conn)
        proxy.cursor = self.conn.cursor
        proxy.commit = lambda: None
        with mock.patch("khipu.machine_identity.local_origin",
                        side_effect=MachineIdentityError("unreadable")), \
                mock.patch.object(migrate, "plan", return_value={
                    "applied": [], "pending": [VERSION], "unknown_applied": [],
                    "_pending_paths": [(VERSION, tmp / f"{VERSION}.sql")]}):
            with self.assertRaises(MachineIdentityError):
                migrate.run(conn=proxy, directory=tmp, claim_local_rows=True)
        self.assertFalse(self._has_column())
        self.assertEqual(self._recorded(), 0)


    def test_none_adds_column_index_and_gateway_backfill_but_leaves_local_rows_null(self):
        gw = [self._episode("grokbot:x"), self._episode("grok_bot:y")]
        local = [self._episode(p) for p in
                 ("claude_code:s1", "cursor:s2", "codex:s3", "aegis:s4")]
        other = [self._episode(s) for s in ("plainsession", None, "unknown:zzz")]

        self._apply("none")

        self.assertTrue(self._has_column())
        self.assertEqual(self._recorded(), 1)
        self.cur.execute("SELECT to_regclass('public.idx_episodes_origin')")
        self.assertIsNotNone(self.cur.fetchone()[0])
        for eid in gw:
            self.assertEqual(self._origin_of(eid), "gateway:default")
        for eid in local + other:
            self.assertIsNone(self._origin_of(eid), eid)

    def test_migration_file_is_one_statement_so_a_refusal_applies_nothing(self):
        # Under a psql that continues past errors, every top-level statement
        # after a failure would still run. One DO block is one statement.
        body = "\n".join(line for line in _migration_sql().splitlines()
                         if not line.lstrip().startswith("--")).strip()
        self.assertTrue(body.startswith("DO $$"))
        self.assertTrue(body.endswith("$$;"))
        self.assertEqual(body.count("$$"), 2)

    def test_default_runner_applies_none_and_never_reads_the_identity(self):
        from khipu import migrate

        tmp = Path(tempfile.mkdtemp(prefix="khipu-mig-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        shutil.copy(_migration_path(), tmp / f"{VERSION}.sql")
        eid = self._episode("codex:runner")
        gid = self._episode("grokbot:runner")
        real = self.conn
        proxy = mock.MagicMock(wraps=real)
        proxy.cursor = real.cursor
        proxy.commit = lambda: None
        with mock.patch("khipu.machine_identity.local_origin",
                        side_effect=AssertionError("must not be called")) as lo, \
                mock.patch.object(migrate, "plan", return_value={
                    "applied": [], "pending": [VERSION], "unknown_applied": [],
                    "_pending_paths": [(VERSION, tmp / f"{VERSION}.sql")]}):
            result = migrate.run(conn=proxy, directory=tmp)
        lo.assert_not_called()
        self.assertEqual(result["ran"], [VERSION])
        self.assertIsNone(self._origin_of(eid))
        self.assertEqual(self._origin_of(gid), "gateway:default")
        self.assertEqual(self._recorded(), 1)


class WriteOriginScratchTest(unittest.TestCase):
    """capture.write_pg against real SQL: the server stamps origin."""

    @classmethod
    def setUpClass(cls):
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('khipu.local_origin', %s, true)", (LOCAL,))
                cur.execute(_migration_sql())
            conn.commit()

    def setUp(self):
        from khipu import db as _db

        _db._TABLE_COLUMNS_CACHE.clear()
        self.ids: list[int] = []
        for p in (mock.patch("khipu.db.connect", side_effect=lambda *a, **k: _direct_connect()),
                  mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}),
                  mock.patch("khipu.config.path_setting", return_value=None),
                  mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no key")),
                  mock.patch("khipu.machine_identity.local_origin", return_value=LOCAL)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("KHIPU_GATEWAY_ACTIVE", None)

    def tearDown(self):
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM episodes WHERE id = ANY(%s)", (self.ids,))
            conn.commit()

    def _payload(self, **kw):
        nonce = os.urandom(6).hex()
        return {"ts": "2026-10-11T12:00:00Z", "summary": f"origin capture {nonce}",
                "harness": "claude_code", "session_id": f"claude_code:{nonce}",
                "transcript_range": "0:100", "window_id": f"w{nonce}", "part": "1/1", **kw}

    def _write(self, payload):
        from khipu import capture

        stats = capture.write_pg(payload)
        if stats.get("episode_id"):
            self.ids.append(int(stats["episode_id"]))
        return stats

    def _origin(self, eid):
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT origin, raw FROM episodes WHERE id = %s", (eid,))
                return cur.fetchone()

    def test_gateway_capture_is_stamped_with_the_token_label(self):
        from khipu.request_context import gateway_request

        with gateway_request("default"):
            stats = self._write(self._payload())
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(self._origin(stats["episode_id"])[0], "gateway:default")

    def test_local_capture_is_stamped_with_this_machine(self):
        stats = self._write(self._payload())
        self.assertEqual(self._origin(stats["episode_id"])[0], LOCAL)

    def test_gateway_process_outside_a_request_is_left_unset(self):
        with mock.patch.dict(os.environ, {"KHIPU_GATEWAY_ACTIVE": "1"}):
            stats = self._write(self._payload())
        self.assertIsNone(self._origin(stats["episode_id"])[0])

    def test_unreadable_identity_still_captures_with_no_origin(self):
        from khipu.machine_identity import MachineIdentityError

        with mock.patch("khipu.machine_identity.local_origin",
                        side_effect=MachineIdentityError("unreadable")):
            stats = self._write(self._payload())
        self.assertTrue(stats["episode_inserted"])
        self.assertIsNone(self._origin(stats["episode_id"])[0])

    def test_payload_claimed_origin_is_overwritten(self):
        from khipu.request_context import gateway_request

        stats = self._write(self._payload(origin="gateway:forged"))
        origin, raw = self._origin(stats["episode_id"])
        self.assertEqual(origin, LOCAL)
        self.assertNotIn("origin", raw or {})
        with gateway_request("default"):
            stats = self._write(self._payload(origin=LOCAL))
        origin, raw = self._origin(stats["episode_id"])
        self.assertEqual(origin, "gateway:default")
        self.assertNotIn("origin", raw or {})

    def test_hub_without_the_column_still_captures(self):
        from khipu import db as _db
        from khipu.request_context import gateway_request

        # A pre-0029 hub reports no origin column. The column list is faked at
        # its source, because a stale cache alone is now re-read once.
        real = _db.table_columns

        def pre_0029(cur, table):
            cols = real(cur, table)
            return cols - {"origin"} if table == "episodes" else cols

        with _direct_connect() as conn:
            with conn.cursor() as cur:
                self.assertIn("origin", real(cur, "episodes"))
        with mock.patch("khipu.db.table_columns", side_effect=pre_0029):
            local = self._write(self._payload())
            with gateway_request("default"):
                gw = self._write(self._payload())
        for stats in (local, gw):
            self.assertTrue(stats["episode_inserted"])
            self.assertIsNone(self._origin(stats["episode_id"])[0])

    def test_column_added_after_the_process_cached_the_list_is_stamped(self):
        from khipu import db as _db

        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cols = set(_db.table_columns(cur, "episodes"))
        # What a long-lived server that started before `khipu migrate` holds.
        _db._TABLE_COLUMNS_CACHE["episodes"] = cols - {"origin"}
        stats = self._write(self._payload())
        self.assertTrue(stats["episode_inserted"])
        self.assertEqual(self._origin(stats["episode_id"])[0], LOCAL)
        self.assertIn("origin", _db._TABLE_COLUMNS_CACHE["episodes"])

    def _seed(self, session_id, summary, origin):
        """A row as a file-mirror sync (NULL) or another writer left it."""
        with _direct_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO episodes (ts, session_id, summary, origin) "
                    "VALUES ('2026-10-11T12:00:00Z', %s, %s, %s) RETURNING id",
                    (session_id, summary, origin))
                eid = int(cur.fetchone()[0])
            conn.commit()
        self.ids.append(eid)
        return eid

    def _replay_payload(self, summary, session_id):
        return {"ts": "2026-10-11T12:00:00Z", "summary": summary, "session_id": session_id}

    def test_local_replay_claims_an_unowned_row_and_only_an_unowned_one(self):
        n = os.urandom(6).hex()
        unowned = self._seed(f"claude_code:{n}", f"unowned replay {n}", None)
        gateway_owned = self._seed(f"claude_code:{n}b", f"gateway owned replay {n}", "gateway:other")
        other_machine = self._seed(f"claude_code:{n}c", f"other machine replay {n}", "local:bbbbbbbbbbbb")

        for summary, session in (
                (f"unowned replay {n}", f"claude_code:{n}"),
                (f"gateway owned replay {n}", f"claude_code:{n}b"),
                (f"other machine replay {n}", f"claude_code:{n}c")):
            stats = self._write(self._replay_payload(summary, session))
            self.assertFalse(stats["episode_inserted"])

        self.assertEqual(self._origin(unowned)[0], LOCAL)
        self.assertEqual(self._origin(gateway_owned)[0], "gateway:other")
        self.assertEqual(self._origin(other_machine)[0], "local:bbbbbbbbbbbb")

    def test_gateway_replay_never_claims_an_unowned_row(self):
        from khipu.request_context import gateway_request

        n = os.urandom(6).hex()
        unowned = self._seed(f"claude_code:{n}", f"unowned gateway replay {n}", None)
        mine = self._seed(f"claude_code:{n}b", f"local owned replay {n}", LOCAL)
        with gateway_request("default"):
            self._write(self._replay_payload(f"unowned gateway replay {n}", f"claude_code:{n}"))
            self._write(self._replay_payload(f"local owned replay {n}", f"claude_code:{n}b"))
        self.assertIsNone(self._origin(unowned)[0])
        self.assertEqual(self._origin(mine)[0], LOCAL)

    def test_replay_from_a_gateway_process_outside_a_request_claims_nothing(self):
        n = os.urandom(6).hex()
        unowned = self._seed(f"claude_code:{n}", f"unowned process replay {n}", None)
        with mock.patch.dict(os.environ, {"KHIPU_GATEWAY_ACTIVE": "1"}):
            self._write(self._replay_payload(f"unowned process replay {n}", f"claude_code:{n}"))
        self.assertIsNone(self._origin(unowned)[0])
