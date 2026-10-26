"""Session-ended owed closes must not retire live or future-trigger rows.

Promoted from grokq-triage-1 commitment probes (item 4).
"""
from __future__ import annotations

import itertools
import json
import os
from unittest import mock

import pytest

from khipu import commitments as co
from khipu import hygiene
from tests.fact_seed import announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_commitments import _no_cosine

PROJECT = "acme/widget"
S1, S2 = "claude_code:s1", "claude_code:s2"
_N = itertools.count()
TEXT = "I will rebase the migration branch onto main"


@pytest.fixture(autouse=True)
def _aliases():
    with mock.patch.dict(os.environ, {"KHIPU_USER_ALIASES": "matt"}):
        co.reset_user_patterns()
        yield
    co.reset_user_patterns()


@pytest.fixture
def cur(hub):  # noqa: F811
    from khipu import db

    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    conn = hub.connect()
    with conn.cursor() as c:
        announce(c)
        yield c
    conn.commit()


def _episode(cur, session, *, hours_ago, event=None):
    cur.execute(
        "INSERT INTO episodes (ts, summary, raw, session_id, project) "
        "VALUES (now() - make_interval(hours => %s), %s, %s::jsonb, %s, %s) RETURNING id",
        (hours_ago, f"summary {next(_N)}", json.dumps({"event": event} if event else {}),
         session, PROJECT))
    return int(cur.fetchone()[0])


def _open(cur, session, text, episode, kind="promise"):
    payload = {"project": PROJECT, "session_id": session, "open_loops": [{"text": text, "kind": kind}]}
    with mock.patch.object(co, "_cosine_scores", _no_cosine), \
            mock.patch.object(co, "_has_commitment_embeddings", lambda c, ids: False):
        return co.open_from_episode(cur, payload, episode)


def _status(cur, cid=None):
    cur.execute("SELECT id, status, close_reason FROM commitments ORDER BY id")
    rows = cur.fetchall()
    return rows if cid is None else next(r for r in rows if r[0] == cid)


def test_restatement_from_a_live_session_keeps_the_row_open_through_the_nightly_pass(cur):
    """S1 opened it; S2 (live) restates it. Nightly must not close from S1 alone."""
    e1 = _episode(cur, S1, hours_ago=8)
    e2 = _episode(cur, S2, hours_ago=0)
    assert _open(cur, S1, TEXT, e1) == 1
    assert _open(cur, S2, TEXT, e2) == 0, "the restatement folds into the open row"
    cur.execute("SELECT id FROM commitments")
    cid = cur.fetchone()[0]
    hygiene.run_session_ended_pass(cur, apply=True, project=PROJECT)
    assert _status(cur, cid)[1] == "open", _status(cur, cid)


def test_a_restatement_that_adds_next_session_is_not_closed_with_the_session(cur):
    """Folded 'next session' wording must protect the row at sessionend."""
    e1 = _episode(cur, S1, hours_ago=1)
    e1b = _episode(cur, S1, hours_ago=0)
    assert _open(cur, S1, TEXT, e1) == 1
    assert _open(cur, S1, TEXT + " next session", e1b) == 0, "folded by containment"
    cur.execute("SELECT id FROM commitments")
    cid = cur.fetchone()[0]
    end = _episode(cur, S1, hours_ago=0, event="sessionend")
    co.close_session_plan(cur, {"session_id": S1, "event": "sessionend", "project": PROJECT}, end)
    assert _status(cur, cid)[1] == "open", _status(cur, cid)


def test_reopen_after_a_sessionend_close_survives_the_nightly_pass(cur):
    """Capture and nightly must share a session-ended key so reopen sticks."""
    e1 = _episode(cur, S1, hours_ago=8)
    assert _open(cur, S1, TEXT, e1) == 1
    cur.execute("SELECT id FROM commitments")
    cid = cur.fetchone()[0]
    end = _episode(cur, S1, hours_ago=8, event="sessionend")
    assert co.close_session_plan(cur, {"session_id": S1, "event": "sessionend", "project": PROJECT}, end) == 1
    assert co.set_status(cur, cid, "open")
    hygiene.run_session_ended_pass(cur, apply=True, project=PROJECT)
    assert _status(cur, cid)[1] == "open", _status(cur, cid)


def test_a_sessionend_episode_does_not_close_items_opened_after_it(cur):
    """A prior sessionend in the same session_id must not end a later open."""
    end = _episode(cur, S1, hours_ago=2, event="sessionend")  # noqa: F841
    later = _episode(cur, S1, hours_ago=0)
    assert _open(cur, S1, TEXT, later) == 1
    cur.execute("SELECT id FROM commitments")
    cid = cur.fetchone()[0]
    hygiene.run_session_ended_pass(cur, apply=True, project=PROJECT)
    assert _status(cur, cid)[1] == "open", _status(cur, cid)


def test_a_row_opened_with_next_session_wording_survives_sessionend(cur):
    """Control: the trigger phrase protects when it is the row's own text."""
    e1 = _episode(cur, S1, hours_ago=0)
    assert _open(cur, S1, TEXT + " next session", e1) == 1
    cur.execute("SELECT id FROM commitments")
    cid = cur.fetchone()[0]
    end = _episode(cur, S1, hours_ago=0, event="sessionend")
    co.close_session_plan(cur, {"session_id": S1, "event": "sessionend", "project": PROJECT}, end)
    assert _status(cur, cid)[1] == "open", _status(cur, cid)
