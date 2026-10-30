"""Automatic push ranking (2026-10-29): every item pushed across five prompts
in a Khipu session was irrelevant while the right episodes existed. Each test
rebuilds one observed case on a synthetic replica, offline."""
import json
import os
import socket
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from khipu import hub_snapshot as hs
from khipu import recall_eval
from khipu import recall_prompt as rp
from khipu import recency
from khipu.search_text import search_tokens

KHIPU = "mrkinglollipop/Khipu"
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    assert os.environ.get("KHIPU_KEYCHAIN") == "0"
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_FEATURE_RELEVANCE_FLOOR", "1")
    monkeypatch.setenv("KHIPU_FEATURE_GRAPH_CANDIDATES", "0")
    monkeypatch.setenv("KHIPU_FEATURE_TIME_INTERPRETATION", "0")

    def forbidden(*args, **kwargs):
        raise AssertionError("network and hub access forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr("khipu.db.connect", forbidden)
    monkeypatch.setattr(rp, "_deliverable_context_line", lambda *a, **kw: "")
    monkeypatch.setattr(rp, "_project_for_cwd", lambda cwd: KHIPU if cwd else None)


def _days_ago(n):
    return (NOW - timedelta(days=n)).isoformat()


def _replica(tmp_path, monkeypatch, *, episodes=(), topics=(), cosine=()):
    """``cosine`` is the cosine leg's answer, best first: (kind, id, score)."""
    path = tmp_path / "replica.sqlite"
    con = sqlite3.connect(path)
    hs._create_schema(con)
    text = {}
    for eid, summary, project, ts, *decisions in episodes:
        con.execute("INSERT INTO episodes(id,summary,project,ts,decisions) VALUES(?,?,?,?,?)",
                    (eid, summary, project, ts, json.dumps(decisions)))
        text[("episode", str(eid))] = " ".join([summary, *decisions])
    for slug, title, body, status, project, ts in topics:
        # Through the replica writer's own metadata pass, which records the size.
        prepared = hs._topic_routing_metadata(
            {"slug": slug, "body": body, "frontmatter": {"project": project} if project else None})
        con.execute("INSERT INTO topics(slug,title,body,status,updated_at,frontmatter) "
                    "VALUES(?,?,?,?,?,?)",
                    (slug, title, body, status, ts, json.dumps(prepared["frontmatter"])))
        text[("topic", slug)] = f"{title} {body[:300]}"
    con.commit()
    con.close()
    monkeypatch.setattr(hs, "snapshot_path", lambda: path)
    monkeypatch.setattr(hs, "snapshot_is_fresh", lambda: (True, {"exists": True}))
    if cosine:
        monkeypatch.setattr(hs, "active_snapshot_profile", lambda: "synthetic@2")
        monkeypatch.setattr(hs, "warm_matrix", lambda *a: None)
        monkeypatch.setattr(rp, "_cached_query_embed", lambda *a, **kw: [1.0, 0.0])

        def fake_cosine(vec, profile, *, limit, kind=None, deadline=None):
            rows = []
            for knd, rid, score in list(cosine)[:limit]:
                chunk = text[(knd, str(rid))]
                rows.append({"kind": knd, "id": str(rid), "chunk_idx": 0, "score": score,
                             "label": chunk[:80], "snippet": chunk[:200], "rank_text": chunk})
            return rows

        monkeypatch.setattr(hs, "cosine_candidates_snapshot", fake_cosine)
    else:
        monkeypatch.setattr(hs, "active_snapshot_profile", lambda: None)


def _catch_all(words):
    return (" ".join(words) + " ") * 2000


def test_catch_all_pages_are_not_pushed_and_the_episode_is(tmp_path, monkeypatch):
    words = ["recorded", "mobile", "followup", "task", "thread", "memory"]
    _replica(
        tmp_path, monkeypatch,
        episodes=[(10, "Recorded the mobile followup task for the phone app", KHIPU, _days_ago(1))],
        topics=[
            ("note:raw-memories", "Raw Memories", "# Raw Memories — Merged stage-1 raw memories " + _catch_all(words), "active", None, _days_ago(0)),
            ("note:memory-summary", "Memory Summary", _catch_all(words), "active", None, _days_ago(0)),
            ("note:aegis-status-board-2026-09-05", "Aegis status board", _catch_all(words), "active", "mrkinglollipop/aegis", _days_ago(54)),
        ],
        cosine=[("topic", "note:raw-memories", 0.83), ("topic", "note:memory-summary", 0.81),
                ("topic", "note:aegis-status-board-2026-09-05", 0.80), ("episode", 10, 0.74)],
    )
    out = rp.prior_work_for_prompt("Recorded mobile followup task", cwd="/khipu/worktree")
    assert [r["id"] for r in out["hits"]] == ["10"], out


def test_replica_records_body_size_without_search_reading_the_body(tmp_path):
    con = sqlite3.connect(tmp_path / "r.sqlite")
    hs._create_schema(con)
    body = "x" * 40_000
    prepared = hs._topic_routing_metadata({"slug": "note:big", "body": body, "frontmatter": None})
    con.execute("INSERT INTO topics(slug,body,status,frontmatter) VALUES('note:big',?,'active',?)",
                (body, json.dumps(prepared["frontmatter"])))

    def authorize(action, table, column, *args):
        deny = action == sqlite3.SQLITE_READ and table == "topics" and column == "body"
        return sqlite3.SQLITE_DENY if deny else sqlite3.SQLITE_OK

    con.set_authorizer(authorize)
    [row] = hs.snapshot_row_metadata(con, [{"kind": "topic", "id": "note:big"}])
    assert row["page_chars"] == 40_000 and recency.is_broad_page(row)


def test_catch_all_page_does_not_answer_an_unrelated_prompt(tmp_path, monkeypatch):
    words = ["massive", "issue", "solve", "fix", "proceed"]
    _replica(
        tmp_path, monkeypatch,
        topics=[("note:raw-memories", "Raw Memories", _catch_all(words), "active", None, _days_ago(0))],
        cosine=[("topic", "note:raw-memories", 0.70)],
    )
    out = rp.prior_work_for_prompt(
        "Yeah, that is a massive issue. We need to solve this and fix it. Proceed.",
        cwd="/khipu/worktree")
    assert out["hits"] == [], out


def test_catch_all_page_stays_searchable_but_is_deranked():
    small = {"kind": "topic", "id": "note:small", "score": 0.03, "page_chars": 2000}
    broad = {"kind": "topic", "id": "note:raw-memories", "score": 0.04, "page_chars": 1_000_000}
    ranked = recency.apply_project_and_status([broad, small])
    assert [r["id"] for r in ranked] == ["note:small", "note:raw-memories"]


def test_placeholder_summary_episode_loses_to_a_real_one(tmp_path, monkeypatch):
    _replica(
        tmp_path, monkeypatch,
        episodes=[
            (50, recency.PLACEHOLDER_SUMMARY, KHIPU, _days_ago(0),
             "dispatch the recall ranking job", "recall ranking job brief"),
            (51, "Sent the ranking job to the second account", KHIPU, _days_ago(0)),
        ],
        cosine=[("episode", 50, 0.85), ("episode", 51, 0.75)],
    )
    out = rp.prior_work_for_prompt("dispatch the recall ranking job", cwd="/khipu/worktree")
    assert [r["id"] for r in out["hits"]][:1] == ["51"], out


def test_same_project_episode_beats_old_pages_from_other_projects(tmp_path, monkeypatch):
    others = [(f"note:other-{i}", f"Orchestration note {i}", "lane routing orchestration jobs", "active",
               "mrkinglollipop/frost-line" if i % 2 else "mrkinglollipop/aegis", _days_ago(70 + i * 10))
              for i in range(9)]
    _replica(
        tmp_path, monkeypatch,
        episodes=[(30, "Khipu lane routing for orchestration jobs this week", KHIPU, _days_ago(1))],
        topics=others,
        cosine=[("topic", slug, round(0.84 - i * 0.005, 4)) for i, (slug, *_r) in enumerate(others)]
               + [("episode", 30, 0.78)],
    )
    out = rp.prior_work_for_prompt("lane routing orchestration jobs", cwd="/khipu/worktree")
    assert [r["id"] for r in out["hits"]][:1] == ["30"], out


def test_cross_project_row_is_not_pushed_by_default():
    # Matt 2026-10-30: same project by default.
    rows = [{"kind": "topic", "id": "note:frost-line-system", "score": 0.04, "project": "mrkinglollipop/frost-line"},
            {"kind": "episode", "id": "1", "score": 0.03, "project": KHIPU}]
    ranked = recency.apply_push_policy(rows, project=KHIPU)
    assert [r["id"] for r in ranked] == ["1"]


def test_superseded_topic_is_not_pushed_but_stays_searchable(tmp_path, monkeypatch):
    _replica(
        tmp_path, monkeypatch,
        episodes=[(40, "Recall ranking plan replaced by the push policy", KHIPU, _days_ago(1))],
        topics=[("note:old-recall-plan", "Old recall ranking plan", "recall ranking plan details", "superseded", KHIPU, _days_ago(2))],
        cosine=[("topic", "note:old-recall-plan", 0.90), ("episode", 40, 0.72)],
    )
    out = rp.prior_work_for_prompt("recall ranking plan", cwd="/khipu/worktree")
    assert "note:old-recall-plan" not in [r["id"] for r in out["hits"]], out
    assert [r["id"] for r in out["hits"]] == ["40"], out
    searched = recency.apply_project_and_status(
        [{"kind": "topic", "id": "note:old-recall-plan", "score": 0.05, "status": "superseded"}])
    assert [r["id"] for r in searched] == ["note:old-recall-plan"]


def test_recall_eval_prompt_path_on_a_fake_golden_set(tmp_path, monkeypatch):
    words = ["recorded", "mobile", "followup", "task"]
    _replica(
        tmp_path, monkeypatch,
        episodes=[(10, "Recorded the mobile followup task for the phone app", KHIPU, _days_ago(1))],
        topics=[("note:raw-memories", "Raw Memories", _catch_all(words), "active", None, _days_ago(0)),
                ("note:aegis-2026-09-01-state", "Aegis state", _catch_all(words), "active", "mrkinglollipop/aegis", _days_ago(58))],
        cosine=[("topic", "note:raw-memories", 0.83), ("topic", "note:aegis-2026-09-01-state", 0.80),
                ("episode", 10, 0.74)],
    )
    golden = tmp_path / "golden.jsonl"
    golden.write_text(json.dumps({"query": "Recorded mobile followup task", "expect": ["10"],
                                  "stale": ["note:raw-memories"], "k": 3, "cwd": "/khipu/worktree"}) + "\n")
    report = recall_eval.run_eval_paths(recall_eval.load_golden(golden), ("prompt",))
    row = report["rows"][0]
    assert row["hit"] and row["reciprocal_rank"] == 1.0 and not row["stale_violation"], row


# ---- fix round 1 (Kimi review of b68952cb) ----------------------------------


def test_recency_reorders_but_never_drops_a_hit_that_cleared_the_floor():
    # A fresh hit's recency bonus must not lift the relative floor over an
    # older hit that cleared it on relevance alone.
    fresh = {"kind": "episode", "id": "fresh-relevant", "score": 0.0564, "recency": 0.0164}
    old = {"kind": "episode", "id": "old-relevant", "score": 0.025, "recency": 0.00001}
    kept = rp._apply_score_floor([fresh, old])
    assert [r["id"] for r in kept] == ["fresh-relevant", "old-relevant"]


def test_episode_under_an_old_project_name_is_same_project(tmp_path, monkeypatch):
    _replica(
        tmp_path, monkeypatch,
        episodes=[(60, "Recall ranking alias fix for the hook", "mrkinglollipop/alzy", _days_ago(3)),
                  (61, "Recall ranking alias fix for the hook", "mrkinglollipop/aegis", _days_ago(3))],
        cosine=[("episode", 61, 0.80), ("episode", 60, 0.79)],
    )
    con = sqlite3.connect(hs.snapshot_path())
    con.execute("INSERT INTO project_aliases(alias, project) VALUES(?, ?)",
                ("mrkinglollipop/alzy", KHIPU))
    con.commit()
    con.close()
    out = rp.prior_work_for_prompt("recall ranking alias fix", cwd="/khipu/worktree")
    assert [r["id"] for r in out["hits"]][:1] == ["60"], out


def test_alias_group_is_neither_deranked_nor_unboosted():
    rows = [{"kind": "episode", "id": "same-project-old-alias", "score": 0.04, "project": "Khipu-old"},
            {"kind": "episode", "id": "other", "score": 0.04, "project": "mrkinglollipop/aegis"}]
    ranked = recency.rank_for_push(rows, project=KHIPU, project_group=[KHIPU, "Khipu-old"])
    assert ranked[0]["id"] == "same-project-old-alias"
    assert ranked[0]["score"] == pytest.approx(0.04 * recency.PROJECT_BOOST)


def _order_fixture():
    return [
        {"kind": "episode", "id": "fresh-other", "score": 0.05, "project": "mrkinglollipop/aegis",
         "ts": _days_ago(0), "label": "recall ranking order"},
        {"kind": "episode", "id": "old-current", "score": 0.042, "project": KHIPU,
         "ts": _days_ago(400), "label": "recall ranking order"},
        {"kind": "episode", "id": "unrelated", "score": 0.045, "project": KHIPU,
         "ts": _days_ago(1), "label": "nothing in common"},
    ]


def test_replica_and_hub_fallback_rank_the_same_fixture_identically(monkeypatch):
    from khipu import embed

    from khipu.search_text import search_tokens

    query = "recall ranking order"
    local = rp._trim_result({"hits": recency.rank_for_push(_order_fixture(), project=KHIPU,
                                                           tokens=search_tokens(query), prompt=query),
                             "legs": [], "degraded": None}, rp.TOP_N)

    def fake_hybrid_search(query, *, project_boost=None, rank=None, **kw):
        # The real search's ranking step, on rows its candidate legs would return.
        return {"results": embed._rank_search_rows(_order_fixture(), None,
                                                   project_boost=project_boost, rank=rank)}

    monkeypatch.setattr(embed, "hybrid_search", fake_hybrid_search)
    monkeypatch.setattr(rp, "_snapshot_search_hits",
                        lambda *a, **kw: (_ for _ in ()).throw(rp._SnapshotUnusable("test")))
    monkeypatch.setattr(rp, "_hub_known_down", lambda: None)
    monkeypatch.setattr("khipu.hub_reach.note_reachable", lambda: None)
    hub = rp._search_hits(query, cwd="/khipu/worktree", limit=rp.TOP_N)
    assert [r["id"] for r in local["hits"]] == ["old-current"]
    assert [r["id"] for r in hub["hits"]] == [r["id"] for r in local["hits"]]
    assert [r["score"] for r in hub["hits"]] == [r["score"] for r in local["hits"]]


# ---- fix round 2 (graded replay of 418 real prompts, 2026-10-30) -------------
# Only 4% of pushed items were relevant; 251 of 267 prompts that needed no
# memory still got a push. Fake data only: the graded prompts are private.

FROST = "mrkinglollipop/frost-line"


def test_long_self_contained_prompt_pushes_nothing(tmp_path, monkeypatch):
    _replica(tmp_path, monkeypatch,
             episodes=[(70, "Recall ranking fix round for the push hook", KHIPU, _days_ago(1))])
    brief = ("You are a dispatched agent. Recall ranking fix round for the push hook. "
             + "Read the scope doc first and follow every step in it exactly. " * 15)
    assert len(brief) > rp.LONG_PROMPT_CHARS
    out = rp.prior_work_for_prompt(brief, cwd="/khipu/worktree")
    assert out["hits"] == [] and out["context"] == "", out
    assert out["reason"] == "self-contained prompt"
    short = rp.prior_work_for_prompt("recall ranking fix round push hook", cwd="/khipu/worktree")
    assert [r["id"] for r in short["hits"]] == ["70"], short


def test_harness_message_prompt_pushes_nothing(tmp_path, monkeypatch):
    _replica(tmp_path, monkeypatch,
             episodes=[(71, "Task notification for the recall ranking job finished", KHIPU, _days_ago(1))])
    out = rp.prior_work_for_prompt(
        "<task-notification>\n<task-id>b1</task-id> recall ranking job finished</task-notification>",
        cwd="/khipu/worktree")
    assert out["hits"] == [] and out["reason"] == "harness message", out


def test_shape_gate_is_an_abstention_not_a_gate_for_status_clients(tmp_path, monkeypatch):
    _replica(tmp_path, monkeypatch, episodes=[(72, "Unrelated episode", KHIPU, _days_ago(1))])
    out = rp.prior_work_for_prompt("word " * 300, cwd="/khipu/worktree", budget_ms=300)
    assert out["prior_work_meta"]["outcome"] == "no_match", out


def test_row_with_thin_keyword_coverage_is_not_pushed(tmp_path, monkeypatch):
    # Six content tokens: a push needs five of them (or a clear cosine match).
    _replica(tmp_path, monkeypatch,
             episodes=[(73, "Installed the update for the phone simulator", KHIPU, _days_ago(1))],
             cosine=[("episode", 73, 0.60)])
    out = rp.prior_work_for_prompt("installing update phone sorry quick moment", cwd="/khipu/worktree")
    assert out["hits"] == [], out


def test_row_covering_most_prompt_words_is_pushed(tmp_path, monkeypatch):
    _replica(tmp_path, monkeypatch,
             episodes=[(74, "Olive video jump glitch on scroll fixed after refresh", KHIPU, _days_ago(1))],
             cosine=[("episode", 74, 0.60)])
    out = rp.prior_work_for_prompt("video jump glitch scroll refresh", cwd="/khipu/worktree")
    assert [r["id"] for r in out["hits"]] == ["74"], out


def test_push_cosine_bar_sits_above_the_search_floor():
    from khipu import relevance

    bar = relevance.cosine_floor() + recency.PUSH_COSINE_MARGIN
    tokens = ["alpha", "beta", "gamma", "delta"]
    rows = [{"kind": "episode", "id": "clear", "score": 0.04, "project": KHIPU,
             "cosine": bar + 0.01, "lexical_hits": 1},
            {"kind": "episode", "id": "near", "score": 0.05, "project": KHIPU,
             "cosine": bar - 0.02, "lexical_hits": 1}]
    kept = recency.apply_push_policy(rows, project=KHIPU, tokens=tokens)
    assert [r["id"] for r in kept] == ["clear"]


def test_other_project_strong_match_is_not_pushed_unless_named(tmp_path, monkeypatch):
    _replica(tmp_path, monkeypatch,
             episodes=[(75, "Frost line snow shader tuning for the ridge", FROST, _days_ago(1))],
             cosine=[("episode", 75, 0.78)])
    out = rp.prior_work_for_prompt("snow shader tuning ridge", cwd="/khipu/worktree")
    assert out["hits"] == [], out
    named = rp.prior_work_for_prompt("snow shader tuning ridge in frost-line", cwd="/khipu/worktree")
    assert [r["id"] for r in named["hits"]] == ["75"], named


def test_other_project_named_by_its_alias_is_pushed(tmp_path, monkeypatch):
    _replica(tmp_path, monkeypatch,
             episodes=[(76, "Snow shader tuning for the ridge", FROST, _days_ago(1))],
             cosine=[("episode", 76, 0.72)])
    con = sqlite3.connect(hs.snapshot_path())
    con.execute("INSERT INTO project_aliases(alias, project) VALUES(?, ?)",
                ("mrkinglollipop/icecap", FROST))
    con.commit()
    con.close()
    out = rp.prior_work_for_prompt("icecap snow shader tuning ridge", cwd="/khipu/worktree")
    assert [r["id"] for r in out["hits"]] == ["76"], out


def test_other_project_clears_only_the_much_stricter_bar():
    from khipu import relevance

    strict = relevance.cosine_floor() + recency.CROSS_PROJECT_COSINE_MARGIN
    tokens = ["snow", "shader", "ridge"]
    base = {"kind": "episode", "score": 0.05, "project": FROST, "lexical_hits": 3}
    rows = [{**base, "id": "exceptional", "cosine": strict + 0.01},
            {**base, "id": "merely-good", "cosine": strict - 0.03},
            {**base, "id": "partial", "cosine": strict + 0.05, "lexical_hits": 2}]
    kept = recency.apply_push_policy(rows, project=KHIPU, tokens=tokens)
    assert [r["id"] for r in kept] == ["exceptional"]


def test_placeholder_episode_is_never_pushed(tmp_path, monkeypatch):
    _replica(tmp_path, monkeypatch,
             episodes=[(77, recency.PLACEHOLDER_SUMMARY, KHIPU, _days_ago(0),
                        "dispatch the recall ranking job")],
             cosine=[("episode", 77, 0.90)])
    out = rp.prior_work_for_prompt("dispatch the recall ranking job", cwd="/khipu/worktree")
    assert out["hits"] == [], out


def test_validity_derank_keeps_the_floor_on_pre_recency_relevance(monkeypatch):
    # Opus P3: the floor took the full recency bonus off a score the validity
    # step had already halved, so a fresh superseded episode went below zero.
    from khipu import validity

    monkeypatch.setenv("KHIPU_FEATURE_VALIDITY_RANKING", "1")
    top = {"kind": "episode", "id": "top", "score": 0.025, "recency": 0.0}
    sup = {"kind": "episode", "id": "sup-fresh", "score": 0.03 + 0.047619, "recency": 0.047619,
           "validity": {"state": "superseded"}}
    ranked = validity.apply_ranking([top, sup])
    sup_after = next(r for r in ranked if r["id"] == "sup-fresh")
    assert rp._floor_score(sup_after) == pytest.approx(0.03 * recency.STATUS_DERANK, abs=1e-6)
    assert sorted(r["id"] for r in rp._apply_score_floor(ranked)) == ["sup-fresh", "top"]


def test_question_words_do_not_count_toward_coverage():
    tokens = ["what", "approve", "widget", "batching", "endpoint", "pagination"]
    row = {"kind": "episode", "id": "1", "score": 0.05, "project": KHIPU, "lexical_hits": 4}
    assert [r["id"] for r in recency.apply_push_policy([row], project=KHIPU, tokens=tokens)] == ["1"]


def test_graph_candidate_rides_on_its_seed():
    tokens = ["billing", "service", "module"]
    seed = {"kind": "topic", "id": "billing-service", "score": 0.05, "project": KHIPU, "lexical_hits": 3}
    linked = {"kind": "topic", "id": "rate-limits", "score": 0.04, "project": KHIPU,
              "lexical_hits": 0, "via": "topic:billing-service"}
    orphan = {"kind": "topic", "id": "orphan", "score": 0.04, "project": KHIPU,
              "lexical_hits": 0, "via": "topic:weak-seed"}
    weak = {"kind": "topic", "id": "weak-seed", "score": 0.03, "project": KHIPU, "lexical_hits": 1}
    kept = recency.apply_push_policy([seed, linked, orphan, weak], project=KHIPU, tokens=tokens)
    assert [r["id"] for r in kept] == ["billing-service", "rate-limits"]


# Fix round 3 (replay of 1530e7b4, 2026-10-30): job briefs share a template,
# and earlier job episodes named all of a brief's first words, so 155 of 168
# briefs that needed nothing still got a push. Coverage now weighs every
# prompt word by how rare it is in memory. Fake template and data only.

TEMPLATE = ("You are a dispatched agent on the khipu repo. Do the work yourself. First read "
            "/Users/someone/jobs/brief.md. Oracle: run pytest from packages/cli. Never set TMPDIR. "
            "Commit; no push. ")
TEMPLATE_WORDS = ("dispatched agent work yourself first read users someone jobs brief oracle "
                  "run pytest packages cli never set tmpdir commit push khipu repo").split()


def _term_df(rare=(), n=3000):
    df = {w: n // 2 for w in TEMPLATE_WORDS}
    df.update({w: 2 for w in rare})
    df.update({"review": 300, "findings": 200, "importer": 150, "fix": 600, "tidy": 100, "docs": 400})
    return n, df


def _row(rid, text, **extra):
    return {"kind": "episode", "id": rid, "score": 0.05, "project": KHIPU,
            "label": text[:80], "snippet": text, "lexical_hits": 8, **extra}


def test_template_overlap_alone_is_not_pushed():
    prompt = TEMPLATE + "Task: tidy the docs index."
    old_job = _row("old-job", "Dispatched agent did the work yourself: first read the brief in "
                   "users someone jobs, ran the oracle (pytest, packages cli), never set TMPDIR, "
                   "commit, no push, khipu repo")
    tokens = search_tokens(prompt)
    # The first words a search leg sees are all template, and the row names them all.
    assert set(tokens) <= set(TEMPLATE_WORDS) | {"you"}
    assert [r["id"] for r in recency.apply_push_policy(
        [dict(old_job)], project=KHIPU, tokens=tokens, prompt=prompt, term_df=None)] == ["old-job"]
    kept = recency.apply_push_policy([old_job], project=KHIPU, tokens=tokens, prompt=prompt,
                                     term_df=_term_df())
    assert kept == []


@pytest.mark.parametrize("tail, rare, text", [
    ("Task: fix the review findings on PR 1193.", ["1193"],
     "Dispatched agent work on PR 1193: review findings were two P2s"),
    ("Task: fix rankForPush so the importer keeps order.", ["rankforpush"],
     "Dispatched agent changed rankForPush in the khipu repo"),
    ("Task: fix the zephyrine importer.", ["zephyrine"],
     "Dispatched agent work: zephyrine importer drafted"),
])
def test_one_rare_word_plus_one_more_is_not_pushed(tail, rare, text):
    # Fix round 5 (replay of ed760233): one rare word plus one more admitted
    # 412 unwanted brief pushes, so a row must cover the prompt's weight.
    prompt = TEMPLATE + tail
    filler = _row("old-job", "Dispatched agent did the work: first read the brief, ran the "
                  "oracle with pytest, never set TMPDIR, commit, no push")
    match = _row("match", text)
    kept = recency.apply_push_policy([filler, match], project=KHIPU, tokens=search_tokens(prompt),
                                     prompt=prompt, term_df=_term_df(rare))
    assert kept == []


def test_short_typed_prompt_weighs_its_rare_words():
    prompt = "zephyrine importer review"
    df = _term_df(["zephyrine"])
    both = _row("both", "Review of the zephyrine importer")
    common = _row("common", "Importer review notes")
    kept = recency.apply_push_policy([both, common], project=KHIPU, tokens=search_tokens(prompt),
                                     prompt=prompt, term_df=df)
    assert [r["id"] for r in kept] == ["both"] and kept[0]["push_evidence"] == "keywords"


def test_small_replica_falls_back_to_plain_coverage():
    prompt = TEMPLATE + "Task: tidy the docs index."
    row = _row("old-job", " ".join(TEMPLATE_WORDS))
    kept = recency.apply_push_policy([row], project=KHIPU, tokens=search_tokens(prompt),
                                     prompt=prompt, term_df=(recency.TERM_DF_MIN_DOCS - 1, {}))
    assert [r["id"] for r in kept] == ["old-job"]


def test_replica_template_brief_pushes_nothing_on_keywords(tmp_path, monkeypatch):
    from khipu import relevance

    template_summary = ("Dispatched agent did the work yourself: first read the brief in users "
                        "someone jobs, ran the oracle with pytest in packages cli, never set "
                        "TMPDIR, commit and no push in the khipu repo, round {i}")
    fillers = [(1000 + i, template_summary.format(i=i), KHIPU, _days_ago(2)) for i in range(240)]
    match = (901, "Dispatched agent work on PR 1193: review findings fixed in the importer",
             KHIPU, _days_ago(1))
    weak = relevance.cosine_floor()
    _replica(tmp_path, monkeypatch, episodes=[*fillers, match],
             cosine=[("episode", 1000, weak), ("episode", 1001, weak), ("episode", 901, weak)])
    hs._TERM_DF_CACHE.clear()
    out = rp.prior_work_for_prompt(TEMPLATE + "Task: fix the review findings on PR 1193.",
                                   cwd="/khipu/worktree")
    assert out["hits"] == [], out
    plain = rp.prior_work_for_prompt(TEMPLATE + "Task: tidy the docs index.", cwd="/khipu/worktree")
    assert plain["hits"] == [], plain


def test_term_document_frequency_counts_live_rows_and_never_raises(tmp_path, monkeypatch):
    _replica(tmp_path, monkeypatch, episodes=[(1, "alpha beta", KHIPU, _days_ago(1)),
                                              (2, "alpha gamma", KHIPU, _days_ago(1))])
    hs._TERM_DF_CACHE.clear()
    n, df = hs.term_document_frequency()
    assert n == 2 and df["alpha"] == 2 and df["beta"] == 1
    monkeypatch.setattr(hs, "snapshot_path", lambda: tmp_path / "missing.sqlite")
    assert hs.term_document_frequency() is None


# Fix round 4 (replay of c78a418f, 2026-10-30): 413 pushes on job briefs that
# needed nothing were admitted by rare words from the brief's own file paths.
# A path, URL or command line names something to open or run; only prose
# counts. Fake briefs and data only.

PATH_BRIEF = ("You are a dispatched agent on the khipu repo. Do the work yourself. First read "
              "/Users/someone/state/RULES.md, then your brief "
              "/Users/someone/state/jobs/qx-land-7731-0412/brief.md and re-read "
              "/Users/someone/state/jobs/qx-land-7731-0412/inbox.md before each step. "
              "Oracle: run pytest from packages/cli. Never set TMPDIR. Commit; no push. ")
PATH_WORDS = ["rules", "md", "state", "qx", "land", "7731", "0412", "inbox", "users", "someone",
              "jobs"]
# The brief's prose words, all common in an agent's memory.
BRIEF_PROSE = "you then your re each step oracle follow both fully task".split()


def _brief_df(rare):
    n, df = _term_df(rare=rare)
    df.update({w: n // 3 for w in BRIEF_PROSE})
    return n, df


def test_prose_text_drops_paths_urls_and_command_lines():
    from khipu.search_text import prompt_terms

    prompt = ("Fix the zephyrine importer, see https://example.test/zz/qq?id=9 and "
              "~/notes/kelpie.md, and run `env -u FOO_URL HOME=$(mktemp -d) pytest -q -k bramble`.\n"
              "env -u FOO_URL BAR=1 python3 -m pytest -q -p no:cacheprovider tests/ -k \"marrow\"\n"
              "Keep `rankForPush` and vendor.py as they are; and/or ask owner/repo.")
    terms = prompt_terms(prompt)
    for word in ("zephyrine", "importer", "rankforpush", "vendor", "owner", "repo", "keep"):
        assert word in terms, (word, terms)
    for word in ("example", "zz", "qq", "notes", "kelpie", "mktemp", "bramble", "marrow",
                 "foo_url", "cacheprovider"):
        assert word not in terms, (word, terms)


def test_brief_whose_rare_words_are_only_in_paths_pushes_nothing():
    prompt = PATH_BRIEF + "Follow both fully."
    df = _brief_df(PATH_WORDS)
    old_job = _row("old-job", "Dispatched agent work: read RULES.md and the qx land 7731 0412 "
                   "brief, inbox checked, khipu repo")
    kept = recency.apply_push_policy([old_job], project=KHIPU, tokens=search_tokens(prompt),
                                     prompt=prompt, term_df=df)
    assert kept == []
    # The plain-count fallback (no rarity table) counts prose words only, too.
    short = "Read /Users/someone/state/jobs/qx-land-7731-0412/brief.md and follow it."
    path_only = _row("path-only", "users someone state jobs qx land 7731 0412 brief")
    path_only.pop("lexical_hits")
    assert recency.apply_push_policy([path_only], project=KHIPU, tokens=search_tokens(short),
                                     prompt=short, term_df=None) == []


def test_same_brief_with_one_rare_prose_term_pushes_nothing():
    prompt = PATH_BRIEF + "Task: fix the zephyrine importer."
    df = _brief_df([*PATH_WORDS, "zephyrine"])
    old_job = _row("old-job", "Dispatched agent work: read RULES.md and the qx land 7731 0412 "
                   "brief, inbox checked, khipu repo")
    match = _row("match", "Dispatched agent work: zephyrine importer drafted")
    kept = recency.apply_push_policy([old_job, match], project=KHIPU, tokens=search_tokens(prompt),
                                     prompt=prompt, term_df=df)
    assert kept == []


def test_replica_path_brief_pushes_nothing(tmp_path, monkeypatch):
    from khipu import relevance

    fillers = [(1000 + i, "Dispatched agent did the work yourself: first read your brief, then "
                f"each step, oracle run, fix task, khipu repo, round {i}", KHIPU, _days_ago(2))
               for i in range(240)]
    path_twin = (900, "Read RULES.md and inbox for qx land 7731 0412, dispatched agent", KHIPU,
                 _days_ago(1))
    match = (901, "Dispatched agent work: zephyrine importer drafted", KHIPU, _days_ago(1))
    weak = relevance.cosine_floor()
    _replica(tmp_path, monkeypatch, episodes=[*fillers, path_twin, match],
             cosine=[("episode", 900, weak), ("episode", 901, weak), ("episode", 1000, weak)])
    hs._TERM_DF_CACHE.clear()
    assert rp.prior_work_for_prompt(PATH_BRIEF + "Follow both fully.",
                                    cwd="/khipu/worktree")["hits"] == []
    out = rp.prior_work_for_prompt(PATH_BRIEF + "Task: fix the zephyrine importer.",
                                   cwd="/khipu/worktree")
    assert out["hits"] == [], out


def test_term_frequency_is_kept_on_disk_by_freshness_stamp(tmp_path, monkeypatch):
    _replica(tmp_path, monkeypatch, episodes=[(1, "alpha beta", KHIPU, _days_ago(1)),
                                              (2, "alpha gamma", KHIPU, _days_ago(1))])
    hs._TERM_DF_CACHE.clear()
    assert hs.term_document_frequency(terms=["alpha", "beta", "delta"]) == (2, {"alpha": 2, "beta": 1})
    side = hs.term_df_path()
    assert side.is_file() and b"alpha" not in side.read_bytes()
    # A one-shot process: no table in memory, and counting would fail.
    hs._TERM_DF_CACHE.clear()
    monkeypatch.setattr(hs, "_count_term_df", lambda con: pytest.fail("recounted"))
    assert hs.term_document_frequency(terms=["gamma", "beta"]) == (2, {"gamma": 1, "beta": 1})
    # A catch-up changes the stamp, and the next reader counts again.
    hs._TERM_DF_CACHE.clear()
    monkeypatch.setattr(hs, "caught_up_at", lambda *a, **kw: "2026-10-30T06:00:00+00:00")
    monkeypatch.setattr(hs, "_count_term_df", lambda con: (3, {"alpha": 3}))
    assert hs.term_document_frequency(terms=["alpha"]) == (3, {"alpha": 3})
