"""The per-prompt routing read and the replica cosine scan stay fast without
changing what they answer (2026-10-31).

The routing view was refolded on every prompt: its fingerprint held the
capture state folder's stat, and the recall hook rewrites that folder (each
session's state and routing ledger) on every prompt. The view now stays
fresh unless one of the capture files a runner's launches name, or the
Forget stamp, changed. The cosine scan packs a replica generation's vectors
once instead of on every prompt, with the same ranking.

Hermetic: a temp data dir and capture home per test; synthetic vectors.
"""
from __future__ import annotations

import json
import random
import struct
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing, routing_sources, session_capture, vector_scan

T0 = datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def record(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    capture = tmp_path / "capture"
    (capture / "state").mkdir(parents=True)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(capture))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.setattr(routing, "_now", lambda: T0)
    runner = tmp_path / "runner"
    (runner / "state").mkdir(parents=True)
    (runner / "config.json").write_text(json.dumps({"lanes": {"glm": {"kind": "deepseek", "model": "glm-5.3"}}}))
    (runner / "state/global.json").write_text('{"limited": {}}')
    (runner / "state/heartbeat.json").write_text(json.dumps({"time": T0.timestamp(), "interval": 20}))
    (runner / "events.log").write_text(
        "2026-10-31T14:50:00Z job LAUNCH lane=glm harness=claude_code session=job-one\n")
    (data / "config.json").write_text(json.dumps({
        "usage_accounts": [{"label": "Codex", "harness": "codex"}], "routing_reset_check": False,
        "routing_sources": [{"name": "runner", "kind": "lanes", "path": str(runner)}]}))
    events = routing.events_file()
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(json.dumps({"type": "rule_add", "id": "r1", "n": 1, "text": "a rule", "source": "added by you",
                                  "date": "2026-10-01", "seeded": False, "written_at": routing._iso(T0),
                                  "written_by": {"kind": "owner"}, "via": "app"}) + "\n")
    routing.read_view(now=T0)
    return capture / "state"


def _fresh() -> bool:
    return routing._fresh(routing._load_view(), routing._inputs()[0], T0 + timedelta(seconds=1))


def test_the_hooks_own_writes_leave_the_view_fresh(record):
    assert routing_sources.attachment_refs(routing._load_view()["source_cache"]["runners"]["runner"])
    assert _fresh()
    # What the recall hook writes on every prompt: a session's state and its routing ledger.
    (record / "recall--abc.json").write_text("{}")
    (record / "recall--0123abcd.routing.json").write_text("{}")
    (record / "claude_code--someone-else.json").write_text("{}")
    assert _fresh()


def test_a_named_capture_file_or_a_forget_refolds(record):
    assert _fresh()
    (record / "claude_code--job-one.json").write_text(json.dumps({"origin": "job"}))
    assert not _fresh()
    routing.read_view(now=T0 + timedelta(seconds=1))
    assert _fresh()
    (record / session_capture.LOGIN_FORGOTTEN_STAMP).write_text(str(T0.timestamp()))
    assert not _fresh()


def test_a_joined_capture_ages_out_on_time(record):
    seen = T0 - timedelta(seconds=session_capture.LOGIN_HOME_KEEP_S - 60)
    (record / "claude_code--job-one.json").write_text(json.dumps({"origin": "job", "seen_ts": seen.timestamp()}))
    routing.read_view(now=T0)
    kept = routing._load_view()["source_cache"]["captures"]["runner"]
    assert kept["until"] is not None
    (record / "recall--abc.json").write_text("{}")
    view, fingerprint = routing._load_view(), routing._inputs()[0]
    assert routing._fresh(view, fingerprint, T0 + timedelta(seconds=30))
    assert not routing._fresh(view, fingerprint, T0 + timedelta(seconds=61))


def test_an_unchanged_view_goes_stale_when_its_attachment_expires(record):
    seen = T0 - timedelta(seconds=session_capture.LOGIN_HOME_KEEP_S - 60)
    (record / "claude_code--job-one.json").write_text(json.dumps({"origin": "job", "seen_ts": seen.timestamp()}))
    routing.read_view(now=T0)
    view, fingerprint = routing._load_view(), routing._inputs()[0]
    until = routing._parse(view["source_cache"]["captures"]["runner"]["until"])
    assert view["fingerprint"] == fingerprint
    assert routing._fresh(view, fingerprint, until - timedelta(seconds=1))
    assert not routing._fresh(view, fingerprint, until + timedelta(seconds=1))
    # With no attachment kept, nothing but valid_until bounds the view.
    view["source_cache"]["captures"] = {}
    view["source_cache"]["runners"] = {}
    view["valid_until"] = None
    assert routing._fresh(view, fingerprint, until + timedelta(seconds=1))


def _rows(rnd, n, dim):
    rows = []
    for i in range(n):
        v = [rnd.gauss(0, 1) for _ in range(dim)]
        norm = sum(x * x for x in v) ** 0.5
        if i % 11 == 0 and rows:
            blob = rows[-1][5]  # an exact tie with the row before it
        else:
            blob = struct.pack(f"{dim}f", *(x / norm for x in v))
        rows.append((i + 100, "episode", str(i // 3), i % 3, "text", blob))
    return rows


@pytest.mark.parametrize("seed", range(6))
def test_packed_scan_ranks_as_the_exact_scan(seed):
    rnd = random.Random(seed)
    dim = 32
    packed = vector_scan.pack(_rows(rnd, 900, dim), key_at=0, blob_at=5)
    assert packed is not None
    dropped = set(rnd.sample(range(900), 90))  # tombstoned or pending erasure
    rows = [r for i, r in enumerate(packed.rows) if i not in dropped]
    by_rowid = {r[0]: (r[1], r[2]) for r in rows}
    q = [rnd.gauss(0, 1) for _ in range(dim)]
    norm = sum(x * x for x in q) ** 0.5
    q = tuple(x / norm for x in q)
    for limit, entity in ((1, None), (10, by_rowid.__getitem__), (50, by_rowid.__getitem__)):
        pairs = [(r[0], r[5]) for r in rows]
        exact = vector_scan.top_k_by_dot(q, pairs, limit, use_blas=False, entity_key=entity)
        assert vector_scan.top_k_packed(q, pairs, packed, limit, entity_key=entity, max_abs_component=1.0) == exact
        assert vector_scan.top_k_by_dot(q, pairs, limit, entity_key=entity, max_abs_component=1.0) == exact


def test_rows_that_cannot_be_packed_are_left_as_loaded():
    good = struct.pack("2f", 1.0, 0.0)
    assert vector_scan.pack([(1, good), (2, good[:4])], blob_at=1) is None
    assert vector_scan.pack([(1, good), (1, good)], blob_at=1) is None
    assert vector_scan.pack([(1, None)], blob_at=1) is None
    packed = vector_scan.pack([(1, "a", good)], blob_at=2)
    assert packed.dim == 2 and bytes(packed.rows[0][2]) == good and packed.rows[0][:2] == (1, "a")
