"""Doctor honesty: ledger age with hub down, buried outbox recovery, grey rows."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from khipu import jobs, outbox, replica_heal


def test_ledger_copy_is_not_fresh_when_the_hub_is_down_and_the_copy_is_days_old(monkeypatch):
    from khipu import hub_snapshot

    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    monkeypatch.setattr(replica_heal, "_has_ledger_tables", lambda: True)
    monkeypatch.setattr(replica_heal, "read_state", lambda: {})
    monkeypatch.setattr(hub_snapshot, "caught_up_at", lambda *a, **k: old)
    out = replica_heal.ledger_copy_status(hub_ok=False)
    assert out["state"] != "fresh" and out["ok"] is not True, out


def test_ledger_copy_never_copied_is_not_fresh_when_the_hub_is_down(monkeypatch):
    from khipu import hub_snapshot

    monkeypatch.setattr(replica_heal, "_has_ledger_tables", lambda: True)
    monkeypatch.setattr(replica_heal, "read_state", lambda: {})
    monkeypatch.setattr(hub_snapshot, "caught_up_at", lambda *a, **k: None)
    out = replica_heal.ledger_copy_status(hub_ok=False)
    assert out["state"] != "fresh" and out["ok"] is not True, out


def test_doctor_tells_the_owner_to_retry_dead_when_only_buried_outbox_jobs_remain(tmp_path, monkeypatch):
    """drain() cannot clear dead letters; doctor must name retry-dead instead."""
    monkeypatch.setenv("HOME", str(tmp_path))
    outbox.dead_dir().mkdir(parents=True, exist_ok=True)
    (outbox.dead_dir() / "job-1.json").write_text(
        json.dumps({"payload": {"summary": "x"}, "attempts": 25, "last_error": "boom"})
    )
    before = outbox.status()
    assert before["pending"] == 0 and before["dead"] == 1, before
    outbox.drain()
    assert outbox.status()["dead"] == 1, "drain must leave buried jobs for retry-dead"

    from khipu import cli

    local = cli._doctor_local_checks(None)
    assert local["outbox_ok"] is False
    assert "retry-dead" in (local["outbox"].get("fix") or ""), local["outbox"]


def test_non_applicable_nightly_steps_are_named_for_a_grey_row(monkeypatch):
    monkeypatch.setattr(jobs, "_is_index_sync_host", lambda: False)
    health = jobs.nightly_step_health()
    skipped = jobs.skipped_step_names(health)
    assert "notes_reconcile" in skipped and "embed_provider" in skipped, (health, skipped)
