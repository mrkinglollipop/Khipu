"""Doctor's schema check: red while the hub lacks a migration this code ships
(0028 sat unapplied on the hub on 2026-10-16 with doctor green), a warning
for migrations the hub has that the code does not know. The connection is
faked; no database is opened.
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from khipu import cli, migrate

from tests.test_cli_recall_quality import _green_doctor_patches


class _Cur:
    def __init__(self, applied):
        self.applied = set(applied)
        self._rows = []

    def execute(self, sql, params=None):
        if "to_regclass" in sql:
            self._rows = [(True,)]
        elif sql.strip().startswith("SELECT version"):
            self._rows = [(v,) for v in sorted(self.applied)]
        else:
            raise AssertionError(f"doctor must only read: {sql}")

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, applied):
        self.cur = _Cur(applied)

    def cursor(self):
        return self.cur


def _dir(names):
    d = Path(tempfile.mkdtemp())
    for n in names:
        (d / f"{n}.sql").write_text("SELECT 1;\n")
    return d


class DoctorCheckTest(unittest.TestCase):
    def test_pending_migration_is_red_with_its_name_in_plain_words(self):
        d = _dir(["0027_a", "0028_ledger"])
        block = migrate.doctor_check(_Conn({"0027_a"}), d)
        self.assertFalse(block["ok"])
        self.assertEqual(block["pending"], ["0028_ledger"])
        self.assertEqual(
            block["reason"],
            "The memory database needs an update this version of Khipu expects: 0028_ledger",
        )
        self.assertEqual(block["warnings"], [])

    def test_up_to_date_hub_is_green(self):
        d = _dir(["0027_a", "0028_ledger"])
        block = migrate.doctor_check(_Conn({"0027_a", "0028_ledger"}), d)
        self.assertTrue(block["ok"])
        self.assertEqual(block["pending"], [])

    def test_migration_newer_than_the_code_is_a_warning_not_red(self):
        d = _dir(["0027_a"])
        block = migrate.doctor_check(_Conn({"0027_a", "0030_future"}), d)
        self.assertTrue(block["ok"])
        self.assertEqual(block["unknown_applied"], ["0030_future"])
        self.assertEqual(len(block["warnings"]), 1)
        self.assertIn("0030_future", block["warnings"][0])

    def test_no_database_configured_is_a_skip(self):
        with mock.patch("khipu.db.dsn_configured", return_value=False):
            block = migrate.doctor_check()
        self.assertTrue(block["ok"])
        self.assertIn("skipped", block)

    def test_a_failed_read_is_red_not_a_pass(self):
        with mock.patch("khipu.db.dsn_configured", return_value=True), \
             mock.patch("khipu.db.connect", side_effect=OSError("refused")):
            block = migrate.doctor_check()
        self.assertFalse(block["ok"])
        self.assertIn("refused", block["error"])


_PENDING = {"ok": False, "pending": ["0028_ledger"], "unknown_applied": [], "warnings": [],
            "reason": "The memory database needs an update this version of Khipu expects: 0028_ledger"}
_NEWER = {"ok": True, "pending": [], "unknown_applied": ["0030_future"],
          "warnings": ["The memory database has updates newer than this version of Khipu: 0030_future"]}


class QuickDoctorTest(unittest.TestCase):
    def _run(self, schema):
        local = {k: True for k in cli.LOCAL_GATES}
        local["secrets"] = {"dsn": True}
        args = cli.build_parser().parse_args(["doctor", "--quick"])
        with mock.patch.object(cli, "_doctor_local_checks", return_value=local), \
             mock.patch("khipu.migrate.doctor_check", return_value=schema), \
             mock.patch("khipu.config.load_config", return_value={}):
            with redirect_stdout(io.StringIO()) as out:
                code = cli.cmd_doctor(args)
        return code, json.loads(out.getvalue())

    def test_quick_doctor_is_red_while_a_migration_is_pending(self):
        code, out = self._run(_PENDING)
        self.assertEqual(code, 2)
        self.assertFalse(out["ok"])
        self.assertFalse(out["schema_migrations_ok"])
        self.assertEqual(out["schema_migrations"]["pending"], ["0028_ledger"])

    def test_quick_doctor_warns_but_stays_green_for_a_newer_hub(self):
        code, out = self._run(_NEWER)
        self.assertEqual(code, 0, out)
        self.assertTrue(out["schema_migrations_ok"])
        self.assertIn("0030_future", out["warnings"][0])


class FullDoctorTest(unittest.TestCase):
    def setUp(self):
        for p in _green_doctor_patches():
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}),
                  mock.patch("khipu.drift.recall_quality", return_value={})):
            p.start()
            self.addCleanup(p.stop)

    def _run(self, schema):
        args = cli.build_parser().parse_args(["doctor"])
        with mock.patch("khipu.migrate.doctor_check", return_value=schema):
            with redirect_stdout(io.StringIO()) as out:
                cli.cmd_doctor(args)
        return json.loads(out.getvalue())

    def test_full_doctor_is_red_while_a_migration_is_pending(self):
        out = self._run(_PENDING)
        self.assertFalse(out["ok"])
        self.assertFalse(out["schema_migrations_ok"])
        self.assertEqual(out["schema_migrations"]["pending"], ["0028_ledger"])

    def test_full_doctor_carries_the_newer_hub_warning(self):
        out = self._run(_NEWER)
        self.assertTrue(out["schema_migrations_ok"])
        self.assertTrue(any("0030_future" in w for w in out["warnings"]))
