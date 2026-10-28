"""Nightly regressions with bounded work, synthetic archives and recording SQL."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
import os
from unittest.mock import Mock

import psycopg
import pytest

from khipu import close_rules, db, jobs, mirror, notes, outbox, provenance
from khipu.capture_identity import payload_sha256
from tests.test_completion_transactions import NOW, harness, item  # noqa: F401
from tests.test_notes import _FakeConn, _FakeTopicsCursor
from tests.test_outbox_legacy_projection import ORIGINAL, REASON, projection, world  # noqa: F401


def archive(number=0):
    original = {**deepcopy(ORIGINAL), "summary": f"Synthetic archive {number}"}
    queued = projection(original)
    record = {"payload": original, "queued_evidence": {
        "payload": queued, "origin": None, "reason": REASON, "attempts": 0},
        "resolution": {"kind": "legacy_comma_projection", "episode_id": 1,
                       "original_payload_sha256": payload_sha256(original),
                       "queued_payload_sha256": payload_sha256(queued), "resolved_at": "earlier"}}
    outbox._validate_resolution(record)
    outbox.resolved_dir().mkdir(parents=True, exist_ok=True)
    path = outbox.resolved_dir() / f"{number}.json"
    outbox._atomic_write(path, record)
    return path, record


def test_recovery_builds_one_index_for_n_lines_without_scanning_under_lock(world, monkeypatch):
    records = [archive(n) for n in range(50)]
    monkeypatch.setattr(outbox, "_RECOVERY_INDEX", None)
    locked = False
    lock = outbox._locked_outbox
    builds = Mock(wraps=outbox._RecoveryIndex)
    monkeypatch.setattr(outbox, "_RecoveryIndex", builds)
    validate = outbox._validate_resolution
    validations = []

    @contextmanager
    def tracked_lock():
        nonlocal locked
        with lock() as directory:
            locked = True
            try:
                yield directory
            finally:
                locked = False

    def checked(record):
        validations.append(locked)
        return validate(record)

    monkeypatch.setattr(outbox, "_locked_outbox", tracked_lock)
    monkeypatch.setattr(outbox, "_validate_resolution", checked)
    for path, record in records:
        assert outbox.enqueue(record["queued_evidence"]["payload"], origin=None, recovery=True) == path
    assert builds.call_count == 1
    assert validations == [False] * 50 + [True] * 50
    assert not outbox.jobs()


@pytest.mark.parametrize("change", ["add", "remove", "replace", "corrupt"])
def test_warm_archive_index_rechecks_changes_and_damage(world, monkeypatch, change):
    path, record = archive()
    queued = record["queued_evidence"]["payload"]
    assert outbox.is_resolved_payload(queued)
    if change == "add":
        path, record = archive(1)
        queued = record["queued_evidence"]["payload"]
    elif change == "remove":
        path.unlink()
    elif change == "replace":
        outbox._atomic_write(path, {})
    else:
        # Directory mtime does not change for an in-place corrupt write.
        path.write_text("{}")
    result = outbox.enqueue(queued, origin=None, recovery=True)
    assert (result.parent == outbox.resolved_dir()) is (change == "add")
    assert outbox.is_resolved_payload(queued) is (change == "add")


def test_archive_membership_change_between_scan_and_lock_rebuilds(world, monkeypatch):
    path, record = archive()
    index = outbox._RecoveryIndex
    calls = []

    def build():
        result = index()
        calls.append(result)
        if len(calls) == 1:
            path.unlink()
        return result

    monkeypatch.setattr(outbox, "_RECOVERY_INDEX", None)
    monkeypatch.setattr(outbox, "_RecoveryIndex", build)
    result = outbox.enqueue(record["queued_evidence"]["payload"], origin=None, recovery=True)
    assert len(calls) == 2
    assert result.parent == outbox.outbox_dir()


def test_warm_archive_index_does_not_suppress_direct_capture_or_different_origin(world):
    _path, record = archive()
    queued = record["queued_evidence"]["payload"]
    assert outbox.is_resolved_payload(queued)
    direct = outbox.enqueue(queued, origin=None)
    assert direct.parent == outbox.outbox_dir()
    direct.unlink()
    assert outbox.enqueue(queued, origin="local:synthetic", recovery=True).parent == outbox.outbox_dir()


def test_mirror_skips_both_archived_payloads_but_queues_distinct_evidence(world, monkeypatch):
    _path, record = archive()
    monkeypatch.setattr(provenance, "ready", lambda cur: True)
    conflicts = Mock(side_effect=lambda cur, lines: [obj for obj, _key in lines])
    queue = Mock()
    monkeypatch.setattr(mirror, "_legacy_key_conflicts", conflicts)
    monkeypatch.setattr(mirror, "_queue_conflict", queue)
    distinct = {**record["queued_evidence"]["payload"], "preferences": ["keep this extra fact"]}
    lines = [(payload, (payload["ts"], "synthetic")) for payload in (
        record["payload"], record["queued_evidence"]["payload"], distinct)]
    assert mirror._queue_key_conflicts(object(), lines) == 1
    queue.assert_called_once_with(distinct)
    assert conflicts.call_args.args[1] == [lines[-1]]


def questions(harness, count=20):
    harness.rows = {n: {**item(), "id": n, "text": f"Decide release option {n}",
                        "kind": "question", "prs": []} for n in range(1, count + 1)}


def test_apply_reuses_one_connection_and_fetches_decisions_once_per_project(harness, monkeypatch):
    questions(harness)
    for n in range(11, 21):
        harness.rows[n]["project"] = "acme/other"
    reads = Mock(wraps=close_rules._read_decisions)
    monkeypatch.setattr(close_rules, "_read_decisions", reads)
    monkeypatch.setattr(close_rules.GitHub, "state", Mock(side_effect=AssertionError("no PRs")))
    result = close_rules.run_nightly()
    assert result["ok"], result
    assert harness.events.count("connect") == 2  # gather + one apply connection
    assert reads.call_count == 2
    locks = [i for i, event in enumerate(harness.events) if isinstance(event, tuple) and event[0] == "lock"]
    assert len(locks) == 20
    for before, after in zip(locks, locks[1:]):
        assert "commit" in harness.events[before + 1:after]
    assert harness.active == 0


def test_decision_cache_is_built_after_github_and_preserves_item_date_filter(harness, monkeypatch):
    questions(harness, 3)
    harness.rows[1].update(kind="followup", text="Finish PR #41", prs=[41])
    harness.rows[3]["opened_at"] = NOW + timedelta(seconds=1)
    harness.decisions = []

    def state(*args):
        assert harness.active == 0 and harness.events[-1] == "close"
        harness.decisions = [(5, "Resolved item #2 and item #3", 10, NOW)]
        return "merged", None

    monkeypatch.setattr(close_rules.GitHub, "state", state)
    result = close_rules.run_nightly()
    assert result["ok"], result
    assert harness.rows[2]["status"] == "closed"
    assert harness.rows[3]["status"] == "open"


def test_withdrawn_cached_decision_never_closes_an_item(harness, monkeypatch):
    questions(harness, 2)
    harness.decisions = [(5, "Resolved item #2", 10, NOW)]
    original = close_rules._open_items

    def open_items(cur, project, **kwargs):
        if kwargs.get("commitment_id") == 2:
            harness.decisions = []
        return original(cur, project, **kwargs)

    monkeypatch.setattr(close_rules, "_open_items", open_items)
    result = close_rules.run_nightly()
    assert result["ok"] and result["closed_decision"] == 0, result
    assert harness.rows[2]["status"] == "open"


@pytest.mark.parametrize("broken", [False, True])
def test_apply_failure_rolls_back_and_only_reconnects_when_unusable(harness, monkeypatch, broken):
    harness.rows[2] = {**item(), "id": 2}
    original = close_rules._open_items

    def open_items(cur, project, **kwargs):
        if kwargs.get("commitment_id") == 1:
            if broken:
                cur.conn.close()
            raise psycopg.errors.SerializationFailure("synthetic candidate interruption")
        return original(cur, project, **kwargs)

    monkeypatch.setattr(close_rules, "_open_items", open_items)
    monkeypatch.setattr(close_rules.GitHub, "state", lambda *a: ("merged", None))
    result = close_rules.run_nightly()
    assert result["ok"] and result["deferred_transient"] == 1, result
    assert result["closed_pr"] == 1
    assert harness.rows[1]["status"] == "open" and harness.rows[2]["status"] == "closed"
    assert harness.events.count("connect") == (3 if broken else 2)


@pytest.mark.parametrize("errors,expected", [
    ([{"error": "DeadlineExceeded: redirect"}, {"error": "other"}], "2 failed: DeadlineExceeded: redirect"),
    ([{"reason": "synthetic failure"}], "1 failed: synthetic failure"),
    (["synthetic failure"], "1 failed: synthetic failure"),
    ([], None),
])
def test_step_result_reports_error_lists(errors, expected):
    assert jobs._step_result({"ok": False, "errors": errors})["error"] == expected
    assert jobs._step_result({"ok": False, "error": "primary", "errors": errors})["error"] == "primary"


def test_large_note_plan_includes_redirects_in_deadline(tmp_path, monkeypatch):
    count = 849
    raw = [{"path": str(tmp_path / f"{n}.md"), "harness": "codex",
            "parsed": {"slug": f"note:synthetic-{n}"},
            "signature": {"mtime": 1, "size": 1}} for n in range(count)]
    redirects = [{"old_slug": f"note:old-{n}", "new_slugs": [], "source_path": "synthetic"}
                 for n in range(39)]
    monkeypatch.setattr(notes, "_read_state", lambda: {})
    monkeypatch.setattr(notes, "_build_plan", lambda: raw)
    monkeypatch.setattr(notes, "_resolve_collisions", lambda plan: (plan, {
        "redirects": redirects, "duplicate_copies": 0, "collisions_reslugged": 39}))
    monkeypatch.setattr(notes, "claude_projects_root", lambda: tmp_path)
    monkeypatch.setattr(notes, "codex_memories_root", lambda: tmp_path)
    monkeypatch.setattr(notes, "_any_memory_root_exists", lambda: False)
    monkeypatch.setattr(notes, "_refresh_scan_report", lambda: None)
    monkeypatch.setattr(notes, "_write_state", lambda state: None)
    monkeypatch.setattr("khipu.organise.after_reconcile", lambda **kw: {"ok": True})
    monkeypatch.setattr("khipu.offline.setting", lambda name: 30)
    monkeypatch.setattr(db, "connect", lambda **kw: _FakeConn(_FakeTopicsCursor()))
    clock = [0.0]
    monkeypatch.setattr(notes.time, "monotonic", lambda: clock[0])
    written = []

    def upsert(cur, parsed, *args, **kwargs):
        written.append(parsed["slug"])
        # Notes consume 849 seconds; redirects must have their own allowance.
        clock[0] += 1.0

    monkeypatch.setattr(mirror, "_upsert_topic", upsert)
    result = notes.reconcile()
    assert result["ok"] and result["written"] == count, result
    assert len(written) == count + 39
    assert clock[0] == 888


def test_killed_driver_keeps_unbuffered_progress(tmp_path, monkeypatch):
    script = tmp_path / "driver.py"
    script.write_text("import time\nprint('synthetic progress before kill')\ntime.sleep(60)\n")
    output, error = tmp_path / "out.log", tmp_path / "err.log"
    monkeypatch.setattr(jobs, "_log_paths", lambda stem: (output, error))
    monkeypatch.setattr(jobs, "ensure_data_dir", lambda: tmp_path)
    monkeypatch.setattr(jobs, "DRIVER_TIMEOUT_SECONDS", 2)
    monkeypatch.setenv("PYTHONUNBUFFERED", "0")
    assert jobs._run_script(script, log_stem="synthetic", state_name="synthetic") == 124
    assert "synthetic progress before kill" in output.read_text()
    assert "driver timed out after 2s" in error.read_text()
    assert os.environ["PYTHONUNBUFFERED"] == "0"


def test_steady_archive_churn_cannot_starve_recovery(world, monkeypatch):
    # Every lock-free build races one archive (a concurrent drain): the stale
    # index is rebuilt once under the lock instead of retried without bound.
    path, record = archive(0)
    index = outbox._RecoveryIndex
    builds = []

    def build():
        result = index()
        builds.append(1)
        if len(builds) > 4:
            raise RuntimeError(f"still rebuilding after {len(builds)} builds")
        if not getattr(build, "locked", False):
            archive(len(builds))
        return result

    lock = outbox._locked_outbox

    @contextmanager
    def tracked_lock():
        with lock() as directory:
            build.locked = True
            try:
                yield directory
            finally:
                build.locked = False

    monkeypatch.setattr(outbox, "_RECOVERY_INDEX", None)
    monkeypatch.setattr(outbox, "_RecoveryIndex", build)
    monkeypatch.setattr(outbox, "_locked_outbox", tracked_lock)
    assert outbox.enqueue(record["queued_evidence"]["payload"], origin=None, recovery=True) == path
    assert len(builds) == 2
    builds.clear()
    monkeypatch.setattr(outbox, "_RECOVERY_INDEX", None)
    assert outbox.is_resolved_payload(record["queued_evidence"]["payload"])
    assert len(builds) == 2


def test_concurrent_archiver_does_not_starve_recovery_enqueue(world, monkeypatch):
    import threading
    import time

    for n in range(300):
        archive(1000 + n)
    path, record = archive(0)
    index = outbox._RecoveryIndex

    def slow_build():
        result = index()
        time.sleep(0.3)  # stands in for a 12.7k-record scan
        return result

    monkeypatch.setattr(outbox, "_RECOVERY_INDEX", None)
    monkeypatch.setattr(outbox, "_RecoveryIndex", slow_build)
    stop = threading.Event()

    def archiver():  # a drain archiving one resolution every 100 ms
        n = 5000
        while not stop.is_set():
            with outbox._locked_outbox():
                archive(n)
            n += 1
            time.sleep(0.1)

    churn = threading.Thread(target=archiver, daemon=True)
    churn.start()
    done = threading.Event()
    result = {}

    def run():
        result["path"] = outbox.enqueue(record["queued_evidence"]["payload"], origin=None, recovery=True)
        done.set()

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    finished = done.wait(10)
    stop.set()
    churn.join()
    worker.join(30)
    assert finished, "recovery enqueue starved by a steady archiver"
    assert result["path"] == path


def test_step_result_ok_with_only_transient_deferrals_reports_no_failure():
    deferred = [{"phase": "apply", "transient": True, "reason": "SerializationFailure: synthetic"}]
    assert jobs._step_result({"ok": True, "errors": deferred})["error"] is None
    mixed = deferred + [{"phase": "apply", "transient": False, "reason": "ValueError: synthetic"}]
    assert jobs._step_result({"ok": False, "errors": mixed})["error"] == "2 failed: SerializationFailure: synthetic"
    assert jobs._step_result({"ok": False, "errors": deferred})["error"] == "1 failed: SerializationFailure: synthetic"
