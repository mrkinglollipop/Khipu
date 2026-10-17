"""The one-time clean-up of an Owed list (scope
docs/plans/2026-10-16-owed-and-ledger-ssot.md, design (d), slice 4, with
Matt's decision 4: rules first, then a capped model pass for what the rules
cannot sort; a preview before anything changes; nothing deleted; every change
reversible).

``khipu owed tidy --project P`` plans: each open item is kept, reassigned (by
the owner rules re-run on its text and origin) or closed (every named pull
request done, settled by a decision, or a duplicate of a kept item). Applying
a plan writes one batch that an undo restores, leaving alone any item
changed since. A plan or undo that changes the user's own items passes the
owner gate.

Everything runs on the throwaway PostgreSQL of tests/hermetic_pg.py. The
model is a fake; GitHub is the session's failing ``gh`` shim (a PR it is
asked about stays open); the gate's inputs are faked.
"""
from __future__ import annotations

import io
import json
import string
from datetime import datetime, timedelta, timezone

import pytest

from khipu import cli, commitments as co, config, db, owed_tidy, routing, work
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

PROJECT = "acme/widget"
BASE_AT = datetime(2026, 10, 8, tzinfo=timezone.utc)
FIELDS = ("status", "owner", "owner_label", "origin", "owner_reassigned_at", "closed_at", "closed_episode",
          "close_reason", "closed_by", "close_rule_key", "close_evidence", "tidy_batch", "text", "kind")


@pytest.fixture(autouse=True)
def _aliases(monkeypatch):
    monkeypatch.setenv("KHIPU_USER_ALIASES", "matt")
    co.reset_user_patterns()
    yield
    co.reset_user_patterns()


@pytest.fixture
def owed(hub, tmp_path, monkeypatch):  # noqa: F811
    """A migrated hub every ``khipu.db.connect`` reaches, a temp home, no job
    runner, a caller that is not the app and a stdin that is not a terminal."""
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
    real = work.settings
    monkeypatch.setattr(work, "settings", lambda config=None: dict(real({}), work_projects=[]))
    conn.autocommit = True
    return conn


def _word(i: int, salt: int) -> str:
    """A made-up five-letter word, distinct for every (i, salt), so the
    flood's texts never look like duplicates of each other by accident."""
    n = i * 7 + salt * 100_003
    letters = string.ascii_lowercase
    out = ""
    for _ in range(5):
        n, r = divmod(n, 26)
        out += letters[r]
    return out


def _words(i: int, k: int = 4) -> str:
    return " ".join(_word(i, s) for s in range(k))


class Flood:
    """A synthetic flood shaped like project A's: job and conversation
    captures, stored owner 'user' as the old extractor wrote it."""

    def __init__(self, conn):
        self.conn = conn
        self.groups: dict[str, list[int]] = {}
        self._n = 0

    def episode(self, *, parent: str | None = None, summary: str | None = None, at=None) -> int:
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO episodes (ts, session_id, summary, raw, project, parent_session_id) "
                        "VALUES (%s, %s, %s, '{}'::jsonb, %s, %s) RETURNING id",
                        (at or BASE_AT, f"s-{self._n}", summary or f"episode {self._n}", PROJECT, parent))
            self._n += 1
            return int(cur.fetchone()[0])

    def item(self, group: str, text: str, *, kind="followup", episode: int, owner="user", at=None) -> int:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO commitments (text, project, owner, kind, opened_episode, opened_at, status, "
                "content_hash) VALUES (%s, %s, %s, %s, %s, %s, 'open', %s) RETURNING id",
                (text, PROJECT, owner, kind, episode, at or BASE_AT, co.content_hash(PROJECT, text)))
            cid = int(cur.fetchone()[0])
        self.groups.setdefault(group, []).append(cid)
        return cid


def _flood(conn, *, model_questions: int = 6) -> Flood:
    f = Flood(conn)
    job = f.episode(parent="orch-1")
    conv = f.episode()
    for i in range(12):
        f.item("job_question", f"Should the {_words(i)} migration renumber after the rebase?",
               kind="question", episode=job)
    for i in range(10):
        f.item("job_followup", f"Rebase the {_words(i + 100)} stack and resolve the conflicts",
               episode=job)
    for i in range(6):
        f.item("job_blocker", f"Blocked until the {_words(i + 200)} fixture lands", kind="blocker",
               episode=job, owner="assistant")
    for i in range(5):
        f.item("job_names_user", f"Matt to decide the {_words(i + 300)} rollout order", episode=job)
    for i in range(5):
        f.item("agent_named", f"The orchestrator must resolve the {_words(i + 400)} conflict", episode=conv)
    for i in range(4):
        f.item("conv_names_user", f"Matt to approve the {_words(i + 500)} release notes", episode=conv)
    for i in range(6):
        f.item("conv_question", f"Should the {_words(i + 600)} cache use a shorter window?",
               kind="question", episode=conv)
    # No owner rule decides these: the capture's own owner, the assistant.
    for i in range(model_questions):
        f.item("conv_cache", f"Tune the {_words(i + 650)} cache window after the load test", episode=conv,
               owner="assistant")
    for i in range(3):
        f.item("conv_assistant", f"Write the {_words(i + 700)} changelog entry", episode=conv,
               owner="assistant")
    # PRs: 801 and 802 merged (a later capture carries git's and gh's lines), 900 still open.
    for i, prs in enumerate(("PR #801", "PR #801 and PR #802", "PR #802")):
        f.item("pr_merged", f"Land the {_words(i + 800)} fix in {prs} before the release", episode=conv)
    f.item("pr_open", f"Review the {_words(850)} change in PR #900 once checks pass", episode=conv)
    f.item("pr_half", f"Land the {_words(860)} fixes in PR #801 and PR #900", episode=conv)
    # Settled by a later decision.
    for i in range(3):
        f.item("decided", f"Decide whether the gateway keeps the {_words(i + 900)} route", kind="question",
               episode=conv)
    # Duplicates of kept items (the older one is the keeper).
    for i in range(4):
        keeper = f"Add the {_words(i + 1000)} retry to the importer"
        f.item("dup_keeper", keeper, episode=conv, owner="assistant")
        f.item("dup", keeper + " soon", episode=conv, owner="assistant", at=BASE_AT + timedelta(hours=1))
    later = f.episode(summary="Merge pull request #801 from acme/fix\n✓ Merged pull request acme/widget#802 (Fix)",
                      at=BASE_AT + timedelta(days=1))
    with conn.cursor() as cur:
        for i in range(3):
            cur.execute("INSERT INTO decisions (project, text, episode_id, decided_at) VALUES (%s, %s, %s, %s)",
                        (PROJECT, f"Decide whether the gateway keeps the {_words(i + 900)} route", later,
                         BASE_AT + timedelta(days=1)))
    return f


class FakeModel:
    """Says the orchestrator owns anything about a cache, the user the rest;
    records every text it is shown and every call."""

    def __init__(self):
        self.seen: list[str] = []
        self.calls = 0

    def __call__(self, texts):
        self.calls += 1
        self.seen += texts
        return [{"owner": "agent", "label": "orchestrator", "reason": "the job's question"} if "cache" in t
                else {"owner": "user", "reason": "the user's"} for t in texts]


def _rows(conn, ids=None) -> dict[int, dict]:
    with conn.cursor() as cur:
        where = "WHERE id = ANY(%s)" if ids is not None else ""
        cur.execute(f"SELECT id, {', '.join(FIELDS)} FROM commitments {where} ORDER BY id",
                    (list(ids),) if ids is not None else ())
        return {int(r[0]): dict(zip(FIELDS, r[1:])) for r in cur.fetchall()}


def _snapshot(conn) -> dict:
    """Every row of every table the clean-up could touch."""
    out = {}
    with conn.cursor() as cur:
        for table in ("commitments", "commitment_tidy_batches", "commitment_tidy_items", "decisions"):
            cur.execute(f"SELECT to_jsonb(t) FROM {table} t ORDER BY 1::text")
            out[table] = sorted(json.dumps(r[0], sort_keys=True, default=str) for r in cur.fetchall())
    return out


def _plan(conn, **kw):
    with conn.cursor() as cur:
        return owed_tidy.plan(cur, PROJECT, **kw)


def _by_id(plan) -> dict[int, dict]:
    return {it["id"]: it for it in plan["items"]}


def _app(monkeypatch):
    monkeypatch.setattr(routing, "_caller", lambda: {"app": True, "chain": ["Khipu"]})


# ---- the plan ---------------------------------------------------------------


def test_the_plan_counts_add_up_to_the_open_total(owed):
    f = _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    t = plan["totals"]
    open_total = sum(len(v) for v in f.groups.values())
    assert t["open"] == open_total == len(plan["items"])
    assert t["stay_user"] + t["keep_other"] + t["move"]["total"] + t["close"]["total"] == open_total
    assert t["close"]["total"] == t["close"]["pr"] + t["close"]["decision"] + t["close"]["duplicate"]
    assert t["move"]["total"] == sum(v for k, v in t["move"].items() if k != "total")
    assert plan["format"] == owed_tidy.PLAN_FORMAT and plan["version"] == owed_tidy.PLAN_VERSION
    for group, sample in plan["samples"].items():
        assert len(sample) <= 20, group


def test_the_plan_sorts_the_flood_by_rule_with_evidence(owed):
    f = _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    items = _by_id(plan)

    def actions(group):
        return {items[i]["action"] for i in f.groups[group]}

    # A job's asks stored as the owner's: the lineage of the capture that
    # opened them says they are the orchestrator's, but a stored owner moves
    # only by the owner, so the move is proposed, never made.
    for group in ("job_question", "job_followup", "job_names_user"):
        assert actions(group) == {"keep"}, group
        for i in f.groups[group]:
            assert items[i]["before"]["owner"] == "user", group
            proposed = items[i]["proposed"]
            assert proposed["action"] == "reassign" and proposed["rule"], group
            assert proposed["after"]["owner"] == "agent" and proposed["after"]["origin"] == "job", group
    assert items[f.groups["job_question"][0]]["proposed"]["after"]["owner_label"] == "orchestrator"
    # The owner's own conversation naming the orchestrator as the subject:
    # the orchestrator's whoever said it, already shown so, so it is kept.
    assert actions("agent_named") == {"keep"}
    assert (items[f.groups["agent_named"][0]]["before"]["owner"],
            items[f.groups["agent_named"][0]]["before"]["owner_label"]) == ("agent", "orchestrator")
    assert actions("conv_names_user") == {"keep"}
    # The owner's own items whose pull requests all merged: suggested, never
    # closed (Matt, 2026-10-17: "Suggest, don't close").
    assert actions("pr_merged") == {"keep"}
    assert {items[i]["proposed"]["close"] for i in f.groups["pr_merged"]} == {"pr"}
    assert "#801" in items[f.groups["pr_merged"][1]]["proposed"]["reason"]
    assert actions("pr_open") == actions("pr_half") == {"keep"}
    # The owner's own questions: a decision that settles them is a proposal.
    assert actions("decided") == {"keep"}
    assert {items[i]["proposed"]["close"] for i in f.groups["decided"]} == {"decision"}
    assert {items[i]["close"] for i in f.groups["dup"]} == {"duplicate"}
    assert actions("dup_keeper") == {"keep"}
    keeper = f.groups["dup_keeper"][0]
    assert items[f.groups["dup"][0]]["evidence"]["duplicate_of"] == keeper
    # The model sorted what no owner rule decided, never the owner's own.
    assert actions("conv_cache") == {"reassign"}
    assert items[f.groups["conv_cache"][0]]["rule"].startswith("model")
    assert actions("conv_question") == {"keep"}
    assert not any("proposed" in items[i] for i in f.groups["conv_question"])


def test_the_plan_writes_nothing(owed):
    _flood(owed)
    before = _snapshot(owed)
    _plan(owed, judge=FakeModel())
    assert _snapshot(owed) == before


def test_the_model_never_sees_items_naming_the_user(owed):
    f = _flood(owed)
    model = FakeModel()
    _plan(owed, judge=model)
    assert model.seen, "the model pass ran"
    texts = {r["text"] for r in _rows(owed).values()}
    named = [t for t in texts if "matt" in t.lower()]
    assert named and not any("matt" in t.lower() for t in model.seen)
    # Only items no rule decided reach it.
    shown = set(model.seen)
    for group in ("job_question", "pr_merged", "decided", "dup", "conv_question"):
        assert not any(_rows(owed, [i])[i]["text"] in shown for i in f.groups[group]), group


def test_no_model_flag_skips_the_pass(owed):
    f = _flood(owed)
    model = FakeModel()
    plan = _plan(owed, judge=model, use_model=False)
    assert model.calls == 0 and plan["model"]["calls"] == 0
    assert {_by_id(plan)[i]["action"] for i in f.groups["conv_cache"]} == {"keep"}


def test_the_cap_stops_the_model_pass_and_0_lifts_it(owed):
    _flood(owed, model_questions=owed_tidy.JUDGE_BATCH + 5)
    config.set_count_setting("owed_tidy_calls", 1)
    capped = FakeModel()
    plan = _plan(owed, judge=capped)
    assert capped.calls == 1 and plan["model"]["calls"] == 1 and plan["model"]["cap"] == 1
    assert plan["model"]["unjudged"] >= 5
    assert any(it.get("unjudged") for it in plan["items"])
    config.set_count_setting("owed_tidy_calls", 0)
    lifted = FakeModel()
    plan = _plan(owed, judge=lifted)
    assert lifted.calls >= 2 and plan["model"]["unjudged"] == 0 and plan["model"]["cap"] == 0


def test_the_cap_is_a_count_setting():
    assert config.COUNT_SETTINGS["owed_tidy_calls"] == owed_tidy.DEFAULT_CALLS


# ---- apply and undo ---------------------------------------------------------


def test_apply_then_undo_returns_every_row_to_its_prior_state(owed, monkeypatch):
    _flood(owed)
    before = _rows(owed)
    plan = _plan(owed, judge=FakeModel())
    _app(monkeypatch)
    res = owed_tidy.apply(plan)
    assert res["ok"] and res["changed"] == plan["totals"]["move"]["total"] + plan["totals"]["close"]["total"]
    # Only other owners' items change, so the owner gate is not asked.
    assert res["changed"] and res["skipped"] == [] and res["owner_gate"] is None
    after = _rows(owed)
    assert after != before
    changed = [i for i, it in _by_id(plan).items() if it["action"] != "keep"]
    assert all(after[i]["tidy_batch"] == res["batch"] for i in changed)
    assert all(after[i]["status"] == "closed" for i, it in _by_id(plan).items() if it["action"] == "close")
    assert len(after) == len(before), "nothing deleted"
    undone = owed_tidy.undo(res["batch"])
    assert undone["ok"] and undone["restored"] == len(changed) and undone["left_alone"] == []
    assert _rows(owed) == before
    # The rules that closed an item there never close it again by the same evidence.
    with owed.cursor() as cur:
        cur.execute("SELECT count(*) FROM commitments WHERE array_length(reopened_rule_keys, 1) > 0")
        assert cur.fetchone()[0] == plan["totals"]["close"]["total"]


def test_an_item_changed_after_apply_is_left_alone_by_undo_and_listed(owed, monkeypatch):
    f = _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    _app(monkeypatch)
    res = owed_tidy.apply(plan)
    moved = f.groups["conv_cache"][0]
    closed = f.groups["dup"][0]
    with owed.cursor() as cur:
        assert co.reassign(cur, moved, "assistant")
        assert co.set_status(cur, closed, "open")
    touched = _rows(owed, [moved, closed])
    undone = owed_tidy.undo(res["batch"])
    assert {x["id"] for x in undone["left_alone"]} == {moved, closed}
    assert _rows(owed, [moved, closed]) == touched
    assert _rows(owed, [f.groups["job_question"][1]])[f.groups["job_question"][1]]["owner"] == "user"


def test_a_plan_whose_items_changed_skips_them_and_lists_them(owed, monkeypatch):
    f = _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    changed = f.groups["conv_cache"][0]
    gone = f.groups["dup"][0]
    with owed.cursor() as cur:
        cur.execute("UPDATE commitments SET text = text || ' today' WHERE id = %s", (changed,))
        assert co.set_status(cur, gone, "closed", reason="done", by=co.BY_APP)
    _app(monkeypatch)
    res = owed_tidy.apply(plan)
    assert {x["id"] for x in res["skipped"]} == {changed, gone}
    rows = _rows(owed, [changed, gone])
    assert rows[changed]["tidy_batch"] is None and rows[changed]["owner"] == "assistant"
    assert rows[gone]["close_reason"] == "done"


def _edited_to_move_the_owners_item(f, plan) -> int:
    """A plan edited by hand to apply the move proposed for one of the owner's
    items: the plan itself never makes it actionable."""
    mine = f.groups["job_question"][0]
    it = _by_id(plan)[mine]
    it.update(action="reassign", after=it["proposed"]["after"])
    return mine


def test_apply_is_refused_without_the_gate_for_user_items(owed, monkeypatch):
    f = _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    _edited_to_move_the_owners_item(f, plan)
    before = _snapshot(owed)
    res = owed_tidy.apply(plan)
    assert res["ok"] is False and res["changed"] == 0 and res["needs_owner"]
    assert res["error"] == co.BULK_OWNER_REFUSAL
    assert _snapshot(owed) == before


def test_apply_passes_the_gate_with_a_typed_yes_and_still_never_moves_the_owners_item(owed, monkeypatch):
    f = _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    mine = _edited_to_move_the_owners_item(f, plan)
    asked: list[str] = []
    monkeypatch.setattr(co, "bulk_owner_gate", lambda n: asked.append(n) or "terminal")
    res = owed_tidy.apply(plan)
    assert res["ok"] and res["owner_gate"] == "terminal" and asked
    assert mine in {s["id"] for s in res["skipped"]}
    assert _rows(owed, [mine])[mine]["owner"] == "user" and _rows(owed, [mine])[mine]["tidy_batch"] is None


def test_a_batch_of_only_assistant_and_agent_items_needs_no_gate(owed, monkeypatch):
    f = Flood(owed)
    ep = f.episode()
    for i in range(3):
        keeper = f"Add the {_words(i + 1000)} retry to the importer"
        f.item("k", keeper, episode=ep, owner="assistant")
        f.item("d", keeper + " soon", episode=ep, owner="assistant")
    monkeypatch.setattr(co, "bulk_owner_gate", lambda n: pytest.fail("the gate was asked"))
    plan = _plan(owed, judge=FakeModel())
    res = owed_tidy.apply(plan)
    assert res["ok"] and res["changed"] == 3 and res["owner_gate"] is None
    assert owed_tidy.undo(res["batch"])["restored"] == 3


def test_undo_twice_restores_nothing_the_second_time(owed, monkeypatch):
    _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    _app(monkeypatch)
    res = owed_tidy.apply(plan)
    owed_tidy.undo(res["batch"])
    again = owed_tidy.undo(res["batch"])
    assert again["ok"] and again["restored"] == 0


# ---- the command ------------------------------------------------------------


def test_cli_plan_out_apply_and_undo(owed, monkeypatch, tmp_path, capsys):
    _flood(owed)
    monkeypatch.setattr(owed_tidy, "_model_owner_judge", FakeModel())
    out = tmp_path / "plan.json"
    assert cli.main(["owed", "tidy", "--project", PROJECT, "--plan-out", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert summary["plan_out"] == str(out) and summary["totals"] == doc["totals"]
    assert cli.main(["owed", "tidy", "--project", PROJECT, "--no-model"]) == 0
    assert json.loads(capsys.readouterr().out)["model"]["calls"] == 0
    # The plan changes only other owners' items: no gate, from a terminal too.
    assert cli.main(["owed", "tidy", "--project", PROJECT, "--apply", str(out)]) == 0
    batch = json.loads(capsys.readouterr().out)["batch"]
    assert cli.main(["owed", "tidy", "--project", PROJECT, "--undo", batch]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]


# ---- the migration ----------------------------------------------------------


def test_a_pre_migration_hub_plans_and_apply_says_why(hub, monkeypatch):  # noqa: F811
    conn = hub.connect()
    conn.autocommit = True
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, "connect", lambda *a, **k: hub.connect())
    f = Flood(conn)
    f.item("q", f"Should the {_words(1)} cache use a shorter window?", kind="question", episode=f.episode())
    with conn.cursor() as cur:
        plan = owed_tidy.plan(cur, PROJECT, use_model=False)
    assert plan["totals"]["open"] == 1 and plan["notes"]
    res = owed_tidy.apply(plan)
    assert res == {"ok": False, "changed": 0, "error": owed_tidy.NOT_READY}
    db._TABLE_COLUMNS_CACHE.clear()


def test_the_migration_is_safe_to_run_twice(owed):
    from tests.hermetic_pg import migration_sql

    with owed.cursor() as cur, owed.transaction():
        cur.execute(migration_sql("0036_commitment_tidy.sql"))
        cur.execute("SELECT count(*) FROM schema_migrations WHERE version = '0036_commitment_tidy'")
        assert cur.fetchone()[0] == 1


# ---- the review's probes: the model budget and undo's project ----------------

def test_tidy_model_cap_does_not_allow_a_retry_per_budgeted_call(monkeypatch):
    """One budgeted tidy batch must produce at most one provider attempt."""
    from khipu import extract

    attempts = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"candidates":[{"content":{"parts":[{"text":"{\\"owners\\":[]}"}]}}]}'

    def fake_urlopen(*args, **kwargs):
        attempts.append((args, kwargs))
        if len(attempts) == 1:
            raise OSError("synthetic transient failure")
        return Response()

    def real_adapter(prompt, **kwargs):
        return extract._generate_gemini(prompt, model_id="synthetic", **kwargs)

    monkeypatch.setattr(extract, "_generate", real_adapter)
    monkeypatch.setattr(extract, "_key", lambda: "synthetic")
    monkeypatch.setattr(extract, "_synth_budget_take", lambda: None)
    monkeypatch.setattr(extract.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(extract.time, "sleep", lambda _seconds: None)
    owed_tidy._model_owner_judge(["Route the cache repair to the orchestrator"])

    assert len(attempts) <= 1


def test_a_retry_of_a_failed_batch_takes_another_unit(owed):
    _flood(owed)
    config.set_count_setting("owed_tidy_calls", 1)

    class Failing:
        calls = 0

        def __call__(self, texts):
            self.calls += 1
            return [{"owner": "user", "reason": "model unavailable: OSError", "failed": True} for _ in texts]

    once = Failing()
    plan = _plan(owed, judge=once)
    assert once.calls == 1 and plan["model"]["calls"] == 1
    config.set_count_setting("owed_tidy_calls", 2)
    twice = Failing()
    plan = _plan(owed, judge=twice)
    assert twice.calls == 2 and plan["model"]["calls"] == 2
    config.set_count_setting("owed_tidy_calls", 0)


def test_tidy_undo_cannot_change_a_batch_for_another_project(monkeypatch):
    """The required --project must constrain undo as it already constrains apply."""
    import argparse

    from khipu import cli

    state = {"undo_calls": 0, "mutated": False}

    def fake_undo(batch):
        state["undo_calls"] += 1
        state["mutated"] = True
        return {"ok": True, "batch": batch, "project": "other/project", "restored": 1,
                "left_alone": [], "owner_gate": "app"}

    monkeypatch.setattr(owed_tidy, "undo", fake_undo)
    monkeypatch.setattr(owed_tidy, "batch_project", lambda batch: "other/project")
    args = argparse.Namespace(project="expected/project", apply=None, undo="tidy-other",
                              plan_out=None, no_model=False)

    assert cli.cmd_owed_tidy(args) == 2
    assert state == {"undo_calls": 0, "mutated": False}


def test_undo_with_another_project_changes_nothing_on_the_hub(owed, monkeypatch):
    _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    _app(monkeypatch)
    res = owed_tidy.apply(plan)
    before = _rows(owed)
    refused = owed_tidy.undo(res["batch"], project="other/project")
    assert refused["ok"] is False and refused["restored"] == 0
    assert _rows(owed) == before
    assert owed_tidy.batch_project(res["batch"]) == PROJECT
    assert owed_tidy.undo(res["batch"], project=PROJECT)["ok"] is True


# ---- the fourth review: the owner's own items only get proposals ------------

def _probe_row(cid, owner, label, origin, text="Rebase the release branch"):
    out = {key: None for key in (*owed_tidy.FINGERPRINT_FIELDS, *owed_tidy.LINEAGE_FIELDS)}
    out.update(id=cid, project="p", opened_episode=None, opened_at=None, text=text, kind="followup",
               owner=owner, owner_label=label, origin=origin, status="open", future_trigger=False,
               owner_reassigned_at=None, lineage=None)
    return out


class _NowCursor:
    def execute(self, sql, params=()):
        assert sql == "SELECT now()"

    def fetchone(self):
        return (datetime(2026, 10, 16, tzinfo=timezone.utc),)


def test_hygiene_dedup_does_not_merge_identical_commitments_of_different_owners():
    from khipu import hygiene

    rows = [
        {"id": 1, "project": "p", "text": "Rebase the release branch", "owner": "agent",
         "owner_label": "orchestrator", "origin": "job"},
        {"id": 2, "project": "p", "text": "Rebase the release branch", "owner": "user",
         "owner_label": None, "origin": "conversation"},
    ]
    assert hygiene._duplicate_groups(rows) == {}


def test_tidy_plan_does_not_close_a_cross_owner_duplicate(monkeypatch):
    from khipu import close_rules

    rows = {1: _probe_row(1, "agent", "orchestrator", "job"), 2: _probe_row(2, "user", None, "conversation")}
    monkeypatch.setattr(owed_tidy, "_select", lambda cur, project: rows)
    monkeypatch.setattr(close_rules, "ready", lambda cur: False)
    plan = owed_tidy._plan(_NowCursor(), "p", judge=None, use_model=False)
    assert {item["id"]: item["action"] for item in plan["items"]} == {1: "keep", 2: "keep"}


def test_tidy_model_does_not_override_the_deterministic_speakers_i_will(monkeypatch):
    from khipu import close_rules

    row = _probe_row(1, "user", None, "conversation", text="I will rebase the release branch")
    monkeypatch.setattr(owed_tidy, "_select", lambda cur, project: {1: row})
    monkeypatch.setattr(close_rules, "ready", lambda cur: False)
    monkeypatch.setattr(config, "count_setting", lambda name: 1)
    plan = owed_tidy._plan(_NowCursor(), "p", use_model=True,
                           judge=lambda texts: [{"owner": "agent", "label": "orchestrator", "reason": "bad"}])
    assert plan["items"][0]["action"] == "keep"
    assert "proposed" not in plan["items"][0], "a model proposal that moves the owner's item is dropped"


def test_a_duplicate_of_the_owners_item_is_only_proposed(monkeypatch):
    from khipu import close_rules

    rows = {1: _probe_row(1, "user", None, "conversation"), 2: _probe_row(2, "user", None, "conversation")}
    monkeypatch.setattr(owed_tidy, "_select", lambda cur, project: rows)
    monkeypatch.setattr(close_rules, "ready", lambda cur: False)
    plan = owed_tidy._plan(_NowCursor(), "p", judge=None, use_model=False)
    second = _by_id(plan)[2]
    assert second["action"] == "keep"
    assert second["proposed"]["action"] == "close" and second["proposed"]["close"] == "duplicate"
    assert plan["totals"]["proposed"] == {"total": 1, "move": 0, "close": 1}
    assert plan["totals"]["stay_user"] == 2 and plan["samples"]["proposed"][0]["id"] == 2


def test_apply_never_changes_the_owners_item_even_from_an_edited_plan(owed, monkeypatch):
    f = _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    items = _by_id(plan)
    decided, named = f.groups["decided"][0], f.groups["conv_names_user"][0]
    assert items[decided]["action"] == items[named]["action"] == "keep"
    # A plan edited by hand to close or move them is refused at the write.
    items[decided].update(action="close", close="decision", key="decision:1", reason="settled")
    items[named].update(action="reassign", after={"owner": "agent", "owner_label": "orchestrator",
                                                  "origin": "conversation"}, rule="owner rules")
    before = _rows(owed, [decided, named])
    _app(monkeypatch)
    out = owed_tidy.apply(plan)
    assert out["ok"] is True
    refused = {s["id"] for s in out["skipped"] if "owner's own item" in s["why"]}
    assert refused == {decided, named}
    assert _rows(owed, [decided, named]) == before
    # Not even a close on the owner's item's own pull requests is made.
    assert all(_rows(owed, [i])[i]["status"] == "open" for i in f.groups["pr_merged"])


def test_tidy_never_moves_an_item_stored_as_the_users(monkeypatch):
    """Third confirmation review: a legacy question stored as the user's, whose
    capture had a parent session, was planned as the orchestrator's and Apply
    moved it. The guard reads the stored owner, never the owner the plan
    proposes; the move stays a proposal."""
    import json as _json

    from khipu import close_rules, db, hygiene
    from khipu import commitments as co
    from khipu import owed_tidy as tidy
    from tests.test_owed_owner_guard import _RacingCursor

    class LocalCursor(_RacingCursor):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    db._TABLE_COLUMNS_CACHE.clear()
    cur = LocalCursor()
    try:
        for spec in ("origin TEXT", "owner_label TEXT", "tidy_batch TEXT"):
            cur.inner.conn.execute("ALTER TABLE commitments ADD COLUMN " + spec)
        cur.inner.conn.executescript("""
            CREATE TABLE commitment_tidy_batches(id TEXT, project TEXT, plan_version INT, planned_at TEXT,
                                                 owner_gate TEXT, counts TEXT, skipped TEXT);
            CREATE TABLE commitment_tidy_items(batch_id TEXT, commitment_id INT, action TEXT, reason TEXT,
                                               prior TEXT, after_fingerprint TEXT);
        """)
        cur.episode(1, parent="orchestrator")
        cid = cur.commitment("Should the migration be renumbered after the rebase?", project="acme/widget",
                             owner=co.OWNER_USER, kind="question")
        cur.inner.conn.execute("UPDATE commitments SET origin = 'conversation' WHERE id = ?", (cid,))

        def select(cur_, project, **kwargs):
            c = cur.inner.conn.execute("SELECT * FROM commitments WHERE id = ?", (cid,))
            row = dict(zip([d[0] for d in c.description], c.fetchone()))
            for field in ("prs", "pr_check", "close_evidence"):
                if isinstance(row.get(field), str):
                    row[field] = _json.loads(row[field])
            row["lineage"] = "orchestrator"
            return {cid: row}

        monkeypatch.setattr(tidy, "_select", select)
        monkeypatch.setattr(close_rules, "ready", lambda cur: False)
        monkeypatch.setattr(hygiene, "_duplicate_groups", lambda *a, **k: {})
        doc = tidy._plan(cur, "acme/widget", judge=None, use_model=False)
        item = doc["items"][0]
        assert item["before"]["owner"] == co.OWNER_USER
        assert item["action"] == "keep" and item["proposed"]["action"] == "reassign", item

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def cursor(self):
                return cur

        monkeypatch.setattr(db, "connect", lambda **k: Conn())
        monkeypatch.setattr(tidy, "_ready", lambda cur: True)
        monkeypatch.setattr(co, "bulk_owner_gate", lambda count: "app")
        # Even a plan edited to apply the move is refused at the write.
        item.update(action="reassign", after=item["proposed"]["after"])
        result = tidy.apply(doc)
        assert cur.value("commitments", cid, "owner") == co.OWNER_USER, result
        assert result["changed"] == 0
    finally:
        cur.inner.conn.close()
        db._TABLE_COLUMNS_CACHE.clear()


# ---- apply and undo on the owner's direction -------------------------------


def _direction(tmp_path, monkeypatch, session="s-tidy-apply"):
    from khipu import owner_turns

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    owner_turns.record({"session_id": session, "prompt": "apply that clean-up", "cwd": str(tmp_path)},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"})


def _plan_file(tmp_path, plan) -> str:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan, default=str), encoding="utf-8")
    return str(path)


def _owner_undo(line_id: str) -> tuple[int, dict]:
    from contextlib import redirect_stdout

    out = io.StringIO()
    with redirect_stdout(out):
        rc = cli.main(["owner-direction", "undo", line_id, "--owner-direction"])
    return rc, json.loads(out.getvalue())


def _no_gate_but_direction(monkeypatch):
    monkeypatch.setattr(routing, "typed_yes", lambda prompt: pytest.fail("the terminal was asked"))
    monkeypatch.setattr(co, "bulk_owner_gate", lambda n: pytest.fail("the app or terminal gate was asked"))


def test_apply_by_direction_passes_the_gate_is_logged_and_its_undo_restores(owed, monkeypatch, tmp_path):
    from khipu import owner_act, owner_direction

    f = _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    mine = _edited_to_move_the_owners_item(f, plan)
    before = _rows(owed)
    _no_gate_but_direction(monkeypatch)
    _direction(tmp_path, monkeypatch)
    ok_, out = owner_act.act("owed.tidy", {"project": PROJECT, "plan": _plan_file(tmp_path, plan)})
    assert ok_ and out["owner_gate"] == "direction" and out["changed"] > 0 and out["owner_action"], out
    assert _rows(owed, [mine])[mine]["owner"] == "user"
    line = next(x for x in owner_direction.actions() if x["id"] == out["owner_action"])
    assert line["action"] == "owed.tidy" and line["auth"] == "direction" and "no_undo" not in line
    rc, undone = _owner_undo(out["owner_action"])
    assert rc == 0 and undone["restored"] == out["changed"], undone
    assert _rows(owed) == before


def test_apply_by_direction_is_refused_without_a_recent_owner_turn(owed, monkeypatch, tmp_path):
    from khipu import owner_act

    _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    before = _snapshot(owed)
    _no_gate_but_direction(monkeypatch)
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-nobody-typed")
    ok_, out = owner_act.act("owed.tidy", {"project": PROJECT, "plan": _plan_file(tmp_path, plan)})
    assert not ok_ and out["code"] == "no_recorded_turn" and "Nothing was changed" in out["error"], out
    assert _snapshot(owed) == before


def test_undo_by_direction_restores_the_batch_and_its_undo_puts_the_changes_back(owed, monkeypatch, tmp_path):
    from khipu import owner_act, owner_direction

    f = _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    _edited_to_move_the_owners_item(f, plan)
    before = _rows(owed)
    _app(monkeypatch)
    applied = owed_tidy.apply(plan)
    assert applied["ok"] and applied["changed"] > 0
    after_apply = _rows(owed)
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["claude"]})
    _no_gate_but_direction(monkeypatch)
    _direction(tmp_path, monkeypatch)
    ok_, out = owner_act.act("owed.tidy", {"project": PROJECT, "batch": applied["batch"]})
    assert ok_ and out["restored"] == applied["changed"] and out["owner_action"], out
    assert _rows(owed) == before
    line = next(x for x in owner_direction.actions() if x["id"] == out["owner_action"])
    assert line["action"] == "owed.tidy" and "no_undo" not in line
    rc, redone = _owner_undo(out["owner_action"])
    assert rc == 0 and redone["reapplied"] == applied["changed"], redone
    assert _rows(owed) == after_apply
    # The batch is applied again, so its own undo works once more.
    assert owed_tidy.undo(applied["batch"])["restored"] == applied["changed"]
    assert _rows(owed) == before


def test_redo_leaves_alone_an_item_changed_since_the_undo(owed, monkeypatch):
    _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    _app(monkeypatch)
    applied = owed_tidy.apply(plan)
    owed_tidy.undo(applied["batch"])
    changed = _batch_ids(owed, applied["batch"])[0]
    with owed.cursor() as cur:
        cur.execute("UPDATE commitments SET close_reason = 'touched by hand' WHERE id = %s", (changed,))
    out = owed_tidy.redo(applied["batch"], project=PROJECT)
    assert out["ok"] and out["reapplied"] == applied["changed"] - 1
    assert [x["id"] for x in out["left_alone"]] == [changed]


def _batch_ids(conn, batch) -> list[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT commitment_id FROM commitment_tidy_items WHERE batch_id = %s ORDER BY 1", (batch,))
        return [int(r[0]) for r in cur.fetchall()]


def test_tidy_by_direction_takes_a_plan_or_a_batch_and_nothing_else(owed, tmp_path):
    from khipu import owner_act

    for args in ({"project": PROJECT}, {"project": PROJECT, "plan": "p.json", "batch": "tidy-1"},
                 {"plan": "p.json"}, {"project": PROJECT, "plan": "p.json", "ids": [1]}):
        ok_, out = owner_act.act("owed.tidy", args)
        assert not ok_ and out["code"] == "bad_args", (args, out)
    head, pos = owner_act.argv("owed.tidy", owner_act._values("owed.tidy", {"project": PROJECT, "plan": "-x.json"}))
    ns = cli.build_parser().parse_args(owner_act.full_argv(head, pos, ["--owner-direction"]))
    assert (ns.apply, ns.project, ns.by_direction) == ("-x.json", PROJECT, True)
    head, pos = owner_act.argv("owed.tidy", owner_act._values("owed.tidy", {"project": PROJECT, "batch": "tidy-1"}))
    ns = cli.build_parser().parse_args(owner_act.full_argv(head, pos, ["--owner-direction"]))
    assert (ns.undo, ns.by_direction) == ("tidy-1", True)
