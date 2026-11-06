"""Blank summaries keep their text; distinct natural captures get subsecond keys."""
from copy import deepcopy

import pytest

from khipu import capture, outbox, forget
from tests.test_capture_replay_duplicate import pg, at_the_key, job, counts  # noqa: F401
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


def blank(session="one", **changes):
    return job(summary="", summary_kind="verbatim_only", session_id="codex:" + session,
               **changes)


def test_two_blank_sessions_replay_and_forget(at_the_key):
    first, second = blank(), blank("two")
    a = capture.write_pg(deepcopy(first))
    b = capture.write_pg(deepcopy(second))
    assert a["episode_id"] != b["episode_id"]
    assert a["verbatim_stored"] and b["verbatim_stored"]
    for payload, saved in ((first, a), (second, b)):
        for _ in range(3):
            replay = capture.write_pg(deepcopy(payload))
            assert replay["episode_id"] == saved["episode_id"]
            assert not replay["episode_inserted"] and replay["verbatim_stored"]
    assert counts(at_the_key)["episodes"] == 2
    with at_the_key.connect() as conn:
        rows = conn.execute("SELECT ts, summary, raw->>'summary_kind' FROM episodes ORDER BY id").fetchall()
        assert rows[0][0] != rows[1][0]
        assert all(r[1:] == ("", "verbatim_only") for r in rows)
    # Forget reads both the allocated legacy key and immutable natural source.
    with at_the_key.connect() as conn, conn.cursor() as cur:
        result = forget.forget_episode(cur, b["episode_id"])
        conn.commit()
    assert result["ok"], result
    assert capture.write_pg(deepcopy(second)).get("forgotten_source")
    assert not capture.write_pg(deepcopy(first)).get("forgotten_source")


def test_nonblank_and_missing_natural_keys_still_conflict(at_the_key):
    capture.write_pg(job())
    with pytest.raises(capture.CaptureEvidenceConflict):
        capture.write_pg(job(session_id="codex:different"))
    capture.write_pg(blank())
    with pytest.raises(capture.CaptureEvidenceConflict):
        capture.write_pg(blank("none", transcript_range=None))
    with pytest.raises(capture.CaptureEvidenceConflict):
        capture.write_pg(blank(verbatim={"note": "different evidence"}))


def test_three_dead_copies_land_once(at_the_key):
    import json
    capture.write_pg(blank())
    payload = blank("dead")
    path = outbox.enqueue(deepcopy(payload), reason="CaptureEvidenceConflict")
    envelope = json.loads(path.read_text())
    envelope.update(attempts=25, evidence_conflict=True)
    outbox.dead_dir().mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (outbox.dead_dir() / f"copy-{i}.json").write_text(json.dumps(envelope))
    path.unlink()
    assert outbox.retry_dead()["moved"] == 3
    report = outbox.drain()
    assert report["failed"] == 0, report
    assert counts(at_the_key)["episodes"] == 2
    assert not outbox.jobs() and not outbox.dead_jobs()


def test_open_loop_only_capture_is_confirmed_and_replayable(at_the_key, monkeypatch):
    payload = {k: v for k, v in blank(verbatim={}).items()
               if k in ("ts", "summary", "summary_kind", "session_id", "harness", "event",
                        "transcript_range", "window_id", "open_loops")}
    monkeypatch.setenv("KHIPU_HUB_FILE_MIRROR", "0")
    for _ in range(2):
        result = {}
        assert capture.capture(deepcopy(payload), mode="hub", result=result) == 0, result
        assert result["verbatim_stored"] is True
    assert counts(at_the_key)["episodes"] == 1


def test_timestamp_projection_needs_an_allocation_receipt():
    payload = blank()
    shifted = {**payload, "ts": "2026-11-01T01:00:00.123456Z"}
    assert capture._replay_lacks(shifted, payload) is None
    shifted["blank_summary_original_ts"] = payload["ts"]
    assert capture._replay_lacks(shifted, payload) == []
    assert capture._replay_lacks(shifted, blank("other")) is None


def test_parallel_blank_sessions_land_and_replay(at_the_key):
    from concurrent.futures import ThreadPoolExecutor

    payloads = [{k: v for k, v in blank(str(i)).items()
                 if k in ("ts", "summary", "summary_kind", "session_id", "harness",
                          "transcript_range", "window_id", "verbatim")} for i in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        landed = list(pool.map(capture.write_pg, deepcopy(payloads)))
    assert len({r["episode_id"] for r in landed}) == 4
    for payload, saved in zip(payloads, landed):
        assert capture.write_pg(deepcopy(payload))["episode_id"] == saved["episode_id"]
    assert counts(at_the_key)["episodes"] == 4


def test_replay_survives_the_original_blank_key_becoming_free(at_the_key):
    capture.write_pg(blank())
    payload = blank("allocated")
    saved = capture.write_pg(deepcopy(payload))
    # A normal later summary enrichment can free the original legacy key.
    with at_the_key.connect() as conn:
        conn.execute("UPDATE episodes SET summary = 'Later summary' WHERE id <> %s",
                     (saved["episode_id"],))
        conn.commit()
    replay = capture.write_pg(deepcopy(payload))
    assert replay["episode_id"] == saved["episode_id"]
    assert not replay["episode_inserted"]
    assert counts(at_the_key)["episodes"] == 2
