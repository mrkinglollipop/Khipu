"""Radio A backup dump/restore — docker exec when host pg clients are absent."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu.components_backup import (
    DRILL_CONTAINER,
    DRILL_VOLUME,
    _dump_live_db,
    _password_from_dsn,
    _pg_restore_ok,
    _restore_drill,
    _restore_dump_file,
    _restore_error_is_already_exists,
    _start_drill_cluster,
)
from khipu.components_postgres import PG_CONTAINER as LIVE_CONTAINER


class DumpDockerExecTest(unittest.TestCase):
    def test_dump_uses_docker_exec_when_host_pg_dump_unset(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)
            calls: list[list[str]] = []

            def fake_docker(args, **kwargs):
                calls.append(list(args))
                proc = mock.Mock()
                proc.returncode = 0
                proc.stderr = ""
                proc.stdout = ""
                if args and args[0] == "cp":
                    Path(args[2]).write_bytes(b"FAKEDUMP")
                return proc

            with (
                mock.patch.dict(
                    os.environ, {"KHIPU_PG_DUMP": "", "KHIPU_PG_RESTORE": ""}
                ),
                mock.patch("khipu.components_backup._docker", side_effect=fake_docker),
                mock.patch(
                    "khipu.db.resolve_dsn",
                    return_value="postgresql://khipu:secret@127.0.0.1:54329/khipu?sslmode=disable",
                ),
            ):
                result = _dump_live_db(dest)

            self.assertTrue(result["ok"], msg=result)
            self.assertTrue(
                any("pg_dump" in args and LIVE_CONTAINER in args for args in calls),
                msg=calls,
            )
            self.assertTrue(any(args and args[0] == "cp" for args in calls), msg=calls)
            self.assertNotIn("pg_dump", [args[0] for args in calls if args])

    def test_restore_uses_docker_exec_when_host_pg_restore_unset(self):
        with tempfile.TemporaryDirectory() as td:
            dump = Path(td) / "x.dump"
            dump.write_bytes(b"DUMP")
            calls: list[list[str]] = []

            def fake_docker(args, **kwargs):
                calls.append(list(args))
                proc = mock.Mock()
                proc.returncode = 0
                proc.stderr = ""
                proc.stdout = ""
                return proc

            with (
                mock.patch.dict(os.environ, {"KHIPU_PG_RESTORE": ""}),
                mock.patch("khipu.components_backup._docker", side_effect=fake_docker),
            ):
                result = _restore_dump_file(
                    dump,
                    container=DRILL_CONTAINER,
                    dsn="postgresql://khipu:x@127.0.0.1:54338/khipu",
                    password="x",
                )

            self.assertTrue(result["ok"], msg=result)
            self.assertTrue(
                any("pg_restore" in args and DRILL_CONTAINER in args for args in calls),
                msg=calls,
            )

    def test_dump_honors_khipu_pg_dump_override(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)

            def fake_run(args, **kwargs):
                for arg in args:
                    if str(arg).startswith("--file="):
                        Path(str(arg).split("=", 1)[1]).write_bytes(b"HOSTDUMP")
                proc = mock.Mock()
                proc.returncode = 0
                proc.stderr = ""
                proc.stdout = ""
                return proc

            with (
                mock.patch.dict(os.environ, {"KHIPU_PG_DUMP": "/opt/custom/pg_dump"}),
                mock.patch("khipu.components_backup._docker") as docker,
                mock.patch("khipu.components_backup._run", side_effect=fake_run),
                mock.patch(
                    "khipu.db.resolve_dsn",
                    return_value="postgresql://khipu:secret@127.0.0.1:54329/khipu",
                ),
            ):
                result = _dump_live_db(dest)

            self.assertTrue(result["ok"], msg=result)
            docker.assert_not_called()

    def test_start_drill_wipes_and_recreates_volume(self):
        calls: list[list[str]] = []

        def fake_docker(args, **kwargs):
            calls.append(list(args))
            proc = mock.Mock()
            proc.stderr = ""
            proc.stdout = ""
            if args[:2] == ["volume", "inspect"]:
                proc.returncode = 1
            else:
                proc.returncode = 0
            return proc

        with mock.patch("khipu.components_backup._docker", side_effect=fake_docker):
            result = _start_drill_cluster("img:tag", 54338, "secret")

        self.assertTrue(result["ok"], msg=result)

        def first(pred):
            return next(i for i, args in enumerate(calls) if pred(args))

        rm_ctr = first(lambda a: a[:2] == ["rm", "-f"] and DRILL_CONTAINER in a)
        rm_vol = first(lambda a: a[:3] == ["volume", "rm", "-f"] and DRILL_VOLUME in a)
        inspect = first(lambda a: a[:2] == ["volume", "inspect"] and DRILL_VOLUME in a)
        create = first(lambda a: a[:2] == ["volume", "create"] and DRILL_VOLUME in a)
        run = first(lambda a: a and a[0] == "run")
        self.assertLess(rm_ctr, rm_vol)
        self.assertLess(rm_vol, inspect)
        self.assertLess(inspect, create)
        self.assertLess(create, run)
        self.assertIn(f"{DRILL_VOLUME}:/var/lib/postgresql", calls[run])

    def test_restore_exit_1_extension_already_exists_is_success(self):
        with tempfile.TemporaryDirectory() as td:
            dump = Path(td) / "x.dump"
            dump.write_bytes(b"DUMP")

            def fake_docker(args, **kwargs):
                proc = mock.Mock()
                proc.stdout = ""
                if "pg_restore" in args:
                    proc.returncode = 1
                    proc.stderr = (
                        "pg_restore: error: could not execute query: "
                        'ERROR:  extension "vector" already exists\n'
                    )
                else:
                    proc.returncode = 0
                    proc.stderr = ""
                return proc

            with (
                mock.patch.dict(os.environ, {"KHIPU_PG_RESTORE": ""}),
                mock.patch("khipu.components_backup._docker", side_effect=fake_docker),
            ):
                result = _restore_dump_file(
                    dump,
                    container=DRILL_CONTAINER,
                    dsn="postgresql://khipu:x@127.0.0.1:54338/khipu",
                    password="x",
                )

            self.assertTrue(result["ok"], msg=result)

    def test_restore_exit_1_other_error_is_failure(self):
        with tempfile.TemporaryDirectory() as td:
            dump = Path(td) / "x.dump"
            dump.write_bytes(b"DUMP")

            def fake_docker(args, **kwargs):
                proc = mock.Mock()
                proc.stdout = ""
                if "pg_restore" in args:
                    proc.returncode = 1
                    proc.stderr = (
                        "pg_restore: error: could not execute query: "
                        "ERROR:  out of memory\n"
                    )
                else:
                    proc.returncode = 0
                    proc.stderr = ""
                return proc

            with (
                mock.patch.dict(os.environ, {"KHIPU_PG_RESTORE": ""}),
                mock.patch("khipu.components_backup._docker", side_effect=fake_docker),
            ):
                result = _restore_dump_file(
                    dump,
                    container=DRILL_CONTAINER,
                    dsn="postgresql://khipu:x@127.0.0.1:54338/khipu",
                    password="x",
                )

            self.assertFalse(result["ok"], msg=result)

    def test_operator_class_already_exists_is_ok_after_create_extension(self):
        line = (
            "pg_restore: error: could not execute query: "
            'ERROR:  operator class "vector_cosine_ops" already exists'
        )
        self.assertTrue(_restore_error_is_already_exists(line))
        self.assertTrue(_pg_restore_ok(1, "", line))

        with tempfile.TemporaryDirectory() as td:
            dump = Path(td) / "x.dump"
            dump.write_bytes(b"DUMP")

            def fake_docker(args, **kwargs):
                proc = mock.Mock()
                proc.stdout = ""
                if "pg_restore" in args:
                    proc.returncode = 1
                    proc.stderr = line + "\n"
                else:
                    proc.returncode = 0
                    proc.stderr = ""
                return proc

            with (
                mock.patch.dict(os.environ, {"KHIPU_PG_RESTORE": ""}),
                mock.patch("khipu.components_backup._docker", side_effect=fake_docker),
            ):
                result = _restore_dump_file(
                    dump,
                    container=DRILL_CONTAINER,
                    dsn="postgresql://khipu:x@127.0.0.1:54338/khipu",
                    password="x",
                )

            self.assertTrue(result["ok"], msg=result)

    def test_mixed_fatal_with_already_exists_is_not_ok(self):
        stderr = (
            "pg_restore: error: could not execute query: "
            'ERROR:  extension "vector" already exists\n'
            "FATAL:  terminating connection due to administrator command\n"
        )
        self.assertFalse(_pg_restore_ok(1, "", stderr))

    def test_password_from_dsn_unquotes_percent_encoding(self):
        self.assertEqual(
            _password_from_dsn(
                "postgresql://khipu:p%40ss@127.0.0.1:54329/khipu?sslmode=disable"
            ),
            "p@ss",
        )

    def test_restore_honors_khipu_pg_restore_override(self):
        with tempfile.TemporaryDirectory() as td:
            dump = Path(td) / "x.dump"
            dump.write_bytes(b"DUMP")

            def fake_run(args, **kwargs):
                proc = mock.Mock()
                proc.returncode = 0
                proc.stderr = ""
                proc.stdout = ""
                return proc

            with (
                mock.patch.dict(
                    os.environ, {"KHIPU_PG_RESTORE": "/opt/custom/pg_restore"}
                ),
                mock.patch("khipu.components_backup._docker") as docker,
                mock.patch("khipu.components_backup._run", side_effect=fake_run) as run,
            ):
                result = _restore_dump_file(
                    dump,
                    container=DRILL_CONTAINER,
                    dsn="postgresql://khipu:x@127.0.0.1:54338/khipu",
                    password="x",
                )

            self.assertTrue(result["ok"], msg=result)
            docker.assert_not_called()
            args = run.call_args[0][0]
            self.assertEqual(args[0], "/opt/custom/pg_restore")

    def test_restore_drill_connect_fail_returns_not_ok(self):
        fake = mock.Mock()
        fake.connect.side_effect = ConnectionError("refused")
        with mock.patch.dict(sys.modules, {"psycopg": fake}):
            result = _restore_drill(Path("/nonexistent.dump"), 54338, "secret")
        self.assertFalse(result["ok"], msg=result)
        self.assertIn("ConnectionError", result["error"])

    def test_start_drill_fails_if_volume_survives_cleanup(self):
        def fake_docker(args, **kwargs):
            proc = mock.Mock()
            proc.stderr = ""
            proc.stdout = ""
            if args[:2] == ["volume", "inspect"]:
                proc.returncode = 0
            else:
                proc.returncode = 0
            return proc

        with mock.patch("khipu.components_backup._docker", side_effect=fake_docker):
            result = _start_drill_cluster("img:tag", 54338, "secret")

        self.assertFalse(result["ok"], msg=result)
        self.assertEqual(result["error"], "drill_volume_not_removed")

    def test_dump_docker_cp_timeout_returns_not_ok(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)

            def fake_docker(args, **kwargs):
                if args and args[0] == "cp":
                    raise subprocess.TimeoutExpired(cmd=args, timeout=600)
                proc = mock.Mock()
                proc.returncode = 0
                proc.stderr = ""
                proc.stdout = ""
                return proc

            with (
                mock.patch.dict(
                    os.environ, {"KHIPU_PG_DUMP": "", "KHIPU_PG_RESTORE": ""}
                ),
                mock.patch("khipu.components_backup._docker", side_effect=fake_docker),
                mock.patch(
                    "khipu.db.resolve_dsn",
                    return_value="postgresql://khipu:secret@127.0.0.1:54329/khipu?sslmode=disable",
                ),
            ):
                result = _dump_live_db(dest)

            self.assertFalse(result["ok"], msg=result)
            self.assertEqual(result["error"], "docker_cp_dump_timeout")

    def test_restore_docker_cp_timeout_returns_not_ok(self):
        with tempfile.TemporaryDirectory() as td:
            dump = Path(td) / "x.dump"
            dump.write_bytes(b"DUMP")

            def fake_docker(args, **kwargs):
                if args and args[0] == "cp":
                    raise subprocess.TimeoutExpired(cmd=args, timeout=600)
                proc = mock.Mock()
                proc.returncode = 0
                proc.stderr = ""
                proc.stdout = ""
                return proc

            with (
                mock.patch.dict(os.environ, {"KHIPU_PG_RESTORE": ""}),
                mock.patch("khipu.components_backup._docker", side_effect=fake_docker),
            ):
                result = _restore_dump_file(
                    dump,
                    container=DRILL_CONTAINER,
                    dsn="postgresql://khipu:x@127.0.0.1:54338/khipu",
                    password="x",
                )

            self.assertFalse(result["ok"], msg=result)
            self.assertEqual(result["error"], "docker_cp_restore_timeout")
