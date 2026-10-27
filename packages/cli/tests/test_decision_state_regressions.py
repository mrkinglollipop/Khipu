"""Regressions for decision state on restatements, API gaps, and topic: briefs."""
from __future__ import annotations

import contextlib
import itertools
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from khipu import briefs, decisions as de, reflect
from tests.fact_seed import announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

PROJECT = "acme/widget"
_N = itertools.count()
T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
SENTENCE = "Use sqlite for the replica"


@pytest.fixture
def world(hub):  # noqa: F811
    from khipu import db

    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    conn = hub.connect()
    with conn.cursor() as cur:
        announce(cur)
        yield hub, conn, cur
    conn.commit()


def _episode(cur, day, text):
    cur.execute(
        "INSERT INTO episodes (ts, summary, raw, session_id, project, decisions) "
        "VALUES (%s::timestamptz, %s, '{}'::jsonb, 'claude_code:s', %s, %s::jsonb) RETURNING id",
        (f"2026-09-{day:02d}T12:00:00Z", f"summary {next(_N)}", PROJECT, json.dumps([text])),
    )
    return int(cur.fetchone()[0])


def _record(cur, episode, text, *, ts=None):
    if ts is None:
        cur.execute("SELECT ts FROM episodes WHERE id = %s", (episode,))
        ts = cur.fetchone()[0].isoformat()
    return de.insert_decisions_from_episode(
        cur,
        {"project": PROJECT, "session_id": "claude_code:s", "ts": ts, "decisions": [text]},
        episode,
    )


def _row(cur, text):
    cur.execute("SELECT id FROM decisions WHERE text = %s ORDER BY id", (text,))
    return cur.fetchone()[0]


def test_a_restated_decision_shows_its_state_on_the_restating_episode(world):
    """Dedup attaches a restatement to the earlier row; get must still show state."""
    hub, conn, cur = world
    e10 = _episode(cur, 1, "use sqlite")
    e11 = _episode(cur, 2, "use sqlite")
    e12 = _episode(cur, 3, "use postgres")
    assert _record(cur, e10, "use sqlite") == 1
    assert _record(cur, e11, "use sqlite") == 0
    assert _record(cur, e12, "use postgres") == 1
    assert de.supersede(cur, _row(cur, "use sqlite"), _row(cur, "use postgres"))
    conn.commit()

    @contextlib.contextmanager
    def fake_connect(*a, **k):
        yield hub.connect()

    with mock.patch("khipu.db.connect", fake_connect):
        got = de.decision_states_for_episode(e11)
    assert got["decision_states"], f"episode 11 repeats 'use sqlite' but reports {got}"


def test_retraction_survives_forgetting_the_original_episode(hub):  # noqa: F811
    """A retracted decision forgotten with its episode must not reappear as
    (validity unknown) on a later restatement inside the dedup window."""
    from khipu import db

    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    conn = hub.connect()
    cur = conn.cursor()
    announce(cur)
    a = _episode_at(cur, T0, [SENTENCE])
    b = _episode_at(cur, T0 + timedelta(days=1), [SENTENCE])
    assert _record(cur, a, SENTENCE, ts=T0.isoformat()) == 1
    _record(cur, b, SENTENCE, ts=(T0 + timedelta(days=1)).isoformat())
    cur.execute("SELECT id FROM decisions WHERE episode_id = %s", (a,))
    assert de.retract(cur, cur.fetchone()[0], "wrong")
    cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (a,))
    sources = reflect._episode_sources(cur, [b])
    lines = sources[b]["decisions"]
    assert lines == [], f"offered to the model: {lines!r}"


def _episode_at(cur, ts, decisions, topics=("alpha",), project=PROJECT):
    cur.execute(
        "INSERT INTO episodes (ts, session_id, summary, topics, decisions, project) "
        "VALUES (%s, 'probe:s', %s, %s::jsonb, %s::jsonb, %s) RETURNING id",
        (ts, "summary", json.dumps(list(topics)), json.dumps(list(decisions)), project),
    )
    return cur.fetchone()[0]


def test_hub_down_get_reports_decision_states_from_the_replica():
    from khipu import hub_snapshot
    from tests.replica_copy import certify

    con = sqlite3.connect(":memory:")
    hub_snapshot._create_schema(con)
    con.execute(
        "INSERT INTO episodes (id, ts, session_id, summary, topics, people, decisions, "
        "preferences, edges) VALUES (5, '2026-09-01T00:00:00Z', 's', 'sum', '[]', '[]', "
        "'[\"use sqlite\"]', '[]', '[]')"
    )
    con.execute(
        "INSERT INTO decisions (id, project, text, episode_id, superseded_by) "
        "VALUES (1, 'p', 'use sqlite', 5, 2)"
    )
    certify(con)
    with mock.patch.object(hub_snapshot, "open_snapshot", lambda **k: con):
        row = hub_snapshot.episode_detail_snapshot(5)
    assert row is not None
    assert "decision_states" in row and "validity" in row, sorted(row)
    assert row["decision_states"][0]["state"] == "superseded"
    assert row["validity"]["superseded"] == 1


def test_topic_citation_fetches_the_text_that_was_offered(hub):  # noqa: F811
    """With a current brief, reflect offers the brief under topic:<slug>;
    khipu_get / topic_detail must return that same body, not the wiki page."""
    from khipu import activity, db, features

    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()

    @contextlib.contextmanager
    def fake_connect(*a, **k):
        yield hub.connect()

    with hub.connect() as conn, conn.cursor() as cur:
        announce(cur)
        e = _episode_at(cur, T0, [])
        brief_body = f"- plan stands [episode:{e}]"
        cur.execute(
            "INSERT INTO topics (slug, title, body) VALUES ('alpha', 'Alpha', 'PAGE BODY')"
        )
        cur.execute(
            "INSERT INTO briefs (topic_slug, body, claims, source_episode_ids, source_hash, "
            "derivation_version, state) VALUES ('alpha', %s, '[]'::jsonb, %s::bigint[], 'h', %s, "
            "'current')",
            (brief_body, [e], briefs.DERIVATION_VERSION),
        )
        conn.commit()
        current = {"found": True, "state": "current", "body": brief_body}
        with mock.patch.object(features, "enabled", lambda name: name == "briefs"), \
                mock.patch.object(briefs, "read_brief", lambda c, s: current):
            offered = reflect._topic_sources(cur, ["alpha"])["alpha"]["summary"]
    with mock.patch.object(features, "enabled", lambda name: name == "briefs"), \
            mock.patch.object(briefs, "read_brief", lambda c, s: current), \
            mock.patch.object(activity, "connect", fake_connect):
        fetched = activity.topic_detail("alpha")["body"]
    assert offered == brief_body
    assert offered == fetched, f"offered {offered!r} but get returns {fetched!r}"


def test_brief_topic_page_marks_source_and_uses_brief_id_as_hash(hub):  # noqa: F811
    """A brief page must not reuse the wiki content_hash: paging guards on
    (content_hash, updated_at), so a brief→wiki flip mid-page would splice
    undetected if the hash stayed the wiki's."""
    from khipu import activity, db, features

    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()

    @contextlib.contextmanager
    def fake_connect(*a, **k):
        yield hub.connect()

    with hub.connect() as conn, conn.cursor() as cur:
        announce(cur)
        e = _episode_at(cur, T0, [])
        brief_body = f"- plan stands [episode:{e}]"
        cur.execute(
            "INSERT INTO topics (slug, title, body, content_hash) "
            "VALUES ('alpha', 'Alpha', 'PAGE BODY', 'wiki-hash')"
        )
        cur.execute(
            "INSERT INTO briefs (topic_slug, body, claims, source_episode_ids, source_hash, "
            "derivation_version, state) VALUES ('alpha', %s, '[]'::jsonb, %s::bigint[], 'h', %s, "
            "'current') RETURNING id",
            (brief_body, [e], briefs.DERIVATION_VERSION),
        )
        brief_id = int(cur.fetchone()[0])
        conn.commit()
        current = {
            "found": True, "state": "current", "body": brief_body, "brief_id": brief_id,
        }
    with mock.patch.object(features, "enabled", lambda name: name == "briefs"), \
            mock.patch.object(briefs, "read_brief", lambda c, s: current), \
            mock.patch.object(activity, "connect", fake_connect):
        page = activity.topic_detail("alpha")
    assert page["body"] == brief_body
    assert page.get("source") == "brief", page
    assert page["content_hash"] == str(brief_id), page["content_hash"]
    assert page["content_hash"] != "wiki-hash"


def test_wiki_flag_returns_the_raw_topic_page_when_briefs_are_on(hub):  # noqa: F811
    """With briefs on, callers must still be able to read the wiki page."""
    from khipu import activity, db, features

    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()

    @contextlib.contextmanager
    def fake_connect(*a, **k):
        yield hub.connect()

    with hub.connect() as conn, conn.cursor() as cur:
        announce(cur)
        e = _episode_at(cur, T0, [])
        brief_body = f"- plan stands [episode:{e}]"
        cur.execute(
            "INSERT INTO topics (slug, title, body, content_hash) "
            "VALUES ('alpha', 'Alpha', 'PAGE BODY', 'wiki-hash')"
        )
        cur.execute(
            "INSERT INTO briefs (topic_slug, body, claims, source_episode_ids, source_hash, "
            "derivation_version, state) VALUES ('alpha', %s, '[]'::jsonb, %s::bigint[], 'h', %s, "
            "'current') RETURNING id",
            (brief_body, [e], briefs.DERIVATION_VERSION),
        )
        brief_id = int(cur.fetchone()[0])
        conn.commit()
        current = {
            "found": True, "state": "current", "body": brief_body, "brief_id": brief_id,
        }
    with mock.patch.object(features, "enabled", lambda name: name == "briefs"), \
            mock.patch.object(briefs, "read_brief", lambda c, s: current), \
            mock.patch.object(activity, "connect", fake_connect):
        page = activity.topic_detail("alpha", wiki=True)
    assert page["body"] == "PAGE BODY", page["body"]
    assert page["content_hash"] == "wiki-hash"
    assert page.get("source") != "brief"
