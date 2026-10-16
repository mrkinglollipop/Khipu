"""Owner direction: privacy and the purge timer (docs/plans/2026-10-16-owner-direction.md).

The owner's typed words are kept for a day and no longer: a read past the retention
removes them, and the replica-refresh timer removes them when no further prompt comes.
Forgetting an episode removes its session's recorded owner words (turn files, briefs,
widenings waiting for a yes) and scrubs the words from its owner-actions lines, which
keep their hash so the log's chain still holds. On the gateway the scan does not apply
and touches nothing. A purge runs on its own once its day has passed, from the same
timer, and from its deadline on it can no longer be cancelled.

Hermetic: temp data and capture folders; turns are recorded the way the prompt hook
records them; forget runs against the in-memory world of tests/test_forget_ownership.py."""
from __future__ import annotations

import json
import os
import time
from unittest import mock

import pytest

from khipu import capture_stores, forget, owner_direction, owner_turns
from tests import test_owner_direction as base

world = base.world  # the fixture, shared

CLAUDE_HOOK = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}


@pytest.fixture
def folders(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    return tmp_path


def _say(session: str, text: str, at: float, cwd) -> None:
    owner_turns.record({"session_id": session, "prompt": text, "cwd": str(cwd)}, CLAUDE_HOOK, now=at)


# ---- the original probes ----------------------------------------------------------------------


def test_owner_turn_plaintext_is_not_left_past_retention_without_another_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    session = "review-retention"
    at = 1_700_000_000.0
    owner_turns.record(
        {"session_id": session, "prompt": "pause Codex until five", "cwd": str(tmp_path)},
        {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"},
        now=at,
    )
    path = owner_turns.session_file(session)
    assert "pause Codex until five" in path.read_text(encoding="utf-8")

    # Retention is a privacy deadline, not merely a filtered read result.
    assert owner_turns.turns(session, now=at + owner_turns.RETENTION_S + 1) == []
    assert not path.exists(), "expired owner words remain plaintext until a future prompt is recorded"


def test_completed_local_forget_does_not_leave_raw_owner_turn_words(tmp_path, monkeypatch):
    """A fake local forget may complete only if its matching owner words are gone too."""
    from tests import test_forget_ownership as fo

    data = tmp_path / "data"
    root = tmp_path / "memory"
    data.mkdir()
    root.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    payload = {"ts": "2026-10-01T09:00:00Z", "summary": "forget review source", "harness": "codex",
               "session_id": "codex:forget-review", "transcript_range": "0:10"}
    fo.write_lines(root, [payload])
    world = {
        "episodes": {1: {"id": 1, "ts": fo.BASE, "summary": payload["summary"],
                          "session_id": payload["session_id"], "raw": dict(payload), "deleted": False}},
        "sources": [fo.ForgetOwnershipTest._source(None, 1, payload)],
        "sources_enabled": True,
    }
    owner_turns.record(
        {"session_id": "codex:forget-review", "prompt": "forget review source", "cwd": str(tmp_path)},
        {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"},
    )
    raw_turn = owner_turns.session_file("codex:forget-review")
    assert fo.run_forget(world, 1, root)["complete"] is True
    assert not raw_turn.exists(), "forget completed while matching raw owner words remained"


# ---- retention --------------------------------------------------------------------------------


def test_a_read_keeps_the_fresh_lines_and_drops_only_the_expired_ones(folders):
    at = 1_700_000_000.0
    _say("s-mixed", "first words long ago", at, folders)
    _say("s-mixed", "fresh words", at + owner_turns.RETENTION_S - 10, folders)
    later = at + owner_turns.RETENTION_S + 1
    assert [t["text"] for t in owner_turns.turns("s-mixed", now=later)] == ["fresh words"]
    raw = owner_turns.session_file("s-mixed").read_text(encoding="utf-8")
    assert "first words long ago" not in raw and "fresh words" in raw


def test_the_sweep_prunes_every_file_line_by_line_and_expires_waiting_widenings(folders):
    at = 1_700_000_000.0
    day = owner_turns.RETENTION_S
    _say("s-old", "all of this is old", at, folders)
    _say("s-both", "old line in a fresh file", at, folders)
    _say("s-both", "new line in that file", at + day - 5, folders)
    # A fresh mtime must not keep old lines: expiry is by each line's time.
    os.utime(owner_turns.session_file("s-old"), None)
    owner_turns.note_brief("a dispatched brief from long ago, long enough", parent="s-old", harness="claude_code",
                           now=at)
    owner_turns.note_brief("a dispatched brief from just now, long enough", parent="s-both",
                           harness="claude_code", now=at + day - 5)
    owner_direction._save_pending([
        {"action": "direction.set", "target": "latest_window_h", "session": "s-old", "ts": at, "words": "old"},
        {"action": "direction.set", "target": "earlier_window_min", "session": "s-both", "ts": at + day - 5,
         "words": "new"}])

    out = owner_turns.sweep(now=at + day + 1)

    assert out["ok"] is True, out
    assert not owner_turns.session_file("s-old").exists()
    both = owner_turns.session_file("s-both").read_text(encoding="utf-8")
    assert "old line in a fresh file" not in both and "new line in that file" in both
    briefs = (owner_turns.folder() / "briefs.jsonl").read_text(encoding="utf-8")
    assert "long ago" not in briefs and "just now" in briefs
    assert [p["words"] for p in owner_direction._pending()] == ["new"]


def test_the_replica_refresh_job_runs_the_sweep(folders, monkeypatch):
    from khipu import cli, replica_heal

    long_ago = time.time() - owner_turns.RETENTION_S - 60
    _say("s-timer", "words nobody followed up", long_ago, folders)
    monkeypatch.setattr(replica_heal, "run", lambda: {"ok": True, "action": "none"})
    with mock.patch("sys.stdout"):
        rc = cli.main(["snapshot", "heal"])
    assert rc == 0
    assert not owner_turns.session_file("s-timer").exists()


def test_the_replica_refresh_job_still_heals_when_the_sweep_fails(folders, monkeypatch):
    from khipu import cli, replica_heal

    monkeypatch.setattr(replica_heal, "run", lambda: {"ok": True, "action": "none"})
    monkeypatch.setattr(owner_turns, "sweep", lambda now=None: (_ for _ in ()).throw(OSError("disk")))
    with mock.patch("sys.stdout"):
        assert cli.main(["snapshot", "heal"]) == 0


# ---- forget: the owner words scan -------------------------------------------------------------


def _direction_with_words(world, session: str, words: str, verb: str) -> dict:
    world.be(session)
    token = world.say(words, session=session)
    rc, out = world.run(["route", verb, "Codex"], words, token)
    assert rc == 0 and out["ok"], out
    return next(x for x in owner_direction.read_log() if x.get("session") == session and x.get("words"))


def test_forget_scrubs_the_sessions_words_and_the_chain_still_holds(world):
    _direction_with_words(world, "s-forgotten", "pause Codex for now", "pause")
    _direction_with_words(world, "s-kept", "lift Codex please", "lift")
    owner_turns.note_brief("a dispatched brief from the forgotten session", parent="s-forgotten",
                           harness="claude_code")
    owner_direction._save_pending([{"action": "direction.set", "target": "latest_window_h",
                                    "session": "s-forgotten", "ts": time.time(), "words": "pending words"}])

    out = forget._owner_words_scan("claude_code:s-forgotten", gateway=False)

    assert out["ok"] is True, out
    assert not owner_turns.session_file("s-forgotten").exists()
    assert owner_turns.session_file("s-kept").exists()
    assert "forgotten session" not in (owner_turns.folder() / "briefs.jsonl").read_text(encoding="utf-8")
    assert owner_direction._pending() == []
    raw = owner_direction.log_path().read_text(encoding="utf-8")
    assert "pause Codex for now" not in raw and "lift Codex please" in raw
    scrubbed = [x for x in owner_direction.read_log() if x.get("session") == "s-forgotten" and x.get("scrubbed")]
    assert scrubbed and all(x["words"] is None and x["words_sha"] for x in scrubbed)
    assert owner_direction.verify_log()["ok"] is True, owner_direction.verify_log()


def test_scrubbing_a_line_from_before_the_chain_keeps_the_chain(folders):
    log = owner_direction.log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"kind": "action", "id": "old-1", "action": "route.pause", "target": "codex|",
                               "session": "s-legacy", "words": "pause Codex long ago"}) + "\n", encoding="utf-8")
    owner_direction._append({"kind": "action", "id": "new-1", "action": "route.lift", "target": "codex|",
                             "session": "s-other", "words": "lift Codex now"})
    assert owner_direction.verify_log()["ok"] is True
    assert owner_direction.scrub_words({"s-legacy"}) == 1
    assert "long ago" not in log.read_text(encoding="utf-8")
    assert owner_direction.verify_log()["ok"] is True, owner_direction.verify_log()
    # Any other change to that line still breaks the chain.
    row = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    row["target"] = "grok|"
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join([json.dumps(row)] + lines[1:]) + "\n", encoding="utf-8")
    assert owner_direction.verify_log()["ok"] is False


def test_forget_matches_a_bare_episode_session_to_a_prefixed_turn_file(folders):
    _say("codex:abc", "words under a prefixed id", time.time(), folders)
    out = forget._owner_words_scan("abc", gateway=False)
    # A bare episode id names only bare files; the prefixed file is another id.
    assert owner_turns.session_file("codex:abc").exists() and out["ok"]
    out = forget._owner_words_scan("codex:abc", gateway=False)
    assert not owner_turns.session_file("codex:abc").exists() and out["ok"]


def test_forget_does_not_apply_on_the_gateway_and_touches_nothing(folders):
    _say("s-gw", "words on this machine", time.time(), folders)
    out = forget._owner_words_scan("s-gw", gateway=True)
    assert out["ok"] is True
    assert [loc["state"] for loc in out["locations"]] == ["not applicable"]
    assert owner_turns.session_file("s-gw").exists()


def test_an_owner_words_scan_that_cannot_finish_keeps_the_forget_open(tmp_path, monkeypatch):
    from tests import test_forget_ownership as fo

    root = tmp_path / "memory"
    root.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    payload = {"ts": "2026-10-01T09:00:00Z", "summary": "kept open source", "harness": "codex",
               "session_id": "codex:kept-open", "transcript_range": "0:10"}
    fo.write_lines(root, [payload])
    world = {"episodes": {1: {"id": 1, "ts": fo.BASE, "summary": payload["summary"],
                              "session_id": payload["session_id"], "raw": dict(payload), "deleted": False}},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, payload)], "sources_enabled": True}
    monkeypatch.setattr(owner_direction, "scrub_words", lambda sessions: (_ for _ in ()).throw(OSError("busy")))
    out = fo.run_forget(world, 1, root)
    assert out["complete"] is False and "owner_words" in out["partial"], out
    assert any(loc["place"] == "owner words" and loc["state"] == "not checked" for loc in out["locations"])


def test_both_owner_stores_name_the_owner_words_scan():
    stores = {s.name: s for s in capture_stores.STORES}
    assert stores["owner turns"].forget == "owner words"
    assert stores["owner actions"].forget == "owner words"
    assert "owner words" in capture_stores.FORGET_SCANS
