"""The nightly's snapshot_refresh step must not read ok while its fact copy failed.

A copy that gave up because another copy held the lock is a deliberate
deferral (recorded with its reason, still not red); any other miss fails
the step so doctor shows it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from khipu import hub_snapshot, jobs

BUSY = "another fact copy held the copy lock until this one gave up"


def _refresh_returning(facts):
    return mock.patch("khipu.hub_snapshot.refresh", return_value={"ok": True, "episodes": 3, "facts": facts})


def test_a_failed_fact_copy_makes_the_step_not_ok():
    with _refresh_returning({"ok": False, "refreshed": False, "error": "RegistryError: boom"}):
        res = jobs._refresh_snapshot()
    step = jobs._step_result(res)
    assert step["ok"] is False
    assert "facts copy failed" in step["error"] and "boom" in step["error"]
    assert "deferred" not in step


def test_a_lock_held_fact_copy_is_a_deferral_with_its_reason_not_ok_silently():
    facts = {"ok": False, "refreshed": False, "error": BUSY, "deferred": True}
    with _refresh_returning(facts):
        res = jobs._refresh_snapshot()
    step = jobs._step_result(res)
    assert step["ok"] is True and step["error"] is None
    assert "facts copy deferred" in step["deferred"] and "copy lock" in step["deferred"]


def test_a_good_fact_copy_stays_plain_ok():
    for facts in ({"ok": True, "refreshed": True}, {"ok": True, "skipped": True}):
        with _refresh_returning(facts):
            step = jobs._step_result(jobs._refresh_snapshot())
        assert step["ok"] is True and "deferred" not in step


def test_copy_facts_marks_a_busy_lock_as_deferred(tmp_path):
    snap = tmp_path / "hub_snapshot.sqlite"
    snap.write_bytes(b"")

    class Busy:
        def __enter__(self):
            raise hub_snapshot._CopyBusy(BUSY)

        def __exit__(self, *a):
            return False

    with mock.patch.object(hub_snapshot, "snapshot_path", return_value=snap), \
            mock.patch.object(hub_snapshot, "_fact_copy_lock", return_value=Busy()):
        out = hub_snapshot.copy_facts(deadline=10**12)
    assert out["ok"] is False and out["deferred"] is True and "copy lock" in out["error"]


def _record(tmp: Path, result: dict) -> list[dict]:
    steps: list[dict] = []
    with mock.patch.object(jobs, "ensure_data_dir", return_value=tmp):
        jobs._record_nightly_step(steps, "snapshot_refresh", **jobs._step_result(result))
    return steps


def test_doctor_reads_the_recorded_step_as_failed_deferred_or_ok(tmp_path):
    def health(result):
        _record(tmp_path, result)
        with mock.patch.object(jobs, "_is_index_sync_host", return_value=True), \
                mock.patch.object(jobs, "ensure_data_dir", return_value=tmp_path):
            return jobs.nightly_step_health()["snapshot_refresh_ok"]

    failed = health({"ok": False, "error": "facts copy failed: boom", "facts": {"ok": False}})
    assert failed["ok"] is False and "boom" in failed["error"] and failed["fix"]

    deferred = health({"ok": True, "deferred": "facts copy deferred: " + BUSY})
    assert deferred["ok"] is True and "copy lock" in deferred["deferred"]
    entry = json.loads((tmp_path / "nightly-last.json").read_text())["steps"][-1]
    assert entry["ok"] is True and "copy lock" in entry["deferred"]

    plain = health({"ok": True})
    assert plain["ok"] is True and "deferred" not in plain
    assert datetime.fromisoformat(entry["ts"]).tzinfo is timezone.utc
