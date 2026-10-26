"""Hermetic connection-establishment budgets for the nightly graph mirror."""

from __future__ import annotations

import threading
from unittest import mock

import psycopg
import pytest

from khipu import graph_sync as gs


@pytest.fixture
def graph_source(tmp_path, monkeypatch):
    path = tmp_path / "graph.sqlite"
    path.write_bytes(b"")
    monkeypatch.setattr(gs, "is_graph_producer", lambda: True)
    monkeypatch.setattr(gs, "_read_sqlite", lambda _: ([], []))
    monkeypatch.setattr(gs, "load_sources", lambda: {"sources": []})
    monkeypatch.setattr(gs, "disabled_or_unreachable_ids", lambda: set())
    monkeypatch.setattr("khipu.sources.sources_file", lambda: tmp_path / "sources.json")
    return path


def _connection():
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []
    cur.fetchone.return_value = (0,)
    cur.rowcount = 0
    return conn


def test_mirror_retries_connect_with_long_timeout_and_backoff(graph_source, monkeypatch, capsys):
    conn = _connection()
    connect = mock.Mock(side_effect=[psycopg.OperationalError("synthetic private detail"),
                                    psycopg.OperationalError("synthetic private detail"), conn])
    monkeypatch.setattr("khipu.db.connect", connect)
    sleep = mock.Mock()
    monkeypatch.setattr(gs.time, "sleep", sleep)
    drift = mock.Mock(return_value={"ok": True})
    monkeypatch.setattr(gs, "graph_drift", drift)

    result = gs.sync_from_sqlite(graph_source)

    assert result["drift"]["ok"]
    assert connect.call_args_list == [mock.call(connect_timeout=30)] * 3
    assert sleep.call_args_list == [mock.call(2), mock.call(4)]
    conn.commit.assert_called_once_with()
    drift.assert_called_once_with(graph_source, excuse_pending=False, mirror_connections=True)
    log = capsys.readouterr().err
    assert "retry" in log.lower()
    assert "synthetic private detail" not in log
    assert all(line.startswith("graph mirror connect:") for line in log.splitlines())


def test_mirror_exhaustion_raises_last_failure_and_logs_no_details(graph_source, monkeypatch, capsys):
    failures = [psycopg.OperationalError("synthetic secret detail") for _ in range(3)]
    connect = mock.Mock(side_effect=failures)
    monkeypatch.setattr("khipu.db.connect", connect)
    sleep = mock.Mock()
    monkeypatch.setattr(gs.time, "sleep", sleep)
    drift = mock.Mock()
    monkeypatch.setattr(gs, "graph_drift", drift)

    with pytest.raises(psycopg.OperationalError) as caught:
        gs.sync_from_sqlite(graph_source)

    assert caught.value is failures[-1]
    assert connect.call_args_list == [mock.call(connect_timeout=30)] * 3
    assert sleep.call_args_list == [mock.call(2), mock.call(4)]
    drift.assert_not_called()
    log = capsys.readouterr().err.lower()
    assert "3" in log and "failed" in log
    assert "synthetic secret detail" not in log
    assert all(line.startswith("graph mirror connect:") for line in log.splitlines())


@pytest.mark.parametrize("error_type", [RuntimeError, psycopg.ProgrammingError])
def test_non_connection_errors_are_not_retried(graph_source, monkeypatch, error_type):
    error = error_type("synthetic error")
    connect = mock.Mock(side_effect=error)
    monkeypatch.setattr("khipu.db.connect", connect)
    sleep = mock.Mock()
    monkeypatch.setattr(gs.time, "sleep", sleep)

    with pytest.raises(error_type) as caught:
        gs.sync_from_sqlite(graph_source)

    assert caught.value is error
    connect.assert_called_once_with(connect_timeout=30)
    sleep.assert_not_called()


@pytest.mark.parametrize("failure_site", ["enter", "execute", "commit"])
def test_established_connection_failures_never_replay_transaction(graph_source, monkeypatch, failure_site):
    conn = _connection()
    error = psycopg.OperationalError("synthetic established-connection failure")
    if failure_site == "enter":
        conn.__enter__.side_effect = error
    elif failure_site == "execute":
        conn.cursor.return_value.__enter__.return_value.execute.side_effect = error
    else:
        conn.commit.side_effect = error
    connect = mock.Mock(return_value=conn)
    monkeypatch.setattr("khipu.db.connect", connect)
    sleep = mock.Mock()
    monkeypatch.setattr(gs.time, "sleep", sleep)
    drift = mock.Mock()
    monkeypatch.setattr(gs, "graph_drift", drift)

    with pytest.raises(psycopg.OperationalError) as caught:
        gs.sync_from_sqlite(graph_source)

    assert caught.value is error
    connect.assert_called_once_with(connect_timeout=30)
    sleep.assert_not_called()
    drift.assert_not_called()
    conn.__enter__.assert_called_once_with()
    if failure_site == "execute":
        conn.cursor.return_value.__enter__.return_value.execute.assert_called_once()
    if failure_site == "commit":
        conn.commit.assert_called_once_with()


def test_post_sync_drift_retries_both_parallel_connections(graph_source, monkeypatch):
    attempts = {}
    lock = threading.Lock()

    def establish(**kwargs):
        assert kwargs == {"connect_timeout": 30}
        with lock:
            worker = threading.get_ident()
            attempts[worker] = attempts.get(worker, 0) + 1
            attempt = attempts[worker]
        # Keep both executor workers occupied so the test distinguishes the legs.
        if attempt == 1:
            barrier.wait(timeout=5)
            raise psycopg.OperationalError("synthetic connection failure")
        return _connection()

    barrier = threading.Barrier(2)
    connect = mock.Mock(side_effect=establish)
    monkeypatch.setattr("khipu.db.connect", connect)
    sleep = mock.Mock()
    monkeypatch.setattr(gs.time, "sleep", sleep)

    result = gs.graph_drift(graph_source, excuse_pending=False, mirror_connections=True)

    assert result["ok"]
    assert sorted(attempts.values()) == [2, 2]
    assert connect.call_count == 4
    assert sleep.call_args_list == [mock.call(2), mock.call(2)]


def test_standalone_drift_retains_fast_connection_defaults(graph_source, monkeypatch):
    connect = mock.Mock(side_effect=lambda: _connection())
    monkeypatch.setattr("khipu.db.connect", connect)
    sleep = mock.Mock()
    monkeypatch.setattr(gs.time, "sleep", sleep)

    assert gs.graph_drift(graph_source)["ok"]

    assert connect.call_args_list == [mock.call(), mock.call()]
    sleep.assert_not_called()


def test_post_sync_drift_exhausts_each_connection_budget(graph_source, monkeypatch):
    attempts = {}
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def establish(**kwargs):
        assert kwargs == {"connect_timeout": 30}
        with lock:
            worker = threading.get_ident()
            attempts[worker] = attempts.get(worker, 0) + 1
            attempt = attempts[worker]
        if attempt == 1:
            barrier.wait(timeout=5)
        raise psycopg.OperationalError("synthetic connection failure")

    connect = mock.Mock(side_effect=establish)
    monkeypatch.setattr("khipu.db.connect", connect)
    sleep = mock.Mock()
    monkeypatch.setattr(gs.time, "sleep", sleep)

    with pytest.raises(psycopg.OperationalError):
        gs.graph_drift(graph_source, excuse_pending=False, mirror_connections=True)

    assert sorted(attempts.values()) == [3, 3]
    assert connect.call_count == 6
    assert sorted(call.args[0] for call in sleep.call_args_list) == [2, 2, 4, 4]


def test_post_sync_drift_sql_failure_never_replays_queries(graph_source, monkeypatch):
    connections = []
    lock = threading.Lock()

    def establish(**kwargs):
        assert kwargs == {"connect_timeout": 30}
        conn = _connection()
        conn.cursor.return_value.__enter__.return_value.execute.side_effect = (
            psycopg.OperationalError("synthetic query failure")
        )
        with lock:
            connections.append(conn)
        return conn

    connect = mock.Mock(side_effect=establish)
    monkeypatch.setattr("khipu.db.connect", connect)
    sleep = mock.Mock()
    monkeypatch.setattr(gs.time, "sleep", sleep)

    with pytest.raises(psycopg.OperationalError):
        gs.graph_drift(graph_source, excuse_pending=False, mirror_connections=True)

    assert connect.call_count == 2
    for conn in connections:
        conn.cursor.return_value.__enter__.return_value.execute.assert_called_once()
    sleep.assert_not_called()


def test_standalone_drift_connection_failure_does_not_retry(graph_source, monkeypatch):
    connect = mock.Mock(side_effect=psycopg.OperationalError("synthetic failure"))
    monkeypatch.setattr("khipu.db.connect", connect)
    sleep = mock.Mock()
    monkeypatch.setattr(gs.time, "sleep", sleep)

    with pytest.raises(psycopg.OperationalError):
        gs.graph_drift(graph_source)

    assert connect.call_args_list == [mock.call(), mock.call()]
    sleep.assert_not_called()


def test_non_producer_mirror_and_drift_skip_without_connecting(graph_source, monkeypatch):
    monkeypatch.setattr(gs, "is_graph_producer", lambda: False)
    connect = mock.Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr("khipu.db.connect", connect)

    assert gs.sync_from_sqlite(graph_source)["skipped"]
    assert gs.graph_drift(graph_source, mirror_connections=True)["skipped"]

    connect.assert_not_called()
