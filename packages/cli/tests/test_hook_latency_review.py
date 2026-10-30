"""Round-1 review fixes for the hook-latency work: the schema shortcut is per
database, a failed bulk column read leaves a usable transaction, the
delegation hook's log line keeps an error outcome, cannot be forged and times
its own file work, and the benchmark refuses a failing hook."""
from __future__ import annotations

import contextlib
import io
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from psycopg.errors import UndefinedTable

from khipu import db, identity, routing_delegation


@pytest.fixture(autouse=True)
def _clean_cache():
    db._TABLE_COLUMNS_CACHE.clear()
    getattr(db, "_SCHEMA_SEEN", {}).clear()
    yield
    db._TABLE_COLUMNS_CACHE.clear()
    getattr(db, "_SCHEMA_SEEN", {}).clear()


class _Conn:
    """A connection to ``dbname`` whose information_schema answers ``rows``."""

    def __init__(self, dbname, rows=(), fail_bulk=False):
        self.info = SimpleNamespace(host="h", port=5432, dbname=dbname)
        self.rows, self.fail_bulk, self.failed = list(rows), fail_bulk, False
        self.rollbacks = 0
        outer = self

        class Cur:
            connection = outer

            def execute(self, sql, params=()):
                if outer.failed:
                    raise RuntimeError("InFailedSqlTransaction")
                if "table_name = ANY" in sql and outer.fail_bulk:
                    outer.failed = True
                    raise RuntimeError("bulk read failed")

            def fetchall(self):
                return outer.rows

        self._cur = Cur()

    @contextlib.contextmanager
    def cursor(self):
        yield self._cur

    def rollback(self):
        self.rollbacks += 1
        self.failed = False


class _OldHubCursor:
    def __init__(self, conn):
        self.connection = conn
        self.savepoints = 0

    def execute(self, sql, params=()):
        if "SAVEPOINT" in sql:
            self.savepoints += 1
            return
        raise UndefinedTable("project_aliases")

    def fetchall(self):
        return []


def test_a_modern_hubs_schema_does_not_skip_the_older_hubs_fallback():
    modern = _Conn("modern", [("project_aliases", "alias"), ("project_aliases", "project")])
    db.prime_table_columns(modern, "project_aliases")
    old_cur = _OldHubCursor(_Conn("old"))
    with identity.group_memo():
        assert identity.project_group(old_cur, "o/x") == ["o/x"]
    assert old_cur.savepoints == 3


def test_a_failed_bulk_column_read_leaves_the_transaction_usable():
    conn = _Conn("hub", fail_bulk=True)
    db.prime_table_columns(conn, "episodes")
    assert not conn.failed
    db.table_columns(conn._cur, "episodes")  # the per-table fallback runs


def _run_main(monkeypatch, tmp_path, raw, **patches):
    monkeypatch.setattr("khipu.paths.capture_home", lambda: tmp_path)
    monkeypatch.setattr(routing_delegation.wire, "read_stdin", lambda: raw)
    monkeypatch.setattr(routing_delegation, "hook_main", lambda r: ({}, {}, "agent"))
    for name, value in patches.items():
        monkeypatch.setattr(routing_delegation.hook_shape, name, value)
    with contextlib.redirect_stdout(io.StringIO()):
        routing_delegation.main()
    return (tmp_path / "logs" / "prompt-recall.log").read_text().splitlines()


def test_a_reshape_failure_stays_visible_in_the_log(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("synthetic")

    raw = json.dumps({"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Agent"})
    line = _run_main(monkeypatch, tmp_path, raw, reshape=boom)[-1]
    assert "outcome=error:RuntimeError" in line
    assert "call=agent" in line


def test_control_characters_in_fields_cannot_forge_a_log_line(monkeypatch, tmp_path):
    raw = json.dumps({"session_id": "s\n2099 [khipu-delegation-hook] outcome=forged",
                      "hook_event_name": "Pre\r\nTool", "tool_name": "Ba sh\x00"})
    monkeypatch.setattr("khipu.paths.capture_home", lambda: tmp_path)
    routing_delegation._log_call(raw, "pass", None, "", time.monotonic())
    lines = (tmp_path / "logs" / "prompt-recall.log").read_text().splitlines()
    assert len(lines) == 1
    fields = lines[0].split(" ")
    assert [f for f in fields if f.startswith("outcome=")] == ["outcome=pass"]
    assert fields.count("[khipu-delegation-hook]") == 1


def test_the_logged_duration_includes_the_file_work(monkeypatch, tmp_path):
    monkeypatch.setattr("khipu.paths.capture_home", lambda: tmp_path)
    original = Path.open

    def slow(self, *a, **k):
        if self.name == "prompt-recall.log":
            time.sleep(0.06)
        return original(self, *a, **k)

    monkeypatch.setattr(Path, "open", slow)
    routing_delegation._log_call("{}", "pass", None, "", time.monotonic())
    line = (tmp_path / "logs" / "prompt-recall.log").read_text().splitlines()[-1]
    assert float(line.rsplit("ms=", 1)[1]) >= 50


def test_the_benchmark_refuses_a_hook_that_exits_nonzero(tmp_path):
    from tests import bench_hook_latency

    with pytest.raises(RuntimeError, match="exit"):
        bench_hook_latency.run(["/bin/sh", "-c", "exit 7"], {"cwd": str(tmp_path)}, {}, 1)
