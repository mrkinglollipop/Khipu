"""A faithful replay of a landed capture is a duplicate, not a conflict.

The source hash is taken before enrichment and a row's raw is stored after it,
so at the legacy key that hash can never match an enriched row: the replay
failed 25 times and was buried. ``capture._replay_lacks`` proves the replay
instead, and ``CaptureEvidenceConflict`` keeps guarding everything else.

Also pinned here, on the real schema: which writers the refusal for not
announcing a job's identities can reach. Every database is a hermetic one.
"""
from __future__ import annotations

from copy import deepcopy
import json
from unittest import mock

import pytest
from psycopg import errors as pg_errors

from khipu import capture, capture_stores, commitments, db, outbox, provenance, tombstones
from khipu.capture_identity import source_identity
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

PROJECT = "acme/replay"
RECOVERY = "recovery: the legacy key holds different facts"
# The shape that reaches the legacy key: a window id with no part (the Sep 14
# app never sets it), so the source key is unsafe to answer and the column
# match compares raw->>'part' with NULL.
JOB = {
    "ts": "2026-11-01T01:00:00Z",
    "summary": "Shipped the replay fence after reviewing the release checklist",
    "session_id": "codex:replay", "harness": "codex", "event": "stop",
    "transcript_range": "100:200", "window_id": "window-1", "project": PROJECT,
    "scope": "codex stop " + "with a run-on sentence that normalize_scope refuses to keep " * 8,
    "topics": ["Replay-Fence", "tmp"],
    "decisions": ["Keep the fence strict"],
    "open_loops": [{"text": "Review the deployment checklist before release", "kind": "followup",
                    "owner": "assistant"}],
    "closed_loops": [{"text": "Finished the release notes"}],
    "people": ["Ada"], "preferences": ["Short commit subjects"],
    "decision_details": [{"text": "Keep the fence strict", "rationale": "a real conflict stays held"}],
    "verbatim": {"quotes": ["the fence stays"]}, "truncated_chars": 0, "edges": [], "topic_pages": [],
}
SECRET_TEXT = "only-in-this-job"


@pytest.fixture
def pg(hub, monkeypatch, tmp_path):  # noqa: F811
    from khipu import embed, topic_graph

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "native"))
    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    provenance._reset_ready_cache()
    monkeypatch.setattr(db, "connect", lambda *a, **kw: hub.connect())
    monkeypatch.setattr(embed, "embed_on_capture", lambda *a, **kw: None)
    monkeypatch.setattr(embed, "embed_one", mock.Mock(side_effect=RuntimeError("no provider")))
    monkeypatch.setattr(topic_graph, "persist_capture_graph", lambda *a, **kw: {})
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    monkeypatch.setattr(commitments, "_has_commitment_embeddings", lambda cur, ids: False)
    return hub


@pytest.fixture
def at_the_key(pg, monkeypatch):
    """The similarity leg off: these tests are about the legacy key itself."""
    monkeypatch.setattr(capture, "_dedup_candidates", lambda cur, payload: [])
    return pg


def job(**changes):
    out = deepcopy(JOB)
    out.update(changes)
    return out


def land(payload=None):
    return capture.write_pg(deepcopy(JOB if payload is None else payload))["episode_id"]


def row(hub, episode_id):  # noqa: F811
    with hub.connect() as conn:
        return conn.execute("SELECT raw, decisions, tags, topics FROM episodes WHERE id = %s",
                            (episode_id,)).fetchone()


def counts(hub):  # noqa: F811
    with hub.connect() as conn:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("episodes", "commitments", "decisions")}


def bury(payload, *, reason, origin=None, **fields):
    queued = outbox.enqueue(deepcopy(payload), reason=reason, origin=origin)
    envelope = json.loads(queued.read_text())
    envelope.update(attempts=outbox.MAX_ATTEMPTS, **fields)
    outbox.dead_dir().mkdir(parents=True, exist_ok=True)
    (outbox.dead_dir() / queued.name).write_text(json.dumps(envelope))
    queued.unlink()


def records():
    return [json.loads(p.read_text()) for p in sorted(outbox.resolved_dir().glob("*.json"))]


# ---- why the replay fails: no list is gated on the way to raw ---------------------

def test_write_rewrites_labels_only_so_a_list_difference_is_a_different_payload(at_the_key):
    raw, _decisions, tags, _topics = row(at_the_key, land())
    for key in ("decisions", "open_loops", "closed_loops", "people", "preferences", "decision_details"):
        assert raw[key] == JOB[key], key
    assert "tmp" in tags and raw["topics"] != JOB["topics"]
    assert raw["scope"] != JOB["scope"]
    assert source_identity(deepcopy(JOB))["payload_sha256"] != source_identity(raw)["payload_sha256"]


# ---- a faithful replay ------------------------------------------------------------

def test_a_buried_faithful_replay_resolves_as_a_duplicate_with_a_record(at_the_key):
    episode = land()
    bury(JOB, reason="CaptureEvidenceConflict", evidence_conflict=True,
         last_error="CaptureEvidenceConflict: legacy episode key conflicts")
    before, raw_before = counts(at_the_key), row(at_the_key, episode)
    assert outbox.retry_dead()["moved"] == 1
    report = outbox.drain()
    assert report.get("resolved") == 1 and not report["failed"], report
    assert (outbox.jobs(), outbox.dead_jobs()) == ([], [])
    (record,) = records()
    assert record["resolution"]["kind"] == capture.REPLAY_KIND
    assert record["resolution"]["episode_id"] == episode
    assert record["payload"] == raw_before[0]
    assert record["queued_evidence"]["payload"] == JOB
    # Nothing moved on the hub, and forget reads both identities in the record.
    assert counts(at_the_key) == before and row(at_the_key, episode) == raw_before
    assert JOB in capture_stores.outbox_payloads(record)
    assert outbox.status()["resolved"] == 1 and outbox.status()["evidence_conflicts"] == 0
    assert outbox.is_resolved_payload(deepcopy(JOB))
    # Idempotent: the same capture again is the same duplicate, not a second row.
    assert capture.write_pg(deepcopy(JOB))["episode_id"] == episode
    assert counts(at_the_key) == before


def test_a_job_queued_before_origins_resolves_too(at_the_key):
    land()
    bury(JOB, reason="CaptureEvidenceConflict")
    (path,) = outbox.dead_jobs()
    envelope = json.loads(path.read_text())
    del envelope["origin"]
    path.write_text(json.dumps(envelope))
    outbox.retry_dead()
    assert outbox.drain().get("resolved") == 1
    assert outbox.is_resolved_payload(deepcopy(JOB))


def test_a_row_holding_more_than_the_job_is_still_its_replay(at_the_key):
    land(job(decisions=[*JOB["decisions"], "A decision only the row holds"],
             people=["Ada", "Grace"]))
    outbox.enqueue(job(), reason="CaptureEvidenceConflict")
    assert outbox.drain().get("resolved") == 1


def test_a_live_capture_that_is_a_replay_returns_no_row_text_and_queues_nothing(at_the_key, monkeypatch):
    monkeypatch.setenv("KHIPU_HUB_FILE_MIRROR", "0")
    episode = land()
    out = {}
    assert capture.capture(job(), mode="hub", result=out) == 0
    assert out["episode_id"] == episode and out["dedup"]["matched_via"] == capture.REPLAY_KIND
    assert "legacy_projection_duplicate" not in out
    assert outbox.jobs() == []


def test_the_record_proves_itself_and_a_forged_one_is_refused(at_the_key):
    from khipu.capture_identity import payload_sha256

    land()
    outbox.enqueue(job(), reason="CaptureEvidenceConflict")
    outbox.drain()
    (record,) = records()
    outbox._validate_resolution(record)
    record["payload"]["decisions"] = []
    record["resolution"]["original_payload_sha256"] = payload_sha256(record["payload"])
    with pytest.raises(ValueError):
        outbox._validate_resolution(record)


def test_any_other_kind_still_needs_a_recovery_envelope(at_the_key):
    land()
    outbox.enqueue(job(), reason="CaptureEvidenceConflict")
    outbox.drain()
    (record,) = records()
    record["resolution"]["kind"] = "legacy_landed_line"
    with pytest.raises(ValueError, match="queued envelope"):
        outbox._validate_resolution(record)


# ---- the guard stays --------------------------------------------------------------

@pytest.mark.parametrize("changes", [
    {"transcript_range": "100:999"}, {"session_id": "codex:other"}, {"event": "sessionend"},
    {"window_id": "window-2"}, {"harness": "cursor"},
], ids=lambda c: next(iter(c)))
def test_another_capture_at_the_key_still_conflicts(at_the_key, changes):
    episode = land()
    before, raw_before = counts(at_the_key), row(at_the_key, episode)
    with pytest.raises(capture.CaptureEvidenceConflict):
        capture.write_pg(job(**changes))
    assert counts(at_the_key) == before and row(at_the_key, episode) == raw_before
    assert records() == []


@pytest.mark.parametrize("held", [
    {"summary": "A different summary on the row's raw"}, {"ts": "2026-11-01T02:00:00Z"},
], ids=lambda c: next(iter(c)))
def test_a_raw_with_another_summary_or_ts_at_the_key_still_conflicts(at_the_key, held):
    episode = land()
    with at_the_key.connect() as conn:
        conn.execute("UPDATE episodes SET raw = raw || %s::jsonb WHERE id = %s", (json.dumps(held), episode))
        conn.commit()
    with pytest.raises(capture.CaptureEvidenceConflict):
        capture.write_pg(job())


@pytest.mark.parametrize("changes", [
    {"summary": "A different summary is another key"}, {"ts": "2026-11-01T03:00:00Z"},
], ids=lambda c: next(iter(c)))
def test_another_summary_or_ts_is_another_key_and_lands_whole(at_the_key, changes):
    episode = land()
    stats = capture.write_pg(job(**changes))
    assert stats["episode_inserted"] and stats["episode_id"] != episode


@pytest.mark.parametrize("field,value", [
    ("decisions", ["Another decision entirely"]),
    ("open_loops", [{"text": "Call the vendor back", "kind": "followup", "owner": "assistant"}]),
    ("closed_loops", [{"text": "Closed the vendor thread"}]),
    ("people", ["Grace"]),
    ("preferences", ["Long commit subjects"]),
    ("decision_details", [{"text": "Another", "rationale": "another"}]),
    ("deliverables", [{"kind": "file", "path": "docs/other.md", "title": "other"}]),
    ("verbatim", {"quotes": ["a quote the row lacks"]}),
    ("a_field_added_later", "evidence no allowlist names"),
], ids=lambda v: v if isinstance(v, str) else "")
def test_same_capture_with_content_the_row_lacks_is_held_and_named(at_the_key, field, value):
    episode = land()
    before, raw_before = counts(at_the_key), row(at_the_key, episode)
    more = job(**{field: value})
    with pytest.raises(capture.CaptureEvidenceConflict) as err:
        capture.write_pg(deepcopy(more))
    assert f"only the job holds: {field}" in str(err.value)
    assert counts(at_the_key) == before and row(at_the_key, episode) == raw_before

    queued = outbox.enqueue(more, reason="CaptureEvidenceConflict")
    report = outbox.drain()
    assert not report.get("resolved") and report["failed"] == 1
    envelope = json.loads(queued.read_text())
    assert envelope["payload"] == more and envelope["evidence_conflict"] is True
    assert field in envelope["last_error"] and records() == []


def test_the_conflict_names_fields_never_their_text(at_the_key):
    land()
    with pytest.raises(capture.CaptureEvidenceConflict) as err:
        capture.write_pg(job(decisions=[SECRET_TEXT], **{f"Free text {SECRET_TEXT}": SECRET_TEXT}))
    assert SECRET_TEXT not in str(err.value)
    assert "decisions" in str(err.value) and "other" in str(err.value)


def test_held_content_is_buried_at_the_attempt_limit_like_any_conflict(at_the_key):
    land()
    queued = outbox.enqueue(job(decisions=["Another decision entirely"]), reason="CaptureEvidenceConflict")
    envelope = json.loads(queued.read_text())
    envelope["attempts"] = outbox.MAX_ATTEMPTS - 1
    queued.write_text(json.dumps(envelope))
    assert outbox.drain().get("buried") == 1
    (dead,) = outbox.dead_jobs()
    assert json.loads(dead.read_text())["hold_reason"].startswith("Distinct capture evidence")


def test_an_item_a_merged_capture_brought_counts_as_held():
    stored = {**JOB, "decisions": [], "merged_from": [{"payload": {"decisions": JOB["decisions"]}}]}
    assert capture._replay_lacks(stored, job()) == []
    assert capture._replay_lacks({**stored, "merged_from": []}, job()) == ["decisions"]


@pytest.mark.parametrize("stored,incoming,expected", [
    ({"ts": "2026-11-01T01:00:00+00:00"}, {}, []),
    ({"ts": "2026-10-31T21:00:00-04:00"}, {}, []),
    ({"part": "1/2"}, {}, []),
    ({}, {"part": "2/2"}, []),
    ({"part": "1/2"}, {"part": "2/2"}, None),
    ({"transcript_sha256": "a"}, {"transcript_sha256": "b"}, None),
    ({"truncated_chars": 7}, {}, []),
    ({}, {"truncated_chars": 7}, ["truncated_chars"]),
    ({}, {"transcript_range": None}, None),
    ({}, {"session_id": ""}, None),
], ids=str)
def test_replay_identity(stored, incoming, expected):
    assert capture._replay_lacks({**JOB, **stored}, {**JOB, **incoming}) == expected


# ---- the file-recovery path -------------------------------------------------------

def recovery_replay(payload):
    return capture.write_pg(deepcopy(payload), legacy_projection=True)


def test_a_recovery_replay_resolves_as_a_duplicate_without_touching_the_hub(at_the_key):
    episode = land()
    before, raw_before = counts(at_the_key), row(at_the_key, episode)
    bury(JOB, reason=RECOVERY, evidence_conflict=True)
    outbox.retry_dead()
    report = outbox.drain()
    assert report.get("resolved") == 1 and not report["failed"], report
    (record,) = records()
    assert record["resolution"]["kind"] == capture.REPLAY_KIND
    assert record["queued_evidence"]["reason"] == RECOVERY
    assert counts(at_the_key) == before and row(at_the_key, episode) == raw_before


@pytest.mark.parametrize("changes", [
    {"transcript_range": "100:999"}, {"session_id": "codex:other"}, {"event": "sessionend"},
    {"decisions": ["Another decision entirely"]}, {"open_loops": [{"text": "Call the vendor back"}]},
], ids=lambda c: next(iter(c)))
def test_a_recovery_line_that_is_not_a_replay_still_conflicts(at_the_key, changes):
    land()
    with pytest.raises(capture.CaptureEvidenceConflict):
        recovery_replay(job(**changes))


def test_a_lossy_line_with_no_range_proves_nothing_and_stays_for_the_owner(at_the_key):
    land()
    line = job()
    for key in ("transcript_range", "window_id", "harness", "event"):
        line.pop(key)
    with pytest.raises(capture.CaptureEvidenceConflict):
        recovery_replay(line)


REFUSED = {"last_error": "CheckViolation: khipu: an episode write must announce its source identities "
                         "(khipu.source_identities)"}


# ---- who can reach the unannounced refusal -------------------------------------

def test_an_unannounced_insert_is_refused_and_buried_and_the_current_writer_lands_it(at_the_key, monkeypatch):
    with monkeypatch.context() as old_app:
        # A writer from before announce_hub_identities (2026-10-15).
        old_app.setattr(tombstones, "announce_hub_identities", lambda *a, **kw: None)
        with pytest.raises(pg_errors.CheckViolation) as err:
            capture.write_pg(job())
        assert err.value.diag.constraint_name == "khipu_unannounced"
        queued = outbox.enqueue(job(), reason="CheckViolation")
        envelope = json.loads(queued.read_text())
        envelope["attempts"] = outbox.MAX_ATTEMPTS - 1
        queued.write_text(json.dumps(envelope))
        # Unlike 08K01 ("predates fact provenance") it is not deferred: it buries.
        assert outbox.drain().get("buried") == 1
    assert outbox.retry_dead()["moved"] == 0
    assert capture.write_pg(job())["episode_inserted"]
    assert counts(at_the_key)["episodes"] == 1


def test_a_failed_probe_raises_its_own_error_not_a_refusal_later(at_the_key, monkeypatch):
    def failing(cur, table):
        cur.execute("SELECT 1/0")

    with at_the_key.connect() as conn, conn.cursor() as cur:
        monkeypatch.setattr(db, "has_table", failing)
        with pytest.raises(pg_errors.DivisionByZero):
            tombstones.announce_hub_identities(cur, job(), source_identity(job()))
        conn.rollback()


def test_a_cursor_with_no_database_still_skips_the_announcement(monkeypatch):
    cur = mock.Mock()
    monkeypatch.setattr(db, "has_table", mock.Mock(side_effect=RuntimeError("no real cursor")))
    tombstones.announce_hub_identities(cur, job(), source_identity(job()))
    cur.execute.assert_not_called()


def test_a_false_or_zero_field_is_evidence_except_truncated_chars():
    assert capture._replay_lacks({**JOB, "flag": True}, {**JOB, "flag": False}) == ["flag"]
    assert capture._replay_lacks({**JOB, "ratio": 1.5}, {**JOB, "ratio": 0.0}) == ["ratio"]
    assert capture._replay_lacks(dict(JOB), {**JOB, "truncated_chars": 0}) == []


def test_a_job_project_on_a_row_without_one_is_not_a_replay():
    row = {**JOB, "project": None, "repo_root": None}
    assert capture._replay_lacks(row, {**JOB, "project": "acme/other", "repo_root": "/x"}) == ["project", "repo_root"]


def test_a_merged_list_counts_only_where_the_merge_folds_it():
    merged = {"merged_from": [{"payload": {"edges": ["a"], "decisions": ["d"]}}]}
    row = {**JOB, **merged}
    assert capture._replay_lacks(row, {**JOB, "edges": ["a"]}) == ["edges"]
    assert capture._replay_lacks(row, {**JOB, "decisions": [*JOB.get("decisions", []), "d"]}) == []
