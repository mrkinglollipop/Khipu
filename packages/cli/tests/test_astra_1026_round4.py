"""Re-check findings on round 3 of the memory pipeline privacy branch: a
re-emitted history that itself repeated a request, and a rule close quoting
a forgotten decision."""
# ruff: noqa: F811 - pytest fixtures imported, then requested by name
import json

from khipu import commitments, forget, hub_snapshot as hs, session_capture as sc
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401 - fixtures
from tests.test_astra_1026_capture import row, write
from tests.test_astra_1026_forget import PRIVATE, _ids
from tests.test_provenance_forget import PROJECT, _capture, _rows, env, pg  # noqa: F401, F811 - fixtures


def test_reemitted_history_with_a_repeated_request_is_not_recaptured(tmp_path):
    p = tmp_path / "transcript.jsonl"
    instruction = "Run the full migration checklist again and report every failure you see."
    first_user, second_user = row("user", instruction), row("user", instruction)
    first_reply = row("assistant", "First pass: two failures. " + "a" * 100)
    first_reply["message"]["id"] = "msg_1"
    second_reply = row("assistant", "Second pass: all clear now. " + "b" * 100)
    second_reply["message"]["id"] = "msg_2"
    write(p, [first_user, first_reply, second_user, second_reply])
    stats = {}
    _, offset, _ = sc.read_window(p, 0, seen=(), stats=stats)
    compact = {"type": "user", "isCompactSummary": True,
               "message": {"role": "user", "content": "This session is being continued. " + "s" * 80}}
    with p.open("a") as f:
        for entry in [compact, first_user, first_reply, second_user, second_reply]:
            f.write(json.dumps(entry) + "\n")
    messages, _, _ = sc.read_window(p, offset, seen=stats["fresh"])
    # Only Claude Code's own summary is new.
    assert [r for r, _ in messages] == ["user"] and messages[0][1].startswith("This session"), messages
    assert not any(instruction in t or "pass:" in t for _, t in messages), messages


def _closed_by_decision(pg, *, forget_first=False):
    pg.memory.mkdir(exist_ok=True)
    item_ep = _capture(loop="Choose the launch codename", kind="question", owner="user")
    victim = _capture(loop=None, decision=PRIVATE)
    cid, _ = _ids(pg, item_ep)
    _, did = _ids(pg, victim)
    if forget_first:
        assert forget.forget_everywhere(victim, memory_root=pg.memory, gateway=False).get("soft_deleted")
    # What close_by_rule writes for a strict decision close (close_rules._decision_rule).
    with pg.connect() as conn:
        conn.execute("UPDATE commitments SET status='closed', closed_at=now(), closed_episode=%s, close_reason=%s, "
                     "closed_by='rule', close_rule_key=%s, close_evidence=%s::jsonb WHERE id=%s",
                     (victim, f"settled by decision {did}: {PRIVATE[:150]!r}", f"decision:{did}",
                      json.dumps({"decision_id": did, "text": PRIVATE}), cid))
    return cid, victim


def test_rule_close_quoting_a_forgotten_decision_is_cleared(pg):
    cid, victim = _closed_by_decision(pg)
    out = forget.forget_everywhere(victim, memory_root=pg.memory, gateway=False)
    assert out.get("soft_deleted"), out
    with pg.connect() as conn, conn.cursor() as cur:
        closed = commitments.list_owed(cur, project=PROJECT, status="closed", limit=None)
        dumped = hs._hub_commitments(cur)
    assert [c["id"] for c in closed] == [cid], closed
    assert "aubergine" not in json.dumps(closed, default=str), closed
    assert "aubergine" not in json.dumps(dumped, default=str), dumped
    stored = _rows(pg, "SELECT status, close_reason, close_evidence::text FROM commitments WHERE id=%s", (cid,))
    assert stored == [("closed", "forgotten", None)], stored


def test_reader_hides_a_close_reason_the_forget_missed(pg):
    # A row written after the forget ran, or by older code.
    _closed_by_decision(pg, forget_first=True)
    with pg.connect() as conn, conn.cursor() as cur:
        closed = commitments.list_owed(cur, project=PROJECT, status="closed", limit=None)
        dumped = hs._hub_commitments(cur)
    assert [c["close_reason"] for c in closed] == ["forgotten"], closed
    assert "aubergine" not in json.dumps(dumped, default=str), dumped


def test_reemitted_repeat_followed_by_another_request_is_not_recaptured(tmp_path):
    # User X, reply A, user X, user Y, reply B: the repeat is followed by a
    # user message, not the reply that proves the old history held it.
    p = tmp_path / "transcript.jsonl"
    instruction = "Run the full migration checklist again and report every failure you see."
    follow_up = "Also list which of those failures block tonight's deploy, with owners."
    first_reply = row("assistant", "First pass: two failures. " + "a" * 100)
    first_reply["message"]["id"] = "msg_1"
    second_reply = row("assistant", "Second pass: one blocker. " + "b" * 100)
    second_reply["message"]["id"] = "msg_2"
    history = [row("user", instruction), first_reply, row("user", instruction), row("user", follow_up), second_reply]
    write(p, history)
    stats = {}
    _, offset, _ = sc.read_window(p, 0, seen=(), stats=stats)
    compact = {"type": "user", "isCompactSummary": True,
               "message": {"role": "user", "content": "This session is being continued. " + "s" * 80}}
    with p.open("a") as f:
        for entry in [compact, *history]:
            f.write(json.dumps(entry) + "\n")
    messages, _, _ = sc.read_window(p, offset, seen=stats["fresh"])
    assert not any(instruction in t or follow_up in t or "pass:" in t for _, t in messages), messages
