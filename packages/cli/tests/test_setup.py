# --bypass-harness (sonnet lane): authored by a dispatched on-sub
# Agent(model="sonnet") subagent for Phase 1 of docs/plans/2026-09-05-
# setup-that-cannot-strand-you.md.
"""khipu.setup — the one connect pipeline shared by the CLI and the desktop
Database step. Fake connections throughout; no live database except the one
Live check the phase brief calls out separately (test_cli.py / manual run).
"""

from __future__ import annotations

import unittest
from unittest import mock

from khipu import setup


class MaskDsnTest(unittest.TestCase):
    def test_password_never_appears(self) -> None:
        dsn = "postgresql://myuser:supersecret@db.example.com:5432/khipu"
        masked = setup.mask_dsn(dsn)
        self.assertNotIn("supersecret", masked)
        self.assertIn("myuser@db.example.com:5432", masked)
        self.assertIn("khipu", masked)

    def test_unparseable_dsn_is_masked_not_raised(self) -> None:
        self.assertEqual(setup.mask_dsn("not a dsn at all"), "***")

    def test_no_password_case_still_masks_cleanly(self) -> None:
        masked = setup.mask_dsn("postgresql://host:5432/db")
        self.assertEqual(masked, "postgres://host:5432/db")


class HostKindTest(unittest.TestCase):
    def test_localhost_is_this_mac(self) -> None:
        self.assertEqual(setup.host_kind("postgresql://u@127.0.0.1:5432/d"), "this-mac")
        self.assertEqual(setup.host_kind("postgresql://u@localhost:5432/d"), "this-mac")

    def test_docker_bridge_address_is_local_docker(self) -> None:
        self.assertEqual(setup.host_kind("postgresql://u@172.17.0.2:5432/d"), "local-docker")

    def test_named_host_is_remote(self) -> None:
        self.assertEqual(setup.host_kind("postgresql://u@db.example.com:5432/d"), "remote")


class ExplainConnectionErrorTest(unittest.TestCase):
    def _fix_and_title(self, text: str) -> tuple[str, str]:
        title, detail, fix = setup.explain_connection_error(RuntimeError(text))
        self.assertEqual(detail, text)
        self.assertTrue(fix)
        return title, fix

    def test_host_not_found(self) -> None:
        title, fix = self._fix_and_title(
            'could not translate host name "bad.example" to address: nodename nor servname provided, or not known'
        )
        self.assertIn("could not find", title.lower())
        self.assertIn("host", fix.lower())

    def test_connection_refused(self) -> None:
        title, fix = self._fix_and_title(
            'connection to server at "10.0.0.5", port 5432 failed: Connection refused'
        )
        self.assertIn("could not reach", title.lower())
        self.assertIn("host and port", fix.lower())

    def test_timeout(self) -> None:
        title, fix = self._fix_and_title("connection to server timed out")
        self.assertIn("could not reach", title.lower())

    def test_timeout_expired_wording(self) -> None:
        title, fix = self._fix_and_title("timeout expired")
        self.assertIn("could not reach", title.lower())

    def test_password_authentication_failed(self) -> None:
        title, fix = self._fix_and_title(
            'FATAL:  password authentication failed for user "khipu"'
        )
        self.assertIn("username or password", title.lower())
        self.assertIn("password", fix.lower())

    def test_database_does_not_exist(self) -> None:
        title, fix = self._fix_and_title('FATAL:  database "khipu_prod" does not exist')
        self.assertIn("does not exist", title.lower())
        self.assertIn("create database", fix.lower())

    def test_certificate_verify_failed(self) -> None:
        title, fix = self._fix_and_title(
            "SSL error: certificate verify failed: unable to get local issuer certificate"
        )
        self.assertIn("certificate", title.lower())
        self.assertIn("certificate", fix.lower())

    def test_sslrootcert_missing_file(self) -> None:
        title, fix = self._fix_and_title(
            'root certificate file "/Users/matthewsc" does not exist'
        )
        self.assertIn("certificate", title.lower())

    def test_sslmode_not_supported(self) -> None:
        title, fix = self._fix_and_title(
            'server does not support SSL, but SSL was required'
        )
        self.assertIn("does not support", title.lower())
        self.assertIn("sslmode", fix.lower())

    def test_insufficient_privilege(self) -> None:
        title, fix = self._fix_and_title('permission denied for database "khipu"')
        self.assertIn("privileges", title.lower())
        self.assertIn("grant", fix.lower())

    def test_unknown_error_still_gets_a_fix(self) -> None:
        title, detail, fix = setup.explain_connection_error(RuntimeError("something weird"))
        self.assertTrue(title)
        self.assertTrue(fix)
        self.assertIn("something weird", detail)


class _FakeCur:
    """Scripted cursor: an ordered list of (sql_prefix, value_or_exception).
    ``execute`` matches the first unconsumed entry whose prefix the stripped
    SQL starts with, pops it, and either raises or stashes the value for the
    next ``fetchone``/``fetchall``. Unmatched SQL (e.g. a plain SAVEPOINT)
    stashes ``None`` and is otherwise a no-op — exactly what the code needs.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[str] = []
        self._last = None

    def execute(self, sql, params=None):
        stripped = " ".join(sql.split())
        self.calls.append(stripped)
        for i, (prefix, val) in enumerate(self.script):
            if stripped.startswith(prefix):
                self.script.pop(i)
                if isinstance(val, BaseException):
                    raise val
                self._last = val
                return
        self._last = None

    def fetchone(self):
        return self._last

    def fetchall(self):
        return [] if self._last is None else [self._last]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class StageVersionTest(unittest.TestCase):
    def test_old_server_fails_with_plain_words(self) -> None:
        cur = _FakeCur(
            [
                ("SHOW server_version_num", (160000,)),
                ("SELECT current_setting('server_version')", ("16.4",)),
            ]
        )
        out = setup._stage_version(_FakeConn(cur))
        self.assertFalse(out["ok"])
        self.assertIn("16.4", out["detail"])
        self.assertIn("19", out["detail"])
        self.assertTrue(out["fix"])

    def test_pg19_passes(self) -> None:
        cur = _FakeCur(
            [
                ("SHOW server_version_num", (190001,)),
                ("SELECT current_setting('server_version')", ("19beta3",)),
            ]
        )
        out = setup._stage_version(_FakeConn(cur))
        self.assertTrue(out["ok"])


class StagePrivilegesTest(unittest.TestCase):
    def _script(self, *, can_create=True, vector_available=True, create_ext_error=None):
        script = [
            ("SELECT current_database()", ("mydb",)),
            ("SELECT has_schema_privilege", (can_create,)),
        ]
        if not can_create:
            return script
        script.append(("SELECT 1 FROM pg_available_extensions", (1,) if vector_available else None))
        if not vector_available:
            return script
        script.append(("SAVEPOINT khipu_probe_ext", None))
        script.append(("CREATE EXTENSION IF NOT EXISTS vector", create_ext_error))
        if create_ext_error is not None:
            script.append(("ROLLBACK TO SAVEPOINT khipu_probe_ext", None))
        return script

    def test_no_create_privilege(self) -> None:
        conn = _FakeConn(_FakeCur(self._script(can_create=False)))
        out = setup._stage_privileges(conn)
        self.assertFalse(out["ok"])
        self.assertIn("cannot create tables", out["title"].lower())
        self.assertEqual(conn.rollbacks, 1)

    def test_vector_not_available_on_server(self) -> None:
        conn = _FakeConn(_FakeCur(self._script(vector_available=False)))
        out = setup._stage_privileges(conn)
        self.assertFalse(out["ok"])
        self.assertIn("vector extension is not available", out["title"].lower())
        self.assertEqual(conn.rollbacks, 1)

    def test_insufficient_privilege_to_create_extension_has_the_documented_fix(self) -> None:
        class InsufficientPrivilege(Exception):
            pass

        err = InsufficientPrivilege('permission denied to create extension "vector"')
        conn = _FakeConn(_FakeCur(self._script(create_ext_error=err)))
        out = setup._stage_privileges(conn)
        self.assertFalse(out["ok"])
        self.assertIn("CREATE EXTENSION vector", out["fix"])
        self.assertIn("mydb", out["fix"])
        self.assertIn("managed providers", out["fix"])
        self.assertEqual(conn.rollbacks, 1)

    def test_success_commits(self) -> None:
        conn = _FakeConn(_FakeCur(self._script()))
        out = setup._stage_privileges(conn)
        self.assertTrue(out["ok"])
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)


class StageSchemaTest(unittest.TestCase):
    def test_pending_after_a_real_run_is_a_failure(self) -> None:
        with mock.patch("khipu.migrate.run", return_value={"pending": ["0002_x"], "ran": []}):
            out = setup._stage_schema(object())
        self.assertFalse(out["ok"])
        self.assertIn("0002_x", out["detail"])

    def test_nothing_pending_is_current(self) -> None:
        with mock.patch("khipu.migrate.run", return_value={"pending": [], "ran": ["0001_x"]}):
            out = setup._stage_schema(object())
        self.assertTrue(out["ok"])
        self.assertEqual(out["ran"], ["0001_x"])


class StageGraphTest(unittest.TestCase):
    def test_failure_reports_plain_words_and_rolls_back(self) -> None:
        conn = _FakeConn(_FakeCur([]))
        with mock.patch(
            "khipu.components_postgres.probe_graph_table",
            return_value={"ok": False, "error": "boom"},
        ):
            out = setup._stage_graph(conn)
        self.assertFalse(out["ok"])
        self.assertEqual(out["detail"], "boom")
        self.assertEqual(conn.rollbacks, 1)

    def test_success(self) -> None:
        conn = _FakeConn(_FakeCur([]))
        with mock.patch(
            "khipu.components_postgres.probe_graph_table", return_value={"ok": True}
        ):
            out = setup._stage_graph(conn)
        self.assertTrue(out["ok"])


_ALL_STAGES = (
    "reach",
    "version",
    "privileges",
    "schema",
    "graph",
    "store",
    "upkeep",
    "prove",
    "summary",
)


def _ok_stub(stage_id):
    return {"id": stage_id, "ok": True, "title": "ok", "detail": "ok"}


class ConnectDatabasePipelineTest(unittest.TestCase):
    """Stage ordering and stop-at-first-failure, every stage function mocked
    so this never touches real SQL — that is covered per-stage above."""

    def _patch_all(self, **overrides):
        conn = mock.Mock()
        patches = {
            "_connect": mock.patch.object(setup, "_connect", return_value=conn),
            "_stage_version": mock.patch.object(setup, "_stage_version", return_value=_ok_stub("version")),
            "_stage_privileges": mock.patch.object(setup, "_stage_privileges", return_value=_ok_stub("privileges")),
            "_stage_schema": mock.patch.object(setup, "_stage_schema", return_value=_ok_stub("schema")),
            "_stage_graph": mock.patch.object(setup, "_stage_graph", return_value=_ok_stub("graph")),
            "_stage_store": mock.patch.object(setup, "_stage_store", return_value=_ok_stub("store")),
            "_stage_upkeep": mock.patch.object(setup, "_stage_upkeep", return_value=_ok_stub("upkeep")),
            "_stage_prove": mock.patch.object(setup, "_stage_prove", return_value=_ok_stub("prove")),
            "_stage_summary": mock.patch.object(
                setup, "_stage_summary", return_value=(_ok_stub("summary"), {"host": "h", "database": "d"})
            ),
        }
        for name, value in overrides.items():
            patches[name] = mock.patch.object(setup, name, **value)
        started = [p.start() for p in patches.values()]
        self.addCleanup(lambda: [p.stop() for p in patches.values()])
        return conn, {name: started[i] for i, name in enumerate(patches)}

    def test_all_stages_succeed_in_order(self) -> None:
        conn, _ = self._patch_all()
        out = setup.connect_database("postgres://u@h/d")
        self.assertTrue(out["ok"])
        self.assertEqual([s["id"] for s in out["stages"]], list(_ALL_STAGES))
        self.assertTrue(all(s.get("ok") for s in out["stages"]))
        self.assertEqual(out["summary"], {"host": "h", "database": "d"})
        self.assertTrue(conn.close.called)

    def test_version_failure_skips_everything_after(self) -> None:
        conn, _ = self._patch_all(
            _stage_version={"return_value": {"id": "version", "ok": False, "title": "t", "detail": "d", "fix": "f"}}
        )
        out = setup.connect_database("postgres://u@h/d")
        self.assertFalse(out["ok"])
        ids = [s["id"] for s in out["stages"]]
        self.assertEqual(ids, list(_ALL_STAGES))
        statuses = {s["id"]: s for s in out["stages"]}
        self.assertFalse(statuses["version"]["ok"])
        for later in ("privileges", "schema", "graph", "store", "upkeep", "prove", "summary"):
            self.assertEqual(statuses[later].get("status"), "skipped")
        self.assertTrue(conn.close.called)

    def test_reach_failure_is_mapped_through_explain_connection_error(self) -> None:
        patches = [
            mock.patch.object(setup, "_connect", side_effect=RuntimeError("connection refused")),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        out = setup.connect_database("postgres://u@h/d")
        self.assertFalse(out["ok"])
        reach = out["stages"][0]
        self.assertEqual(reach["id"], "reach")
        self.assertFalse(reach["ok"])
        self.assertIn("could not reach", reach["title"].lower())
        self.assertEqual(len(out["stages"]), len(_ALL_STAGES))
        self.assertTrue(all(s.get("status") == "skipped" for s in out["stages"][1:]))

    def test_store_false_skips_store_but_continues(self) -> None:
        conn, mocks = self._patch_all()
        out = setup.connect_database("postgres://u@h/d", store=False)
        self.assertTrue(out["ok"])
        statuses = {s["id"]: s for s in out["stages"]}
        self.assertEqual(statuses["store"].get("status"), "skipped")
        self.assertIn("store=False", statuses["store"]["detail"])
        mocks["_stage_store"].assert_not_called()
        self.assertEqual(statuses["upkeep"]["ok"], True)
        self.assertNotEqual(statuses["upkeep"].get("status"), "skipped")

    def test_install_jobs_false_skips_only_upkeep(self) -> None:
        conn, mocks = self._patch_all()
        out = setup.connect_database("postgres://u@h/d", install_jobs=False)
        self.assertTrue(out["ok"])
        statuses = {s["id"]: s for s in out["stages"]}
        self.assertEqual(statuses["upkeep"].get("status"), "skipped")
        mocks["_stage_upkeep"].assert_not_called()
        self.assertNotEqual(statuses["prove"].get("status"), "skipped")

    def test_prove_false_skips_only_prove(self) -> None:
        conn, mocks = self._patch_all()
        out = setup.connect_database("postgres://u@h/d", prove=False)
        self.assertTrue(out["ok"])
        statuses = {s["id"]: s for s in out["stages"]}
        self.assertEqual(statuses["prove"].get("status"), "skipped")
        mocks["_stage_prove"].assert_not_called()
        self.assertNotEqual(statuses["summary"].get("status"), "skipped")

    def test_preflight_shape_store_and_jobs_and_prove_all_off(self) -> None:
        conn, mocks = self._patch_all()
        out = setup.connect_database(
            "postgres://u@h/d", store=False, install_jobs=False, prove=False
        )
        self.assertTrue(out["ok"])
        statuses = {s["id"]: s for s in out["stages"]}
        for stage_id in ("store", "upkeep", "prove"):
            self.assertEqual(statuses[stage_id].get("status"), "skipped")
        # reach..graph still ran for real (mocked, but called).
        for stage_id in ("version", "privileges", "schema", "graph"):
            self.assertTrue(statuses[stage_id]["ok"])


class StageProveModelKeyTest(unittest.TestCase):
    def test_legacy_capture_mode_is_skipped_not_failed(self) -> None:
        with mock.patch("khipu.config.capture_mode", return_value="legacy"):
            out = setup._stage_prove()
        self.assertTrue(out["ok"])
        self.assertEqual(out.get("status"), "skipped")
        self.assertIn("legacy", out["detail"].lower())

    def test_no_model_key_is_skipped_with_a_fix_pointing_at_settings(self) -> None:
        with mock.patch("khipu.config.capture_mode", return_value="dual"), \
             mock.patch("khipu.keychain.get_gemini_key", return_value=None), \
             mock.patch("khipu.keychain.get_openai_compat_key", return_value=None):
            out = setup._stage_prove()
        self.assertTrue(out["ok"])
        self.assertEqual(out.get("status"), "skipped")
        self.assertIn("Settings", out["fix"])


if __name__ == "__main__":
    unittest.main()
