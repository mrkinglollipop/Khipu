"""Hermetic regressions for audit1010 hubb findings."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from unittest import mock

from khipu import dbmove
from khipu.components_backup import _dump_live_db
from khipu.components_postgres import check_remote_postgres, install_local_postgres, upgrade_postgres


class _Copy:
    def __init__(self, db, table, writing):
        self.db, self.table, self.writing = db, table, writing
        self.blocks = 0

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        if self.writing:
            self.db.counts[self.table] = self.blocks
        return False

    def __iter__(self):
        return iter([b"x"] * self.db.counts.get(self.table, 0))

    def write(self, _block):
        self.blocks += 1


class _Cursor:
    def __init__(self, db):
        self.db = db
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False

    def execute(self, sql, params=None):
        statement = " ".join(sql.split())
        self.db.sql.append(statement)
        upper = statement.upper()
        if "INFORMATION_SCHEMA.TABLES" in upper:
            self.rows = [(name,) for name in self.db.tables]
        elif "PG_POSTMASTER_START_TIME" in upper:
            self.rows = [(self.db.name, "2026-10-10 00:00:00+00", "127.0.0.1", 5432)]
        elif upper.startswith("SELECT COUNT(*)"):
            table = statement.split('"')[1]
            self.rows = [(self.db.counts.get(table, 0),)]
        elif upper.startswith("SELECT ID, PROVIDER, MODEL, DIM, NORMALIZE, IS_ACTIVE, NOTE FROM EMBEDDING_PROFILES"):
            self.rows = self.db.profiles
        elif "INFORMATION_SCHEMA.COLUMNS" in upper:
            self.rows = []
        elif upper.startswith("TRUNCATE"):
            self.db.counts[statement.split('"')[1]] = 0
            self.rows = []
        else:
            self.rows = []

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows

    def copy(self, sql):
        table = " ".join(sql.split()).split('"')[1]
        return _Copy(self.db, table, "FROM STDIN" in sql.upper())


class _DB:
    def __init__(self, tables, counts, name="khipu"):
        self.tables, self.counts, self.name = list(tables), dict(counts), name
        self.sql: list[str] = []


class _Conn:
    def __init__(self, db):
        self.db = db
        self.original = dict(db.counts)
        self.commits = self.rollbacks = 0

    def cursor(self):
        return _Cursor(self.db)

    def commit(self):
        self.commits += 1
        self.original = dict(self.db.counts)

    def rollback(self):
        self.rollbacks += 1
        self.db.counts = dict(self.original)

    def close(self):
        pass


_PREFLIGHT = {"ok": True, "stages": [{"id": key, "ok": True} for key in ("reach", "version", "privileges", "schema", "graph")]}


class DbMoveSafetyTest(unittest.TestCase):
    def test_rv4_04_fresh_migration_profiles_allow_move(self):
        profiles = [
            ("gemini-embedding-001@768", "gemini", "gemini-embedding-001", 768, "l2", True,
             "P1 lock (plan.md Models/LLM); cloud default through P3, local is P4"),
            ("gemini-embedding-2@768", "gemini", "gemini-embedding-2", 768, "l2", False,
             "Gemini Embedding 2 @768; task prefixes at API time; activate after full memory backfill"),
        ]
        source = _DB(["embedding_profiles", "episodes"], {"embedding_profiles": 2, "episodes": 1})
        target = _DB(source.tables, {"embedding_profiles": 2, "episodes": 0})
        target.profiles = profiles
        result, _, _ = self._move(source, target, dry_run=True)
        self.assertTrue(result["ok"], result)
        self.assertEqual(target.counts["embedding_profiles"], 2)
        for column in range(7):
            with self.subTest(column=column):
                modified = list(profiles[0])
                modified[column] = "custom"
                target.profiles = [tuple(modified), profiles[1]]
                result, _, _ = self._move(source, target, dry_run=True)
                self.assertEqual(result["error"], "target_not_empty")

    def _move(self, source, target, **kwargs):
        source.name, target.name = "source", "target"
        source_conn, target_conn = _Conn(source), _Conn(target)
        with (
            mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@source:5432/khipu"),
            mock.patch("khipu.setup.connect_database", return_value=_PREFLIGHT),
            mock.patch("psycopg.connect", side_effect=[source_conn, target_conn]),
        ):
            result = dbmove.move_database("postgres://u@target:5432/khipu", **kwargs)
        return result, source_conn, target_conn

    def test_refuses_library_only_nonempty_target(self):
        source = _DB(["episodes", "library_sources"], {"episodes": 0, "library_sources": 0})
        target = _DB(["episodes", "library_sources"], {"episodes": 0, "library_sources": 1})
        result, _source_conn, _target_conn = self._move(source, target)
        self.assertEqual(result["error"], "target_not_empty")
        self.assertEqual(target.counts["library_sources"], 1)

    def test_refuses_schema_mismatch_before_target_mutation(self):
        source = _DB(["episodes", "memory_embeddings"], {"episodes": 1, "memory_embeddings": 4})
        target = _DB(["episodes"], {"episodes": 0})
        result, _source_conn, _target_conn = self._move(source, target)
        self.assertEqual(result["error"], "schema_mismatch")
        self.assertIn("memory_embeddings", result["source_only"])
        self.assertEqual(target.counts["episodes"], 0)
        self.assertFalse(result.get("switched", False))

    def test_copy_failure_rolls_back_the_target_replacement(self):
        source = _DB(["episodes", "topics"], {"episodes": 1, "topics": 2})
        target = _DB(["episodes", "topics"], {"episodes": 7, "topics": 8})
        real_copy = dbmove._copy_table
        def copy_then_fail(source_conn, target_conn, table):
            if table == "topics":
                raise RuntimeError("copy exploded after episodes")
            real_copy(source_conn, target_conn, table)
        with mock.patch("khipu.dbmove._copy_table", side_effect=copy_then_fail):
            result, _source_conn, target_conn = self._move(source, target, allow_nonempty=True)
        self.assertEqual(result["error"], "copy_failed")
        self.assertEqual(target.counts["episodes"], 7)
        self.assertEqual(target.counts["topics"], 8)
        self.assertEqual(target_conn.rollbacks, 1)

    def test_current_fk_dependencies_precede_dependents(self):
        tables = {"decisions", "decision_links", "library_sources", "library_documents", "library_chunks", "library_embeddings"}
        order = dbmove._copy_order(tables, tables)
        self.assertLess(order.index("decisions"), order.index("decision_links"))
        self.assertLess(order.index("library_sources"), order.index("library_documents"))
        self.assertLess(order.index("library_documents"), order.index("library_chunks"))
        self.assertLess(order.index("library_chunks"), order.index("library_embeddings"))

    def test_fk_order_allows_each_child_only_after_its_parent(self):
        deps = {
            "decision_links": {"decisions"},
            "library_documents": {"library_sources"},
            "library_chunks": {"library_documents"},
            "library_embeddings": {"library_chunks"},
        }
        tables = set(deps) | {"decisions", "library_sources"}
        order = dbmove._copy_order(tables, tables, deps)
        copied = set()
        for table in order:
            self.assertTrue(deps.get(table, set()) <= copied, table)
            copied.add(table)

    def test_replacement_truncate_requests_transactional_identity_restart(self):
        source = _DB(["episodes"], {"episodes": 1})
        target = _DB(["episodes"], {"episodes": 0})
        result, _source_conn, _target_conn = self._move(source, target)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("TRUNCATE TABLE \"episodes\" RESTART IDENTITY CASCADE" in sql for sql in target.sql))


class BackupSafetyTest(unittest.TestCase):
    def test_remote_dsn_with_retained_local_record_refuses_docker_dump(self):
        with (
            mock.patch.dict("os.environ", {"KHIPU_PG_DUMP": ""}, clear=False),
            mock.patch("khipu.components_backup.read_versions", return_value={"postgres": {"mode": "local_docker", "container": "khipu-pg19", "port": 54329}}),
            mock.patch("khipu.db.resolve_dsn", return_value="postgresql://khipu:pw@db.example.invalid:5432/khipu"),
            mock.patch("khipu.components_backup._docker") as docker,
        ):
            result = _dump_live_db(Path("/private/tmp"))
        self.assertEqual(result["error"], "docker_dump_requires_active_local_dsn")
        docker.assert_not_called()


class PostgresUpgradeSafetyTest(unittest.TestCase):
    def test_existing_local_install_refuses_before_container_or_volume_mutation(self):
        versions = {"postgres": {"mode": "local_docker", "container": "khipu-pg19", "volume": "khipu-pgdata"}}
        with (
            mock.patch("khipu.components_postgres.docker_available", return_value={"ok": True}),
            mock.patch("khipu.components_postgres.read_versions", return_value=versions),
            mock.patch("khipu.components_postgres._generate_password") as password,
            mock.patch("khipu.components_postgres._docker") as docker,
        ):
            result = install_local_postgres()
        self.assertEqual(result["error"], "existing_local_postgres_refused")
        password.assert_not_called()
        docker.assert_not_called()


class RemoteProbeTimeoutTest(unittest.TestCase):
    def test_remote_probe_sets_statement_and_lock_timeouts_before_sql(self):
        class Cursor:
            def __init__(self):
                self.sql = []
                self.row = None
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def execute(self, sql, *unused):
                self.sql.append(sql)
                if sql.startswith("SHOW") and not any("statement_timeout" in item for item in self.sql):
                    raise AssertionError("query ran without statement timeout")
                if sql.startswith("SHOW") and not any("lock_timeout" in item for item in self.sql):
                    raise AssertionError("query ran without lock timeout")
                self.row = ("190001",) if "server_version_num" in sql else ("19.0",)
            def fetchone(self): return self.row
        class Conn:
            def __init__(self): self.cursor_obj = Cursor()
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def cursor(self): return self.cursor_obj
        with mock.patch("khipu.db.connect", return_value=Conn()):
            result = check_remote_postgres()
        self.assertTrue(result["ok"], result)


class UpgradeRecoveryTest(unittest.TestCase):
    def _failed_upgrade(self, *, preserve_fails: bool, restore_fails: bool, old_start_fails: bool):
        versions = {
            "postgres": {"mode": "local_docker", "image": "registry/old:19-pgvector", "port": 54329},
            "graphify": {"semver": "1.0.0"},
        }
        row = {
            "postgres_image": "registry/new:19-pgvector",
            "graphify_semver": "1.0.0",
            "graphify_tarball_url": "https://example.invalid/g",
            "pgvector_min": "0",
        }
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def execute(self, *unused): pass
            def fetchone(self): return (1,)
        class Conn:
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def cursor(self): return Cursor()
            def commit(self): pass
        copy_runs = 0
        service_runs = 0
        volumes = {"khipu-pgdata", "khipu-pgdata-prev"}
        def docker(args, **kwargs):
            nonlocal copy_runs, service_runs, volumes
            proc = mock.Mock(returncode=0, stdout="", stderr="")
            if args[:2] == ["volume", "inspect"]:
                proc.returncode = 0 if args[2] in volumes else 1
                if proc.returncode:
                    proc.stderr = "Error: No such volume"
            elif args[:2] == ["volume", "create"]:
                volumes.add(args[2])
            elif args[:3] == ["volume", "rm", "-f"]:
                volumes.discard(args[3])
            if args and args[0] == "run" and "--rm" in args and args[-1] == "cp -a /from/. /to/":
                volumes.add(next(arg.split(":", 1)[0] for arg in args if arg.endswith(":/to")))
                copy_runs += 1
                if preserve_fails and copy_runs == 2:
                    proc.returncode = 1
                    proc.stderr = "preserve failed"
                if restore_fails and copy_runs == 3:
                    proc.returncode = 1
                    proc.stderr = "restore previous failed"
            elif args and args[0] == "run" and "--rm" not in args:
                service_runs += 1
                if old_start_fails and service_runs == 2:
                    proc.returncode = 1
                    proc.stderr = "old start failed"
            return proc
        with (
            mock.patch("khipu.components_postgres.docker_available", return_value={"ok": True}),
            mock.patch("khipu.components_matrix.refresh_matrix_cache"),
            mock.patch("khipu.components_postgres.read_versions", return_value=versions),
            mock.patch("khipu.components_matrix.effective_matrix", return_value=([row], {})),
            mock.patch("khipu.components_matrix.find_full_row", return_value=row),
            mock.patch("khipu.components_matrix.khipu_app_version", return_value="1.0.0"),
            mock.patch("khipu.components_postgres._read_dsn_password_port", return_value={"ok": True, "password": "pw", "port": 54329}),
            mock.patch("khipu.components_postgres.ensure_postgres_image", return_value={"ok": True}),
            mock.patch("khipu.components_backup._dump_live_db", return_value={"ok": True, "path": "/private/tmp/safe.dump"}),
            mock.patch("khipu.components_backup._backup_dir", return_value=Path("/private/tmp")),
            mock.patch("khipu.components_backup._restore_dump_file", return_value={"ok": False, "error": "restore failed"}),
            mock.patch("khipu.components_postgres._wait_pg_ready", return_value=True),
            mock.patch("psycopg.connect", return_value=Conn()),
            mock.patch("khipu.components_postgres._docker", side_effect=docker),
        ):
            return upgrade_postgres()

    def test_failed_volume_preservation_is_not_reported_as_rollback(self):
        result = self._failed_upgrade(preserve_fails=True, restore_fails=False, old_start_fails=False)
        self.assertFalse(result.get("rolled_back"), result)
        self.assertEqual(result.get("error"), "rollback_recovery_failed")
        self.assertEqual(result.get("recovery_step"), "preserve_failed_volume")

    def test_previous_volume_restore_failure_is_not_reported_as_rollback(self):
        result = self._failed_upgrade(preserve_fails=False, restore_fails=True, old_start_fails=False)
        self.assertFalse(result.get("rolled_back"), result)
        self.assertEqual(result.get("error"), "rollback_recovery_failed")
        self.assertEqual(result.get("recovery_step"), "restore_previous_volume")

    def test_previous_container_start_failure_is_not_reported_as_rollback(self):
        result = self._failed_upgrade(preserve_fails=False, restore_fails=False, old_start_fails=True)
        self.assertFalse(result.get("rolled_back"), result)
        self.assertEqual(result.get("error"), "rollback_recovery_failed")
        self.assertEqual(result.get("recovery_step"), "start_previous_container")


class DbMoveAliasIdentityTest(unittest.TestCase):
    def test_aliases_for_same_connected_database_refuse_before_copy(self):
        source = _DB(["episodes"], {"episodes": 1}, name="same")
        target = _DB(["episodes"], {"episodes": 7}, name="same")
        source_conn, target_conn = _Conn(source), _Conn(target)
        with (
            mock.patch("khipu.db.resolve_dsn", return_value="postgres://u@one:5432/khipu"),
            mock.patch("khipu.setup.connect_database", return_value=_PREFLIGHT),
            mock.patch("psycopg.connect", side_effect=[source_conn, target_conn]),
        ):
            result = dbmove.move_database("postgres://u@alias:5432/khipu")
        self.assertEqual(result["error"], "target_is_current")
        self.assertEqual(target.counts["episodes"], 7)
        self.assertFalse(any(sql.startswith("TRUNCATE") for sql in target.sql))

    def test_upgrade_prepares_image_before_stopping_original_container(self):
        versions = {"postgres": {"mode": "local_docker", "image": "registry/old:19-pgvector", "port": 54329}, "graphify": {"semver": "1.0.0"}}
        row = {"postgres_image": "registry/new:19-pgvector", "graphify_semver": "1.0.0", "graphify_tarball_url": "https://example.invalid/g", "pgvector_min": "0"}
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def execute(self, *unused): pass
            def fetchone(self): return (0,)
        class Conn:
            def __enter__(self): return self
            def __exit__(self, *unused): return False
            def cursor(self): return Cursor()
        with (
            mock.patch("khipu.components_postgres.docker_available", return_value={"ok": True}),
            mock.patch("khipu.components_postgres.read_versions", return_value=versions),
            mock.patch("khipu.components_matrix.effective_matrix", return_value=([row], {})),
            mock.patch("khipu.components_matrix.find_full_row", return_value=row),
            mock.patch("khipu.components_matrix.khipu_app_version", return_value="1.0.0"),
            mock.patch("khipu.components_postgres._read_dsn_password_port", return_value={"ok": True, "password": "pw", "port": 54329}),
            mock.patch("khipu.components_postgres.ensure_postgres_image", return_value={"ok": False, "error": "image_unavailable"}),
            mock.patch("khipu.components_backup._dump_live_db", return_value={"ok": True, "path": "/private/tmp/safe.dump"}),
            mock.patch("psycopg.connect", return_value=Conn()),
            mock.patch("khipu.components_postgres._docker", side_effect=AssertionError("original container touched")) as docker,
        ):
            result = upgrade_postgres()
        self.assertEqual(result["error"], "image_pull_failed")
        docker.assert_not_called()


if __name__ == "__main__":
    unittest.main()
