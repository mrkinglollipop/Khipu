"""Slice 4 of docs/plans/2026-10-16-offline-and-self-healing.md: prompt recall
answers from this Mac's copy, never the hub, when the hub is down.

A real sqlite replica on disk (7 hours old, distrusted by a recent "behind"
measurement), a hub that fails the test if anything reaches for it, and no
network (the query embedding raises)."""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

import psycopg
import pytest

from khipu import hub_reach
from khipu import hub_snapshot as hs
from khipu import recall_prompt as rp

PROMPT = "quarterly harvest festival planning"
PROFILE = "gemini-embedding-2@768"


def _ts(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.setenv("KHIPU_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    return tmp_path


def _replica(tmp_path, monkeypatch, *, dump_age_h: float = 7, behind: bool = True):
    now = datetime.now(timezone.utc)
    dump = now - timedelta(hours=dump_age_h)
    snap = tmp_path / "hub_snapshot.sqlite"
    con = sqlite3.connect(snap)
    hs._create_schema(con)
    con.execute("INSERT INTO episodes (id, summary, ingested_at) VALUES (1, ?, ?)",
                ("Settled the quarterly harvest festival planning budget", _ts(dump - timedelta(hours=1))))
    con.execute("INSERT INTO embedding_profiles (id, provider, model, dim, is_active) "
                "VALUES (?, 'gemini', 'gemini-embedding-2', 768, 1)", (PROFILE,))
    con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('refreshed_at', ?)", (_ts(dump),))
    con.commit()
    con.close()
    (tmp_path / "meta.json").write_text(json.dumps({"refreshed_at": _ts(dump), "hub_dumped_at": _ts(dump)}))
    if behind:
        (tmp_path / "freshness.json").write_text(json.dumps({
            "measured_at": _ts(now - timedelta(minutes=1)), "checked_at": _ts(now - timedelta(minutes=1)),
            "refreshed_at": _ts(dump), "state": "behind",
            "lag_seconds": hs.SNAPSHOT_LAG_BEHIND_S + 600, "complete": True, "in_step": False, "tables": {},
        }))
    monkeypatch.setattr(hs, "snapshot_path", lambda: snap)
    monkeypatch.setattr(hs, "meta_path", lambda: tmp_path / "meta.json")
    monkeypatch.setattr(hs, "freshness_path", lambda: tmp_path / "freshness.json")


def _no_hub(monkeypatch):
    def boom(*_a, **_k):
        pytest.fail("the hub was reached")

    monkeypatch.setattr("khipu.embed.hybrid_search", boom)
    monkeypatch.setattr(hs, "try_hub_connect", boom)
    monkeypatch.setattr("khipu.db.connect", boom)


def _no_network(monkeypatch):
    def offline(*_a, **_k):
        raise RuntimeError("offline")

    monkeypatch.setattr(rp, "_cached_query_embed", offline)


def test_a_seven_hour_distrusted_replica_answers_marked_stale_inside_the_deadline(home, monkeypatch):
    _replica(home, monkeypatch)
    _no_hub(monkeypatch)
    _no_network(monkeypatch)
    fresh, health = hs.snapshot_is_fresh()
    assert not fresh and health["distrusted_by"] == "measurement"
    t0 = time.monotonic()
    out = rp.prior_work_for_prompt(PROMPT)
    took = time.monotonic() - t0
    assert [h["id"] for h in out["hits"]] == ["1"], out
    assert "This Mac's copy of memory is 7 hours old" in out["context"]
    assert out["replica_stale"]["why"] == "measurement"
    assert took < rp.TIMEOUT_S, took
    assert (home / "data" / "state" / "replica-wake").exists()


def test_the_provisional_keyword_result_carries_the_stale_marker(home, monkeypatch):
    _replica(home, monkeypatch)
    _no_hub(monkeypatch)
    monkeypatch.setattr(rp, "_cached_query_embed", lambda *_a, **_k: time.sleep(3) or [1.0])
    monkeypatch.setattr(rp, "TIMEOUT_S", 0.5)
    out = rp.prior_work_for_prompt(PROMPT)
    assert [h["id"] for h in out["hits"]] == ["1"]
    assert out["replica_stale"]["age_seconds"] >= 7 * 3600
    assert "7 hours old" in out["context"]


def test_a_trusted_replica_has_no_stale_marker(home, monkeypatch):
    _replica(home, monkeypatch, dump_age_h=1, behind=False)
    _no_hub(monkeypatch)
    _no_network(monkeypatch)
    out = rp.prior_work_for_prompt(PROMPT)
    assert out["hits"] and "replica_stale" not in out
    assert "copy is" not in out["context"]


def test_a_missing_replica_with_the_memo_set_skips_the_hub(home, monkeypatch):
    monkeypatch.setattr(hs, "snapshot_path", lambda: home / "none.sqlite")
    monkeypatch.setattr(hs, "meta_path", lambda: home / "meta.json")
    monkeypatch.setattr(hs, "freshness_path", lambda: home / "freshness.json")
    _no_hub(monkeypatch)
    hub_reach.note_unreachable("ConnectionTimeout")
    t0 = time.monotonic()
    out = rp.prior_work_for_prompt(PROMPT)
    assert time.monotonic() - t0 < 0.5
    assert out["hits"] == [] and out["degraded"] == "hub unreachable"


def test_a_failed_hub_search_sets_the_memo_and_a_good_one_clears_it(home, monkeypatch):
    monkeypatch.setattr(hs, "snapshot_path", lambda: home / "none.sqlite")
    monkeypatch.setattr(hs, "meta_path", lambda: home / "meta.json")
    monkeypatch.setattr(hs, "freshness_path", lambda: home / "freshness.json")
    monkeypatch.setattr("khipu.embed.hybrid_search", mock.Mock(side_effect=psycopg.OperationalError("refused")))
    with pytest.raises(psycopg.OperationalError):
        rp._search_hits(PROMPT, cwd=None)
    assert hub_reach.known_unreachable() is not None
    monkeypatch.setattr(hub_reach, "_window_s", lambda: 60.0)
    hub_reach.memo_path().unlink()
    monkeypatch.setattr("khipu.embed.hybrid_search", mock.Mock(return_value={"results": []}))
    rp._search_hits(PROMPT, cwd=None)
    assert hub_reach.known_unreachable() is None and not hub_reach.memo_path().exists()


# ---- the memo -----------------------------------------------------------------


def test_the_memo_is_set_expires_and_clears(home, monkeypatch):
    assert hub_reach.known_unreachable() is None
    hub_reach.note_unreachable("ConnectionTimeout: no route")
    first = hub_reach.known_unreachable()
    assert first and "no route" in first["error"] and first["since"] == first["at"]
    later = datetime.now(timezone.utc) + timedelta(seconds=30)
    monkeypatch.setattr(hub_reach, "_now", lambda: later)
    hub_reach.note_unreachable("again")
    kept = hub_reach.known_unreachable()
    assert kept["since"] == first["since"] and kept["at"] != first["at"]
    monkeypatch.setattr(hub_reach, "_now", lambda: later + timedelta(seconds=61))
    assert hub_reach.known_unreachable() is None
    monkeypatch.setattr(hub_reach, "_now", lambda: later - timedelta(minutes=5))
    assert hub_reach.known_unreachable() is None  # an "at" in the future never sticks
    monkeypatch.setattr(hub_reach, "_now", lambda: datetime.now(timezone.utc))
    hub_reach.note_unreachable("x")
    hub_reach.note_reachable()
    assert hub_reach.known_unreachable() is None and not hub_reach.memo_path().exists()
    hub_reach.note_reachable()  # nothing to clear: no error


def test_an_unreadable_memo_reads_as_none(home):
    hub_reach.memo_path().parent.mkdir(parents=True)
    hub_reach.memo_path().write_text("{not json")
    assert hub_reach.known_unreachable() is None
    hub_reach.memo_path().write_text(json.dumps({"since": "x", "at": "yesterday"}))
    assert hub_reach.known_unreachable() is None


def test_a_window_of_zero_never_reports_unreachable(home, monkeypatch):
    hub_reach.note_unreachable("down")
    assert hub_reach.known_unreachable() is not None
    monkeypatch.setattr(hub_reach, "_window_s", lambda: 0.0)
    assert hub_reach.known_unreachable() is None
    hub_reach.memo_path().unlink()
    hub_reach.note_unreachable("down")
    assert not hub_reach.memo_path().exists()


def test_the_memo_is_not_written_in_an_isolated_run(home, monkeypatch):
    monkeypatch.setenv("KHIPU_ISOLATED_DIR", str(home / "iso"))
    hub_reach.note_unreachable("down")
    assert not hub_reach.memo_path().exists()


def test_try_hub_connect_clears_the_memo_on_success_and_sets_it_on_failure(home, monkeypatch):
    monkeypatch.setattr("khipu.db.resolve_dsn", lambda: "postgresql://x")
    monkeypatch.setattr("khipu.db.conninfo_with_local_root_cert", lambda dsn: dsn)
    hub_reach.note_unreachable("down")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: "conn")
    assert hs.try_hub_connect() == "conn"
    assert hub_reach.known_unreachable() is None

    def refuse(*_a, **_k):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refuse)
    with pytest.raises(psycopg.OperationalError):
        hs.try_hub_connect()
    assert "refused" in hub_reach.known_unreachable()["error"]

    hub_reach.note_reachable()

    def odd(*_a, **_k):
        raise ValueError("bad dsn syntax")

    monkeypatch.setattr(psycopg, "connect", odd)
    with pytest.raises(ValueError):
        hs.try_hub_connect()
    assert hub_reach.known_unreachable() is None


def test_a_memo_write_failure_never_changes_the_connect(home, monkeypatch):
    monkeypatch.setattr("khipu.db.resolve_dsn", lambda: "postgresql://x")
    monkeypatch.setattr("khipu.db.conninfo_with_local_root_cert", lambda dsn: dsn)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: "conn")
    monkeypatch.setattr(hub_reach, "memo_path", mock.Mock(side_effect=OSError("read-only")))
    assert hs.try_hub_connect() == "conn"


# ---- the marker ---------------------------------------------------------------


@pytest.mark.parametrize("seconds,words", [
    (30 * 60, "30 minutes old"), (7 * 3600 + 120, "7 hours old"), (47 * 3600, "47 hours old"),
    (3 * 86400 + 5, "3 days old"),
])
def test_the_stale_line_wording(seconds, words):
    block = rp.render_block([{"kind": "episode", "id": "1", "snippet": "x"}],
                            stale={"age_seconds": seconds, "why": "overdue"})
    lines = block.splitlines()
    assert lines[1] == f"This Mac's copy of memory is {words}; newer work may be missing."
    assert "—" not in lines[1]
    assert len(block) <= rp.BLOCK_CHAR_BUDGET


def test_no_stale_line_without_hits():
    assert rp.render_block([], stale={"age_seconds": 3600, "why": "overdue"}) == ""
    assert "copy is" not in rp.render_block([{"kind": "episode", "id": "1", "snippet": "x"}], stale=None)
