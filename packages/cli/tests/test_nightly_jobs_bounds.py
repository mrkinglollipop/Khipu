"""Nightly job bounds: statement timeouts, library-cap partials, doctor step checks."""
from __future__ import annotations

import json
import tempfile
import textwrap
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from khipu import jobs


def _note_tree(td: str) -> tuple[Path, Path]:
    claude = Path(td) / "claude_projects"
    note = claude / "-repo-a" / "memory" / "note-one.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
        textwrap.dedent(
            """\
            ---
            name: khipu-state-of-play
            description: x
            ---
            body
            """
        ),
        encoding="utf-8",
    )
    (claude / "-repo-a" / "memory" / "MEMORY.md").write_text("index", encoding="utf-8")
    codex = Path(td) / "codex_memories"
    codex.mkdir(parents=True, exist_ok=True)
    return claude, codex


def test_notes_reconcile_bounds_its_hub_connection(monkeypatch):
    """A hung topics-row lock must not wait forever: notes.reconcile asks connect() for a timeout."""
    from khipu import db, notes

    seen: dict = {}

    class Stop(Exception):
        pass

    def fake_connect(*a, **k):
        seen.update(k)
        raise Stop

    with tempfile.TemporaryDirectory() as td:
        claude, codex = _note_tree(td)
        monkeypatch.setattr(notes, "claude_projects_root", lambda: claude)
        monkeypatch.setattr(notes, "codex_memories_root", lambda: codex)
        monkeypatch.setattr(notes, "_project_for_slug", lambda *a, **k: "acme/repo-a")
        monkeypatch.setattr(notes, "_refresh_scan_report", lambda *a, **k: None)
        monkeypatch.setattr(db, "connect", fake_connect)
        try:
            notes.reconcile(dry_run=False)
        except Stop:
            pass
    assert seen.get("statement_timeout_s"), (
        "notes.reconcile opens connect() with no statement_timeout_s"
    )
    assert "lock_timeout" in Path(notes.__file__).read_text()


def test_lock_timeout_set_config_is_committed_before_per_note_writes(monkeypatch):
    """set_config must not leave the connection INTRANS: otherwise each
    conn.transaction() is a savepoint and nothing commits until the end."""
    from khipu import db, notes

    events: list[str] = []

    class _Cur:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, params=None):
            self._conn.in_txn = True
            if "set_config" in " ".join(str(sql).split()):
                events.append("set_config")

        def fetchone(self):
            return None

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Conn:
        def __init__(self):
            self.in_txn = False
            self._cur = _Cur(self)

        def cursor(self):
            return self._cur

        def commit(self):
            events.append("commit")
            self.in_txn = False

        @contextmanager
        def transaction(self):
            kind = "savepoint" if self.in_txn else "real"
            events.append(f"txn:{kind}")
            if self.in_txn:
                yield
                return
            self.in_txn = True
            try:
                yield
                self.commit()
            except Exception:
                self.in_txn = False
                raise

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    with tempfile.TemporaryDirectory() as td:
        claude, codex = _note_tree(td)
        monkeypatch.setattr(notes, "claude_projects_root", lambda: claude)
        monkeypatch.setattr(notes, "codex_memories_root", lambda: codex)
        monkeypatch.setattr(notes, "_project_for_slug", lambda *a, **k: "acme/repo-a")
        monkeypatch.setattr(notes, "_refresh_scan_report", lambda *a, **k: None)
        monkeypatch.setattr(notes, "_tombstone_missing_notes", lambda *a, **k: {"tombstoned": 0})
        monkeypatch.setattr(db, "connect", lambda **k: _Conn())
        monkeypatch.setattr(
            "khipu.mirror._upsert_topic",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            "khipu.organise.after_reconcile",
            lambda **k: {"ok": True, "mocked": True},
        )
        out = notes.reconcile(dry_run=False)

    assert out["written"] >= 1, out
    assert "set_config" in events
    cfg_i = events.index("set_config")
    assert "commit" in events[cfg_i + 1 :], events
    first_commit_after = events.index("commit", cfg_i + 1)
    first_txn = next(i for i, e in enumerate(events) if e.startswith("txn:"))
    assert first_commit_after < first_txn, events
    assert "txn:savepoint" not in events, events
    assert "txn:real" in events, events


def test_reconcile_stops_when_overall_budget_is_exhausted(monkeypatch):
    """N stuck locks must not each burn the full per-statement timeout."""
    from khipu import db, notes

    ticks = {"n": 0}

    def fake_mono():
        # deadline = first call + timeout; later calls are past it.
        ticks["n"] += 1
        return 0.0 if ticks["n"] == 1 else 1_000.0

    class _Conn:
        def cursor(self):
            return self

        def execute(self, *a, **k):
            return None

        def commit(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    upserts = {"n": 0}

    with tempfile.TemporaryDirectory() as td:
        claude, codex = _note_tree(td)
        # Two notes so the deadline can skip the second without writing it.
        note2 = claude / "-repo-b" / "memory" / "note-two.md"
        note2.parent.mkdir(parents=True, exist_ok=True)
        note2.write_text(
            textwrap.dedent(
                """\
                ---
                name: second-note
                description: x
                ---
                body
                """
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(notes, "claude_projects_root", lambda: claude)
        monkeypatch.setattr(notes, "codex_memories_root", lambda: codex)
        monkeypatch.setattr(notes, "_project_for_slug", lambda *a, **k: "acme/repo-a")
        monkeypatch.setattr(notes, "_refresh_scan_report", lambda *a, **k: None)
        monkeypatch.setattr(notes, "_tombstone_missing_notes", lambda *a, **k: {"tombstoned": 0})
        monkeypatch.setattr(notes.time, "monotonic", fake_mono)
        monkeypatch.setattr(db, "connect", lambda **k: _Conn())
        monkeypatch.setattr(
            "khipu.mirror._upsert_topic",
            lambda *a, **k: upserts.__setitem__("n", upserts["n"] + 1),
        )
        monkeypatch.setattr(
            "khipu.organise.after_reconcile",
            lambda **k: {"ok": True, "mocked": True},
        )
        out = notes.reconcile(dry_run=False)

    assert upserts["n"] == 0, upserts
    assert out["written"] == 0
    assert any("DeadlineExceeded" in e["error"] for e in out["errors"]), out["errors"]


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


def test_reconcile_overall_budget_exceeds_per_statement_timeout(monkeypatch):
    """A slow-but-not-stuck backfill past timeout_s still writes its notes."""
    from khipu import db, notes

    ticks = {"n": 0}

    def fake_mono():
        # Past the 30s statement timeout but inside the overall budget.
        ticks["n"] += 1
        return 0.0 if ticks["n"] == 1 else 100.0

    class _Conn:
        def cursor(self):
            return self

        def execute(self, *a, **k):
            return None

        def commit(self):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    upserts = {"n": 0}

    with tempfile.TemporaryDirectory() as td:
        claude, codex = _note_tree(td)
        monkeypatch.setattr(notes, "claude_projects_root", lambda: claude)
        monkeypatch.setattr(notes, "codex_memories_root", lambda: codex)
        monkeypatch.setattr(notes, "_project_for_slug", lambda *a, **k: "acme/repo-a")
        monkeypatch.setattr(notes, "_refresh_scan_report", lambda *a, **k: None)
        monkeypatch.setattr(notes, "_tombstone_missing_notes", lambda *a, **k: {"tombstoned": 0})
        monkeypatch.setattr(notes.time, "monotonic", fake_mono)
        monkeypatch.setattr(db, "connect", lambda **k: _Conn())
        monkeypatch.setattr(
            "khipu.mirror._upsert_topic",
            lambda *a, **k: upserts.__setitem__("n", upserts["n"] + 1),
        )
        monkeypatch.setattr(
            "khipu.organise.after_reconcile",
            lambda **k: {"ok": True, "mocked": True},
        )
        notes.reconcile(dry_run=False)

    assert upserts["n"] >= 1, upserts
