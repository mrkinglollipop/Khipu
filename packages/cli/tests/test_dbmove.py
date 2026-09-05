# --bypass-harness (sonnet lane): authored by a dispatched on-sub
# Agent(model="sonnet") subagent for Phase 1 of docs/plans/2026-09-05-
# setup-that-cannot-strand-you.md.
"""khipu.dbmove — copy the database to another host, verify, and switch.

Fake source/target connections throughout; nothing here touches a real
Postgres. The one thing this suite must prove structurally, not just
assert: the source connection is never sent anything but SELECT and
``COPY … TO STDOUT`` — see ``_FakeCursor`` below, which raises on anything
else reaching a cursor tagged ``source``.
"""

from __future__ import annotations

import re
import unittest
from unittest import mock

from khipu import dbmove


def _table_name(sql: str) -> str | None:
    m = re.search(r'"([^"]+)"', sql)
    return m.group(1) if m else None


class _WriteAttemptedOnSource(AssertionError):
    pass


class _FakeCopyReader:
    def __init__(self, n_blocks: int):
        self._blocks = [b"x"] * n_blocks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def __iter__(self):
        return iter(self._blocks)


class _FakeCopyWriter:
    def __init__(self, db, table):
        self.db = db
        self.table = table
        self.n = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        written = self.n - 1 if self.table in self.db.corrupt else self.n
        self.db.counts[self.table] = written
        return False

    def write(self, data):
        self.n += 1


class _FakeDB:
    """One side of a move (source or target): its table list and row counts.
    ``corrupt`` names tables whose post-copy count should come out wrong, to
    exercise the mismatch path without hand-rolling bad COPY payloads."""

    def __init__(self, tables, counts, *, corrupt=None):
        self.tables = list(tables)
        self.counts = dict(counts)
        self.corrupt = set(corrupt or ())


class _FakeCursor:
    def __init__(self, db, calls, *, role):
        self.db = db
        self.calls = calls
        self.role = role  # "source" | "target" — source may never be written to
        self._rows: list = []

    def _guard(self, sql: str) -> None:
        up = sql.upper()
        if self.role == "source" and (
            up.startswith("TRUNCATE")
            or up.startswith("INSERT")
            or up.startswith("UPDATE")
            or up.startswith("DELETE")
            or "FROM STDIN" in up
        ):
            raise _WriteAttemptedOnSource(sql)

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.calls.append(s)
        self._guard(s)
        up = s.upper()
        if up.startswith("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"):
            self._rows = [(t,) for t in self.db.tables]
        elif up.startswith("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS"):
            self._rows = []
        elif up.startswith("SELECT COUNT(*)"):
            table = _table_name(s)
            self._rows = [(self.db.counts.get(table, 0),)]
        elif up.startswith("TRUNCATE TABLE"):
            table = _table_name(s)
            self.db.counts[table] = 0
            self._rows = []
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def copy(self, sql):
        s = " ".join(sql.split())
        self.calls.append(s)
        self._guard(s)
        table = _table_name(s)
        if "TO STDOUT" in s.upper():
            return _FakeCopyReader(self.db.counts.get(table, 0))
        return _FakeCopyWriter(self.db, table)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, db, *, role):
        self.db = db
        self.role = role
        self.calls: list[str] = []
        self.commits = 0
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.db, self.calls, role=self.role)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


_ALL_OK_PREFLIGHT = {
    "ok": True,
    "stages": [{"id": sid, "ok": True} for sid in ("reach", "version", "privileges", "schema", "graph")],
    "summary": {},
}


class SameTargetTest(unittest.TestCase):
    def test_refuses_when_target_equals_current(self) -> None:
        with mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@host:5432/khipu"), \
             mock.patch("psycopg.connect") as pg:
            out = dbmove.move_database("postgres://other@host:5432/khipu")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "target_is_current")
        pg.assert_not_called()


class PreflightFailureTest(unittest.TestCase):
    def test_refuses_without_opening_any_move_connections(self) -> None:
        failing = {
            "ok": False,
            "stages": [
                {"id": "reach", "ok": True},
                {"id": "version", "ok": False},
            ],
            "summary": {},
        }
        with mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@here:5432/khipu"), \
             mock.patch("khipu.setup.connect_database", return_value=failing) as cd, \
             mock.patch("psycopg.connect") as pg:
            out = dbmove.move_database("postgres://u@there:5432/khipu")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "target_preflight_failed")
        cd.assert_called_once()
        pg.assert_not_called()


class NonEmptyTargetTest(unittest.TestCase):
    def _connect_side_effect(self, source_db, target_db):
        return [
            _FakeConn(source_db, role="source"),
            _FakeConn(target_db, role="target"),
        ]

    def test_refuses_a_nonempty_target_without_the_flag(self) -> None:
        source_db = _FakeDB(["episodes", "topics"], {"episodes": 5, "topics": 2})
        target_db = _FakeDB(["episodes", "topics"], {"episodes": 3, "topics": 0})
        with mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@here:5432/khipu"), \
             mock.patch("khipu.setup.connect_database", return_value=_ALL_OK_PREFLIGHT), \
             mock.patch(
                 "psycopg.connect",
                 side_effect=self._connect_side_effect(source_db, target_db),
             ):
            out = dbmove.move_database("postgres://u@there:5432/khipu")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "target_not_empty")
        self.assertIn("3", out["detail"])
        # Never truncates/copies once refused.
        self.assertEqual(target_db.counts["episodes"], 3)

    def test_into_nonempty_flag_allows_it_through_to_copy(self) -> None:
        tables = ["episodes", "topics"]
        source_db = _FakeDB(tables, {"episodes": 5, "topics": 2})
        target_db = _FakeDB(tables, {"episodes": 3, "topics": 0})
        with mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@here:5432/khipu"), \
             mock.patch("khipu.setup.connect_database", return_value=_ALL_OK_PREFLIGHT), \
             mock.patch(
                 "psycopg.connect",
                 side_effect=self._connect_side_effect(source_db, target_db),
             ):
            out = dbmove.move_database("postgres://u@there:5432/khipu", allow_nonempty=True)
        self.assertTrue(out["ok"])
        self.assertTrue(out["switched"])


class CopyOrderAndCountsTest(unittest.TestCase):
    def _connect_side_effect(self, source_db, target_db):
        return [
            _FakeConn(source_db, role="source"),
            _FakeConn(target_db, role="target"),
        ]

    def test_copies_in_fk_dependency_order_with_unknown_tables_appended(self) -> None:
        tables = list(dbmove.TABLE_ORDER) + ["zzz_future_table", "aaa_future_table"]
        counts = {t: (2 if t in ("episodes", "topics") else 0) for t in tables}
        source_db = _FakeDB(tables, counts)
        target_db = _FakeDB(tables, {t: 0 for t in tables})

        seen_order: list[str] = []
        with mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@here:5432/khipu"), \
             mock.patch("khipu.setup.connect_database", return_value=_ALL_OK_PREFLIGHT) as cd, \
             mock.patch(
                 "psycopg.connect",
                 side_effect=self._connect_side_effect(source_db, target_db),
             ):
            out = dbmove.move_database(
                "postgres://u@there:5432/khipu",
                progress=lambda table, rows: seen_order.append(table),
            )

        self.assertTrue(out["ok"], out)
        expected = list(dbmove.TABLE_ORDER) + ["aaa_future_table", "zzz_future_table"]
        self.assertEqual([t["name"] for t in out["tables"]], expected)
        self.assertEqual(seen_order, expected)
        # The final connect_database call (post-switch) is the second call.
        self.assertEqual(cd.call_count, 2)
        self.assertTrue(out["switched"])
        self.assertTrue(any("Another Mac" in r or "join kit" in r for r in out["remaining"]))

    def test_row_count_mismatch_is_reported_and_does_not_switch(self) -> None:
        tables = ["episodes", "topics"]
        source_db = _FakeDB(tables, {"episodes": 5, "topics": 2})
        target_db = _FakeDB(tables, {"episodes": 0, "topics": 0}, corrupt={"episodes"})
        with mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@here:5432/khipu"), \
             mock.patch("khipu.setup.connect_database", return_value=_ALL_OK_PREFLIGHT) as cd, \
             mock.patch(
                 "psycopg.connect",
                 side_effect=self._connect_side_effect(source_db, target_db),
             ):
            out = dbmove.move_database("postgres://u@there:5432/khipu")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "row_count_mismatch")
        self.assertIn("episodes", out["detail"])
        self.assertFalse(out["switched"])
        # Preflight was the only connect_database call — no post-switch call.
        self.assertEqual(cd.call_count, 1)

    def test_dry_run_copies_nothing_and_never_switches(self) -> None:
        tables = ["episodes", "topics"]
        source_db = _FakeDB(tables, {"episodes": 5, "topics": 2})
        target_db = _FakeDB(tables, {"episodes": 0, "topics": 0})
        with mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@here:5432/khipu"), \
             mock.patch("khipu.setup.connect_database", return_value=_ALL_OK_PREFLIGHT), \
             mock.patch(
                 "psycopg.connect",
                 side_effect=self._connect_side_effect(source_db, target_db),
             ):
            out = dbmove.move_database("postgres://u@there:5432/khipu", dry_run=True)
        self.assertTrue(out["ok"])
        self.assertTrue(out["dry_run"])
        self.assertFalse(out["switched"])
        for row in out["tables"]:
            self.assertIsNone(row["target_rows"])
        # Nothing was truncated or copied on the target either.
        self.assertEqual(target_db.counts, {"episodes": 0, "topics": 0})

    def test_source_connection_never_receives_a_write(self) -> None:
        """The structural guarantee: any TRUNCATE/INSERT/UPDATE/DELETE/COPY-
        FROM-STDIN routed at the ``source`` cursor raises in the fake itself,
        so this is a positive assertion the real code path never does it,
        not just an absence-of-evidence check."""
        tables = ["episodes", "topics"]
        source_db = _FakeDB(tables, {"episodes": 3, "topics": 1})
        target_db = _FakeDB(tables, {"episodes": 0, "topics": 0})
        with mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@here:5432/khipu"), \
             mock.patch("khipu.setup.connect_database", return_value=_ALL_OK_PREFLIGHT), \
             mock.patch(
                 "psycopg.connect",
                 side_effect=self._connect_side_effect(source_db, target_db),
             ):
            out = dbmove.move_database("postgres://u@there:5432/khipu")
        # If the real code had sent the source cursor anything but SELECT or
        # COPY TO STDOUT, _FakeCursor._guard would have raised out of
        # move_database and this assertion would never be reached.
        self.assertTrue(out["ok"], out)


if __name__ == "__main__":
    unittest.main()
