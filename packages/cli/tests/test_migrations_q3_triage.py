"""Grok 2026-10-26 migration-audit leads (CONFIRMED). Each test fails while the defect is real."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from unittest import mock

from khipu import commitments, db, migrate


class _Cur:
    """Answers the two statements migrate.applied() issues."""

    def __init__(self, versions):
        self.versions = versions
        self._r = []

    def execute(self, sql, params=()):
        self._r = [(True,)] if "to_regclass" in sql else [(v,) for v in self.versions]

    def fetchone(self):
        return self._r[0]

    def fetchall(self):
        return self._r


def test_join_import_does_not_report_ok_when_the_migrations_failed(capsys):
    from khipu import cli

    with tempfile.TemporaryDirectory() as d:
        kit = Path(d) / "kit.bin"
        kit.write_bytes(b"x")
        args = argparse.Namespace(
            join_cmd="import", file=str(kit), passphrase=None, skip_migrate_check=False
        )
        with mock.patch("khipu.join.read_passphrase", return_value="p"), \
             mock.patch("khipu.join.import_kit", return_value={"expected": {}}), \
             mock.patch("khipu.join.verify_live_counts", return_value={"ok": True}), \
             mock.patch("khipu.migrate.run", side_effect=RuntimeError("lock timeout")):
            rc = cli.cmd_join(args)
    out = json.loads(capsys.readouterr().out)
    assert rc != 0 or out.get("ok") is False, (
        f"exit {rc} with ok={out.get('ok')} and migrate={out.get('migrate')}"
    )


def test_a_recorded_0015_can_be_rerun_by_khipu_migrate_once_pg_trgm_exists():
    # The doctor's literal_trgm_ok remedy is "run `khipu migrate`"; 0015 records its
    # version even when it skipped the indexes, so plan() must offer it again.
    every = [v for v, _ in migrate.available()]
    plan = migrate.plan(_Cur(every))
    assert "0015_literal_trgm" in plan["pending"], (
        "khipu migrate has nothing to do after 0015 skipped its indexes"
    )


def test_commitment_columns_probe_sees_a_migration_applied_after_the_first_probe():
    db._TABLE_COLUMNS_CACHE.clear()
    before = {"id", "text"}
    after = before | {"origin", "owner_label"}

    class Cur:
        cols = before

        def execute(self, sql, params=()):
            self._r = [(c,) for c in Cur.cols]

        def fetchall(self):
            return self._r

    cur = Cur()
    assert commitments._owner_columns_ready(cur) is False
    Cur.cols = after  # `khipu migrate` ran 0033 while this process kept running
    try:
        assert commitments._owner_columns_ready(cur) is True, (
            "stale column cache: origin/owner_label omitted from inserts"
        )
    finally:
        db._TABLE_COLUMNS_CACHE.clear()


def test_episode_schema_flags_see_columns_added_after_the_first_probe():
    """Long-lived process: table_columns cached a pre-0008/0010 episodes shape."""
    from khipu import embed

    db._TABLE_COLUMNS_CACHE.clear()
    before = {"id", "summary", "session_id"}
    after = before | {"project", "deleted_at", "harness", "parent_session_id"}

    class Cur:
        cols = before

        def execute(self, sql, params=()):
            self._r = [(c,) for c in Cur.cols]

        def fetchall(self):
            return self._r

    cur = Cur()
    first = embed._episode_schema_flags(cur)
    assert first == {
        "project": False,
        "deleted_at": False,
        "harness": False,
        "parent_session_id": False,
    }
    Cur.cols = after
    try:
        second = embed._episode_schema_flags(cur)
        assert second == {
            "project": True,
            "deleted_at": True,
            "harness": True,
            "parent_session_id": True,
        }, f"stale episode column cache after migrate: {second}"
    finally:
        db._TABLE_COLUMNS_CACHE.clear()


def test_literal_trgm_ready_probe_rolls_back_on_error():
    """A failed catalog probe must not leave the caller's transaction aborted."""

    class Conn:
        def __init__(self):
            self.rolled_back = False

        def rollback(self):
            self.rolled_back = True

    class Cur:
        def __init__(self, conn):
            self.connection = conn

        def execute(self, sql, params=()):
            raise RuntimeError("catalog unavailable")

        def fetchone(self):
            return None

        def fetchall(self):
            return []

    conn = Conn()
    assert migrate._literal_trgm_indexes_ready(Cur(conn)) is True
    assert conn.rolled_back is True, "probe exception left the transaction aborted"
