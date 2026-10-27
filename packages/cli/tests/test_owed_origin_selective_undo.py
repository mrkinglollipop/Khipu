"""Individual origin-correction inverses retain the existing owner gate."""
import io
import json
from contextlib import redirect_stdout

import pytest

from khipu import cli, commitments, owed_tidy, owner_act, routing
from tests.test_owed_origin_correction import origin_db, seed  # noqa: F401
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401


@pytest.fixture
def corrected(origin_db):
    ids = [seed(origin_db), seed(origin_db)]
    with origin_db.transaction(), origin_db.cursor() as cur:
        def change(cid):
            cur.execute("UPDATE commitments SET origin = 'job' WHERE id = %s", (cid,))
            return commitments.reassign(cur, cid, "agent", "job", by="job-origin correction")
        changed, batches = owed_tidy.bulk_batch(cur, ids, "reassign", change,
                                               reason="Synthetic origin repair", gate="app",
                                               origin_correction=True)
    assert all(changed.values()) and len(batches) == 1
    return origin_db, ids, batches[0]


def states(conn, ids):
    with conn.cursor() as cur:
        cur.execute("SELECT owner, origin FROM commitments WHERE id = ANY(%s) ORDER BY id", (ids,))
        return cur.fetchall()


def test_selected_correction_then_remaining_batch(corrected):
    conn, ids, batch = corrected
    result = owed_tidy.undo(batch, ids=[ids[0]], owner_gate="app")
    assert result["ok"] and result["restored"] == 1
    assert states(conn, ids) == [("user", "conversation"), ("agent", "job")]
    assert owed_tidy.redo(batch, owner_gate="direction")["code"] == "owner_confirmation_required"
    result = owed_tidy.undo(batch, owner_gate="app")
    assert result["ok"] and result["restored"] == 1
    assert states(conn, ids) == [("user", "conversation")] * 2


def test_selected_undo_requires_owner_gate(corrected):
    conn, ids, batch = corrected
    result = owed_tidy.undo(batch, ids=[ids[0]])
    assert not result["ok"] and result["needs_owner"] == [ids[0]]
    assert states(conn, ids) == [("agent", "job")] * 2


@pytest.mark.parametrize("selector", [[], [0], [-1], [True], ["1"], "1", [999999]])
def test_invalid_selection_changes_nothing(corrected, selector):
    conn, ids, batch = corrected
    # Include a valid item in the unknown-id case: partial success is unsafe.
    if selector == [999999]:
        selector = [ids[0], 999999]
    result = owed_tidy.undo(batch, ids=selector, owner_gate="app")
    assert not result["ok"] and result["restored"] == 0
    assert states(conn, ids) == [("agent", "job")] * 2


def test_selected_later_edit_stays_protected(corrected):
    conn, ids, batch = corrected
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE commitments SET due_after = NULL WHERE id = %s", (ids[0],))
    result = owed_tidy.undo(batch, ids=[ids[0]], owner_gate="app")
    assert result["restored"] == 0 and result["left_alone"][0]["id"] == ids[0]
    assert owed_tidy.undo(batch, ids=[ids[1]], owner_gate="app")["restored"] == 1
    assert states(conn, ids) == [("agent", "job"), ("user", "conversation")]


def test_selected_redo_does_not_replay_an_earlier_undo(corrected):
    conn, ids, batch = corrected
    # Exercise the generic tidy inverse; origin corrections keep their
    # separate, stricter no-replay policy tested above.
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("UPDATE commitment_tidy_items SET prior = prior - 'origin_correction_stamp' "
                    "WHERE batch_id = %s", (batch,))
    assert owed_tidy.undo(batch, ids=[ids[0]], owner_gate="app")["restored"] == 1
    assert owed_tidy.undo(batch, ids=[ids[1]], owner_gate="app")["restored"] == 1
    assert owed_tidy.redo(batch, ids=[ids[1]], owner_gate="app")["reapplied"] == 1
    assert states(conn, ids) == [("user", "conversation"), ("agent", "job")]
    result = owed_tidy.redo(batch, ids=[ids[1], 999999], owner_gate="app")
    assert not result["ok"] and result["reapplied"] == 0
    assert owed_tidy.redo(batch, owner_gate="app")["reapplied"] == 1


def test_cli_selection_and_owner_act_selection(corrected, monkeypatch, tmp_path):
    from khipu import owner_turns, owner_direction
    conn, ids, batch = corrected
    monkeypatch.setattr(routing, "_caller", lambda: {"app": True, "chain": ["app"]})
    ns = cli.build_parser().parse_args(["owed", "tidy", "--project", "example/widget",
                                       "--undo", batch, "--ids", str(ids[0])])
    output = io.StringIO()
    with redirect_stdout(output):
        rc = ns.func(ns)
    assert rc == 0 and json.loads(output.getvalue())["restored"] == 1
    assert states(conn, ids) == [("user", "conversation"), ("agent", "job")]
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["test"]})
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "selective-undo")
    owner_turns.record({"session_id": "selective-undo", "prompt": "undo the second correction",
                       "cwd": str(tmp_path)}, {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})
    ok, result = owner_act.act("owed.tidy", {"project": "example/widget", "batch": batch,
                                           "ids": [ids[1]]})
    assert ok and result["restored"] == 1
    line = next(a for a in owner_direction.actions() if a["id"] == result["owner_action"])
    assert line["facts"]["ids"] == [ids[1]]
    assert states(conn, ids) == [("user", "conversation")] * 2


@pytest.mark.parametrize("args", [["--ids", ""], ["--ids", "0"], ["--ids", "garbage"],
                                  ["--apply", "unused.json", "--ids", "1"]])
def test_cli_rejects_invalid_selection_before_io(monkeypatch, args):
    monkeypatch.setattr(owed_tidy, "undo", lambda *a, **k: pytest.fail("undo was called"))
    ns = cli.build_parser().parse_args(["owed", "tidy", "--project", "example/widget", *args])
    with redirect_stdout(io.StringIO()):
        assert ns.func(ns) == 2


def test_owner_act_rejects_selection_on_apply():
    ok, result = owner_act.act("owed.tidy", {"project": "example/widget", "plan": "unused.json", "ids": [1]})
    assert not ok and "undo" in result["error"]
