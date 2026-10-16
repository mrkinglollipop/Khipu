"""Round 15 of the cross-family forget review: queued jobs keyed by a natural
identity only the episode's columns hold, and a recall golden rewrite a crash
left behind. No PostgreSQL and no network."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest

from khipu import capture_identity as ci
from khipu import capture_stores, db, forget, outbox, paths, tombstones
from khipu import session_capture as sc
from tests import test_forget_ownership as fo
from tests.test_forget_round13 import COLUMN_ONLY, _world
from tests.test_forget_write_gate import P, _complete_forget

CLI_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith(("KHIPU_", "ALZY_")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "native"))
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "memory" / "outbox"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: pytest.fail("live PG forbidden"))
    monkeypatch.setattr(outbox, "_queued_origin", lambda: {"origin": None})
    db._TABLE_COLUMNS_CACHE.clear()


@pytest.mark.parametrize("store", ["native", "outbox"])
def test_forget_completion_checks_column_identities_in_queued_stores(tmp_path, monkeypatch, store):
    root = tmp_path / "memory"
    root.mkdir()
    world = _world("columns")
    # 0027's historical fallback when an older episode already owned this natural key.
    world["sources"] = [{"episode_id": 1, "role": "origin", "ts": fo.instant(COLUMN_ONLY["ts"]),
                         "summary_md5": fo.md5(COLUMN_ONLY["summary"]),
                         "source_key": ci.episode_key(1), "payload_sha256": None}]
    if store == "native":
        job = {"ts": COLUMN_ONLY["ts"], "harness": "codex", "session_id": "column-only",
               "transcript_range": COLUMN_ONLY["transcript_range"],
               "text": "private transcript of the forgotten window"}
        assert ci.source_key(sc.job_source_identity(job)) == ci.source_key(COLUMN_ONLY)
        queued = sc.enqueue(job)
    else:
        # Another synthesis of exactly the same natural source is forbidden too.
        queued = outbox.enqueue({**COLUMN_ONLY, "summary": "new synthesis of the same window"})
    monkeypatch.setattr(db, "connect", lambda *a, **k: fo._Conn(fo._WorldCur(world)))
    result = forget.forget_everywhere(1, memory_root=root)
    assert tombstones.is_forgotten(COLUMN_ONLY)
    scan = result["native_jobs" if store == "native" else "outboxes"]
    assert result["complete"], result.get("remaining")
    assert not queued.exists(), f"{store} still holds the forgotten natural source: {scan}"


def test_completed_forget_removes_crash_left_golden_rewrite_copies(tmp_path, monkeypatch):
    data = paths.ensure_data_dir()
    golden = data / "recall-golden.jsonl"
    golden.write_text(json.dumps({"query": "first source", "expect": [99]}) + "\n"
                      + json.dumps({"query": P["summary"], "expect": [1]}) + "\n")
    code = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from khipu import forget
original = os.replace
def die_at_publication(src, dst, *args, **kwargs):
    if Path(src).name.startswith('.recall-golden.'):
        os._exit(99)
    return original(src, dst, *args, **kwargs)
os.replace = die_at_publication
forget._scrub_recall_golden(99)
"""
    child = subprocess.run([sys.executable, "-c", code, str(CLI_ROOT)], timeout=30,
                           capture_output=True, text=True)
    assert child.returncode == 99, child.stderr
    remnants = list(data.glob(".recall-golden.*.tmp"))
    assert len(remnants) == 1
    assert P["summary"] in remnants[0].read_text()
    result = _complete_forget(tmp_path, monkeypatch)
    assert result["complete"], result.get("remaining")
    assert P["summary"] not in golden.read_text()
    assert not any(path.exists() for path in remnants)
    assert any(loc["place"] == "recall golden temp" and loc["state"] == "removed"
               for loc in result["recall_golden"]["locations"]), result["recall_golden"]


def test_a_golden_temp_that_cannot_be_removed_keeps_the_forget_open(tmp_path, monkeypatch):
    data = paths.ensure_data_dir()
    stuck = data / ".recall-golden.abc123.tmp"
    stuck.mkdir()  # unlink refuses a directory
    result = forget._scrub_recall_golden(1)
    assert not result["ok"]
    assert result["locations"][0]["place"] == "recall golden temp"
    assert result["locations"][0]["state"] == "copy remains"


def test_backup_import_never_restores_a_golden_rewrite_temp():
    store = next(s for s in capture_stores.STORES if s.name == "recall golden set")
    assert store.restore.claims(("recall-golden.jsonl",))
    assert not store.restore.claims((".recall-golden.abc123.tmp",))
    assert not any(s.restore and s.restore.claims((".recall-golden.abc123.tmp",))
                   for s in capture_stores.STORES)
