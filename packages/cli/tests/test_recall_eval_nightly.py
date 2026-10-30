"""The nightly recall measurement: step record, doctor row thresholds,
forgotten-id skipping and ``recall eval --install``. Fake data throughout:
the replica is an in-memory sqlite and the scorer is stubbed."""
from __future__ import annotations

import json
import sqlite3
import stat
from datetime import datetime, timedelta, timezone

import pytest

from khipu import hub_snapshot, recall_eval, recall_eval_nightly as ren


def _replica(live=(1, 2, 3), topics=()):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE topics (slug TEXT, deleted_at TEXT)")
    con.executemany("INSERT INTO episodes VALUES (?)", [(i,) for i in live])
    con.executemany("INSERT INTO topics VALUES (?, ?)", topics)
    return con


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(ren, "history_path", lambda: tmp_path / "state" / "hist.json")
    golden = tmp_path / "golden.jsonl"
    monkeypatch.setattr(recall_eval, "default_golden_path", lambda: golden)
    monkeypatch.setattr(hub_snapshot, "open_snapshot", lambda **_k: _replica())
    monkeypatch.setattr("khipu.tombstones.forgotten_episode_ids", lambda: set())
    return tmp_path, golden


def _write_golden(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _stub_scorer(monkeypatch, hit_for=lambda entry, path: True, stale=False):
    seen = []

    def fake(entry, path, **_k):
        seen.append((entry["query"], path))
        hit = hit_for(entry, path)
        return {"expect_none": bool(entry.get("expect_none")), "hit": hit,
                "reciprocal_rank": 1.0 if hit else 0.0, "abstain_correct": True,
                "stale_violation": stale, "timeout": False, "error": False, "degraded": None,
                "outcome": "match", "ms": 1.0, "payload_chars": 10}

    monkeypatch.setattr(recall_eval, "eval_one_path", fake)
    return seen


def test_step_records_per_path_figures_and_golden_stat(world, monkeypatch):
    tmp, golden = world
    _write_golden(golden, [{"query": f"q{i}", "expect": ["1"]} for i in range(4)]
                  + [{"query": "none", "expect_none": True}])
    _stub_scorer(monkeypatch, hit_for=lambda e, p: e["query"] != "q0")
    rec = ren.run_nightly_eval(golden=golden)
    assert rec["ok"] and rec["entries"] == 5 and rec["scored"] == 5
    assert set(rec["paths"]) == set(recall_eval.ALL_PATHS)
    assert rec["paths"]["prompt"]["hit_rate"] == 0.75
    assert rec["paths"]["prompt"]["abstain_correct"] == 1
    assert rec["golden"]["bytes"] == golden.stat().st_size and rec["golden"]["mtime"]
    assert rec["previous"] is None
    second = ren.run_nightly_eval(golden=golden)
    assert second["previous"]["paths"]["prompt"]["hit_rate"] == 0.75


def test_history_keeps_30_records_and_never_holds_a_query(world, monkeypatch):
    tmp, golden = world
    _write_golden(golden, [{"query": "very private words", "expect": ["1"]}])
    _stub_scorer(monkeypatch)
    for _ in range(35):
        ren.run_nightly_eval(golden=golden)
    hist = ren.history_path()
    assert len(ren.read_history()) == 30
    assert "very private words" not in hist.read_text()
    assert stat.S_IMODE(hist.stat().st_mode) == 0o600


def test_step_uses_the_replica_only(world, monkeypatch):
    tmp, golden = world
    _write_golden(golden, [{"query": "q", "expect": ["1"]}])
    state = {}

    def fake(entry, path, **_k):
        with pytest.raises(ConnectionError):
            hub_snapshot.try_hub_connect()
        state["checked"] = True
        return {"expect_none": False, "hit": True, "reciprocal_rank": 1.0, "abstain_correct": None,
                "stale_violation": False, "timeout": False, "error": False, "degraded": None,
                "outcome": "match", "ms": 1.0, "payload_chars": 1}

    real = hub_snapshot.try_hub_connect
    monkeypatch.setattr(recall_eval, "eval_one_path", fake)
    ren.run_nightly_eval(golden=golden)
    assert state["checked"] and hub_snapshot.try_hub_connect is real


def test_entries_naming_forgotten_erased_or_deleted_ids_are_skipped_not_missed(world, monkeypatch):
    tmp, golden = world
    monkeypatch.setattr(hub_snapshot, "open_snapshot",
                        lambda **_k: _replica(live=(1, 2), topics=[("gone-topic", "2026-01-01")]))
    monkeypatch.setattr("khipu.tombstones.forgotten_episode_ids", lambda: {2})
    _write_golden(golden, [
        {"query": "alive", "expect": ["1"]},
        {"query": "forgotten", "expect": ["2"]},
        {"query": "erased", "expect": ["99"]},
        {"query": "topic gone", "expect": ["gone-topic"]},
        {"query": "partly", "expect": ["99", "1"]},
    ])
    seen = _stub_scorer(monkeypatch)
    rec = ren.run_nightly_eval(golden=golden)
    assert rec["skipped_forgotten"] == 3
    assert {q for q, _ in seen} == {"alive", "partly"}
    assert rec["paths"]["prompt"]["hit_rate"] == 1.0


def test_step_stops_at_its_time_budget(world, monkeypatch):
    tmp, golden = world
    _write_golden(golden, [{"query": f"q{i}", "expect": ["1"]} for i in range(3)])
    _stub_scorer(monkeypatch)
    rec = ren.run_nightly_eval(golden=golden, budget_s=-1)
    assert rec["truncated"] and rec["scored"] == 0


def test_missing_replica_is_a_failed_step_not_a_zero_score(world, monkeypatch):
    tmp, golden = world
    _write_golden(golden, [{"query": "q", "expect": ["1"]}])

    def boom(**_k):
        raise FileNotFoundError("no replica")

    monkeypatch.setattr(hub_snapshot, "open_snapshot", boom)
    rec = ren.run_nightly_eval(golden=golden)
    assert rec["ok"] is False and rec["paths"] == {}


# ---- doctor row ---------------------------------------------------------------

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _record(world, *, rate=0.8, prev=0.8, entries=25, age_h=2, violations=0, ok=True):
    prompt = {"total": 10, "hit_rate": rate, "abstain_total": 0, "stale_violations": violations}
    rec = {"ts": (NOW - timedelta(hours=age_h)).isoformat(), "ok": ok, "entries": entries,
           "paths": {"prompt": prompt},
           "previous": None if prev is None else {"paths": {"prompt": {"hit_rate": prev}}}}
    ren._write_history([rec])


@pytest.mark.parametrize("kwargs,level", [
    (dict(), "green"),
    (dict(rate=0.76, prev=0.8), "green"),          # a 4-point drop is inside the allowance
    (dict(rate=0.74, prev=0.8), "red"),            # 6 points
    (dict(rate=0.65, prev=0.7), "green"),          # exactly 5 points, above the floor
    (dict(rate=0.55, prev=0.56), "red"),           # under the 0.6 floor
    (dict(violations=1), "red"),
    (dict(entries=19), "yellow"),
    (dict(age_h=49), "yellow"),
    (dict(ok=False), "yellow"),
])
def test_doctor_row_levels(world, kwargs, level):
    _record(world, **kwargs)
    row = ren.recall_eval_health(now=NOW)
    assert row["level"] == level
    assert row["ok"] is (level != "red")
    assert row["summary"]


def test_doctor_row_before_any_run_is_yellow(world, monkeypatch):
    monkeypatch.setattr("khipu.jobs._is_index_sync_host", lambda: True)
    row = ren.recall_eval_health(now=NOW)
    assert row["level"] == "yellow" and row["ok"]


def test_doctor_row_is_not_applicable_off_the_nightly_host(world, monkeypatch):
    monkeypatch.setattr("khipu.jobs._is_index_sync_host", lambda: False)
    assert ren.recall_eval_health(now=NOW)["applicable"] is False


# ---- --install ----------------------------------------------------------------

def test_install_merges_dedupes_validates_and_backs_up(world):
    tmp, golden = world
    _write_golden(golden, [{"query": "old", "expect": ["1"]}])
    before = golden.read_text()
    cand = tmp / "cand.jsonl"
    _write_golden(cand, [
        {"query": "old", "expect": ["2"]},                       # duplicate: existing wins
        {"query": "new", "expect": ["2"], "n": 3, "source": "replay"},
        {"query": "new", "expect": ["3"]},                       # duplicate within candidates
        {"query": "dangling", "expect": ["404"]},                # id not in the replica
        {"query": "abstain", "expect_none": True},
    ])
    result = ren.install_candidates(cand, golden)
    assert result == {"ok": True, "added": 2, "duplicates": 2, "rejected_missing_id": 1, "total": 3}
    merged = [json.loads(line) for line in golden.read_text().splitlines()]
    assert [e["query"] for e in merged] == ["old", "new", "abstain"]
    assert merged[1]["source"] == "replay" and merged[0]["expect"] == ["1"]
    backup = golden.parent / ren.BACKUP_NAME
    assert backup.read_text() == before
    assert stat.S_IMODE(golden.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "old" not in json.dumps(result) and "new" not in json.dumps(result)


def test_install_with_nothing_new_leaves_the_file_alone(world):
    tmp, golden = world
    _write_golden(golden, [{"query": "old", "expect": ["1"]}])
    cand = tmp / "cand.jsonl"
    _write_golden(cand, [{"query": "old", "expect": ["1"]}])
    before = golden.stat().st_mtime_ns
    assert ren.install_candidates(cand, golden)["added"] == 0
    assert golden.stat().st_mtime_ns == before and not (tmp / ren.BACKUP_NAME).exists()


def test_install_without_a_replica_refuses(world, monkeypatch):
    tmp, golden = world
    cand = tmp / "cand.jsonl"
    _write_golden(cand, [{"query": "q", "expect": ["1"]}])

    def boom(**_k):
        raise FileNotFoundError("no replica")

    monkeypatch.setattr(hub_snapshot, "open_snapshot", boom)
    assert ren.install_candidates(cand, golden)["ok"] is False
    assert not golden.exists()
