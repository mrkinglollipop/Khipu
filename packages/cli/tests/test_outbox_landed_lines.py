"""Memory-file lines the hub already holds land; lines with facts the hub
lacks stay held, unchanged, for the owner. Real captures retry first."""
from copy import deepcopy
import hashlib
import json
from unittest.mock import Mock

import pytest

from khipu import capture, hook_background, mirror, outbox
from tests.test_outbox_legacy_projection import REASON, world  # noqa: F401 — fixture

# The hub row carries what the file line dropped (the 2026-10-27 backlog).
HUB = {
    "ts": "2026-10-02T10:00:00Z", "summary": "Synthetic landed line",
    "session_id": "claude_code:landed", "harness": "claude_code", "transcript_range": "0:20",
    "topics": ["outbox"], "preferences": ["Prefer short answers"],
    "decisions": ["Keep the hub, archive the line, never merge"],
    "decision_details": [{"text": "Keep the hub", "why": "superset"}],
    "open_loops": [{"text": "Check tomorrow", "kind": "followup"}], "closed_loops": [],
    "edges": [{"from": "a", "to": "b"}], "topic_pages": [{"slug": "outbox"}],
    "event": "Stop", "parent_session_id": None, "tags": [], "scope": None,
}
LINE = {
    "ts": HUB["ts"], "summary": HUB["summary"], "session_id": HUB["session_id"],
    "topics": ["outbox"], "people": [], "preferences": ["Prefer short answers"],
    "decisions": ["Keep the hub", "archive the line", "never merge"],
}


@pytest.fixture
def hub(world):  # noqa: F811
    cur, _ = world
    cur.episodes[1].update(deepcopy(HUB), raw=deepcopy(HUB))
    return cur


def _writes(cur):
    return [s for s, _ in cur.calls if s.startswith(("INSERT", "UPDATE", "DELETE"))]


def _queue(payload):
    return outbox.enqueue(payload, reason=REASON, origin=None, recovery=True)


def test_a_lossy_line_the_hub_row_holds_lands_without_touching_the_hub(hub):
    before = deepcopy(hub.episodes)
    path = _queue(deepcopy(LINE))
    report = outbox.drain()
    assert report.get("resolved") == 1, report
    assert not path.exists()
    [record] = outbox.resolved_dir().glob("*.json")
    saved = json.loads(record.read_text())
    assert saved["resolution"]["kind"] == "legacy_landed_line"
    assert saved["payload"] == HUB and saved["queued_evidence"]["payload"] == LINE
    outbox._validate_resolution(saved)
    assert hub.episodes == before and not _writes(hub)


@pytest.mark.parametrize("change", [
    {"preferences": ["Prefer short answers", "A preference the hub never got"]},
    {"preferences": ["A preference the hub never got"]},
    {"decisions": ["Keep the hub", "archive the line"]},
    {"decisions": ["Keep the hub", "archive the line", "never merge", "and one more"]},
    {"people": ["Someone the hub lacks"]},
    {"new_field": "text the hub lacks"},
])
def test_a_line_with_facts_the_hub_lacks_stays_held_unchanged(hub, monkeypatch, change):
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 1)
    line = {**deepcopy(LINE), **change}
    before = deepcopy(hub.episodes)
    path = _queue(line)
    report = outbox.drain()
    assert report.get("buried") == 1, report
    assert not list(outbox.resolved_dir().glob("*.json"))
    held = json.loads((outbox.dead_dir() / path.name).read_text())
    assert held["payload"] == line and held["evidence_conflict"] is True
    assert "owner" in held["hold_reason"].lower()
    assert hub.episodes == before and not _writes(hub)


def test_hub_lacking_the_lines_preferences_is_not_landed(hub):
    hub.episodes[1]["raw"]["preferences"] = []
    path = _queue(deepcopy(LINE))
    assert outbox.drain()["failed"] == 1
    assert path.exists() and json.loads(path.read_text())["payload"] == LINE


def test_a_landed_proof_cannot_be_relabelled_in_the_archive(hub):
    _queue(deepcopy(LINE))
    assert outbox.drain().get("resolved") == 1
    [record] = outbox.resolved_dir().glob("*.json")
    saved = json.loads(record.read_text())
    saved["resolution"]["kind"] = "legacy_comma_projection"
    with pytest.raises(ValueError):
        outbox._validate_resolution(saved)
    saved["resolution"]["kind"] = "legacy_landed_line"
    saved["payload"]["preferences"] = []
    with pytest.raises(ValueError):
        outbox._validate_resolution(saved)


@pytest.mark.parametrize("original, incoming, landed", [
    ({"decisions": ["a, b"]}, {"decisions": ["a", "b"]}, True),
    ({"decisions": ["a", "b"]}, {"decisions": ["a, b"]}, True),
    ({"decisions": ["a, b"]}, {"decisions": ["b", "a"]}, False),
    ({"n": 1}, {"n": True}, False),
    ({"n": 1, "extra": 2}, {"n": 1, "empty": [], "none": None, "blank": ""}, True),
    ({}, {"preferences": ["x"]}, False),
    ({"preferences": ["x", "y"]}, {"preferences": ["x"]}, False),
    ("not a row", {"n": 1}, False),
])
def test_landed_line_proof(original, incoming, landed):
    assert capture._legacy_landed_line(original, incoming) is landed


class _ConflictCursor:
    def __init__(self, raw):
        self.raw = raw
        self._rows = []

    def execute(self, sql, params=()):
        assert sql.startswith("SELECT v.ts, v.md,"), sql
        md = hashlib.md5(self.raw["summary"].encode()).hexdigest()
        self._rows = [(self.raw["ts"], md, None, self.raw,
                       *[self.raw.get(k) for k in mirror._FACT_KEYS], *([[]] * len(mirror._FACT_KEYS)))]

    def fetchall(self):
        return self._rows


def _conflicts(raw, line, monkeypatch):
    monkeypatch.setattr(capture, "_refusals_settled", lambda cur: None)
    monkeypatch.setattr("khipu.capture_identity.sources_ready", lambda cur: False)
    key = (line["ts"], hashlib.md5(line["summary"].encode()).hexdigest())
    return mirror._legacy_key_conflicts(_ConflictCursor(raw), [(line, key)])


def test_nightly_conflict_check_skips_a_landed_line(monkeypatch):
    assert _conflicts(deepcopy(HUB), deepcopy(LINE), monkeypatch) == []


@pytest.mark.parametrize("change", [
    {"preferences": ["A preference the hub never got"]},
    {"decisions": ["Keep the hub", "something else"]},
])
def test_nightly_conflict_check_still_queues_a_line_with_new_facts(monkeypatch, change):
    line = {**deepcopy(LINE), **change}
    assert _conflicts(deepcopy(HUB), line, monkeypatch) == [line]


def test_real_captures_retry_before_recovery_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path / "outbox"))
    folder = outbox.outbox_dir()
    folder.mkdir(parents=True)
    jobs = {
        "20260601T000000Z-recovery.json": {"origin": None, "reason": REASON, "attempts": 0},
        "20260602T000000Z-recovery-tried.json": {"origin": None, "reason": REASON, "attempts": 3,
                                                 "last_attempt_at": "2026-10-27T06:00:00Z"},
        "20261027T120000Z-real.json": {"origin": "local:aaaaaaaaaaaa", "reason": "pg write failed",
                                       "attempts": 0},
        "20261027T110000Z-real-tried.json": {"origin": None, "reason": "pg write failed", "attempts": 1,
                                             "last_attempt_at": "2026-10-27T11:00:00Z"},
        "20261027T100000Z-real-deferred.json": {"origin": "local:aaaaaaaaaaaa", "reason": "",
                                                "attempts": 1, "claim_deferred": True,
                                                "last_attempt_at": "2026-10-27T10:00:00Z"},
    }
    for name, job in jobs.items():
        (folder / name).write_text(json.dumps({"payload": {}, **job}))
    order = [p.name for p in sorted(outbox.jobs(), key=outbox._retry_order)]
    assert order == ["20261027T120000Z-real.json", "20261027T110000Z-real-tried.json",
                     "20261027T100000Z-real-deferred.json",
                     "20260601T000000Z-recovery.json", "20260602T000000Z-recovery-tried.json"]


def test_bounded_drain_reaches_a_real_capture_behind_a_recovery_backlog(world, monkeypatch):  # noqa: F811
    write = Mock(side_effect=capture.CaptureEvidenceConflict("synthetic"))
    monkeypatch.setattr(capture, "write_pg", write)
    for n in range(30):
        _queue({**deepcopy(LINE), "summary": f"backlog line {n}", "ts": "2026-06-01T00:00:00Z"})
    real = outbox.enqueue({"ts": "2026-10-27T12:00:00Z", "summary": "A real capture"},
                          reason="pg write failed", origin="local:aaaaaaaaaaaa")
    write.side_effect = None
    write.return_value = {"episode_inserted": False, "episode_id": 9}
    report = outbox.drain(limit=1)
    assert report["jobs"] == 1 and report["replayed"] == 1, report
    assert not real.exists() and len(outbox.jobs()) == 30


def test_stop_hook_outbox_drain_is_bounded(monkeypatch):
    import khipu.config
    import khipu.embed
    import khipu.notes
    from khipu import ledger, owner_writes

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    drain = Mock(return_value={"jobs": 20})
    monkeypatch.setattr(hook_background, "_drain", boom)
    monkeypatch.setattr(khipu.config, "path_setting", boom)
    monkeypatch.setattr(outbox, "jobs", lambda: ["one"])
    monkeypatch.setattr(outbox, "drain", drain)
    monkeypatch.setattr(owner_writes, "waiting", boom)
    monkeypatch.setattr(ledger, "_outbox_jobs", boom)
    monkeypatch.setattr(khipu.notes, "reconcile", boom)
    monkeypatch.setattr(khipu.embed, "embed_recent_missing", boom)
    hook_background.run("", "claude_code", "sid-bounded")
    drain.assert_called_once_with(limit=20, time_budget_s=60)
