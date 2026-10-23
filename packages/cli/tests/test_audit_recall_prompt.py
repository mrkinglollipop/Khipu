"""Synthetic, offline regression probes; failures demonstrate audited defects."""
import json
import os
import socket
import sqlite3
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from khipu import hub_snapshot as hs
from khipu import recall_prompt as rp


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    assert os.environ.get("KHIPU_KEYCHAIN") == "0"
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_FEATURE_RELEVANCE_FLOOR", "1")
    monkeypatch.setenv("KHIPU_FEATURE_GRAPH_CANDIDATES", "0")
    monkeypatch.setenv("KHIPU_FEATURE_TIME_INTERPRETATION", "0")
    def forbidden(*args, **kwargs):
        raise AssertionError("network and hub access forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("khipu.db.connect", forbidden)
    monkeypatch.setattr(rp, "_cached_query_embed", forbidden)
    monkeypatch.setattr(rp, "_deliverable_context_line", lambda *a, **kw: "")


@pytest.mark.parametrize("location", ["summary_tail", "decisions", "topic_tail"])
def test_keyword_hit_survives_prompt_relevance_floor(tmp_path, monkeypatch, location):
    path = tmp_path / "replica.sqlite"
    con = sqlite3.connect(path)
    hs._create_schema(con)
    now = datetime.now(timezone.utc).isoformat()
    query = "zephyr lantern"
    prefix = "General synthetic meeting notes. " * 25
    if location == "topic_tail":
        con.execute("INSERT INTO topics(slug,title,body,status,updated_at) VALUES('sample','General notes',?,'active',?)",
                    (prefix + query, now))
        expected = "sample"
    else:
        summary = prefix + query if location == "summary_tail" else "General synthetic meeting notes."
        decisions = json.dumps(["Use zephyr lantern"]) if location == "decisions" else "[]"
        con.execute("INSERT INTO episodes(id,summary,decisions,ts) VALUES(1,?,?,?)", (summary, decisions, now))
        expected = "1"
    con.commit()
    con.close()
    monkeypatch.setattr(hs, "snapshot_path", lambda: path)
    monkeypatch.setattr(hs, "snapshot_is_fresh", lambda: (True, {"exists": True}))
    lexical = hs.search_snapshot(query, 8, indexed_tokens_only=True)
    assert [r["id"] for r in lexical] == [expected], lexical
    out = rp.prior_work_for_prompt(query)
    print(json.dumps({"location": location, "lexical_ids": [r["id"] for r in lexical], "prompt_result": out}))
    assert [r["id"] for r in out["hits"]] == [expected], out


def test_three_intervening_prompts_expire_dedup():
    hit = {"kind": "episode", "id": "7", "score": 0.1, "snippet": "zephyr lantern choice"}
    with patch.object(rp, "_search_hits", return_value={"hits": [hit], "legs": ["lexical"], "degraded": None}):
        first = rp.prior_work_for_prompt("zephyr lantern", session_id="synthetic-session")
        assert first["hits"]
        for _ in range(rp.DEDUP_WINDOW):
            gated = rp.prior_work_for_prompt("yes", session_id="synthetic-session")
            assert gated["reason"] == "trivial acknowledgment"
        again = rp.prior_work_for_prompt("zephyr lantern", session_id="synthetic-session")
    print(json.dumps({"first": first["reason"], "intervening_prompts": rp.DEDUP_WINDOW, "again": again}))
    assert again["hits"], again


def test_head_keyword_and_forgotten_controls(tmp_path, monkeypatch):
    path = tmp_path / "controls.sqlite"
    con = sqlite3.connect(path)
    hs._create_schema(con)
    con.execute("INSERT INTO episodes(id,summary,deleted_at) VALUES(1,'zephyr lantern',NULL),(2,'zephyr lantern','2026-10-20')")
    con.commit()
    con.close()
    monkeypatch.setattr(hs, "snapshot_path", lambda: path)
    monkeypatch.setattr(hs, "snapshot_is_fresh", lambda: (True, {"exists": True}))
    out = rp.prior_work_for_prompt("zephyr lantern")
    assert [r["id"] for r in out["hits"]] == ["1"], out


def test_late_embedding_preserves_lexical_result_within_budget(tmp_path, monkeypatch):
    import threading
    import time
    path = tmp_path / "late.sqlite"
    con = sqlite3.connect(path)
    hs._create_schema(con)
    con.execute("INSERT INTO episodes(id,summary) VALUES(1,'zephyr lantern')")
    con.commit()
    con.close()
    monkeypatch.setattr(hs, "snapshot_path", lambda: path)
    monkeypatch.setattr(hs, "snapshot_is_fresh", lambda: (True, {"exists": True}))
    monkeypatch.setattr(hs, "active_snapshot_profile", lambda: "synthetic@2")
    monkeypatch.setattr(hs, "warm_matrix", lambda *a: None)
    release = threading.Event()
    ended = threading.Event()
    def delayed(*a):
        release.wait(5)
        ended.set()
        raise TimeoutError("synthetic late embedding")
    monkeypatch.setattr(rp, "_cached_query_embed", delayed)
    started = time.monotonic()
    try:
        out = rp.prior_work_for_prompt("zephyr lantern")
        elapsed = time.monotonic() - started
        print(json.dumps({"late_embedding_ms": round(elapsed * 1000, 1), "ids": [r["id"] for r in out["hits"]]}))
        assert [r["id"] for r in out["hits"]] == ["1"], out
        assert elapsed < rp.TIMEOUT_S, elapsed
    finally:
        release.set()
        ended.wait(1)
