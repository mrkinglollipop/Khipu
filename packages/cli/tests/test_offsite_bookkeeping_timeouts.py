"""Offsite receipt deadlines, with fake PostgreSQL and no upload process."""

import json
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest import mock

import psycopg
import pytest

from khipu import graph_backup, jobs


PRIVATE_ERROR = "fixture private DSN password must never be logged"


def _fake_db(monkeypatch, *, stage=None, row=None):
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = row
    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value.__enter__.return_value = cursor
    calls = []

    def statement(*args):
        # A finite fallback models an unbounded driver without hanging pytest.
        bounded = bool(calls[-1].get("statement_timeout_s"))
        Event().wait(0.001 if bounded else 0.1)
        raise psycopg.errors.QueryCanceled(PRIVATE_ERROR)

    def connect(**kwargs):
        calls.append(kwargs)
        if stage == "connect":
            Event().wait(0.001 if kwargs.get("connect_timeout") else 0.1)
            raise psycopg.errors.ConnectionTimeout(PRIVATE_ERROR)
        return conn

    if stage == "statement":
        cursor.execute.side_effect = statement
    monkeypatch.setattr("khipu.db.connect", connect)
    return calls, conn, cursor


def _assert_deadlines(calls):
    assert len(calls) == 1
    assert 0 < calls[0].get("connect_timeout", 0) <= 30
    assert 0 < calls[0].get("statement_timeout_s", 0) <= 30


def _job_log(monkeypatch, tmp_path):
    path = tmp_path / "graph.log"
    monkeypatch.setattr(jobs, "_log_paths", lambda stem: (path, tmp_path / "err.log"))
    return path


@pytest.mark.parametrize("stage", ["connect", "statement"])
@pytest.mark.parametrize("mirror_rc", [0, 17])
def test_due_read_timeout_stops_upload_logs_safe_failure_and_preserves_mirror_rc(
    monkeypatch, tmp_path, stage, mirror_rc
):
    calls, _, _ = _fake_db(monkeypatch, stage=stage)
    log = _job_log(monkeypatch, tmp_path)
    monkeypatch.setattr(jobs, "GRAPHIFY_NIGHTLY", tmp_path / "fake-mirror.py")
    mirror = mock.Mock(return_value=mirror_rc)
    monkeypatch.setattr(jobs, "_run_script", mirror)
    upload = mock.Mock(return_value={"ok": True})
    monkeypatch.setattr(graph_backup, "run_offsite", upload)

    assert jobs.run_graph_build() == mirror_rc

    upload.assert_not_called()
    outcome = json.loads(log.read_text().removeprefix("offsite: "))
    assert outcome["ok"] is False
    assert "timeout" in outcome["reason"].lower() or "timed out" in outcome["reason"].lower()
    assert "receipt" in outcome["reason"].lower()
    assert PRIVATE_ERROR not in log.read_text()
    mirror.assert_called_once()
    _assert_deadlines(calls)


@pytest.mark.parametrize("days_old,expected_uploads", [(1, 0), (8, 1)])
def test_bounded_due_read_keeps_successful_receipt_scheduling(
    monkeypatch, tmp_path, days_old, expected_uploads
):
    created = datetime.now(timezone.utc) - timedelta(days=days_old)
    calls, _, cursor = _fake_db(
        monkeypatch, row=("ok", {}, created, timedelta(days=days_old))
    )
    _job_log(monkeypatch, tmp_path)
    upload = mock.Mock(return_value={"ok": True})
    monkeypatch.setattr(graph_backup, "run_offsite", upload)

    jobs._offsite_if_due()

    _assert_deadlines(calls)
    cursor.execute.assert_called_once()
    assert upload.call_count == expected_uploads


@pytest.mark.parametrize("stage", ["connect", "statement"])
@pytest.mark.parametrize("upload_ok", [True, False])
def test_result_insert_timeout_fails_safely_and_preserves_upload_result(
    monkeypatch, stage, upload_ok
):
    calls, _, _ = _fake_db(monkeypatch, stage=stage)
    upload_result = {"ok": upload_ok, "dest": "r2:fixture/graph/snapshot.sqlite"}
    if not upload_ok:
        upload_result.update(reason="offsite copy failed", returncode=23)
    monkeypatch.setattr(graph_backup, "_run_offsite", mock.Mock(return_value=upload_result))

    result = graph_backup.run_offsite()

    assert result["ok"] is False
    assert "timeout" in result["ops_error"].lower() or "timed out" in result["ops_error"].lower()
    assert PRIVATE_ERROR not in json.dumps(result)
    assert result["dest"] == upload_result["dest"]
    if not upload_ok:
        assert result["reason"] == "offsite copy failed"
        assert result["returncode"] == 23
    _assert_deadlines(calls)


def test_bounded_result_insert_preserves_successful_receipt(monkeypatch):
    created = datetime(2026, 10, 26, tzinfo=timezone.utc)
    calls, conn, cursor = _fake_db(monkeypatch, row=(42, created))
    monkeypatch.setattr(graph_backup, "_run_offsite", mock.Mock(return_value={"ok": True}))

    result = graph_backup.run_offsite()

    _assert_deadlines(calls)
    assert result["ok"] is True
    assert result["event"]["id"] == 42
    assert result["event"]["created_at"] == created.isoformat()
    assert result["event"]["status"] == "ok"
    assert cursor.execute.call_args.args[1][0:2] == ("graph_snapshot_offsite", "ok")
    conn.commit.assert_called_once()
