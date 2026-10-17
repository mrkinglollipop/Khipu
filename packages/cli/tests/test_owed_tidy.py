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
    for i in range(model_questions):
        f.item("conv_question", f"Should the {_words(i + 600)} cache use a shorter window?",
               kind="question", episode=conv)
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

    # A job's asks move to the orchestrator: the lineage of the capture that
    # opened them says they came from a job, though the row says conversation.
    for group in ("job_question", "job_followup", "job_names_user"):
        assert actions(group) == {"reassign"}, group
        for i in f.groups[group]:
            assert items[i]["before"]["owner"] == "user", group
            assert items[i]["after"]["owner"] == "agent" and items[i]["rule"], group
            assert items[i]["after"]["origin"] == "job", group
    assert items[f.groups["job_question"][0]]["after"]["owner_label"] == "orchestrator"
    # The owner's own conversation naming the orchestrator: no keyword moves
    # it (owners come from lineage); only the model pass may, and here it
    # says the user's, so it is kept.
    assert actions("agent_named") == {"keep"}
    assert items[f.groups["agent_named"][0]]["before"]["owner"] == "user"
    assert actions("conv_names_user") == {"keep"}
    assert actions("pr_merged") == {"close"}
    assert {items[i]["close"] for i in f.groups["pr_merged"]} == {"pr"}
    assert "#801" in items[f.groups["pr_merged"][1]]["reason"]
    assert items[f.groups["pr_merged"][0]]["evidence"]["rule"] == "pr"
    assert actions("pr_open") == actions("pr_half") == {"keep"}
    assert {items[i]["close"] for i in f.groups["decided"]} == {"decision"}
    assert {items[i]["close"] for i in f.groups["dup"]} == {"duplicate"}
    assert actions("dup_keeper") == {"keep"}
    keeper = f.groups["dup_keeper"][0]
    assert items[f.groups["dup"][0]]["evidence"]["duplicate_of"] == keeper
    # The model sorted what the rules left as the user's, unnamed.
    assert actions("conv_question") == {"reassign"}
    assert items[f.groups["conv_question"][0]]["rule"].startswith("model")


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
    for group in ("job_question", "pr_merged", "decided", "dup"):
        assert not any(_rows(owed, [i])[i]["text"] in shown for i in f.groups[group]), group


def test_no_model_flag_skips_the_pass(owed):
    f = _flood(owed)
    model = FakeModel()
    plan = _plan(owed, judge=model, use_model=False)
    assert model.calls == 0 and plan["model"]["calls"] == 0
    assert {_by_id(plan)[i]["action"] for i in f.groups["conv_question"]} == {"keep"}


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
    assert res["skipped"] == [] and res["owner_gate"] == "app"
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
    moved = f.groups["job_question"][0]
    closed = f.groups["pr_merged"][0]
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
    changed = f.groups["job_followup"][0]
    gone = f.groups["pr_merged"][0]
    with owed.cursor() as cur:
        cur.execute("UPDATE commitments SET text = text || ' today' WHERE id = %s", (changed,))
        assert co.set_status(cur, gone, "closed", reason="done", by=co.BY_APP)
    _app(monkeypatch)
    res = owed_tidy.apply(plan)
    assert {x["id"] for x in res["skipped"]} == {changed, gone}
    rows = _rows(owed, [changed, gone])
    assert rows[changed]["tidy_batch"] is None and rows[changed]["owner"] == "user"
    assert rows[gone]["close_reason"] == "done"


def test_apply_is_refused_without_the_gate_for_user_items(owed, monkeypatch):
    _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    before = _snapshot(owed)
    res = owed_tidy.apply(plan)
    assert res["ok"] is False and res["changed"] == 0 and res["needs_owner"]
    assert res["error"] == co.BULK_OWNER_REFUSAL
    assert _snapshot(owed) == before


def test_apply_passes_the_gate_with_a_typed_yes_and_undo_asks_again(owed, monkeypatch):
    _flood(owed)
    plan = _plan(owed, judge=FakeModel())
    asked: list[str] = []
    monkeypatch.setattr(co, "bulk_owner_gate", lambda n: asked.append(n) or "terminal")
    res = owed_tidy.apply(plan)
    assert res["ok"] and res["owner_gate"] == "terminal" and asked
    monkeypatch.setattr(co, "bulk_owner_gate", lambda n: None)
    refused = owed_tidy.undo(res["batch"])
    assert refused["ok"] is False and refused["restored"] == 0


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
    assert cli.main(["owed", "tidy", "--project", PROJECT, "--apply", str(out)]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False  # not the app, no typed yes
    _app(monkeypatch)
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
