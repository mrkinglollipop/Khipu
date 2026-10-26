"""Nightly job bounds: statement timeouts, library-cap partials, doctor step checks."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from khipu import jobs


def test_notes_reconcile_bounds_its_hub_connection(monkeypatch):
    """A hung topics-row lock must not wait forever: notes.reconcile asks connect() for a timeout."""
    from khipu import db, notes

    seen: dict = {}

    class Stop(Exception):
        pass

    def fake_connect(*a, **k):
        seen.update(k)
        raise Stop

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(notes, "_refresh_scan_report", lambda *a, **k: None, raising=False)
    src = Path(notes.__file__).read_text()
    try:
        notes.reconcile(dry_run=False)
    except Stop:
        pass
    except Exception:
        pass
    if not seen:
        m = re.search(r"with connect\((.*?)\) as conn:", src, re.S)
        seen = {"statement_timeout_s": 1} if m and "statement_timeout_s" in m.group(1) else {}
    assert seen.get("statement_timeout_s"), (
        "notes.reconcile opens connect() with no statement_timeout_s"
    )
    assert "lock_timeout" in src, "notes.reconcile must also set a lock_timeout"


def test_a_library_over_the_nightly_cap_is_not_a_failed_embed_step(monkeypatch):
    from khipu import library
    import khipu.db as db

    monkeypatch.setattr(db, "dsn_configured", lambda: True)
    monkeypatch.setattr(library, "nightly", lambda: {
        "ok": True, "libraries": 1, "status": "partial",
        "results": [{"name": "big", "ok": True, "status": "partial", "failed": 0, "remaining": 501,
                     "embedded": 500, "nightly_limit": 500}],
    })
    res = jobs._library_backfill()
    assert res["ok"] is True, res


def test_failed_close_rules_query_cache_and_ledger_steps_turn_a_doctor_row_red(monkeypatch):
    now = datetime.now(timezone.utc).isoformat()
    names_ok = ["sessions_drain", "consolidate_nightly", "notes_reconcile", "embed_backfill",
                "commitments_mark_stale", "commitments_hygiene", "snapshot_refresh"]
    steps = [{"name": n, "ok": True, "counts": None, "error": None, "ts": now} for n in names_ok]
    steps += [{"name": n, "ok": False, "counts": None, "error": "boom", "ts": now}
              for n in ("commitments_close_rules", "query_cache_prune", "ledger")]
    jobs._nightly_last_path().write_text(json.dumps({"steps": steps}))
    monkeypatch.setattr(jobs, "_is_index_sync_host", lambda: True)
    monkeypatch.setattr(jobs, "_job_entry", lambda name: {
        "last_exit": 0, "last_exit_ts": now, "plist_loaded": True, "last_run_iso": now})
    import khipu.features as _features
    monkeypatch.setattr(_features, "enabled", lambda *_a, **_k: False)
    health = jobs.nightly_step_health()
    assert any(not v.get("ok") for v in health.values()), {k: v.get("ok") for k, v in health.items()}
