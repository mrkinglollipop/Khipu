"""Row 11: captures stop writing a file copy in hub mode; one append-only
export a night (khipu/capture_export.py).

The export runs against the hermetic PostgreSQL (tests/hermetic_pg.py) at the
full schema and a temporary memory folder. Every row and line is synthetic."""
from __future__ import annotations

import hashlib
import json
from unittest import mock

import pytest

from khipu import capture, capture_export, jobs, mirror, outbox, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

_REAL_CAPTURE_EXPORT = jobs._capture_export


# ---- the hermetic hub -------------------------------------------------------

@pytest.fixture
def pg(hub, monkeypatch, tmp_path):  # noqa: F811
    from khipu import db, embed, topic_graph

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_TEST", "1")
    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, "connect", lambda *a, **kw: hub.connect())
    monkeypatch.setattr(embed, "embed_on_capture", lambda *a, **kw: None)
    monkeypatch.setattr(embed, "embed_one", mock.Mock(side_effect=RuntimeError("no provider")))
    monkeypatch.setattr(topic_graph, "persist_capture_graph", lambda *a, **kw: {})
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    monkeypatch.setattr(capture, "_dedup_candidates", lambda cur, payload: [])
    hub.memory = tmp_path / "memory"
    hub.memory.mkdir()
    return hub


def _payload(n: int, **extra) -> dict:
    payload = {
        "ts": f"2026-11-0{1 + n // 50}T10:{n % 50:02d}:00Z",
        "summary": f"synthetic capture {n} about export step {n}",
        "session_id": f"codex:export-{n}",
        "harness": "codex",
        "project": "acme/export-fixture",
        "transcript_range": f"0:{n}",
        "topics": [f"topic-{n}"],
        "people": [],
        "decisions": [],
        "preferences": [],
        "scope": "build",
    }
    payload.update(extra)
    return payload


def _write(payload: dict) -> int:
    out = capture.write_pg(dict(payload))
    assert out["episode_inserted"], out
    return int(out["episode_id"])


def _sql(pg, sql, params=()):
    with pg.connect() as conn:
        rows = conn.execute(sql, params).fetchall() if sql.lstrip().upper().startswith("SELECT") else None
        if rows is None:
            conn.execute(sql, params)
        conn.commit()
    return rows


def _line(payload: dict, **override) -> str:
    """A line as the folder's writer (capture_v2 → capture.py) shaped it."""
    obj = {k: payload[k] for k in ("ts", "summary", "topics", "people", "decisions", "preferences", "scope")}
    obj["session_id"] = payload["session_id"]
    obj.update(override)
    return json.dumps(obj, ensure_ascii=False)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def world(pg):
    """A hub and a folder holding every case the export must tell apart."""
    p = {n: _payload(n) for n in range(10)}
    ids = {n: _write(p[n]) for n in (1, 2, 3, 4, 5, 6, 7, 8)}
    # 5: merged/edited in the hub after its line was written.
    _sql(pg, "UPDATE episodes SET summary = summary || ' (edited in the hub)' WHERE id = %s", (ids[5],))
    # 6 and 7: forgotten in the hub (6 has a line, 7 has none).
    _sql(pg, "UPDATE episodes SET deleted_at = now() WHERE id = ANY(%s)", ([ids[6], ids[7]],))
    # 8: forgotten on this Mac only (the hub row is still live).
    tombstones.publish_episode_ids({ids[8]})

    current = pg.memory / "episodes.jsonl"
    rotated = pg.memory / "episodes_2025.jsonl"
    current_text = "\n".join([
        _line(p[1]),                                        # identical
        _line(p[2], preferences=["file-only preference"]),  # differs: left alone
        _line(p[5]),                                        # matched through episode_sources
        _line(p[6]),                                        # matches a forgotten row
        _line(p[9]),                                        # no hub row at all
        "{not json",                                        # unparseable
    ])  # no trailing newline on purpose
    current.write_text(current_text, encoding="utf-8")
    rotated.write_text(_line(p[3]) + "\n", encoding="utf-8")  # 3 has its line in a rotated file
    # 4 has no line anywhere: the one episode the export must add.
    return {"p": p, "ids": ids, "current": current, "rotated": rotated,
            "before": current.read_bytes(), "rotated_before": rotated.read_bytes()}


# ---- the export ---------------------------------------------------------------

def test_dry_run_writes_nothing_and_counts_every_case(pg, world):
    out = capture_export.export_new_lines(pg.memory, dry_run=True)
    assert out["ok"] and out["dry_run"] and out["added"] == 0
    assert world["current"].read_bytes() == world["before"]
    assert world["rotated"].read_bytes() == world["rotated_before"]
    assert out["would_add"] == 1
    assert out["present_identical"] == 2          # 1 and 3 (rotated)
    assert out["present_different"] == 1          # 2
    assert out["matched_via_sources"] == 1        # 5
    assert out["matches_forgotten"] == 1          # 6
    assert out["file_only"] == 1                  # 9
    assert out["unparseable"] == 1
    assert out["skipped_forgotten_here"] == 1     # 8


def test_a_run_adds_exactly_the_missing_live_episode_and_nothing_else(pg, world):
    dry = capture_export.export_new_lines(pg.memory, dry_run=True)
    out = capture_export.export_new_lines(pg.memory)
    assert out["ok"] and out["added"] == dry["would_add"] == 1

    data = world["current"].read_bytes()
    before = world["before"]
    # Every earlier byte is unchanged; the old last line got only its newline.
    assert _sha(data[:len(before)]) == _sha(before)
    assert data[len(before):len(before) + 1] == b"\n"
    added = [json.loads(x) for x in data[len(before) + 1:].decode("utf-8").splitlines()]
    p4 = world["p"][4]
    assert added == [{
        "ts": p4["ts"], "summary": p4["summary"], "topics": p4["topics"], "people": [],
        "decisions": [], "preferences": [], "scope": "build", "session_id": p4["session_id"],
    }]
    assert world["rotated"].read_bytes() == world["rotated_before"]
    summaries = {x["summary"] for x in added}
    for n in (6, 7, 8):  # forgotten in the hub, or on this Mac
        assert world["p"][n]["summary"] not in summaries
    assert world["p"][5]["summary"] not in summaries  # merged: matched through its source


def test_a_second_run_adds_nothing(pg, world):
    capture_export.export_new_lines(pg.memory)
    after_first = world["current"].read_bytes()
    out = capture_export.export_new_lines(pg.memory)
    assert out["ok"] and out["added"] == 0 and out["would_add"] == 0
    assert world["current"].read_bytes() == after_first
    assert out["present_identical"] == 3  # 1, 3 and the line the first run added


def test_a_line_that_differs_from_its_hub_row_is_byte_identical_afterwards(pg, world):
    line2 = _line(world["p"][2], preferences=["file-only preference"]).encode("utf-8")
    capture_export.export_new_lines(pg.memory)
    data = world["current"].read_bytes()
    assert data.count(line2) == 1
    assert data.index(line2) == world["before"].index(line2)


def test_an_empty_folder_gets_one_line_per_live_episode(pg):
    ids = [_write(_payload(n)) for n in (20, 21)]
    out = capture_export.export_new_lines(pg.memory)
    assert out["added"] == 2
    lines = (pg.memory / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(x)["summary"] for x in lines] == [_payload(20)["summary"], _payload(21)["summary"]]
    assert len(ids) == 2


def test_a_missing_memory_root_is_a_reported_failure_not_a_write(tmp_path):
    out = capture_export.export_new_lines(tmp_path / "absent")
    assert out["ok"] is False and "memory root missing" in out["error"]
    assert not (tmp_path / "absent").exists()


def test_the_new_line_has_the_readers_shape():
    from datetime import datetime, timezone

    row = {"ts": datetime(2026, 11, 10, 21, 5, tzinfo=timezone.utc), "summary": "s",
           "topics": ["a"], "people": None, "decisions": ["d1", "d2"], "preferences": [],
           "scope": None, "session_id": None}
    assert capture_export.line_for(row) == {
        "ts": "2026-11-10T21:05:00Z", "summary": "s", "topics": ["a"], "people": [],
        "decisions": ["d1", "d2"], "preferences": []}


# ---- hub mode no longer runs the file leg ------------------------------------

def _no_file_leg(*_a, **_k):
    raise AssertionError("capture_v2 ran in hub mode")


def _stats(**extra):
    return {"episode_inserted": True, "topics_written": 0, "episode_id": 7,
            "source_key": "k", "dedup": None, **extra}


def test_a_hub_capture_succeeds_without_capture_v2_and_never_calls_it(monkeypatch):
    monkeypatch.setattr(capture, "run_capture_v2", _no_file_leg)
    monkeypatch.setattr(capture, "_capture_v2", lambda: None)
    monkeypatch.setattr(capture, "write_pg", lambda payload: _stats())
    monkeypatch.setattr("khipu.embed.embed_on_capture", lambda *a, **k: None)
    monkeypatch.setattr("khipu.t3.thread_for_session", lambda sid: None)
    result: dict = {}
    rc = capture.capture({"ts": "2026-11-10T21:00:00Z", "summary": "hub only"}, mode="hub", result=result)
    assert rc == 0 and "reason" not in result and result["episode_id"] == 7


def test_a_hub_capture_whose_hub_write_failed_is_queued_without_the_file_leg(monkeypatch):
    monkeypatch.setattr(capture, "run_capture_v2", _no_file_leg)
    monkeypatch.setattr(capture, "write_pg", mock.Mock(side_effect=ConnectionError("hub down")))
    monkeypatch.setattr("khipu.t3.thread_for_session", lambda sid: None)
    queued = []
    monkeypatch.setattr("khipu.outbox.enqueue", lambda payload, reason: queued.append(reason))
    monkeypatch.delenv("KHIPU_HUB_FILE_MIRROR", raising=False)
    result: dict = {}
    assert capture.capture({"ts": "2026-11-10T21:00:00Z", "summary": "q"}, mode="hub", result=result) == 0
    assert queued == ["ConnectionError"] and "reason" not in result
    # A caller that asks for a failed hub write to read as one still gets that.
    monkeypatch.setenv("KHIPU_HUB_FILE_MIRROR", "0")
    result = {}
    assert capture.capture({"ts": "2026-11-10T21:00:01Z", "summary": "q2"}, mode="hub",
                           result=result) == capture.EX_SOFTWARE
    assert result["reason"] == "hub_pending"


def test_dual_mode_still_runs_the_file_leg(monkeypatch):
    calls = []

    def file_leg(payload, *, suppress_mirror, outcome=None):
        calls.append(suppress_mirror)
        return 0

    monkeypatch.setattr(capture, "run_capture_v2", file_leg)
    monkeypatch.setattr(capture, "write_pg", lambda payload: _stats())
    monkeypatch.setattr("khipu.embed.embed_on_capture", lambda *a, **k: None)
    monkeypatch.setattr("khipu.t3.thread_for_session", lambda sid: None)
    assert capture.capture({"ts": "2026-11-10T21:00:00Z", "summary": "dual"}, mode="dual") == 0
    assert calls == [True]


# ---- the nightly --------------------------------------------------------------

_NIGHTLY_STEPS = ("_drain_sessions", "_capture_export", "_embed_backfill", "_summary_kind_backfill",
                  "_prune_query_cache", "_prune_routing_ledgers", "_storage_sweep",
                  "_provenance_nightly", "_fact_changes_prune", "_ledger_nightly", "_receipt_prune",
                  "_project_renames_sweep", "_recall_eval_step", "_refresh_snapshot")


def _stub_nightly(monkeypatch, tmp_path, order, export):
    monkeypatch.setattr(jobs, "ensure_data_dir", lambda: tmp_path)
    monkeypatch.setattr(jobs, "_CARRIED_STEPS", [])
    monkeypatch.setattr(jobs, "_write_job_state", lambda *a, **k: None)
    for name in _NIGHTLY_STEPS:
        def step(name=name):
            order.append(name)
            return {"ok": True, "failed": 0}
        monkeypatch.setattr(jobs, name, step)
    monkeypatch.setattr(jobs, "_capture_export", lambda: (order.append("_capture_export"), export())[1])

    def script(path, **kw):
        order.append("consolidate")
        return 0
    monkeypatch.setattr(jobs, "_run_script", script)


def test_the_nightly_exports_after_the_drain_and_before_the_consolidate_script(monkeypatch, tmp_path):
    order: list = []
    _stub_nightly(monkeypatch, tmp_path, order, lambda: {"ok": True, "added": 3})
    assert jobs._run_nightly_locked() == 0
    assert order[:3] == ["_drain_sessions", "_capture_export", "consolidate"]
    doc = json.loads((tmp_path / "nightly-last.json").read_text())
    step = next(s for s in doc["steps"] if s["name"] == "capture_export")
    assert step["ok"] is True and step["counts"]["added"] == 3


def test_a_nightly_whose_export_raises_records_it_and_runs_the_rest(monkeypatch, tmp_path):
    from khipu import config

    order: list = []
    # The real step wrapper, around an export that raises.
    _stub_nightly(monkeypatch, tmp_path, order, _REAL_CAPTURE_EXPORT)
    monkeypatch.setattr(config, "path_setting", lambda key: tmp_path if key == "memory_root" else None)
    monkeypatch.setattr(capture_export, "export_new_lines", mock.Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(jobs, "_nightly_log", lambda line: None)
    jobs._run_nightly_locked()
    doc = json.loads((tmp_path / "nightly-last.json").read_text())
    names = [s["name"] for s in doc["steps"]]
    step = next(s for s in doc["steps"] if s["name"] == "capture_export")
    assert step["ok"] is False and "RuntimeError: boom" in step["error"]
    assert names.index("capture_export") < names.index("consolidate_nightly")
    assert "consolidate" in order and "_refresh_snapshot" in order


def test_doctor_has_a_row_for_the_export_step():
    assert jobs._NIGHTLY_STEP_CHECKS["capture_export_ok"][0] == "capture_export"


# ---- the files → hub copy is gone; the topics half stays ---------------------

def test_the_files_to_hub_episode_copy_is_gone():
    for name in ("sync_recent_episodes", "_recover_line_facts", "_legacy_key_conflicts",
                 "_queue_key_conflicts", "_tombstone_only_line"):
        assert not hasattr(mirror, name), name
    # Kept: capture_v2's own write-through in legacy and dual mode imports it.
    assert callable(mirror.mirror_after_capture)


def test_the_outside_nightly_imports_still_resolve():
    # consolidate_nightly._alzy_reconcile_failopen imports these in one try.
    from khipu.embed import backfill  # noqa: F401
    from khipu.mirror import reconcile_memory_root  # noqa: F401
    from khipu.outbox import drain, status  # noqa: F401
    from khipu.session_capture import drain as sessions_drain, liveness_all  # noqa: F401


def test_reconcile_brings_a_topic_edit_into_the_hub_and_no_episode_lines(pg):
    topics = pg.memory / "topics"
    topics.mkdir()
    page = topics / "export-fixture.md"
    page.write_text("---\nstatus: active\n---\n# Export fixture\n\nFirst text.\n", encoding="utf-8")
    (pg.memory / "episodes.jsonl").write_text(_line(_payload(30)) + "\n", encoding="utf-8")

    first = mirror.reconcile_memory_root(pg.memory)
    assert first["topics"] == 1 and first["topics_changed"] == 1 and "episodes_new" not in first
    page.write_text("---\nstatus: active\n---\n# Export fixture\n\nEdited in Obsidian.\n", encoding="utf-8")
    second = mirror.reconcile_memory_root(pg.memory)
    assert second["topics_changed"] == 1
    body = _sql(pg, "SELECT body FROM topics WHERE slug = 'export-fixture' AND deleted_at IS NULL")
    assert body and "Edited in Obsidian." in body[0][0]
    # The episode line was not copied into the hub.
    assert _sql(pg, "SELECT count(*) FROM episodes")[0][0] == 0


def test_the_command_runs_the_export_by_hand(monkeypatch, tmp_path, capsys):
    from khipu import cli

    seen = []

    def export(root, *, dry_run=False):
        seen.append((root, dry_run))
        return {"ok": True, "dry_run": dry_run, "target": "episodes.jsonl", "added": 0, "would_add": 4,
                "present_identical": 1, "present_different": 2, "matched_via_sources": 0,
                "matches_forgotten": 0, "file_only": 0}

    monkeypatch.setattr(capture_export, "export_new_lines", export)
    rc = cli.main(["capture", "export", "--memory-root", str(tmp_path), "--dry-run", "--json"])
    assert rc == 0 and seen == [(tmp_path, True)]
    assert json.loads(capsys.readouterr().out)["would_add"] == 4
