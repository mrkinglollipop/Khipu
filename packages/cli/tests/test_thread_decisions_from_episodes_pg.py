"""A T3 thread's decisions come back from ``episodes.decisions``.

The decisions registry is no longer written, so the per-prompt thread leg
(``recall_prompt._thread_memory_hits``) reads the decision strings capture
still stores on each episode. These tests land the episode through the real
capture writer on a hermetic hub and never seed the registry for the new
decision. The registry is read only to leave out an archived row's string that
was superseded or retracted."""
from __future__ import annotations

from copy import deepcopy
from unittest import mock

import pytest

from khipu import capture, db, hub_snapshot as hs, outbox, provenance
from khipu import recall_prompt as rp
from tests import fact_seed
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

THREAD = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-2222-4333-8444-555555555555"


@pytest.fixture
def pg(hub, monkeypatch, tmp_path):  # noqa: F811
    from khipu import embed, topic_graph

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "native"))
    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    provenance._reset_ready_cache()
    monkeypatch.setattr(db, "connect", lambda *a, **kw: hub.connect())
    monkeypatch.setattr(embed, "embed_on_capture", lambda *a, **kw: None)
    monkeypatch.setattr(embed, "embed_one", mock.Mock(side_effect=RuntimeError("no provider")))
    monkeypatch.setattr(topic_graph, "persist_capture_graph", lambda *a, **kw: {})
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    monkeypatch.setattr(capture, "_dedup_candidates", lambda cur, payload: [])
    # No replica: the hub leg is the one under test.
    monkeypatch.setattr(hs, "open_snapshot", mock.Mock(side_effect=RuntimeError("no replica")))
    monkeypatch.setattr(hs, "try_hub_connect", lambda *a, **kw: hub.connect())
    return hub


def land(n, *, thread=THREAD, decisions=("Keep the recall budget",), ts="2026-11-01T01:00:00Z"):
    payload = {
        "ts": ts, "summary": f"Worked on thread continuity, episode {n}, with enough words to keep",
        "session_id": f"codex:thread-{n}", "harness": "codex", "event": "stop",
        "transcript_range": f"{n}00:{n}99", "window_id": f"window-{n}", "project": "acme/thread",
        "scope": "codex stop", "topics": ["thread-continuity"], "decisions": list(decisions),
        "open_loops": [], "closed_loops": [], "people": [], "preferences": [],
        "verbatim": {"quotes": []}, "truncated_chars": 0, "edges": [], "topic_pages": [],
        "t3_thread_id": thread, "via": "t3",
    }
    return capture.write_pg(deepcopy(payload))["episode_id"]


def registry_rows(hub):  # noqa: F811
    with hub.connect() as conn:
        return conn.execute("SELECT count(*) FROM decisions").fetchone()[0]


def test_a_new_capture_with_no_registry_row_comes_back_from_episode_decisions(pg):
    episode = land(1, decisions=["Keep the recall budget", "Ship behind a switch"])
    assert registry_rows(pg) == 0  # nothing writes the registry any more
    hits = rp._thread_memory_hits(THREAD)
    assert [(h["kind"], h["snippet"], h["episode_id"], h["status"]) for h in hits] == [
        ("decision", "Keep the recall budget", episode, "standing"),
        ("decision", "Ship behind a switch", episode, "standing"),
    ]
    assert len({h["id"] for h in hits}) == 2


def test_decisions_are_this_threads_newest_first_and_bounded_to_three(pg):
    land(1, decisions=["old one"], ts="2026-11-01T01:00:00Z")
    land(2, decisions=["other thread"], thread=OTHER, ts="2026-11-02T01:00:00Z")
    land(3, decisions=["middle one", "middle two"], ts="2026-11-03T01:00:00Z")
    land(4, decisions=["newest one", "newest two"], ts="2026-11-04T01:00:00Z")
    hits = rp._thread_memory_hits(THREAD)
    assert [h["snippet"] for h in hits] == ["newest one", "newest two", "middle one"]


def seed(conn, kind, sql, params):
    """One raw fact row, adopted so the fact gate accepts it at commit."""
    cur = conn.cursor()
    if provenance.ready(cur):
        provenance.declare_writer(cur)
    cur.execute(sql + " RETURNING id", params)
    fact_id = cur.fetchone()[0]
    fact_seed.adopt(cur, kind, fact_id)
    return fact_id


def test_forgotten_episodes_and_archived_superseded_or_retracted_strings_stay_out(pg):
    old = land(1, decisions=["Superseded rule", "Retracted rule", "Standing rule"], ts="2026-11-01T01:00:00Z")
    gone = land(2, decisions=["Forgotten rule"], ts="2026-11-02T01:00:00Z")
    with pg.connect() as conn:
        a = seed(conn, "decision", "INSERT INTO decisions (text, episode_id) VALUES ('Superseded rule', %s)", (old,))
        seed(conn, "decision", "INSERT INTO decisions (text, episode_id) VALUES ('Standing rule', %s)", (old,))
        conn.execute("UPDATE decisions SET superseded_by = %s WHERE id = %s", (a, a))
        b = seed(conn, "decision", "INSERT INTO decisions (text, episode_id) VALUES ('Retracted rule', %s)", (old,))
        conn.execute("UPDATE decisions SET retracted_at = now() WHERE id = %s", (b,))
        conn.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (gone,))
        conn.commit()
    assert [h["snippet"] for h in rp._thread_memory_hits(THREAD)] == ["Standing rule"]


def test_open_commitments_of_the_thread_still_come_back(pg):
    episode = land(1, decisions=["Keep the recall budget"])
    with pg.connect() as conn:
        for text, key, status, due in (("Verify thread continuity", "a", "open", None),
                                       ("Already closed", "b", "closed", None),
                                       ("Snoozed", "c", "open", "2999-01-01")):
            seed(conn, "commitment",
                 "INSERT INTO commitments (text, opened_episode, content_hash, status, due_after) "
                 f"VALUES (%s, %s, '{key}', %s, %s)", (text, episode, status, due))
        conn.commit()
    assert [(h["kind"], h["snippet"]) for h in rp._thread_memory_hits(THREAD)] == [
        ("decision", "Keep the recall budget"), ("commitment", "Verify thread continuity")]
