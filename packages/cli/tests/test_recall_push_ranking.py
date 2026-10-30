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


def test_cross_project_row_is_deranked_not_dropped():
    rows = [{"kind": "topic", "id": "note:frost-line-system", "score": 0.04, "project": "mrkinglollipop/frost-line"},
            {"kind": "episode", "id": "1", "score": 0.03, "project": KHIPU}]
    ranked = recency.apply_push_policy(rows, project=KHIPU)
    assert [r["id"] for r in ranked] == ["1", "note:frost-line-system"]


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
