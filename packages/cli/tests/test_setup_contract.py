# --bypass-harness (sonnet lane): authored by a dispatched on-sub
# Agent(model="sonnet") subagent for the "gaps become oracles" layer 1 of
# docs/plans/2026-09-05-setup-that-cannot-strand-you.md — pure unit tests,
# no Docker, no live hub. See test_setup_live.py for layer 2.
"""Contract tests for khipu.setup / khipu.dbmove / the ``khipu db`` CLI face.

These encode the promises the plan's "gaps become oracles" section makes:
every stage is always reported (never silently dropped), every failure a
human can hit carries plain words (never a bare code), the password never
leaks, the pipeline actually wires ``ensure_scheduled_jobs``/``run_probe``
through rather than stubbing them into no-ops, and the CLI's exit codes are
the documented 0/1/2 contract with JSON output throughout.
"""

from __future__ import annotations

import ast
import io
import json
import re
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from khipu import cli, setup

_SETUP_PY = Path(__file__).resolve().parents[1] / "khipu" / "setup.py"


def _ok_stub(stage_id: str) -> dict:
    return {"id": stage_id, "ok": True, "title": "ok", "detail": "ok"}


def _fail_stub(stage_id: str) -> dict:
    return {"id": stage_id, "ok": False, "title": "t", "detail": "d", "fix": "Check it."}


class _ReachThroughGraphPatches:
    """Patches ``_connect``.._stage_graph and ``_stage_summary`` to succeed,
    leaving ``_stage_store``/``_stage_upkeep``/``_stage_prove`` as the REAL
    code so their own downstream calls (``keychain.set_dsn``,
    ``launchd_gen.ensure_scheduled_jobs``, ``probe.run_probe``) can be
    asserted directly — mocking those three stage functions themselves would
    hide exactly the wiring this test exists to prove.
    """

    def __enter__(self):
        self._patches = [
            mock.patch.object(setup, "_connect", return_value=mock.Mock()),
            mock.patch.object(setup, "_stage_version", return_value=_ok_stub("version")),
            mock.patch.object(setup, "_stage_privileges", return_value=_ok_stub("privileges")),
            mock.patch.object(setup, "_stage_schema", return_value=_ok_stub("schema")),
            mock.patch.object(setup, "_stage_graph", return_value=_ok_stub("graph")),
            mock.patch.object(
                setup, "_stage_summary",
                return_value=(_ok_stub("summary"), {"host": "h", "database": "d"}),
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._patches:
            p.stop()
        return False


class EveryStageIsReportedTest(unittest.TestCase):
    """Every id in setup.STAGE_ORDER shows up in the result, full stop —
    whether the pipeline fails at the very first stage or sails through."""

    def test_fully_failing_pipeline_reports_every_stage(self) -> None:
        with mock.patch.object(setup, "_connect", side_effect=RuntimeError("connection refused")):
            out = setup.connect_database("postgres://u@h/d")
        self.assertFalse(out["ok"])
        ids = [s["id"] for s in out["stages"]]
        self.assertEqual(ids, list(setup.STAGE_ORDER))
        self.assertFalse(out["stages"][0]["ok"])
        for later in out["stages"][1:]:
            self.assertEqual(later.get("status"), "skipped")

    def test_fully_passing_pipeline_reports_every_stage(self) -> None:
        with mock.patch.object(setup, "_connect", return_value=mock.Mock()), \
             mock.patch.object(setup, "_stage_version", return_value=_ok_stub("version")), \
             mock.patch.object(setup, "_stage_privileges", return_value=_ok_stub("privileges")), \
             mock.patch.object(setup, "_stage_schema", return_value=_ok_stub("schema")), \
             mock.patch.object(setup, "_stage_graph", return_value=_ok_stub("graph")), \
             mock.patch.object(setup, "_stage_store", return_value=_ok_stub("store")), \
             mock.patch.object(setup, "_stage_upkeep", return_value=_ok_stub("upkeep")), \
             mock.patch.object(setup, "_stage_prove", return_value=_ok_stub("prove")), \
             mock.patch.object(
                 setup, "_stage_summary",
                 return_value=(_ok_stub("summary"), {"host": "h", "database": "d"}),
             ):
            out = setup.connect_database("postgres://u@h/d")
        self.assertTrue(out["ok"])
        ids = [s["id"] for s in out["stages"]]
        self.assertEqual(ids, list(setup.STAGE_ORDER))
        self.assertTrue(all(s.get("ok") for s in out["stages"]))


_REPRESENTATIVE_MESSAGES = [
    'could not translate host name "bad.example" to address: '
    "nodename nor servname provided, or not known",
    'connection to server at "10.0.0.5", port 5432 failed: Connection refused',
    "timeout expired",
    'FATAL:  password authentication failed for user "khipu"',
    'FATAL:  database "khipu_prod" does not exist',
    "SSL error: certificate verify failed: unable to get local issuer certificate",
    "self signed certificate in certificate chain",
    'server does not support SSL, but SSL was required',
    "permission denied for schema public",
    'must be superuser to create extension "vector"',
    "server version too old",
]

_VERB_ALLOWLIST = (
    "Check", "Double", "Ask", "Create", "Paste", "Upgrade", "Try", "Allow",
    "Make", "Open", "Run", "Add", "Use",
)
_VERB_RE = re.compile(r"^(" + "|".join(_VERB_ALLOWLIST) + r")\b")
_UNDERSCORE_CODE_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")


class ExplainConnectionErrorContractTest(unittest.TestCase):
    def test_every_representative_message_gets_plain_words(self) -> None:
        for msg in _REPRESENTATIVE_MESSAGES:
            with self.subTest(msg=msg):
                title, detail, fix = setup.explain_connection_error(RuntimeError(msg))
                self.assertTrue(title.strip())
                self.assertTrue(detail.strip())
                self.assertTrue(fix.strip())
                self.assertNotIn("Traceback (most recent call last)", title)
                self.assertNotIn("Traceback (most recent call last)", detail)
                self.assertNotIn("Traceback (most recent call last)", fix)
                self.assertIsNone(
                    _UNDERSCORE_CODE_RE.search(title),
                    f"title looks like a raw code: {title!r}",
                )
                self.assertIsNone(
                    _UNDERSCORE_CODE_RE.search(fix),
                    f"fix looks like a raw code: {fix!r}",
                )
                self.assertTrue(
                    _VERB_RE.match(fix.strip()),
                    f"fix does not open with an allowed verb: {fix!r}",
                )


class MaskDsnNeverLeaksTest(unittest.TestCase):
    def test_userpass_at_host(self) -> None:
        masked = setup.mask_dsn("postgresql://myuser:supersecret@db.example.com:5432/khipu")
        self.assertNotIn("supersecret", masked)

    def test_percent_encoded_password(self) -> None:
        masked = setup.mask_dsn("postgresql://myuser:p%40ss%2Fw0rd@db.example.com:5432/khipu")
        self.assertNotIn("p%40ss%2Fw0rd", masked)
        self.assertNotIn("p@ss/w0rd", masked)

    def test_dsn_without_password(self) -> None:
        masked = setup.mask_dsn("postgresql://myuser@db.example.com:5432/khipu")
        self.assertNotIn("supersecret", masked)
        self.assertIn("myuser@db.example.com", masked)

    def test_summary_stage_never_contains_the_password(self) -> None:
        password = "n0-1eak-pls"
        dsn = f"postgresql://myuser:{password}@db.example.com:5432/khipu"
        conn = mock.Mock()
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.side_effect = [(3,), (1,)]
        conn.cursor.return_value = cur
        entry, summary = setup._stage_summary(conn, dsn)
        blob = json.dumps({"entry": entry, "summary": summary})
        self.assertNotIn(password, blob)


class UpkeepAndProveWiringTest(unittest.TestCase):
    """The pipeline must actually call ``ensure_scheduled_jobs``/``run_probe``
    on success, and must call NEITHER of those (nor ``keychain.set_dsn``)
    when the caller asked for a preflight (store/install_jobs/prove all
    off) — the exact shape ``khipu db preflight`` uses."""

    def test_success_pipeline_calls_ensure_scheduled_jobs_and_run_probe(self) -> None:
        with _ReachThroughGraphPatches(), \
             mock.patch("khipu.keychain.set_dsn") as set_dsn, \
             mock.patch(
                 "khipu.launchd_gen.ensure_scheduled_jobs",
                 return_value={"ok": True, "installed": ["nightly"], "refreshed": [],
                               "current": [], "external": []},
             ) as ensure_jobs, \
             mock.patch("khipu.config.capture_mode", return_value="dual"), \
             mock.patch("khipu.keychain.get_gemini_key", return_value="fake-key"), \
             mock.patch(
                 "khipu.probe.run_probe",
                 return_value={"ok": True, "seconds": 0.02, "episode_id": 1},
             ) as run_probe:
            out = setup.connect_database(
                "postgres://u@h/d", store=True, install_jobs=True, prove=True
            )
        self.assertTrue(out["ok"], out)
        set_dsn.assert_called_once()
        ensure_jobs.assert_called_once()
        run_probe.assert_called_once_with("app")

    def test_preflight_shape_calls_neither_set_dsn_nor_ensure_scheduled_jobs(self) -> None:
        with _ReachThroughGraphPatches(), \
             mock.patch("khipu.keychain.set_dsn") as set_dsn, \
             mock.patch("khipu.launchd_gen.ensure_scheduled_jobs") as ensure_jobs, \
             mock.patch("khipu.probe.run_probe") as run_probe:
            out = setup.connect_database(
                "postgres://u@h/d", store=False, install_jobs=False, prove=False
            )
        self.assertTrue(out["ok"], out)
        set_dsn.assert_not_called()
        ensure_jobs.assert_not_called()
        run_probe.assert_not_called()


def _stdout_json(argv: list[str]) -> tuple[int, dict]:
    out = io.StringIO()
    err = io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    return code, json.loads(out.getvalue())


class DbCliExitCodesTest(unittest.TestCase):
    def test_preflight_ok_is_exit_0(self) -> None:
        with mock.patch(
            "khipu.setup.connect_database",
            return_value={"ok": True, "stages": [], "summary": {}},
        ):
            code, payload = _stdout_json(["db", "preflight", "--dsn", "postgres://u@h/d"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_preflight_failed_stage_is_exit_1(self) -> None:
        with mock.patch(
            "khipu.setup.connect_database",
            return_value={"ok": False, "stages": [_fail_stub("reach")], "summary": {}},
        ):
            code, payload = _stdout_json(["db", "preflight", "--dsn", "postgres://u@h/d"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_preflight_missing_dsn_is_exit_2(self) -> None:
        with mock.patch.dict("os.environ", {"KHIPU_DB_DSN": ""}):
            code, payload = _stdout_json(["db", "preflight"])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "dsn_required")

    def test_connect_ok_is_exit_0(self) -> None:
        with mock.patch(
            "khipu.setup.connect_database",
            return_value={"ok": True, "stages": [], "summary": {}},
        ):
            code, payload = _stdout_json(["db", "connect", "--dsn", "postgres://u@h/d"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_connect_failure_is_exit_1(self) -> None:
        with mock.patch(
            "khipu.setup.connect_database",
            return_value={"ok": False, "stages": [_fail_stub("version")], "summary": {}},
        ):
            code, payload = _stdout_json(["db", "connect", "--dsn", "postgres://u@h/d"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_connect_missing_dsn_is_exit_2(self) -> None:
        with mock.patch.dict("os.environ", {"KHIPU_DB_DSN": ""}):
            code, payload = _stdout_json(["db", "connect"])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "dsn_required")

    def test_move_ok_is_exit_0(self) -> None:
        with mock.patch(
            "khipu.dbmove.move_database",
            return_value={"ok": True, "tables": [], "switched": True},
        ):
            code, payload = _stdout_json(["db", "move", "--to", "postgres://u@h/d"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])

    def test_move_failure_is_exit_1(self) -> None:
        with mock.patch(
            "khipu.dbmove.move_database",
            return_value={"ok": False, "error": "row_count_mismatch"},
        ):
            code, payload = _stdout_json(["db", "move", "--to", "postgres://u@h/d"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_move_missing_to_is_exit_2(self) -> None:
        with mock.patch.dict("os.environ", {"KHIPU_DB_TARGET_DSN": ""}):
            code, payload = _stdout_json(["db", "move"])
        self.assertEqual(code, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "dsn_required")

    def test_status_no_dsn_configured_is_exit_1(self) -> None:
        with mock.patch("khipu.db.dsn_configured", return_value=False):
            code, payload = _stdout_json(["db", "status"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_status_ok_is_exit_0(self) -> None:
        conn = mock.MagicMock()
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.fetchone.side_effect = [(5,), (2,)]
        conn.__enter__.return_value = conn
        conn.cursor.return_value = cur
        with mock.patch("khipu.db.dsn_configured", return_value=True), \
             mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@h/d"), \
             mock.patch("khipu.db.connect", return_value=conn):
            code, payload = _stdout_json(["db", "status"])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["episodes"], 5)
        self.assertEqual(payload["topics"], 2)

    def test_status_connect_failure_is_exit_1(self) -> None:
        with mock.patch("khipu.db.dsn_configured", return_value=True), \
             mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@h/d"), \
             mock.patch("khipu.db.connect", side_effect=RuntimeError("connection refused")):
            code, payload = _stdout_json(["db", "status"])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_unknown_db_subcommand_is_a_usage_error(self) -> None:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                cli.main(["db", "bogus"])
        self.assertEqual(ctx.exception.code, 2)


class NoRawCodeLeakGuardTest(unittest.TestCase):
    """The desktop can only show what the engine hands it — so no dict
    literal keyed "fix"/"title"/"detail" in setup.py may be a bare
    underscore_joined code with nothing else in it. Static, not a runtime
    sample: this is the guard the plan's "gaps become oracles" section
    calls out as "the desktop's raw-code leak cannot return from the
    engine side"."""

    def test_no_literal_fix_title_or_detail_is_a_bare_code(self) -> None:
        source = _SETUP_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(_SETUP_PY))
        bare_code_re = re.compile(r"^[a-z_]+$")
        offenders: list[str] = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key_node, value_node in zip(node.keys, node.values):
                if not (isinstance(key_node, ast.Constant) and key_node.value in
                        ("fix", "title", "detail")):
                    continue
                literal = _literal_text(value_node)
                if literal is not None and bare_code_re.match(literal):
                    offenders.append(f"{key_node.value}={literal!r} (line {key_node.lineno})")

        self.assertEqual(offenders, [], f"raw-code-shaped literals found: {offenders}")


def _literal_text(node: ast.expr) -> str | None:
    """Best-effort static text for a Constant string or an f-string's
    literal segments (ignoring interpolated parts, which can only ADD
    characters — never turn a non-bare-code literal into one, and a
    formatted value can never by itself make an otherwise-plain-English
    literal collapse to ``^[a-z_]+$``)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = [p.value for p in node.values if isinstance(p, ast.Constant)]
        return "".join(parts) if parts else None
    return None


if __name__ == "__main__":
    unittest.main()


class SchemaStageNeedsMigrationFilesTest(unittest.TestCase):
    def test_no_migration_files_is_a_loud_failure_not_current(self):
        from unittest import mock

        from khipu import setup as st

        with mock.patch("khipu.migrate.available", return_value=[]), \
                mock.patch("khipu.migrate.run", side_effect=AssertionError("must not run")):
            out = st._stage_schema(conn=object())
        self.assertFalse(out["ok"])
        self.assertIn("schema files are missing", out["title"])
        self.assertTrue(out["fix"].startswith("Reinstall"))
