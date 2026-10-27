"""Paging, filters and bulk changes in the owed tools (scope
docs/plans/2026-10-16-owed-and-ledger-ssot.md, design (c), slice 3, with
Matt's decision 6).

``khipu_owed`` pages on a keyset cursor (``opened_at``, ``id``) and says how
many items match (``total``) and how many each owner and kind holds
(``counts``). ``khipu_owed_update`` takes a list of ids: one transaction, one
result per id, queued and replayed whole when the hub is away. A bulk change
that touches the owner's own items passes the owner gate (the app, or the
owner's yes typed on a terminal); items owned by the assistant or an agent
need no gate. Both caps are settings, and 0 lifts each.

Everything runs on the throwaway PostgreSQL of tests/hermetic_pg.py. The
gate's inputs are faked (the process that started us, a terminal answer);
no real terminal is ever asked.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from khipu import cli, commitments as co, config, db, owner_writes, routing
from khipu import mcp_server as srv
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests import fact_seed

PROJECT = "acme/widget"
DOWN = psycopg.OperationalError("connection timeout expired")
BASE_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _aliases(monkeypatch):
    monkeypatch.setenv("KHIPU_USER_ALIASES", "matt")
    co.reset_user_patterns()
    yield
    co.reset_user_patterns()


@pytest.fixture
def owed(hub, tmp_path, monkeypatch):  # noqa: F811
    """A migrated hub every ``khipu.db.connect`` reaches, a temp data dir, and
    a caller that is not the app and a stdin that is not a terminal."""
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(tmp_path))
    conn = hub.connect()
    hub.migrate(conn=conn)
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, "connect", lambda *a, **k: hub.connect())
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["claude"]})
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    conn.autocommit = True
    return conn


def _seed(conn, n, *, start=0, text="follow up on item {i}", kind="followup", owner=None,
          origin="conversation", label=None, at=None, step_s=1) -> list[int]:
    """``n`` open items; ``opened_at`` steps every three rows, so ids break ties."""
    at = at or BASE_AT
    rows = [(text.format(i=i), PROJECT, owner, kind, at + timedelta(seconds=step_s * ((i - start) // 3)),
             co.content_hash(PROJECT, text.format(i=i)), origin, label)
            for i in range(start, start + n)]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO commitments (text, project, owner, kind, opened_at, status, content_hash, origin, owner_label) "
            "VALUES (%s, %s, %s, %s, %s, 'open', %s, %s, %s)", rows)
        cur.execute("SELECT id FROM commitments WHERE content_hash = ANY(%s) ORDER BY id",
                    ([r[5] for r in rows],))
        ids = [int(r[0]) for r in cur.fetchall()]
        fact_seed.adopt(cur, "commitment", ids)
        return ids


def _status(conn, ids) -> dict[int, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id, status FROM commitments WHERE id = ANY(%s)", (list(ids),))
        return {int(i): s for i, s in cur.fetchall()}


def _fake_terminal(monkeypatch, answer: str) -> list[str]:
    """A stdin that is a terminal and answers ``answer``; returns the prompts."""
    asked: list[str] = []

    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty(answer + "\n"))
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or answer)
    return asked


# ---- paging -----------------------------------------------------------------


def test_4000_items_page_through_with_no_skip_or_repeat_while_50_more_arrive(owed):
    first = _seed(owed, 4000)
    seen: list[int] = []
    cursor = None
    pages = 0
    while True:
        out = srv._tool_owed({"project": PROJECT, "limit": 200, **({"cursor": cursor} if cursor else {})})
        if pages == 0:
            assert out["total"] == 4000
            # 50 more arrive while the caller is paging.
            _seed(owed, 50, start=10_000, at=datetime.now(timezone.utc))
        seen += [r["id"] for r in out["results"]]
        pages += 1
        cursor = out["next_cursor"]
        if not cursor:
            break
        assert pages < 100
    assert len(seen) == len(set(seen)), "a page repeated an item"
    assert set(first) <= set(seen), "a page skipped an item"
    assert pages == 20


def test_the_page_size_is_50_by_default_capped_by_the_setting_and_0_lifts_it(owed):
    _seed(owed, 600)
    assert len(srv._tool_owed({"project": PROJECT})["results"]) == 50
    capped = srv._tool_owed({"project": PROJECT, "limit": 500})
    assert len(capped["results"]) == 200 and capped["limit"] == 200
    assert "owed_page_max" in capped["limit_note"]
    config.set_count_setting("owed_page_max", 0)
    assert len(srv._tool_owed({"project": PROJECT, "limit": 500})["results"]) == 500
    config.set_count_setting("owed_page_max", 10)
    assert len(srv._tool_owed({"project": PROJECT, "limit": 500})["results"]) == 10


def test_filters_and_counts(owed):
    mine = _seed(owed, 3, text="should we ship widget {i}?", kind="question")
    job = _seed(owed, 2, start=100, text="builder to rerun the suite {i}", owner="agent",
                origin="job", label="builder")
    pr = _seed(owed, 1, start=200, text="follow up once PR #412 merges {i}")
    plain = _seed(owed, 4, start=300, text="tidy the release notes {i}")

    every = srv._tool_owed({"project": PROJECT})
    assert every["total"] == 10
    assert every["counts"]["owner"] == {"user": 3, "assistant": 5, "agent": 2}
    assert every["counts"]["kind"] == {"question": 3, "followup": 7}

    user = srv._tool_owed({"project": PROJECT, "owner": "user"})
    assert sorted(r["id"] for r in user["results"]) == mine and user["total"] == 3
    # Counts by owner stay over every owner, so a caller sees what the other headings hold.
    assert user["counts"]["owner"]["assistant"] == 5 and user["counts"]["kind"] == {"question": 3}
    assert sorted(r["id"] for r in srv._tool_owed({"project": PROJECT, "owner": "agent"})["results"]) == job
    assert sorted(r["id"] for r in srv._tool_owed({"project": PROJECT, "origin": "job"})["results"]) == job
    assert sorted(r["id"] for r in srv._tool_owed({"project": PROJECT, "kind": "question"})["results"]) == mine
    assert [r["id"] for r in srv._tool_owed({"project": PROJECT, "pr": 412})["results"]] == pr
    assert sorted(r["id"] for r in srv._tool_owed({"project": PROJECT, "query": "Release NOTES"})["results"]) == plain
    assert srv._tool_owed({"project": PROJECT, "owner": "all"})["total"] == 10
    with pytest.raises(ValueError, match="owner"):
        srv._tool_owed({"owner": "bogus"})
    with pytest.raises(ValueError, match="cursor"):
        srv._tool_owed({"cursor": "not a cursor"})


# ---- bulk -------------------------------------------------------------------


def test_bulk_close_of_500_is_one_transaction_with_one_result_per_id(owed, monkeypatch):
    ids = _seed(owed, 500)
    real = co.set_status
    calls = {"n": 0}

    def fails_late(cur, cid, status, **kw):
        calls["n"] += 1
        if calls["n"] == 300:
            raise RuntimeError("the hub fell over mid-batch")
        return real(cur, cid, status, **kw)

    monkeypatch.setattr(co, "set_status", fails_late)
    with pytest.raises(RuntimeError):
        srv._tool_owed_update({"ids": ids, "action": "close", "reason": "shipped"})
    assert set(_status(owed, ids).values()) == {"open"}, "a failed batch left part of it applied"

    monkeypatch.setattr(co, "set_status", real)
    out = srv._tool_owed_update({"ids": ids, "action": "close", "reason": "shipped"})
    assert out["ok"] is True and out["changed"] == 500
    assert [r["id"] for r in out["results"]] == ids and all(r["ok"] for r in out["results"])
    assert set(_status(owed, ids).values()) == {"closed"}
    with owed.cursor() as cur:
        cur.execute("SELECT count(*) FROM owner_writes_applied WHERE tool = 'owed_update' AND outcome = 'applied'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT DISTINCT close_reason, closed_by FROM commitments WHERE id = ANY(%s)", (ids,))
        assert cur.fetchall() == [("shipped", co.BY_CONVERSATION)]
    # An id that is not there gets its own result; the rest still apply.
    out = srv._tool_owed_update({"ids": [9_999_999, ids[0]], "action": "reopen", "reason": "not yet"})
    assert out["changed"] == 1 and out["results"][0] == {
        "id": 9_999_999, "ok": False, "error": "no such commitment: 9999999"}


def test_a_list_needs_a_reason(owed):
    ids = _seed(owed, 2)
    with pytest.raises(ValueError, match="reason"):
        srv._tool_owed_update({"ids": ids, "action": "close"})
    assert set(_status(owed, ids).values()) == {"open"}


def test_the_bulk_cap_refuses_over_the_limit_and_0_lifts_it(owed):
    ids = _seed(owed, 4)
    config.set_count_setting("owed_bulk_max", 3)
    with pytest.raises(ValueError) as err:
        srv._tool_owed_update({"ids": ids, "action": "close", "reason": "done"})
    assert "owed_bulk_max" in str(err.value) and "nothing was changed" in str(err.value).lower()
    assert set(_status(owed, ids).values()) == {"open"}
    config.set_count_setting("owed_bulk_max", 0)
    assert srv._tool_owed_update({"ids": ids, "action": "close", "reason": "done"})["changed"] == 4


def test_a_queued_bulk_write_replays_whole(owed, hub, monkeypatch):  # noqa: F811
    ids = _seed(owed, 30)
    reach = db.connect

    def down(*a, **k):
        raise DOWN

    # The hub is away, so the owners cannot be read: the app's own call passes the gate.
    monkeypatch.setattr(db, "connect", down)
    monkeypatch.setattr(routing, "_caller", lambda: {"app": True, "chain": ["Khipu"], "where": "/Applications"})
    out = srv._tool_owed_update({"ids": ids, "action": "close", "reason": "shipped"})
    assert out["waiting_to_sync"] is True and [r["id"] for r in out["results"]] == ids
    queued = sorted((owner_writes.queue_dir()).glob("*.json"))
    assert len(queued) == 1
    assert owner_writes.pending_marks("owed_update") == {i: "close" for i in ids}

    monkeypatch.setattr(db, "connect", reach)
    res = owner_writes.drain()
    assert res["replayed"] == 1 and res["waiting"] == 0
    assert set(_status(owed, ids).values()) == {"closed"}
    assert owner_writes.drain()["replayed"] == 0


def test_a_bulk_close_of_the_owners_items_is_refused_without_the_gate(owed):
    mine = _seed(owed, 2, text="should we ship widget {i}?", kind="question")
    theirs = _seed(owed, 2, start=50)
    out = srv._tool_owed_update({"ids": mine + theirs, "action": "close", "reason": "tidy"})
    assert out["ok"] is False and out["changed"] == 0
    assert "Nothing was changed" in out["error"] and sorted(out["needs_owner"]) == mine
    assert set(_status(owed, mine + theirs).values()) == {"open"}
    # A caller cannot claim the gate itself.
    out = srv._tool_owed_update({"ids": mine, "action": "close", "reason": "tidy", "owner_gate": "app"})
    assert out["ok"] is False and set(_status(owed, mine).values()) == {"open"}


def test_the_hub_refuses_a_bulk_change_to_the_owners_items_with_no_gate_stamp(owed):
    mine = _seed(owed, 2, text="should we ship widget {i}?", kind="question")
    out = srv._owed_update_on_hub({"ids": mine, "action": "close", "reason": "tidy", "write_id": "w1"})
    assert out["ok"] is False and "Nothing was changed" in out["error"]
    assert set(_status(owed, mine).values()) == {"open"}


def test_a_bulk_close_of_the_owners_items_passes_with_the_app_or_a_typed_yes(owed, monkeypatch, capsys):
    mine = _seed(owed, 4, text="should we ship widget {i}?", kind="question")
    asked = _fake_terminal(monkeypatch, "yes")
    args = cli.build_parser().parse_args(["owed", "--close", f"{mine[0]},{mine[1]}", "--reason", "Matt said done"])
    assert args.func(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["changed"] == 2 and len(asked) == 1
    assert _status(owed, mine[:2]) == {mine[0]: "closed", mine[1]: "closed"}

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr(routing, "_caller", lambda: {"app": True, "chain": ["Khipu"], "where": "/Applications"})
    args = cli.build_parser().parse_args(["owed", "--close", f"{mine[2]},{mine[3]}", "--reason", "done"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 2
    with owed.cursor() as cur:
        cur.execute("SELECT closed_by FROM commitments WHERE id = %s", (mine[3],))
        assert cur.fetchone()[0] == co.BY_APP


def test_a_typed_no_refuses_and_changes_nothing(owed, monkeypatch, capsys):
    mine = _seed(owed, 2, text="should we ship widget {i}?", kind="question")
    _fake_terminal(monkeypatch, "no")
    args = cli.build_parser().parse_args(["owed", "--close", f"{mine[0]},{mine[1]}", "--reason", "x"])
    assert args.func(args) == 1
    assert "Nothing was changed" in json.loads(capsys.readouterr().out)["error"]
    assert set(_status(owed, mine).values()) == {"open"}


def test_a_conversation_bulk_changes_assistant_and_agent_items_without_the_gate(owed, monkeypatch):
    ids = _seed(owed, 3) + _seed(owed, 2, start=100, text="builder to rerun {i}", owner="agent",
                                 origin="job", label="builder")
    monkeypatch.setattr(routing, "typed_yes", lambda prompt: pytest.fail("the gate was asked"))
    out = srv._tool_owed_update({"ids": ids, "action": "close", "reason": "kept my promises"})
    assert out["ok"] is True and out["changed"] == 5


def test_a_single_close_by_id_stays_as_today(owed, monkeypatch):
    mine = _seed(owed, 1, text="should we ship widget {i}?", kind="question")
    monkeypatch.setattr(routing, "typed_yes", lambda prompt: pytest.fail("the gate was asked"))
    out = srv._tool_owed_update({"id": mine[0], "action": "close"})
    assert out["ok"] is True and out["id"] == mine[0]
    assert _status(owed, mine) == {mine[0]: "closed"}


def test_reassign_moves_owner_and_label_and_it_sticks(owed):
    ids = _seed(owed, 2, start=0, text="rerun the flaky suite {i}")
    out = srv._tool_owed_update({"ids": ids, "action": "reassign", "owner": "agent",
                                 "owner_label": "builder", "reason": "the job owns these"})
    assert out["ok"] is True and out["changed"] == 2
    rows = {r["id"]: r for r in srv._tool_owed({"project": PROJECT})["results"]}
    assert {(rows[i]["owner"], rows[i]["owner_label"]) for i in ids} == {("agent", "builder")}
    # A question moved to the assistant stays the assistant's, though its text reads as the owner's.
    q = _seed(owed, 1, start=500, text="should we ship widget {i}?", kind="question")
    srv._tool_owed_update({"id": q[0], "action": "reassign", "owner": "assistant"})
    row = next(r for r in srv._tool_owed({"project": PROJECT})["results"] if r["id"] == q[0])
    assert row["owner"] == "assistant"
    with pytest.raises(ValueError, match="owner"):
        srv._tool_owed_update({"id": q[0], "action": "reassign", "owner": "someone"})


def _owners(conn, ids):
    with conn.cursor() as cur:
        cur.execute("SELECT id, owner, owner_label, owner_reassigned_at, tidy_batch FROM commitments "
                    "WHERE id = ANY(%s) ORDER BY id", (list(ids),))
        return {int(r[0]): r[1:] for r in cur.fetchall()}


def test_bulk_reassignment_keeps_its_reason_and_prior_owner(owed, monkeypatch):
    """A successful owner-approved bulk move must leave a durable undo/audit record."""
    from khipu import owed_tidy

    reason = "Owner-approved move after a specific review"
    ids = _seed(owed, 1, text="ship widget {i} to staging", owner="user") + _seed(
        owed, 1, start=10, text="reviewer to rebase widget {i}", owner="agent", origin="job", label="reviewer")
    before = _owners(owed, ids)
    monkeypatch.setattr(routing, "_caller", lambda: {"app": True, "chain": ["khipu"]})
    result = srv.owed_change(ids, "reassign", by=co.BY_APP, reason=reason, owner="assistant", queue_ok=False)
    assert result["changed"] == 2
    assert {row[0] for row in _owners(owed, ids).values()} == {"assistant"}
    with owed.cursor() as cur:
        cur.execute("SELECT commitment_id, reason, prior FROM commitment_tidy_items WHERE batch_id = ANY(%s)",
                    (result["batches"],))
        state = {int(c): {"reason": r, "prior": p} for c, r, p in cur.fetchall()}
    assert reason in json.dumps(state), "Successful bulk reassignment discarded its required reason and prior owners"
    assert (state[ids[0]]["prior"]["owner"], state[ids[1]]["prior"]["owner_label"]) == ("user", "reviewer")
    undone = owed_tidy.undo(result["batches"][0], project=PROJECT)
    assert undone["ok"] and undone["restored"] == 2
    assert _owners(owed, ids) == before


# ---- the CLI ----------------------------------------------------------------


def test_cli_filters_cursor_and_json_is_a_no_op(owed, capsys):
    mine = _seed(owed, 3, text="should we ship widget {i}?", kind="question")
    _seed(owed, 3, start=50)

    def run(*argv):
        args = cli.build_parser().parse_args(["owed", *argv])
        assert args.func(args) == 0
        out = json.loads(capsys.readouterr().out)
        # Ages are counted at each read; everything else must match.
        for row in (out["results"] if isinstance(out, dict) else out):
            row.pop("opened_age_s", None)
            row.pop("last_seen_age_s", None)
        return out

    page = run("--project", PROJECT, "--owner", "user", "--limit", "2")
    assert page["total"] == 3 and len(page["results"]) == 2 and page["next_cursor"]
    assert page["counts"]["owner"]["assistant"] == 3
    rest = run("--project", PROJECT, "--owner", "user", "--kind", "question", "--limit", "2",
               "--cursor", page["next_cursor"])
    assert sorted(r["id"] for r in page["results"] + rest["results"]) == mine and rest["next_cursor"] is None
    assert run("--project", PROJECT, "--query", "widget", "--json") == run("--project", PROJECT, "--query", "widget")
    # --json alone changes nothing either.
    assert run("--project", PROJECT, "--json") == run("--project", PROJECT)


def test_cli_reassign_and_reopen_take_lists(owed, capsys):
    ids = _seed(owed, 2)

    def run(*argv):
        args = cli.build_parser().parse_args(["owed", *argv])
        rc = args.func(args)
        return rc, json.loads(capsys.readouterr().out)

    rc, out = run("--reassign", f"{ids[0]},{ids[1]}", "--to", "agent", "--label", "reviewer", "--reason", "review")
    assert rc == 0 and out["changed"] == 2
    rc, out = run("--close", f"{ids[0]},{ids[1]}", "--reason", "done", "--json")
    assert rc == 0 and out["changed"] == 2
    rc, out = run("--reopen", f"{ids[0]},{ids[1]}", "--reason", "not done")
    assert rc == 0 and set(_status(owed, ids).values()) == {"open"}
    rc, out = run("--close", f"{ids[0]},{ids[1]}")
    assert rc == 2 and "reason" in out["error"]


def test_the_two_caps_are_count_settings():
    assert config.COUNT_SETTINGS["owed_page_max"] == 200
    assert config.COUNT_SETTINGS["owed_bulk_max"] == 500


# ---- the second cross-family review: every bulk action keeps the same audit ----


def _state(conn, ids):
    from khipu import owed_tidy

    cols = ", ".join(owed_tidy.BULK_PRIOR_FIELDS)
    with conn.cursor() as cur:
        cur.execute(f"SELECT id, {cols} FROM commitments WHERE id = ANY(%s) ORDER BY id", (list(ids),))
        return {int(r[0]): r[1:] for r in cur.fetchall()}


def _audit(conn, batches):
    with conn.cursor() as cur:
        cur.execute("SELECT commitment_id, action, reason, prior FROM commitment_tidy_items "
                    "WHERE batch_id = ANY(%s) ORDER BY commitment_id", (list(batches),))
        return {int(c): {"action": a, "reason": r, "prior": p} for c, a, r, p in cur.fetchall()}


def test_bulk_snooze_preserves_reason_and_prior_due_after(owed):
    """The review's probe on the hermetic hub: a successful bulk snooze keeps
    its reason and each item's previous due date, and undo restores both."""
    from khipu import owed_tidy

    reason = "Matt asked to defer these until the vendor replies"
    original_due_after = "2026-10-18"
    ids = _seed(owed, 2, owner="assistant")
    with owed.cursor() as cur:
        cur.execute("UPDATE commitments SET due_after = %s::timestamptz WHERE id = %s", (original_due_after, ids[0]))
    before = _state(owed, ids)
    result = srv.owed_change(ids, "snooze", by=co.BY_CONVERSATION, until="7d", reason=reason, queue_ok=False)
    assert result["ok"] and result["changed"] == 2 and result["action"] == "snooze"
    assert result["results"] == [{"id": ids[0], "ok": True}, {"id": ids[1], "ok": True}]
    assert _state(owed, ids)[ids[0]] != before[ids[0]]
    state = _audit(owed, result["batches"])
    assert reason in json.dumps(state)
    assert original_due_after in json.dumps(state)
    assert state[ids[1]]["prior"]["due_after"] is None
    undone = owed_tidy.undo(result["batches"][0], project=PROJECT)
    assert undone["ok"] and undone["restored"] == 2
    assert _state(owed, ids) == before


@pytest.mark.parametrize("action", ["close", "reopen", "snooze", "reassign"])
def test_every_bulk_action_keeps_the_same_audit_record_and_undoes_exactly(owed, action):
    from khipu import owed_tidy

    assert set(owed_tidy.BULK_ACTIONS) == {"close", "reopen", "snooze", "reassign"}
    ids = _seed(owed, 2, text=f"{action} widget {{i}} in the staging area", owner="assistant")
    if action == "reopen":
        with owed.cursor() as cur:
            cur.execute("UPDATE commitments SET status = 'closed', closed_at = now(), close_reason = 'done', "
                        "closed_by = 'a conversation' WHERE id = ANY(%s)", (ids,))
    before = _state(owed, ids)
    reason = f"bulk {action} for the audit test"
    extra = {"until": "2w"} if action == "snooze" else {"owner": "agent", "owner_label": "reviewer"} \
        if action == "reassign" else {}
    result = srv.owed_change(ids, action, by=co.BY_CONVERSATION, reason=reason, queue_ok=False, **extra)
    assert result["ok"] and result["changed"] == 2, result
    audit = _audit(owed, result["batches"])
    assert sorted(audit) == ids
    for cid in ids:
        assert audit[cid]["action"] == action and audit[cid]["reason"] == reason
        assert set(audit[cid]["prior"]) == set(owed_tidy.BULK_PRIOR_FIELDS)
    assert _state(owed, ids) != before
    undone = owed_tidy.undo(result["batches"][0], project=PROJECT)
    assert undone["ok"] and undone["restored"] == 2, undone
    assert _state(owed, ids) == before


def test_plain_cli_listing_obeys_page_cap(monkeypatch, capsys):
    """The review's probe: `khipu owed --limit 500` with owed_page_max 200
    gives one page of 200 and a cursor, through the same listing as the tool."""
    at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = [dict(id=i, text=f"Rebuild deployment index {i}", owner="assistant", kind="followup", opened_at=at)
            for i in range(1, 501)]

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def execute(self, sql, params=()):
            self.sql, self.params = sql, params

        def fetchall(self):
            assert self.sql.startswith("WITH c AS (SELECT COALESCE((SELECT project FROM project_aliases")
            project, fallback = self.params
            assert project == fallback
            return [(project, None)]  # Canonical project with no aliases.

        def fetchone(self):
            return (at,)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def cursor(self):
            return Cursor()

    monkeypatch.setattr(db, "connect", lambda **kwargs: Connection())
    monkeypatch.setattr(co, "fetch_owed_rows", lambda *args, **kwargs: rows)
    monkeypatch.setattr(config, "count_setting", lambda key: 200)
    args = cli.build_parser().parse_args(["owed", "--project", "acme/widget", "--limit", "500"])
    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    results = result["results"] if isinstance(result, dict) else result
    assert len(results) <= 200, f"owed_page_max=200 returned {len(results)} rows"
    assert result["next_cursor"]
    # The same page the tool gives.
    tool = srv._tool_owed({"project": "acme/widget", "limit": 500})
    assert [r["id"] for r in tool["results"]] == [r["id"] for r in results]
    # The same place in the list; each read stamps its own clock for the quiet group.
    assert co.decode_cursor(tool["next_cursor"]) == co.decode_cursor(result["next_cursor"])


# ---- the third cross-family review's bulk probes (fakes, no hub) ----------
# The round-2 snooze probe with its response-shape fake updated for the new
# batches key; its assertions are the reviewer's.

import copy  # noqa: E402
from tests.fact_seed import announce  # noqa: E402



def test_bulk_snooze_keeps_audit_with_new_batch_response(monkeypatch):
    from khipu import commitments, config, db, mcp_server, offline, owed_tidy

    reason = "Matt asked to defer these until the vendor replies"
    original_due_after = "2026-10-18"
    state = {
        "commitments": {1: {"due_after": original_due_after}, 2: {"due_after": None}},
        "owner_writes_applied": [],
        "sql": [], "batches": {}, "items": [],
    }

    class Cursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=()):
            compact = " ".join(sql.split())
            params = tuple(params)
            state["sql"].append((compact, params))
            if compact.startswith("INSERT INTO owner_writes_applied"):
                state["owner_writes_applied"].append({"write_id": params[0], "tool": params[1]})
                self.rowcount = 1
            elif compact.startswith("UPDATE commitments SET due_after"):
                commitment_id = params[-1]
                state["commitments"][commitment_id]["due_after"] = (params[0] if len(params) > 1 else "2026-10-23")
                self.rowcount = 1
            elif compact.startswith("SELECT id, project FROM commitments"):
                self.result = [(cid, "acme/widget") for cid in params[0]]
            elif compact.startswith("INSERT INTO commitment_tidy_batches"):
                state["batches"][params[0]] = {"project": params[1]}
            elif compact.startswith("UPDATE commitments SET tidy_batch"):
                state["commitments"][params[1]]["tidy_batch"] = params[0]
            elif compact.startswith("INSERT INTO commitment_tidy_items"):
                state["items"].append({"batch": params[0], "id": params[1], "action": params[2],
                                       "reason": params[3], "prior": json.loads(params[4])})
            elif compact.startswith("UPDATE commitment_tidy_batches SET counts"):
                state["batches"][params[1]]["counts"] = json.loads(params[0])
            else:
                raise AssertionError(f"unexpected SQL: {compact}")

        def fetchone(self):
            return None

        def fetchall(self):
            return self.result

    class Connection:
        def __init__(self):
            self.commits = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.commits += 1

        def rollback(self):
            return None

    conn = Connection()
    monkeypatch.setattr(db, "connect", lambda **kwargs: conn)
    monkeypatch.setattr(owed_tidy, "_ready", lambda cur: True)
    monkeypatch.setattr(owed_tidy, "_select", lambda cur, project, ids=None, **kwargs: {
        cid: copy.deepcopy(row) for cid, row in state["commitments"].items()
        if ids is None or cid in ids
    })
    monkeypatch.setattr(config, "count_setting", lambda key: 0 if key == "owed_bulk_max" else 200)
    monkeypatch.setattr(offline, "setting", lambda key: 10)
    monkeypatch.setattr(mcp_server, "_ensure_path", lambda: None)
    monkeypatch.setattr(mcp_server, "_owed_owners_items", lambda ids: [])
    monkeypatch.setattr(commitments, "owners_of", lambda cur, ids, **kwargs: {
        commitment_id: {"owner": commitments.OWNER_ASSISTANT} for commitment_id in ids
    })

    result = mcp_server.owed_change(
        [1, 2], "snooze", by=commitments.BY_CONVERSATION,
        until="7d", reason=reason, queue_ok=False,
    )

    assert result['ok'] is True
    assert result['changed'] == 2
    assert result['results'] == [{'id':1,'ok':True},{'id':2,'ok':True}]
    assert result['batches']
    assert len(state['items']) == 2
    assert state['items'][0]['prior']['due_after'] == original_due_after
    assert state['items'][0]['reason'] == reason
    assert conn.commits == 1
    assert state["commitments"][1]["due_after"] != original_due_after
    assert reason in json.dumps(state)
    assert original_due_after in json.dumps(state)


def test_bulk_owner_gate_refuses_and_never_dispatches(monkeypatch):
    from khipu import mcp_server as srv, commitments as co, config, owner_writes
    monkeypatch.setattr(config,'count_setting',lambda name:500)
    monkeypatch.setattr(srv,'_owed_owners_items',lambda ids:[1])
    monkeypatch.setattr(co,'bulk_owner_gate',lambda count:None)
    def forbidden(*a,**kw): raise AssertionError('owner write dispatched without owner gate')
    monkeypatch.setattr(owner_writes,'run',forbidden)
    result=srv.owed_change([1,2],'close',by=co.BY_CONVERSATION,reason='fake requested close')
    assert not result['ok'] and result['changed']==0
    assert result['needs_owner']==[1]


def test_bulk_limit_is_enforced_and_zero_lifts_it(monkeypatch):
    import pytest
    from khipu import mcp_server as srv, commitments as co, config, owner_writes
    monkeypatch.setattr(config,'count_setting',lambda name:1)
    with pytest.raises(ValueError,match='at most 1'):
        srv.owed_change([1,2],'close',by=co.BY_CONVERSATION,reason='fake close')
    monkeypatch.setattr(config,'count_setting',lambda name:0)
    monkeypatch.setattr(srv,'_owed_owners_items',lambda ids:[])
    called=[]
    monkeypatch.setattr(owner_writes,'run',lambda tool,args,**kw:called.append(args) or {'ok':True})
    assert srv.owed_change([1,2],'close',by=co.BY_CONVERSATION,reason='fake close')['ok']
    assert called[0]['ids']==[1,2]


def test_four_thousand_rows_page_stably_while_fifty_arrive():
    from datetime import datetime,timezone,timedelta
    from khipu import commitments as co
    at=datetime(2026,10,1,tzinfo=timezone.utc)
    rows=[dict(id=i,text=f'Rebuild deployment index {i}',owner='assistant',kind='followup',opened_at=at+timedelta(seconds=i//3)) for i in range(1,4001)]
    result=co.page_owed(rows,limit=113)
    seen=[r['id'] for r in result['results']]
    rows.extend(dict(id=i,text=f'Rebuild deployment index {i}',owner='assistant',kind='followup',opened_at=at+timedelta(days=1)) for i in range(4001,4051))
    while result['next_cursor']:
        result=co.page_owed(rows,cursor=result['next_cursor'],limit=113)
        seen.extend(r['id'] for r in result['results'])
    assert seen == list(range(4000,0,-1))


# ---- reassignment metadata and linked items on the hub (migration 0037) -----

def test_a_reassign_keeps_who_why_and_the_prior_owner(owed):
    ids = _seed(owed, 1, start=700, text="The reviewer must rebase the release branch {i}", owner="user")
    out = srv._tool_owed_update({"id": ids[0], "action": "reassign", "owner": "agent",
                                 "reason": "the reviewer owns rebases"})
    assert out["ok"] is True
    with owed.cursor() as cur:
        cur.execute("SELECT owner, owner_label, owner_prior, owner_prior_label, owner_reassigned_by, "
                    "owner_reassign_reason, owner_reassigned_at IS NOT NULL FROM commitments WHERE id = %s",
                    (ids[0],))
        owner, label, prior, prior_label, by, reason, stamped = cur.fetchone()
    # No label given: the owner function's own, the subject the words name.
    assert (owner, label, prior, prior_label, reason, stamped) == (
        "agent", "reviewer", "user", None, "the reviewer owns rebases", True)
    assert by
    row = next(r for r in srv._tool_owed({"project": PROJECT})["results"] if r["id"] == ids[0])
    assert (row["owner_prior"], row["owner_reassign_reason"]) == ("user", "the reviewer owns rebases")


def test_similar_items_of_two_owners_both_stay_linked_on_the_hub(owed):
    from unittest import mock

    # One transaction, as capture runs these writers in (the fact gate checks at COMMIT).
    with owed.transaction(), owed.cursor() as cur:
        announce(cur)
        cur.execute("INSERT INTO episodes (ts, summary, raw) VALUES (now(), 's', '{}'::jsonb) RETURNING id")
        ep = int(cur.fetchone()[0])
        base = {"project": PROJECT, "session_id": "claude_code:host-1"}
        with mock.patch.object(co, "_has_commitment_embeddings", lambda cur, ids: False):
            assert co.open_from_episode(cur, dict(base, open_loops=[
                {"text": "Review the deployment checklist before release", "kind": "followup",
                 "owner": "assistant"}]), ep) == 1
            assert co.open_from_episode(cur, dict(base, open_loops=[
                {"text": "I will review the deployment checklist before release", "kind": "promise",
                 "owner": "user"}]), ep) == 1
            assert co.open_from_episode(cur, dict(base, open_loops=[
                {"text": "Review the deployment checklist before the release", "kind": "followup",
                 "owner": "assistant"}]), ep) == 0
        cur.execute("SELECT id, owner, linked_ids, latest_text FROM commitments WHERE project = %s "
                    "AND status = 'open' ORDER BY id", (PROJECT,))
        (a, a_owner, a_links, a_latest), (u, u_owner, u_links, _u_latest) = cur.fetchall()
    assert (a_owner, u_owner) == ("assistant", "user")
    assert (a_links, u_links) == ([u], [a])
    assert a_latest == "Review the deployment checklist before the release"


# ---- "close those": a bulk change to the owner's items on his direction ---------------------


def _direction(tmp_path, monkeypatch, session="s-close-those"):
    from khipu import owner_turns

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    owner_turns.record({"session_id": session, "prompt": "close those", "cwd": str(tmp_path)},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})


def _owner_undo(line_id: str) -> tuple[int, dict]:
    from contextlib import redirect_stderr, redirect_stdout

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(["owner-direction", "undo", line_id, "--owner-direction"])
    assert out.getvalue().strip(), err.getvalue()
    return rc, json.loads(out.getvalue())


def test_close_those_by_direction_closes_the_owners_items_and_undo_reopens_them(owed, tmp_path, monkeypatch):
    from khipu import owner_act

    mine = _seed(owed, 3, text="should we ship widget {i}?", kind="question")
    monkeypatch.setattr(routing, "typed_yes", lambda prompt: pytest.fail("the terminal was asked"))
    _direction(tmp_path, monkeypatch)
    ok_, out = owner_act.act("owed.change", {"ids": mine, "change": "close", "why": "Matt said close those"})
    assert ok_ and out["changed"] == 3 and out["owner_action"], out
    assert set(_status(owed, mine).values()) == {"closed"}
    rc, undone = _owner_undo(out["owner_action"])
    assert rc == 0 and undone["restored"] == 3, undone
    assert set(_status(owed, mine).values()) == {"open"}
    rc, again = _owner_undo(out["owner_action"])
    assert rc == 2 and "already undone" in again["error"], again


def test_close_those_is_refused_without_a_recent_owner_turn(owed, tmp_path, monkeypatch):
    from khipu import owner_act

    mine = _seed(owed, 2, text="should we ship widget {i}?", kind="question")
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-nobody-typed")
    ok_, out = owner_act.act("owed.change", {"ids": mine, "change": "close", "why": "a job thinks so"})
    assert not ok_ and out["code"] == "no_recorded_turn", out
    assert "Nothing was changed" in out["error"]
    assert set(_status(owed, mine).values()) == {"open"}


def test_a_direction_reassign_is_undone_back_to_the_owner(owed, tmp_path, monkeypatch):
    from khipu import owner_act

    mine = _seed(owed, 2, text="should we ship widget {i}?", kind="question")
    before = _state(owed, mine)
    _direction(tmp_path, monkeypatch)
    ok_, out = owner_act.act("owed.change", {"ids": mine, "change": "reassign", "why": "the builder has these",
                                             "to": "agent", "label": "builder"})
    assert ok_ and out["changed"] == 2, out
    assert _state(owed, mine) != before
    rc, undone = _owner_undo(out["owner_action"])
    assert rc == 0 and undone["restored"] == 2, undone
    assert _state(owed, mine) == before


def test_owed_change_by_direction_takes_only_its_own_arguments():
    from khipu import owner_act

    for args in ({"ids": [1, 2], "change": "reassign", "why": "x"},
                 {"ids": [1, 2], "change": "close", "why": "x", "to": "user"},
                 {"ids": ["1", "-2"], "change": "close", "why": "x"},
                 {"ids": [1, True], "change": "close", "why": "x"},
                 {"ids": [1, 2], "change": "snooze", "why": "x"},
                 {"ids": [1, 2], "change": "close"}):
        ok_, out = owner_act.act("owed.change", args)
        assert not ok_ and out["code"] == "bad_args", (args, out)
    head, pos = owner_act.argv("owed.change", owner_act._values(
        "owed.change", {"ids": [4, 5], "change": "close", "why": "done"}))
    ns = cli.build_parser().parse_args(owner_act.full_argv(head, pos, ["--owner-direction"]))
    assert ns.close == "4,5" and ns.reason == "done" and ns.by_direction is True
