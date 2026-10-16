"""Where the offline slices meet (docs/plans/2026-10-16-offline-and-self-healing.md):
``db.connect`` (slice 5) keeps the hub-down memo (slice 4) current, and a
recall call that skips the hub because of the memo is logged offline, so
doctor's rate window (slice 1) does not count it as a failure."""
from __future__ import annotations

import json

import psycopg
import pytest

from khipu import db, hub_reach
from khipu import hub_snapshot as hs
from khipu import recall_prompt as rp

PROMPT = "quarterly harvest festival planning"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.setattr(db, "resolve_dsn", lambda: "postgresql://x")
    monkeypatch.setattr(db, "conninfo_with_local_root_cert", lambda dsn: dsn)
    return tmp_path


def test_a_failed_connect_writes_the_memo_and_a_good_one_clears_it(home, monkeypatch):
    def refuse(*_a, **_k):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(psycopg, "connect", refuse)
    with pytest.raises(psycopg.OperationalError):
        db.connect()
    memo = hub_reach.known_unreachable()
    assert memo is not None and "refused" in memo["error"]

    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: "conn")
    assert db.connect() == "conn"
    assert hub_reach.known_unreachable() is None
    assert not hub_reach.memo_path().exists()


def test_a_connect_error_that_is_not_the_network_leaves_the_memo_alone(home, monkeypatch):
    def odd(*_a, **_k):
        raise ValueError("bad dsn syntax")

    monkeypatch.setattr(psycopg, "connect", odd)
    with pytest.raises(ValueError):
        db.connect()
    assert not hub_reach.memo_path().exists()


def test_a_memo_write_failure_never_changes_the_connect(home, monkeypatch):
    monkeypatch.setattr(hub_reach, "memo_path", lambda: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: "conn")
    assert db.connect() == "conn"


def test_a_recall_that_skips_the_hub_on_the_memo_is_offline_to_doctor(home, monkeypatch):
    monkeypatch.setattr(hs, "snapshot_path", lambda: home / "none.sqlite")
    monkeypatch.setattr(hs, "meta_path", lambda: home / "meta.json")
    monkeypatch.setattr(hs, "freshness_path", lambda: home / "freshness.json")

    def boom(*_a, **_k):
        pytest.fail("the hub was reached")

    monkeypatch.setattr("khipu.embed.hybrid_search", boom)
    monkeypatch.setattr(psycopg, "connect", boom)
    log = home / "prompt-recall.log"
    monkeypatch.setattr(rp, "_log_path", lambda: log)
    # The memo comes from a failed db.connect, as it would in the hook's process.
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: (_ for _ in ()).throw(
        psycopg.OperationalError("connection timeout")))
    with pytest.raises(psycopg.OperationalError):
        db.connect()
    monkeypatch.setattr(psycopg, "connect", boom)

    for n in range(25):
        rp.hook_main(json.dumps({"prompt": f"{PROMPT} {n}", "session_id": "s-offline"}))

    lines = [ln for ln in log.read_text().splitlines() if " reason=" in ln]
    assert len(lines) == 25 and all(" offline=1" in ln for ln in lines)
    out = rp.prompt_recall_outcomes()
    assert out["offline"] == 25
    assert out["count"] == 0
    assert "reason" not in out or "failed" not in out["reason"]
