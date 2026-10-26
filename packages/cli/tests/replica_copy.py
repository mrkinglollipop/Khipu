"""Shared helpers for the replica fact copy tests (design section 6.2).

Every database is a hermetic one (tests/hermetic_pg.py) and every replica a
temp file; ``attach`` points the replica code at a hub and, by default, runs
the hub's identity index so it says ``fact_copy_ready = 'ready'``.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from khipu import db, hub_snapshot as hs, offline_reads, provenance


@pytest.fixture
def replica(monkeypatch, tmp_path):
    """``replica(hub)``: the replica code reads ``hub`` (``env`` already set
    KHIPU_DATA_DIR to a temp dir and patched ``db.connect``); its endpoint
    is the hub's DSN. ``ready=False`` leaves the hub's readiness as it is."""
    def attach(h, *, ready: bool = True):
        monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
        point(monkeypatch, h)
        if ready:
            mark_ready(h)
        return h
    return attach


def point(monkeypatch, h, *, dsn: str | None = None) -> None:
    """Serve ``h`` behind the configured endpoint ``dsn`` (``h``'s own by
    default)."""
    monkeypatch.setattr(hs, "try_hub_connect", lambda **kw: h.connect())
    monkeypatch.setattr(db, "resolve_dsn", lambda: dsn or h.dsn)


def mark_ready(h) -> None:
    with h.connect() as conn:
        out = provenance.index_identities(conn)
    assert out.get("state") == "ready", out


def certify(con: sqlite3.Connection) -> None:
    """Stamp a hand-built replica as a copy would (design 6.2): readers show
    no fact row without a complete stamp naming this endpoint and registry."""
    if not hs._snapshot_table_columns(con, "snapshot_meta"):
        return
    hs._meta_put(con, hs._FACT_DIGEST_KEY, hs._current_refusal_digest(hs._con_path(con)))
    hs._meta_put(con, hs._FACT_STAMP_KEY, json.dumps(
        {"incarnation": "test", "destination": hs._hub_destination(), "snapshot": "1:1:"}))


def rows(sql: str, params=()) -> list:
    with sqlite3.connect(f"file:{hs.snapshot_path()}?mode=ro", uri=True) as con:
        return con.execute(sql, params).fetchall()


def meta(key: str):
    got = rows("SELECT value FROM snapshot_meta WHERE key = ?", (key,))
    return got[0][0] if got else None


def stamp() -> dict | None:
    """The replica's stamp, or None when it has none."""
    digest, text = meta(hs._FACT_DIGEST_KEY), meta(hs._FACT_STAMP_KEY)
    return None if digest is None or text is None else {**json.loads(text), "fingerprint": digest}


def readers(project: str | None = None) -> str:
    """Every replica fact reader's output, as one string."""
    with sqlite3.connect(f"file:{hs.snapshot_path()}?mode=ro", uri=True) as con:
        deliv = hs.recent_deliverables_snapshot(con, project=project)
    return json.dumps([offline_reads.owed_rows(None, "open"), offline_reads.owed_rows(None, "closed"),
                       offline_reads.decisions(None, None, "all", 500), deliv], default=str)


def shown() -> int:
    """How many fact rows the replica's readers return."""
    with sqlite3.connect(f"file:{hs.snapshot_path()}?mode=ro", uri=True) as con:
        deliv = hs.recent_deliverables_snapshot(con, project=None) or []
    decisions = offline_reads.decisions(None, None, "all", 500) or {}
    return (len(offline_reads.owed_rows(None, "open") or []) + len(offline_reads.owed_rows(None, "closed") or [])
            + len(decisions.get("results") or []) + len(deliv))


def fact_text() -> str:
    """Every fact table's rows in the replica file, as one string."""
    return json.dumps([rows("SELECT * FROM decisions"), rows("SELECT * FROM deliverables"),
                       rows("SELECT * FROM commitments")], default=str)
