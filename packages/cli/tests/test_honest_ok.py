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

def test_capture_says_which_open_loops_were_dropped_and_why(pg):
    out = _capture(open_loops=["fix it", "Review the widget parser migration plan before the release"])
    assert out["ok"] is True and out["episode_id"]
    assert _open_texts(pg) == ["Review the widget parser migration plan before the release"]
    assert out["commitments"]["opened"] == 1
    dropped = out["dropped"]
    assert [d["text"] for d in dropped] == ["fix it"]
    assert dropped[0]["kind"] == "open_loop" and dropped[0]["reason"]
    assert out["partial"] is True


def test_capture_with_every_loop_kept_has_no_dropped_list(pg):
    out = _capture(open_loops=["Review the widget parser migration plan before the release"])
    assert out["ok"] is True and out["dropped"] == []
    assert "partial" not in out or out["partial"] is False
    assert out["commitments"]["opened"] == 1


def test_capture_says_when_a_closed_loop_matched_nothing(pg):
    out = _capture(closed_loops=["Finished the quarterly tax filing for the llama ranch"])
    assert out["ok"] is True
    assert [(d["kind"], d["text"]) for d in out["dropped"]] == [
        ("closed_loop", "Finished the quarterly tax filing for the llama ranch")]
    assert out["dropped"][0]["reason"]


def test_capture_whose_commitment_step_failed_says_so(pg, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("commitments table on fire")

    monkeypatch.setattr(co, "open_from_episode", boom)
    out = _capture(open_loops=["Review the widget parser migration plan before the release"])
    assert out["ok"] is True and out["episode_id"]
    assert [d["kind"] for d in out["dropped"]] == ["open_loop"]
    assert "commitments table on fire" in out["dropped"][0]["reason"]
    assert out["commitments"]["step_failed"]


def test_capture_names_a_fact_step_that_failed_and_was_rolled_back(pg, monkeypatch):
    from khipu import decisions

    def boom(*a, **k):
        raise RuntimeError("decisions table on fire")

    monkeypatch.setattr(decisions, "insert_decisions_from_episode", boom)
    out = _capture(decisions=["Use rolling deploys for the widget service"])
    assert out["ok"] is True and out["episode_id"] and out["partial"] is True
    assert [g["step"] for g in out["fact_gaps"]] == ["decisions"]
    assert "decisions table on fire" in out["fact_gaps"][0]["error"]


def test_a_capture_the_stop_hook_queues_names_the_fields_it_cannot_carry(monkeypatch):
    monkeypatch.setattr(srv, "_stdio_hook_owns_capture", lambda: True)
    with mock.patch("khipu.session_capture.resolve_session_ref", return_value=("claude", "s1", "state")), \
            mock.patch("khipu.session_capture.request_capture_now"):
        out = srv._tool_capture({"summary": "Did the thing.", "open_loops": ["a", "b"],
                                 "closed_loops": ["c"], "decisions": ["d"]})
    assert out["queued"] is True
    kinds = {d["field"]: d["count"] for d in out["dropped"]}
    assert kinds == {"open_loops": 2, "closed_loops": 1, "decisions": 1}
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
    out = srv._tool_owed_update({"ids": [987654, 987655], "action": "reassign", "owner": "agent", "reason": "r"})
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

def test_status_says_why_there_is_no_rerank_model():
    from khipu import rerank

    with mock.patch("khipu.models.synth_settings", return_value={"provider": "claude-cli"}):
        status = rerank.status()
    assert status["usable"] is False and "API provider" in status["reason"]
    with mock.patch("khipu.models.synth_settings",
                    return_value={"provider": "local", "endpoint": "", "model_id": ""}):
        assert rerank.status()["usable"] is False
    with mock.patch("khipu.models.synth_settings",
                    return_value={"provider": "local", "endpoint": "http://127.0.0.1:1/v1", "model_id": "m"}):
        assert rerank.status()["usable"] is True


def test_a_search_with_rerank_on_and_no_model_records_the_skip(monkeypatch):
    from khipu import rerank
    from tests.test_rerank import _corpus_rows, _fake_hub, _search

    rows = _corpus_rows()
    monkeypatch.delenv("KHIPU_FEATURE_RERANK", raising=False)
    with _fake_hub(rows):
        baseline = _search()
    monkeypatch.setenv("KHIPU_FEATURE_RERANK", "1")
    with _fake_hub(rows), mock.patch.object(rerank, "_call_model", side_effect=rerank._Unavailable("no key")):
        staged = _search()
    assert staged["results"] == baseline["results"]
    assert staged["degraded_legs"] == ["rerank"]
    block = staged["rerank"]
    assert block["applied"] is False and block["reason"] == "no-provider" and block["skipped"] is True
    assert staged["timing"]["rerank_skipped"] == "no-provider"


def test_doctor_shows_a_rerank_switch_with_no_model_plainly(monkeypatch):
    from khipu import cli

    monkeypatch.setenv("KHIPU_FEATURE_RERANK", "1")
    with mock.patch("khipu.models.synth_settings", return_value={"provider": "claude-cli"}):
        block, warnings = cli._doctor_rerank()
    assert block["enabled"] is True and block["usable"] is False
    assert len(warnings) == 1 and "rerank" in warnings[0] and "no rerank model" in warnings[0]
    monkeypatch.setenv("KHIPU_FEATURE_RERANK", "0")
    block, warnings = cli._doctor_rerank()
    assert block["enabled"] is False and warnings == []


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
