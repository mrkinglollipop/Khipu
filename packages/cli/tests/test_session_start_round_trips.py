"""The session-start slice (activity.project_slice) reads the hub in as few
round trips as it can without reading any less: the project's alias group is
looked up once (identity.group_memo), the helpers' column checks are one
query (db.prime_table_columns), and a read whose savepoint only guards an
older hub's missing table skips it when the columns are known
(identity.schema_known). Real PostgreSQL from tests/hermetic_pg.py."""
from __future__ import annotations

import contextlib
import json

import psycopg
import pytest

from khipu import activity, db, identity
from tests import fact_seed
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

CANON, ALIAS, OTHER = "o/canon", "o/old-name", "o/unrelated"


@pytest.fixture
def slice_hub(hub, monkeypatch):  # noqa: F811
    hub.migrate()
    db._TABLE_COLUMNS_CACHE.clear()
    with hub.connect() as conn:
        cur = conn.cursor()
        cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s)", (ALIAS, CANON))
        for i, project in enumerate((CANON, ALIAS, OTHER, CANON, ALIAS)):
            fact_seed.announce(cur)
            cur.execute("INSERT INTO episodes (ts, summary, project, session_id, topics) "
                        "VALUES (now() - make_interval(mins => %s), %s, %s, %s, %s)",
                        (i, f"episode {i} in {project}", project, f"claude_code:s{i}",
                         json.dumps([f"topic-{i}", "topic-shared"])))
        for slug in ("topic-0", "topic-1", "topic-shared"):
            cur.execute("INSERT INTO topics (slug, title, body) VALUES (%s, %s, 'b')", (slug, slug.title()))
        for i, (project, owner) in enumerate(((CANON, "user"), (ALIAS, "assistant"), (CANON, "agent"),
                                              (OTHER, "user"))):
            cur.execute("INSERT INTO commitments (text, project, content_hash, owner, kind, status) "
                        "VALUES (%s, %s, %s, %s, 'followup', 'open')", (f"item {i}", project, f"rt-{i}", owner))
        cur.execute("INSERT INTO decisions (project, text) VALUES (%s, 'decided for canon')", (CANON,))
        cur.execute("INSERT INTO project_pairs (a, b, root_commit, state) VALUES (%s, %s, 'r', 'asked')",
                    tuple(sorted((CANON, "o/maybe-same"))))
        fact_seed.adopt_all(cur, "commitment")
        fact_seed.adopt_all(cur, "decision")
        conn.commit()
    monkeypatch.setattr(activity, "connect", lambda **_kw: hub.connect())
    yield hub
    db._TABLE_COLUMNS_CACHE.clear()


def _statements(monkeypatch) -> list[str]:
    sent: list[str] = []
    real = psycopg.Cursor.execute

    def execute(self, query, *args, **kwargs):
        sent.append(" ".join(str(query).split()))
        return real(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", execute)
    return sent


def _slice() -> dict:
    return activity.project_slice(project=CANON, repo_root="/r", host_session_id="claude_code:host")


def test_the_slice_reads_the_alias_group_once_and_takes_no_savepoint(slice_hub, monkeypatch):
    sent = _statements(monkeypatch)
    _slice()
    assert not [s for s in sent if "SAVEPOINT" in s], sent
    assert sum("FROM project_aliases" in s for s in sent) == 1, sent
    assert sum("information_schema.columns" in s for s in sent) == 1, sent
    # Before: 23 statements (four group lookups at three each, three column
    # checks, two savepoints around the pairs read).
    assert len(sent) <= 9, sent


def test_the_slice_reads_exactly_what_it_read_with_every_lookup_repeated(slice_hub, monkeypatch):
    fast = _slice()
    db._TABLE_COLUMNS_CACHE.clear()
    with monkeypatch.context() as m:
        m.setattr(identity, "group_memo", contextlib.nullcontext)
        m.setattr(db, "prime_table_columns", lambda *a, **k: None)
        sent = _statements(m)
        slow = _slice()
    assert sum("SAVEPOINT project_alias_group" in s for s in sent) >= 3  # the old path, really taken
    assert fast == slow
    # The alias's own rows are in it: nothing was dropped to save the trips.
    assert {e["summary"] for e in fast["episodes"]} >= {"episode 1 in o/old-name", "episode 0 in o/canon"}
    assert "item 1" in {c["text"] for c in fast["commitments"]}
    assert [d["text"] for d in fast["decisions"]] == ["decided for canon"]
    assert fast["pairs"] and fast["topics"]


class _Cur:
    def __init__(self) -> None:
        self.connection = object()
        self.sent: list[str] = []
        self._rows: list = []

    def execute(self, sql, params=()):
        self.sent.append(sql)
        self._rows = [("o/canon", "o/old-name")] if "project_aliases" in sql else []

    def fetchall(self):
        return self._rows


def test_the_group_memo_lasts_one_block_and_hands_out_copies():
    cur = _Cur()
    identity.project_group(cur, "o/old-name")
    identity.project_group(cur, "o/old-name")
    assert sum("project_aliases" in s for s in cur.sent) == 2  # outside the memo: every call reads
    cur.sent.clear()
    with identity.group_memo():
        first = identity.project_group(cur, "o/old-name")
        first.append("mutated")
        assert identity.project_group(cur, "o/old-name") == ["o/canon", "o/old-name"]
        identity.project_group(_Cur(), "o/old-name")  # another connection reads for itself
    assert sum("project_aliases" in s for s in cur.sent) == 1
    identity.project_group(cur, "o/old-name")
    assert sum("project_aliases" in s for s in cur.sent) == 2  # the memo ended with its block


def test_a_known_schema_skips_the_savepoint_only_inside_the_memo(monkeypatch):
    cur = _Cur()
    monkeypatch.setitem(db._SCHEMA_SEEN, db.database_identity(cur),
                        {"project_aliases": {"alias", "project", "created_at"}})
    assert not identity.schema_known(cur, "project_aliases", "alias", "project")
    identity.project_group(cur, "o/x")
    assert any("SAVEPOINT" in s for s in cur.sent)
    with identity.group_memo():
        assert identity.schema_known(cur, "project_aliases", "alias", "project")
        assert not identity.schema_known(cur, "project_aliases", "missing_column")
        assert not identity.schema_known(cur, "project_pairs", "a")
        assert not identity.schema_known(_Cur(), "project_aliases", "alias")  # another database
        cur.sent.clear()
        identity.project_group(cur, "o/x")
        assert not any("SAVEPOINT" in s for s in cur.sent)
