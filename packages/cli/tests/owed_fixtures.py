from __future__ import annotations

import io
import string
from datetime import datetime, timezone

import pytest

from khipu import commitments as co, db, routing, work
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.fact_seed import announce
from tests import fact_seed

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
            announce(cur)
            cur.execute("INSERT INTO episodes (ts, session_id, summary, raw, project, parent_session_id) "
                        "VALUES (%s, %s, %s, '{}'::jsonb, %s, %s) RETURNING id",
                        (at or BASE_AT, f"s-{self._n}", summary or f"episode {self._n}", PROJECT, parent))
            self._n += 1
            return int(cur.fetchone()[0])

    def item(self, group: str, text: str, *, kind="followup", episode: int, owner="user", at=None,
             label=None) -> int:
        cols, marks, vals = ["text", "project", "owner", "kind", "opened_episode", "opened_at", "status", "content_hash"], \
            ["%s"] * 6 + ["'open'", "%s"], \
            [text, PROJECT, owner, kind, episode, at or BASE_AT, co.content_hash(PROJECT, text)]
        if label:
            cols.append("owner_label")
            marks.append("%s")
            vals.append(label)
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(f"INSERT INTO commitments ({', '.join(cols)}) VALUES ({', '.join(marks)}) RETURNING id",
                        vals)
            cid = int(cur.fetchone()[0])
            fact_seed.adopt(cur, "commitment", cid)
        self.groups.setdefault(group, []).append(cid)
        return cid


