"""A logged Undo can invert only items that particular action restored."""
import pytest

from khipu import owed_tidy, owner_act, owner_direction
from tests.test_owed_origin_selective_undo import corrected, states  # noqa: F401
from tests.test_owed_origin_correction import origin_db  # noqa: F401
from tests.test_owed_tidy import _direction
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


@pytest.fixture
def ordinary(corrected, monkeypatch, tmp_path):
    conn, ids, batch = corrected
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE commitment_tidy_items SET prior = prior - 'origin_correction_stamp' "
                    "WHERE batch_id = %s", (batch,))
    _direction(tmp_path, monkeypatch)
    return conn, ids, batch


def undo_action(batch, ids=None):
    args = {"project": "example/widget", "batch": batch}
    if ids is not None:
        args["ids"] = ids
    ok, out = owner_act.act("owed.tidy", args)
    assert ok, out
    return next(a for a in owner_direction.actions() if a["id"] == out["owner_action"])


def test_whole_remaining_undo_inverse_does_not_widen_to_prior_undo(ordinary):
    conn, ids, batch = ordinary
    undo_action(batch, [ids[0]])
    second = undo_action(batch)
    assert second["before"]["ids"] == [ids[1]]
    result = owner_direction._undo_owed_tidy(second, "cli")
    assert result["reapplied"] == 1
    assert states(conn, ids) == [("user", "conversation"), ("agent", "job")]


def test_skipped_requested_item_is_excluded_from_inverse(ordinary):
    conn, ids, batch = ordinary
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE commitments SET due_after = '2030-01-01' WHERE id = %s", (ids[0],))
    line = undo_action(batch, ids)
    assert line["facts"]["ids"] == ids
    assert line["before"]["ids"] == [ids[1]]
    assert owner_direction._undo_owed_tidy(line, "cli")["reapplied"] == 1
    assert states(conn, ids) == [("agent", "job")] * 2


def test_lost_finish_preserves_exact_scope(ordinary, monkeypatch):
    conn, ids, batch = ordinary
    undo_action(batch, [ids[0]])
    monkeypatch.setattr(owner_direction, "finish", lambda result: result)
    ok, out = owner_act.act("owed.tidy", {"project": "example/widget", "batch": batch})
    assert ok and out["restored"] == 1
    owner_direction._PENDING.set(None)  # Simulate process exit without a completion line.
    line = next(a for a in owner_direction.actions()
                if a["action"] == "owed.tidy" and a["state"] == "unconfirmed")
    assert line["before"]["ids"] == [ids[1]]
    assert owner_direction._undo_owed_tidy(line, "cli")["reapplied"] == 1
    assert states(conn, ids) == [("user", "conversation"), ("agent", "job")]


def test_zero_restored_action_has_empty_inverse(ordinary, monkeypatch):
    conn, ids, batch = ordinary
    undo_action(batch, [ids[0]])
    monkeypatch.setattr(owed_tidy, "_put", lambda *args, **kwargs: False)
    line = undo_action(batch)
    assert line["before"]["ids"] == []
    result = owner_direction._undo_owed_tidy(line, "cli")
    assert result["ok"] and result["reapplied"] == 0
    assert states(conn, ids) == [("user", "conversation"), ("agent", "job")]


def test_scope_log_failure_rolls_back_database(ordinary, monkeypatch):
    conn, ids, batch = ordinary
    original = owner_direction._append
    def fail_success_scope(line):
        if line.get("kind") == "state" and (line.get("before") or {}).get("ids") == [ids[0]]:
            raise OSError("synthetic scope log failure")
        return original(line)
    monkeypatch.setattr(owner_direction, "_append", fail_success_scope)
    with pytest.raises(OSError, match="synthetic scope log failure"):
        undo_action(batch, [ids[0]])
    assert states(conn, ids) == [("agent", "job")] * 2
