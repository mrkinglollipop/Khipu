"""khipu migrate — the only supported way to apply the schema on a fresh
machine. Never touches a real database here: the cursor is faked.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import migrate


class FakeCursor:
    def __init__(self, applied, table_exists=True):
        self.applied = set(applied)
        self.table_exists = table_exists
        self.executed = []
        self._rows = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "to_regclass" in sql:
            self._rows = [(self.table_exists,)]
        elif sql.strip().startswith("SELECT version"):
            self._rows = [(v,) for v in sorted(self.applied)]
        elif sql.startswith("INSERT INTO schema_migrations"):
            self.applied.add(params[0])
        else:
            # a migration body: the file records itself
            for line in sql.splitlines():
                if line.startswith("-- version:"):
                    self.applied.add(line.split(":", 1)[1].strip())

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _dir(names):
    d = tempfile.mkdtemp()
    for n in names:
        (Path(d) / f"{n}.sql").write_text(f"-- version: {n}\nSELECT 1;\n")
    return Path(d)


class PlanTest(unittest.TestCase):
    def test_pending_is_available_minus_applied_in_order(self):
        d = _dir(["0000_a", "0001_b", "0002_c"])
        cur = FakeCursor(applied={"0000_a"})
        p = migrate.plan(cur, d)
        self.assertEqual(p["pending"], ["0001_b", "0002_c"])
        self.assertEqual(p["applied"], ["0000_a"])

    def test_no_schema_migrations_table_means_everything_is_pending(self):
        d = _dir(["0000_a", "0001_b"])
        p = migrate.plan(FakeCursor(applied=set(), table_exists=False), d)
        self.assertEqual(p["pending"], ["0000_a", "0001_b"])

    def test_files_that_do_not_look_like_migrations_are_ignored(self):
        d = _dir(["0000_a"])
        (d / "notes.sql").write_text("SELECT 1;")
        (d / "README.md").write_text("x")
        self.assertEqual([v for v, _ in migrate.available(d)], ["0000_a"])

    def test_versions_applied_but_absent_from_the_repo_are_reported(self):
        d = _dir(["0000_a"])
        p = migrate.plan(FakeCursor(applied={"0000_a", "0009_ghost"}), d)
        self.assertEqual(p["unknown_applied"], ["0009_ghost"])


class RunTest(unittest.TestCase):
    def test_dry_run_executes_no_migration(self):
        d = _dir(["0000_a", "0001_b"])
        cur = FakeCursor(applied={"0000_a"})
        conn = FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn):
            out = migrate.run(dry_run=True, directory=d)
        self.assertEqual(out["pending"], ["0001_b"])
        self.assertEqual(out["ran"], [])
        self.assertEqual(conn.commits, 0)
        self.assertFalse(any("SELECT 1" in s for s, _ in cur.executed))

    def test_a_real_run_applies_pending_in_order_and_commits_each(self):
        d = _dir(["0000_a", "0001_b", "0002_c"])
        cur = FakeCursor(applied={"0000_a"})
        conn = FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn):
            out = migrate.run(directory=d)
        self.assertEqual(out["ran"], ["0001_b", "0002_c"])
        self.assertEqual(out["pending"], [])
        self.assertEqual(conn.commits, 2)
        bodies = [s for s, _ in cur.executed if "SELECT 1" in s]
        self.assertEqual(len(bodies), 2)
        self.assertLess(bodies[0].index("0001_b"), 1 + bodies[1].index("0002_c"))

    def test_a_fully_applied_database_is_a_no_op(self):
        d = _dir(["0000_a"])
        cur = FakeCursor(applied={"0000_a"})
        conn = FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn):
            out = migrate.run(directory=d)
        self.assertEqual(out["ran"], [])
        self.assertEqual(conn.commits, 0)

    def test_the_runner_records_a_migration_that_forgot_to_record_itself(self):
        d = _dir(["0000_a"])
        (d / "0000_a.sql").write_text("SELECT 1;\n")  # no self-record line
        cur = FakeCursor(applied=set())
        conn = FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn):
            migrate.run(directory=d)
        self.assertIn("0000_a", cur.applied)


class RepoMigrationsTest(unittest.TestCase):
    def test_the_repo_migrations_are_discoverable_and_self_recording(self):
        files = migrate.available()
        self.assertGreaterEqual(len(files), 5)
        for version, path in files:
            self.assertIn("INSERT INTO schema_migrations", path.read_text(), version)
