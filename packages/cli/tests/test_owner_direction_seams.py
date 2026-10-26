"""Owner changes keep their recovery identity before any mutation."""
from __future__ import annotations

import json
from contextlib import nullcontext

import pytest

from khipu import ledger, ledger_import, owed_tidy, owner_direction


@pytest.fixture(autouse=True)
def isolated_action(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(owner_direction, "check", lambda *_a, **_kw: {"auth": "direction"})
    monkeypatch.setattr(owner_direction, "_name_in_push", lambda *_a: None)
    token = owner_direction._PENDING.set(None)
    yield
    owner_direction._PENDING.reset(token)


def _lose_finish(monkeypatch):
    real_append = owner_direction._append

    def append(line):
        if line.get("kind") == "state":
            raise OSError("simulated process-loss after the committed bulk batch")
        return real_append(line)

    monkeypatch.setattr(owner_direction, "_append", append)


class _BatchCursor:
    def __init__(self, projects):
        self.projects = projects
        self.inserted = []
        self.updated = []
        self.rowcount = 1

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO commitment_tidy_batches"):
            self.inserted.append(params[0])
        if sql.startswith("UPDATE commitments"):
            self.updated.append(params)

    def fetchall(self):
        return list(enumerate(self.projects, 1))


def _bulk(monkeypatch, action="close", projects=("p",)):
    monkeypatch.setattr(owed_tidy, "_batch_id", lambda prefix: f"{prefix}-batch-{len(cur.inserted) + 1}")
    monkeypatch.setattr(owed_tidy, "_select", lambda *_a, ids, **_kw: {cid: {} for cid in ids})
    cur = _BatchCursor(projects)
    return cur, lambda change: owed_tidy.bulk_batch(
        cur, list(range(1, len(projects) + 1)), action, change, reason="owner request", gate="direction")


def test_bulk_owner_change_undo_survives_lost_finish(monkeypatch):
    """Port of the lost-finish probe, using the actual batch writer."""
    monkeypatch.setattr(owner_direction, "_before", lambda _want: {"owners_items": [1, 2]})
    owner_direction.gate(owner_direction.intent("owed.change", "1,2", facts={"change": "close"}))
    action_id = owner_direction._PENDING.get()["line"]["id"]
    cur, write = _bulk(monkeypatch)
    _, batches = write(lambda cid: True)
    _lose_finish(monkeypatch)
    result = owner_direction.finish({"ok": True, "batches": batches})
    assert result["record_incomplete"]
    seen = []
    monkeypatch.setattr(owed_tidy, "undo", lambda batch, **_kw: seen.append(batch) or {"ok": True, "restored": 2})
    assert owner_direction.undo(action_id, "direction")["restored"] == 2
    assert seen == cur.inserted == ["close-batch-1"]


@pytest.mark.parametrize("action", owed_tidy.BULK_ACTIONS)
@pytest.mark.parametrize("projects", [("p",), ("p", "q"), (None, "q")])
def test_each_bulk_batch_is_durable_before_its_first_mutation(monkeypatch, action, projects):
    monkeypatch.setattr(owner_direction, "_before", lambda _want: {"owners_items": [1, 2]})
    owner_direction.gate(owner_direction.intent("owed.change", "1,2", facts={"change": action}))
    cur, write = _bulk(monkeypatch, action, projects)

    def change(cid):
        assert cur.inserted[-1] in owner_direction.actions()[0]["before"]["batches"]
        return True

    _, batches = write(change)
    assert owner_direction.actions()[0]["before"]["batches"] == batches


@pytest.mark.parametrize("action", owed_tidy.BULK_ACTIONS)
def test_a_batch_is_not_written_if_its_recovery_record_cannot_be_written(monkeypatch, action):
    monkeypatch.setattr(owner_direction, "_before", lambda _want: {"owners_items": [1, 2]})
    owner_direction.gate(owner_direction.intent("owed.change", "1,2", facts={"change": action}))
    cur, write = _bulk(monkeypatch, action)
    _lose_finish(monkeypatch)
    changed = []
    with pytest.raises(OSError, match="simulated process-loss"):
        write(lambda cid: changed.append(cid) or True)
    assert changed == cur.inserted == cur.updated == []


@pytest.mark.parametrize("action", owed_tidy.BULK_ACTIONS)
def test_directed_bulk_changes_refuse_a_pre_migration_hub(monkeypatch, action):
    from khipu import commitments, db, mcp_server, owner_writes

    cur = _BatchCursor([])
    conn = type("Conn", (), {"cursor": lambda self: nullcontext(cur)})()
    monkeypatch.setattr(db, "connect", lambda **_kw: nullcontext(conn))
    monkeypatch.setattr(commitments, "owners_of", lambda *_a, **_kw: {
        cid: {"owner": "user"} for cid in (1, 2)})
    monkeypatch.setattr(owner_writes, "apply_on_hub", lambda conn, cur, verb, args, write, echo: write())
    monkeypatch.setattr(owed_tidy, "_ready", lambda _cur: False)
    changed = []
    monkeypatch.setattr(commitments, "set_status", lambda cur, cid, *a, **kw: changed.append(cid) or True)
    monkeypatch.setattr(commitments, "reassign", lambda cur, cid, *a, **kw: changed.append(cid) or True)
    args = {"ids": [1, 2], "action": action, "owner_gate": "direction", "by": "owner",
            "reason": "owner request", "until": "7d", "owner": "assistant"}
    result = mcp_server._owed_change_on_hub(args)
    assert result["ok"] is False
    assert result["changed"] == 0
    assert result["error"] == owed_tidy.NOT_READY
    assert changed == cur.inserted == cur.updated == []


@pytest.mark.parametrize("change", ["apply", "undo"])
def test_tidy_undo_survives_lost_finish(monkeypatch, change):
    batch = "tidy-batch-1"
    facts = {"change": change, **({"batch": batch} if change == "undo" else {})}
    owner_direction.gate(owner_direction.intent("owed.tidy", "p", facts=facts))
    action_id = owner_direction._PENDING.get()["line"]["id"]
    if change == "apply":
        from khipu import db

        cur = _BatchCursor([])
        monkeypatch.setattr(db, "connect", lambda **_kw: nullcontext(type("Conn", (), {
            "cursor": lambda self: nullcontext(cur)})()))
        monkeypatch.setattr(owed_tidy, "_ready", lambda _cur: True)
        monkeypatch.setattr(owed_tidy, "_select", lambda *_a, **_kw: {})
        monkeypatch.setattr(owed_tidy, "_batch_id", lambda _prefix: batch)
        result = owed_tidy.apply({"format": owed_tidy.PLAN_FORMAT, "version": owed_tidy.PLAN_VERSION,
                                  "project": "p", "items": []}, owner_gate="direction")
    else:
        owner_direction.record_owed_undo_scope(batch, [1, 2])
        result = {"ok": True, "batch": batch}
    _lose_finish(monkeypatch)
    assert owner_direction.finish(result)["record_incomplete"]
    seen = []
    inverse = "redo" if change == "undo" else "undo"
    monkeypatch.setattr(owed_tidy, inverse, lambda bid, **_kw: seen.append(bid) or {
        "ok": True, "restored": 2, "reapplied": 2})
    out = owner_direction.undo(action_id, "direction")
    assert out["reapplied" if change == "undo" else "restored"] == 2
    assert seen == [batch]


@pytest.mark.parametrize("replacement", ["After", None])
def test_directed_import_undo_identifies_the_file_bytes_actually_imported(tmp_path, monkeypatch, replacement):
    monkeypatch.setattr(ledger_import, "done_imports", lambda *_a: [])
    tracker = tmp_path / "tracker.json"
    tracker.write_text(json.dumps({"rows": {"item": {"title": "Before", "state": "open", "kind": "slice"}}}))
    reads = []
    real_read = ledger_import.read_tracker

    def read(path):
        reads.append(str(path))
        return real_read(path)

    monkeypatch.setattr(ledger_import, "read_tracker", read)
    owner_direction.gate(owner_direction.intent("ledger.import", str(tracker), facts={"project": "test/project"}))
    if replacement is None:
        tracker.unlink()
    else:
        tracker.write_text(json.dumps({"rows": {"item": {"title": replacement, "state": "open", "kind": "slice"}}}))
    monkeypatch.setattr(ledger, "_ledger_lock", nullcontext)
    monkeypatch.setattr(ledger, "drain", lambda _conn: None)
    monkeypatch.setattr(ledger_import, "in_inbox", lambda _path: False)
    saved = {}

    def transaction(conn, project, src, report, **kw):
        saved.update(src)
        return dict(report, result="done")

    monkeypatch.setattr(ledger_import, "_import_locked", transaction)
    report = ledger_import.run_import(tracker, "test/project", conn=object())
    owner_direction.finish(report)
    action = owner_direction.actions()[0]
    assert action["before"]["digest"] == saved["digest"]
    assert saved["rows"]["item"]["title"] == "Before"
    assert reads == [str(tracker)]
    assert "rows" not in action["before"]
