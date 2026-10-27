"""Synthetic legacy comma projections: retain evidence, never invent facts."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from khipu import capture, capture_stores, db, forget, outbox, paths
from tests.test_capture import _EpisodesFakeCursor, _FakeConn
from tests.test_forget_write_gate import _complete_forget


REASON = "recovery: the legacy key holds different facts"
ORIGINAL = {
    "ts": "2026-10-01T09:00:00Z", "summary": "Synthetic legacy projection",
    "session_id": "codex:projection", "harness": "codex", "transcript_range": "0:10",
    "window_id": "window-1", "part": "1/1", "topics": [], "people": [],
    "preferences": [], "decisions": ["Use alpha, beta,, gamma", "  Keep delta, epsilon  "],
}


def projection(original=ORIGINAL):
    queued = deepcopy(original)
    for key in ("harness", "transcript_range", "window_id", "part"):
        queued.pop(key, None)
    queued["decisions"] = [s.strip() for s in ", ".join(original["decisions"]).split(",") if s.strip()]
    return queued


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "native"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    monkeypatch.setattr(capture, "_inherit_project", lambda *a: None)
    monkeypatch.setattr(capture, "_observe_checkout", lambda *a: None)
    monkeypatch.setattr(capture, "_note_project_root", lambda *a: None)
    monkeypatch.setattr(capture, "dedup_before_insert", Mock(return_value={"action": "none"}))
    monkeypatch.setattr(capture, "_stamp_origin", lambda *a: None)
    monkeypatch.setattr(capture, "_claim_unowned_episode", Mock())
    monkeypatch.setattr("khipu.config.path_setting", lambda *a: None)
    monkeypatch.setattr("khipu.hygiene.classify_topics", lambda *a, **kw: ([], [], False))
    monkeypatch.setattr("khipu.topic_graph.persist_capture_graph", Mock())
    monkeypatch.setattr("khipu.embed.embed_on_capture", Mock())
    monkeypatch.setattr(capture, "run_fact_steps", Mock())
    original = deepcopy(ORIGINAL)
    row = dict(original, id=1, raw=deepcopy(original), deleted_at=None)
    cur = _EpisodesFakeCursor({1: row})
    conn = _FakeConn(cur)
    monkeypatch.setattr(db, "connect", lambda: conn)
    return cur, original


def queue(payload=None, **kwargs):
    return outbox.enqueue(projection() if payload is None else payload,
                          reason=kwargs.pop("reason", REASON), origin=kwargs.pop("origin", None))


def test_exact_comma_projection_archives_evidence_without_hub_mutation(world):
    cur, original = world
    before = deepcopy(cur.episodes)
    path = queue()
    evidence = json.loads(path.read_text())
    report = outbox.drain()
    assert report.get("resolved") == 1, report
    assert not path.exists()
    records = list((outbox.outbox_dir() / "resolved").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["payload"] == original  # forget/restore use the original source identity
    assert record["queued_evidence"] == evidence
    assert record["resolution"]["kind"] == "legacy_comma_projection"
    assert record["resolution"]["episode_id"] == 1
    assert record["resolution"]["original_payload_sha256"]
    assert record["resolution"]["queued_payload_sha256"]
    assert cur.episodes == before
    assert not [s for s, _ in cur.calls if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    capture.dedup_before_insert.assert_not_called()
    capture._claim_unowned_episode.assert_not_called()
    capture.run_fact_steps.assert_not_called()
    assert outbox.status()["resolved"] == 1
    assert outbox.drain()["jobs"] == 0
    assert outbox.retry_dead()["moved"] == 0


def test_real_write_pg_raw_defaults_round_trip_through_legacy_projection(world, monkeypatch):
    # Populate raw through the production writer, rather than constructing the
    # stored shape ourselves. Its local copy gains tags/scope; the legacy file
    # writer receives this still-unenriched input in capture().
    cur = _EpisodesFakeCursor()
    monkeypatch.setattr(db, "connect", lambda: _FakeConn(cur))
    pre_enrichment = deepcopy(ORIGINAL)
    assert capture.write_pg(pre_enrichment)["episode_inserted"] is True
    assert "tags" not in pre_enrichment and "scope" not in pre_enrichment
    original_raw = deepcopy(cur.episodes[1]["raw"])
    assert original_raw == {**ORIGINAL, "tags": [], "scope": None}
    before = deepcopy(cur.episodes)
    cur.calls.clear()
    capture.dedup_before_insert.reset_mock()
    capture.run_fact_steps.reset_mock()
    queued = projection(pre_enrichment)
    path = queue(queued)
    evidence = json.loads(path.read_text())
    assert outbox.drain().get("resolved") == 1
    record = json.loads(next((outbox.outbox_dir() / "resolved").glob("*.json")).read_text())
    assert record["payload"] == original_raw and record["queued_evidence"] == evidence
    assert cur.episodes == before
    assert not [s for s, _ in cur.calls if s.startswith(("INSERT", "UPDATE", "DELETE"))]
    capture.dedup_before_insert.assert_not_called()
    capture.run_fact_steps.assert_not_called()


@pytest.mark.parametrize("stored, supplied", [
    ({"tags": []}, {"tags": ["changed-tag"]}), ({"tags": ["real-tag"]}, {"tags": ["other-tag"]}),
    ({"scope": "real-scope"}, {"scope": "other-scope"}),
])
def test_changed_writer_defaults_remain_conflicts(world, stored, supplied):
    cur, _ = world
    cur.episodes[1]["raw"].update(stored)
    path = queue({**projection(), **supplied})
    assert outbox.drain()["failed"] == 1
    assert path.exists()
    assert json.loads(path.read_text())["evidence_conflict"] is True


@pytest.mark.parametrize("change", [
    {"preferences": ["Keep real enrichment"]}, {"unknown_evidence": "new"},
    {"decisions": list(reversed(projection()["decisions"]))},
    {"decisions": ["changed", *projection()["decisions"][1:]]},
    {"decisions": projection()["decisions"][:-1]},
    {"harness": "different"}, {"people": ["extra person"]},
])
def test_distinct_evidence_remains_a_conflict(world, change):
    path = queue({**projection(), **change})
    before = json.loads(path.read_text())["payload"]
    report = outbox.drain()
    assert report["failed"] == 1
    assert path.exists()
    job = json.loads(path.read_text())
    assert job["payload"] == before and job["evidence_conflict"] is True
    assert not list((outbox.outbox_dir() / "resolved").glob("*.json"))


@pytest.mark.parametrize("reason,origin", [("ordinary capture", None), (REASON, "local:aaaaaaaaaaaa")])
def test_projection_requires_explicit_unknown_owner_file_recovery(world, reason, origin):
    path = queue(reason=reason, origin=origin)
    assert outbox.drain()["failed"] == 1
    assert path.exists()


@pytest.mark.parametrize("stored, supplied", [
    ({"tags": ["real-tag"]}, {}), ({"scope": "real-scope"}, {}), ({"scope": None}, {"scope": ""}),
    ({"tags": []}, {"tags": None}), ({"scope": "real-scope"}, {"scope": None}),
])
def test_line_without_a_value_the_hub_holds_is_landed(world, stored, supplied):
    cur, _ = world
    cur.episodes[1]["raw"].update(stored)
    before = deepcopy(cur.episodes)
    path = queue({**projection(), **supplied})
    assert outbox.drain().get("resolved") == 1
    assert not path.exists()
    [record] = (outbox.outbox_dir() / "resolved").glob("*.json")
    assert json.loads(record.read_text())["resolution"]["kind"] == "legacy_landed_line"
    assert cur.episodes == before


def test_line_missing_a_field_the_hub_holds_is_landed(world):
    queued = projection()
    queued.pop("preferences")
    path = queue(queued)
    assert outbox.drain().get("resolved") == 1
    assert not path.exists()


def test_hub_only_fields_do_not_hold_a_landed_line(world):
    cur, _ = world
    cur.episodes[1]["raw"]["future_evidence"] = ["preserve this"]
    before = deepcopy(cur.episodes)
    path = queue()
    assert outbox.drain().get("resolved") == 1
    assert not path.exists()
    assert cur.episodes == before


def test_python_equal_but_json_distinct_values_are_not_projection_proof(world):
    cur, _ = world
    cur.episodes[1]["raw"]["future_evidence"] = 1
    path = queue({**projection(), "future_evidence": True})
    assert outbox.drain()["failed"] == 1
    assert path.exists()


@pytest.mark.parametrize("decisions", [["one, , two,,", "  three  "], ["single decision"], []])
def test_only_explicit_identity_absence_is_ignored(world, decisions):
    cur, _ = world
    original = {**deepcopy(ORIGINAL), "decisions": decisions}
    cur.episodes[1]["raw"] = original
    queued = projection(original)
    queued.pop("session_id")
    queue(queued)
    assert outbox.drain().get("resolved") == 1


@pytest.mark.parametrize("origin", [None, "local:aaaaaaaaaaaa"])
def test_real_conflicts_are_bounded_held_and_visible(world, monkeypatch, origin):
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 2)
    path = queue({**projection(), "preferences": ["Unmerged preference"]}, origin=origin)
    outbox.drain()
    report = outbox.drain()
    assert report.get("buried") == 1, report
    assert not path.exists()
    held = json.loads((outbox.dead_dir() / path.name).read_text())
    assert held["attempts"] == 2 and held["payload"]["preferences"] == ["Unmerged preference"]
    assert "owner" in held["hold_reason"].lower()
    status = outbox.status()
    assert status["pending"] == 0 and status["dead"] == 1
    assert status["evidence_conflicts"] == 1 and status["held_conflicts"] == 1
    assert "owner" in status["fix"].lower()
    assert outbox.drain()["jobs"] == 0


@pytest.mark.parametrize("exc", [capture.ClaimDeferred("claim"), capture.FactStepDeferred("fact")])
def test_transient_deferrals_stay_pending_at_retry_limit(world, monkeypatch, exc):
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(capture, "write_pg", Mock(side_effect=exc))
    path = queue(reason="ordinary capture")
    assert outbox.drain()["deferred"] == 1
    assert path.exists() and not outbox.dead_jobs()


def test_failed_resolution_journal_retains_both_evidence_and_attempt(world, monkeypatch):
    path = queue()
    before = path.read_bytes()
    atomic = outbox._atomic_write

    def fail_record(dst, job):
        if dst.parent.name == "resolved":
            raise OSError("synthetic journal failure")
        return atomic(dst, job)

    monkeypatch.setattr(outbox, "_atomic_write", fail_record)
    report = outbox.drain()
    assert report.get("resolution_failed") == 1, report
    retained = json.loads(path.read_bytes())
    assert retained["payload"] == json.loads(before)["payload"]
    assert retained["attempts"] == 1
    assert retained["resolution_evidence"]["payload"] == ORIGINAL
    assert retained["resolution_evidence"]["queued_evidence"] == json.loads(before)
    assert not list((outbox.outbox_dir() / "resolved").glob("*.json"))


def test_archive_publication_then_unlink_failure_is_idempotent(world, monkeypatch):
    from pathlib import Path
    path = queue()
    unlink = Path.unlink

    def fail_queue_unlink(self, *a, **kw):
        if self == path:
            raise OSError("synthetic crash after journal publication")
        return unlink(self, *a, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail_queue_unlink)
        assert outbox.drain().get("resolution_failed") == 1
    records = list((outbox.outbox_dir() / "resolved").glob("*.json"))
    assert len(records) == 1 and path.exists()
    before = records[0].read_bytes()
    assert outbox.drain().get("resolved") == 1
    assert not path.exists()
    assert records[0].read_bytes() == before
    assert len(list(records[0].parent.glob("*.json"))) == 1


def test_distinct_job_histories_do_not_overwrite_resolution(world):
    path = queue()
    first = json.loads(path.read_text())
    assert outbox.drain().get("resolved") == 1
    path = queue()
    second = json.loads(path.read_text())
    assert first["queued_at"] != second["queued_at"]
    assert outbox.drain().get("resolved") == 1
    records = [json.loads(p.read_text()) for p in (outbox.outbox_dir() / "resolved").glob("*.json")]
    assert len(records) == 2
    assert {r["queued_evidence"]["queued_at"] for r in records} == {first["queued_at"], second["queued_at"]}


def test_original_tombstone_published_after_proof_prevents_archive(world, monkeypatch):
    from khipu import tombstones
    path = queue()
    write = capture.write_pg

    def proof_then_forget(*a, **kw):
        result = write(*a, **kw)
        assert result.get("legacy_projection_duplicate")
        tombstones.publish_payloads([ORIGINAL])
        return result

    monkeypatch.setattr(capture, "write_pg", proof_then_forget)
    assert outbox.drain().get("forgotten") == 1
    assert not path.exists()
    assert not list((outbox.outbox_dir() / "resolved").glob("*.json"))


def test_deleted_original_is_never_used_as_projection_proof(world):
    cur, _ = world
    cur.episodes[1]["deleted_at"] = "2026-10-25T00:00:00Z"
    queue()
    assert outbox.drain().get("resolved", 0) == 0
    assert not list((outbox.outbox_dir() / "resolved").glob("*.json"))


def test_resolved_evidence_is_in_forget_and_restore_inventory(world, tmp_path, monkeypatch):
    queue()
    assert outbox.drain().get("resolved") == 1
    folder = outbox.outbox_dir() / "resolved"
    record = next(folder.glob("*.json"))
    data = record.read_bytes()
    crash_temp = record.parent / ".resolved.synthetic.tmp"
    crash_temp.write_bytes(data)
    assert folder in capture_stores.outbox_folders()
    restore = capture_stores.restore_entry(("outbox", "resolved", record.name))
    assert restore and restore.scrub == "queued job"
    assert any(entry["path"] == record for entry in forget._outbox_entries())
    _complete_forget(tmp_path, monkeypatch, ORIGINAL)
    assert not record.exists() and not crash_temp.exists()
    from khipu.tombstones import read_gate
    assert paths._job_refusal(data, read_gate()) == "forgotten"


def test_two_conflict_histories_survive_bury_and_retry_dead(world, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 1)
    first_path = queue({**projection(), "preferences": ["first real enrichment"]})
    assert outbox.drain().get("buried") == 1
    first_dead = outbox.dead_jobs()[0]
    first_bytes = first_dead.read_bytes()
    second_path = queue({**projection(), "preferences": ["second real enrichment"]})
    assert second_path == first_path  # the readable legacy name became free
    assert outbox.drain().get("buried") == 1
    assert first_dead.read_bytes() == first_bytes
    held = [json.loads(path.read_text()) for path in outbox.dead_jobs()]
    assert len(held) == 2
    histories = {(job["payload"]["preferences"][0], job["queued_at"], job["last_attempt_at"]) for job in held}
    assert len(histories) == 2
    assert outbox.retry_dead()["moved"] == 2
    queued = [json.loads(path.read_text()) for path in outbox.jobs()]
    assert len(queued) == 2
    assert {(job["payload"]["preferences"][0], job["queued_at"], job["last_attempt_at"]) for job in queued} == histories
    assert all(job["attempts"] == 0 for job in queued)


def test_retry_dead_preserves_occupied_root_job_and_held_history(world, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 1)
    path = queue({**projection(), "preferences": ["held evidence"]})
    assert outbox.drain().get("buried") == 1
    held = json.loads(outbox.dead_jobs()[0].read_text())
    occupied = queue({**projection(), "preferences": ["new queued evidence"]})
    assert occupied == path
    before = occupied.read_bytes()
    assert outbox.retry_dead()["moved"] == 1
    assert occupied.read_bytes() == before
    jobs = [json.loads(item.read_text()) for item in outbox.jobs()]
    assert len(jobs) == 2
    retried = next(job for job in jobs if job["payload"]["preferences"] == ["held evidence"])
    assert retried["payload"] == held["payload"]
    assert retried["queued_at"] == held["queued_at"]
    assert retried["last_attempt_at"] == held["last_attempt_at"]


def test_transition_reuses_only_equal_complete_envelope(world):
    from pathlib import Path
    job = {"payload": projection(), "origin": None, "attempts": 1,
           "queued_at": "2026-10-01T09:01:00Z", "last_attempt_at": "2026-10-01T09:02:00Z"}
    preferred = outbox.outbox_dir() / "same-name.json"
    preferred.parent.mkdir(parents=True, exist_ok=True)
    preferred.write_text(json.dumps(job))
    assert outbox._transition_destination(preferred, deepcopy(job)) == preferred
    changed = {**job, "attempts": 2}
    alternate = outbox._transition_destination(preferred, changed)
    assert alternate != preferred
    alternate.write_text(json.dumps(changed))
    assert outbox._transition_destination(preferred, changed) == alternate
    original_bytes = preferred.read_bytes()
    alternate.write_text(json.dumps({**changed, "origin": "local:bbbbbbbbbbbb"}))
    with pytest.raises(FileExistsError):
        outbox._transition_destination(preferred, changed)
    assert preferred.read_bytes() == original_bytes


SECRET = "sk-ant-" + "a1B2c3D4e5" * 3


def _redacted_hub_projection(world, middle):
    cur, _ = world
    stored = deepcopy(ORIGINAL)
    stored["decisions"] = ["Use alpha, [REDACTED], gamma"]
    cur.episodes[1].update(stored, raw=deepcopy(stored))
    queued = deepcopy(stored)
    for key in ("harness", "transcript_range", "window_id", "part"):
        queued.pop(key, None)
    queued["decisions"] = ["Use alpha", middle, "gamma"]
    path = queue(queued)
    return path, outbox.drain()


def test_a_literal_redacted_marker_projection_resolves(world):
    _, report = _redacted_hub_projection(world, "[REDACTED]")
    assert report.get("resolved") == 1, report


def test_a_projection_holding_a_secret_the_hub_stored_redacted_resolves(world):
    path, report = _redacted_hub_projection(world, SECRET)
    assert report.get("resolved") == 1, report
    assert not path.exists()
    [record] = (outbox.outbox_dir() / "resolved").glob("*.json")
    outbox._validate_resolution(json.loads(record.read_text()))


def test_a_buried_evidence_conflict_names_retry_dead(world):
    path = queue(deepcopy(ORIGINAL))
    job = json.loads(path.read_text())
    job.update(evidence_conflict=True, attempts=outbox.MAX_ATTEMPTS)
    outbox._bury(path, job)
    st = outbox.status()
    assert st["pending"] == 0 and st["dead"] == 1 and st["evidence_conflicts"] >= 1, st
    assert "khipu outbox retry-dead" in st["fix"], st["fix"]
