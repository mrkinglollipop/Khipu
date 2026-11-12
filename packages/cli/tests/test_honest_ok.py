"""Writes that answer ok must say what did not happen.

Capture names the commitments it dropped, a batch commitment change answers
per id and refuses when nothing changed, a capture-now note that does not fit
is reported as cut, and a rerank switch with no model behind it is skipped
and shown. Everything runs on the throwaway PostgreSQL of tests/hermetic_pg.py
or on fakes; no provider, key or network is touched.
"""
from __future__ import annotations

import io
import json
from contextlib import contextmanager
from unittest import mock

import pytest

from khipu import commitments as co, config, db, embed, outbox, routing, topic_graph
from khipu import mcp_server as srv
from tests import fact_seed
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


@pytest.fixture
def pg(hub, tmp_path, monkeypatch):  # noqa: F811
    for name in ("KHIPU_DATABASE_URL",):
        monkeypatch.delenv(name, raising=False)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_HUB_FILE_MIRROR", "0")
    monkeypatch.setenv("KHIPU_CAPTURE_MODE", "hub")
    monkeypatch.setenv("KHIPU_USER_ALIASES", "matt")
    co.reset_user_patterns()
    conn = hub.connect()
    hub.migrate(conn=conn)
    conn.commit()
    conn.autocommit = True
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, "connect", lambda *a, **k: hub.connect())
    monkeypatch.setattr(embed, "embed_on_capture", lambda *a, **k: None)
    monkeypatch.setattr(topic_graph, "persist_capture_graph", lambda *a, **k: {})
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    monkeypatch.setattr(config, "capture_mode", lambda: "hub")
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["claude"]})
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr(srv, "_stdio_hook_owns_capture", lambda: False)
    monkeypatch.setattr(srv, "_capture_mode", lambda: "hub")
    yield conn
    co.reset_user_patterns()


def _capture(**extra) -> dict:
    args = {"summary": "Rewrote the widget parser and shipped the migration.",
            "session_id": "grokbot:honest-ok", "project": "acme/widget", **extra}
    return srv._tool_capture(args)


def _open_texts(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT text FROM commitments WHERE status = 'open' ORDER BY id")
        return [r[0] for r in cur.fetchall()]


# ---- 1. capture names the commitments it did not keep ---------------------------







def _with_dedup(monkeypatch, dedup):
    from khipu import capture as cap

    monkeypatch.setattr(cap, "dedup_before_insert", lambda *a, **k: dict(dedup))










def test_a_capture_the_stop_hook_queues_names_the_fields_it_cannot_carry(monkeypatch):
    monkeypatch.setattr(srv, "_stdio_hook_owns_capture", lambda: True)
    with mock.patch("khipu.session_capture.resolve_session_ref", return_value=("claude", "s1", "state")), \
            mock.patch("khipu.session_capture.request_capture_now"):
        out = srv._tool_capture({"summary": "Did the thing.", "open_loops": ["a", "b"],
                                 "closed_loops": ["c"], "decisions": ["d"]})
    assert out["queued"] is True
    kinds = {d["field"]: d["count"] for d in out["dropped"]}
    assert kinds == {"open_loops": 2, "closed_loops": 1, "decisions": 1}


def test_a_queued_capture_names_deliverables_and_verbatim_it_cannot_carry(monkeypatch):
    monkeypatch.setattr(srv, "_stdio_hook_owns_capture", lambda: True)
    with mock.patch("khipu.session_capture.resolve_session_ref", return_value=("claude", "s1", "state")), \
            mock.patch("khipu.session_capture.request_capture_now"):
        out = srv._tool_capture({"summary": "Did the thing.", "deliverables": [{"path": "a"}],
                                 "verbatim": {"note": "n"}})
    assert {d["field"] for d in out["dropped"]} == {"deliverables", "verbatim"} and out["partial"] is True
    assert all(d["reason"] for d in out["dropped"])


# ---- 2. a batch that changed nothing is not ok ------------------------------------

def _seed(conn, texts):
    ids = []
    with conn.transaction(), conn.cursor() as cur:
        for text in texts:
            cur.execute("INSERT INTO commitments (text, project, owner, kind, status, content_hash, origin) "
                        "VALUES (%s, 'acme/widget', 'assistant', 'followup', 'open', %s, 'conversation') "
                        "RETURNING id", (text, co.content_hash("acme/widget", text)))
            ids.append(int(cur.fetchone()[0]))
        fact_seed.adopt(cur, "commitment", ids)
    return ids


@pytest.mark.parametrize("action,extra", [("close", {"reason": "done"}), ("reopen", {"reason": "again"}),
                                          ("snooze", {"until": "7d", "reason": "later"})])
def test_a_batch_where_every_id_fails_is_not_ok(pg, action, extra):
    out = srv._tool_owed_update({"ids": [987654, 987655], "action": action, **extra})
    assert out["ok"] is False and out["changed"] == 0
    assert [(r["id"], r["ok"]) for r in out["results"]] == [(987654, False), (987655, False)]
    assert out["error"]


def test_a_batch_reassign_of_a_missing_id_is_not_ok(pg):
    out = srv._tool_owed_update({"ids": [987654, 987655], "action": "reassign", "owner": "user", "reason": "r"})
    assert out["ok"] is False and out["changed"] == 0


def test_a_mixed_batch_keeps_the_real_change_and_says_it_was_partial(pg):
    (real,) = _seed(pg, ["follow up on the llama ranch audit"])
    out = srv._tool_owed_update({"ids": [987654, real], "action": "close", "reason": "done"})
    assert out["ok"] is True and out["changed"] == 1 and out["partial"] is True
    assert out["failed"] == [987654]
    assert [(r["id"], r["ok"]) for r in out["results"]] == [(987654, False), (real, True)]


def test_a_batch_that_all_lands_is_plainly_ok(pg):
    ids = _seed(pg, ["follow up on the llama ranch audit", "send the llama ranch invoice"])
    out = srv._tool_owed_update({"ids": ids, "action": "close", "reason": "done"})
    assert out["ok"] is True and out["changed"] == 2
    assert "partial" not in out and "failed" not in out


def test_the_tool_call_for_a_failed_batch_is_an_error_that_still_carries_results(pg):
    reply = srv.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                "params": {"name": "khipu_owed_update",
                                           "arguments": {"ids": [987654, 987655], "action": "close",
                                                         "reason": "done"}}})
    result = reply["result"]
    assert result["isError"] is True
    assert "987654" in json.dumps(result)


# ---- 3. a capture-now note that does not fit is reported as cut --------------------

def test_a_note_over_the_cap_is_reported_as_truncated_by_the_tool(tmp_path, monkeypatch):
    from khipu import session_capture as sc

    monkeypatch.setattr(srv, "_stdio_hook_owns_capture", lambda: True)
    note = "x" * (sc.MAX_CAPTURE_NOTE + 500)

    def fake_request(harness, sid, note=None, outcome=None):
        outcome.update(sc._note_outcome(len(note), sc.MAX_CAPTURE_NOTE, 500))

    with mock.patch("khipu.session_capture.resolve_session_ref", return_value=("claude", "s1", "state")), \
            mock.patch("khipu.session_capture.request_capture_now", side_effect=fake_request):
        out = srv._tool_capture({"summary": note})
    assert out["queued"] is True and out["truncated"] is True
    assert out["stored_chars"] == sc.MAX_CAPTURE_NOTE and out["sent_chars"] == len(note)
    assert out["dropped_chars"] == 500


def test_request_capture_now_reports_what_it_kept(tmp_path, monkeypatch):
    from khipu import session_capture as sc

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("HOME", str(tmp_path))
    kept: dict = {}
    sc.request_capture_now("claude", "sess-1", note="y" * (sc.MAX_CAPTURE_NOTE + 7), outcome=kept)
    assert kept["truncated"] is True and kept["stored_chars"] == sc.MAX_CAPTURE_NOTE
    assert kept["dropped_chars"] == 7
    short: dict = {}
    sc.request_capture_now("claude", "sess-2", note="short note", outcome=short)
    assert short["truncated"] is False and short["stored_chars"] == len("short note")
    assert short["dropped_chars"] == 0


def test_a_second_note_that_pushes_an_older_one_out_is_reported(tmp_path, monkeypatch):
    from khipu import session_capture as sc

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("HOME", str(tmp_path))
    sc.request_capture_now("claude", "sess-3", note="a" * (sc.MAX_CAPTURE_NOTE - 10))
    kept: dict = {}
    sc.request_capture_now("claude", "sess-3", note="b" * 100, outcome=kept)
    assert kept["truncated"] is True and kept["dropped_chars"] > 0
    assert kept["sent_chars"] == 100


# ---- 4. rerank on with no model behind it ------------------------------------------







def test_capture_now_on_the_command_line_says_when_it_cut_the_note(tmp_path, monkeypatch, capsys):
    import argparse

    from khipu import cli
    from khipu import session_capture as sc

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("HOME", str(tmp_path))
    args = argparse.Namespace(harness="claude", session_id="sess-cli", note="z" * (sc.MAX_CAPTURE_NOTE + 3))
    with mock.patch("khipu.session_capture.resolve_session_ref", return_value=("claude", "sess-cli", "args")):
        assert cli.cmd_capture_now(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queued"] is True and out["truncated"] is True and out["dropped_chars"] == 3
    args.note = "short"
    args.session_id = "sess-cli-2"
    with mock.patch("khipu.session_capture.resolve_session_ref", return_value=("claude", "sess-cli-2", "args")):
        cli.cmd_capture_now(args)
    assert "truncated" not in json.loads(capsys.readouterr().out)
