"""Q3 projects triage (2026-10-26): alias undo restore, close-rules group,
decision dedup across aliases, and search exact-match for alias members."""
from __future__ import annotations

from unittest import mock

import pytest

from khipu import identity
from tests.fact_seed import adopt, announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

ALPHA, BETA = "owner/alpha", "owner/beta"


@pytest.fixture
def cur(hub, monkeypatch):  # noqa: F811
    import psycopg

    from khipu import migrate

    monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
    monkeypatch.setattr(psycopg, "connect", psycopg.Connection.connect)
    monkeypatch.setattr("khipu.db.connect", lambda *a, **k: hub.connect())
    out = migrate.run(dry_run=False, claim_local_rows=False)
    assert not out["pending"], out
    monkeypatch.setattr("khipu.topic_graph.persist_capture_graph", lambda *a, **k: {})
    monkeypatch.setattr("khipu.config.path_setting", lambda *a, **k: None)
    monkeypatch.setattr("khipu.embed.embed_one", mock.Mock(side_effect=RuntimeError("no key")))
    conn = hub.connect()
    yield conn.cursor()
    conn.close()


def _alias(cur):
    cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s)", (ALPHA, BETA))
    cur.connection.commit()


def test_removing_an_alias_leaves_captures_under_the_name_they_were_made_with(cur):
    from khipu import capture

    _alias(cur)
    capture.write_pg({"ts": "2026-10-19T12:01:00Z", "summary": "unique zebrafish note one",
                      "project": ALPHA, "harness": "claude_code", "session_id": "claude_code:s1",
                      "transcript_range": "0:1"})
    identity.remove_project_alias(cur, ALPHA)
    cur.connection.commit()
    cur.execute("SELECT project FROM episodes")
    assert [r[0] for r in cur.fetchall()] == [ALPHA], "capture rewrote the project to the alias target; undo cannot restore it"


def test_undo_after_a_second_rename_restores_the_typed_capture(cur):
    """P2: after o/a→o/b then o/b→o/c, episode stays under o/b; undo of o/a
    must match the whole group, not only the newest canonical."""
    from khipu import capture

    a, b, c = "owner/a", "owner/b", "owner/c"
    identity.record_rename(cur, a, b, source="checkout", evidence=None)
    cur.connection.commit()
    capture.write_pg({"ts": "2026-10-19T12:02:00Z", "summary": "unique zebrafish note two",
                      "project": a, "harness": "claude_code", "session_id": "claude_code:s2",
                      "transcript_range": "0:1"})
    identity.record_rename(cur, b, c, source="checkout", evidence=None)
    cur.connection.commit()
    out = identity.remove_project_alias(cur, a)
    cur.connection.commit()
    assert out.get("ok") and out.get("episodes_restored") == 1, out
    cur.execute("SELECT project, raw->>'typed_project' FROM episodes")
    assert cur.fetchall() == [(a, a)]


def test_close_rules_see_open_items_stored_under_an_alias_of_the_captured_project(cur):
    from khipu import close_rules

    _alias(cur)
    cur.execute("INSERT INTO commitments (text, project, owner, content_hash) VALUES "
                "('ship the zebrafish thing', %s, 'agent', 'h1') RETURNING id", (ALPHA,))
    adopt(cur, "commitment", cur.fetchone()[0])
    cur.connection.commit()
    items = close_rules._open_items(cur, BETA)
    assert [i["project"] for i in items] == [ALPHA], "alias-stored open item is invisible to a capture under the canonical name"


def test_decision_dedup_sees_a_standing_decision_stored_under_an_alias(cur):
    from khipu import decisions

    _alias(cur)
    cur.execute("INSERT INTO decisions (project, text) VALUES (%s, 'use zebrafish') RETURNING id", (ALPHA,))
    adopt(cur, "decision", cur.fetchone()[0])
    cur.connection.commit()
    assert decisions._find_recent_duplicate(cur, BETA, "use zebrafish", None) is not None, \
        "same decision restated under the canonical name becomes a second standing row"


def test_project_search_filter_does_not_match_a_substring_of_the_old_name(cur):
    from khipu.embed import _SearchFilters

    for n, proj in enumerate(("notes", "notes-v2", "release-notes")):
        announce(cur)
        cur.execute("INSERT INTO episodes (ts, session_id, summary, project) VALUES (now(), %s, %s, %s)",
                    (f"claude:{n}", f"note {n}", proj))
    cur.connection.commit()
    identity.record_rename(cur, "notes", "notes-v2", source="checkout", evidence=None)
    cur.connection.commit()
    f = _SearchFilters(project="notes-v2")
    f.expand_project(cur)
    cur.execute("SELECT project FROM episodes WHERE " + f.episode_sql(cur), f.params)
    got = sorted(r[0] for r in cur.fetchall())
    assert got == ["notes", "notes-v2"], f"filter also returned {got}"
