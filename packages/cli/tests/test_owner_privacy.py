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


def test_undo_refuses_a_scheduled_purge_once_its_deadline_has_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    original_clock = owner_direction._clock
    try:
        owner_direction._clock = lambda: 1_700_000_000.0
        owner_direction._append({
            "kind": "action", "id": "purge-1", "at": "2023-11-14T00:00:00Z",
            "action": "ledger.purge", "target": "atlas#T-12", "state": "scheduled",
            "runs_at_ts": 1_700_000_000.0 - 1,
        })
        with pytest.raises(ValueError, match="already ran|deadline"):
            owner_direction.undo("purge-1", "direction")
    finally:
        owner_direction._clock = original_clock


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


def test_the_replica_refresh_job_runs_the_sweep_and_due_purges(folders, monkeypatch):
    from khipu import cli, replica_heal

    long_ago = time.time() - owner_turns.RETENTION_S - 60
    _say("s-timer", "words nobody followed up", long_ago, folders)
    ran: list = []
    monkeypatch.setattr(replica_heal, "run", lambda: {"ok": True, "action": "none"})
    real_run_due = owner_direction.run_due
    monkeypatch.setattr(owner_direction, "run_due", lambda now=None: ran.append(now) or real_run_due(now))
    with mock.patch("sys.stdout"):
        rc = cli.main(["snapshot", "heal"])
    assert rc == 0
    assert not owner_turns.session_file("s-timer").exists()
    assert ran, "the replica-refresh job did not run the due purges"


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


# ---- the purge timer --------------------------------------------------------------------------


def _scheduled_purge(world, monkeypatch, row: str = "T-12") -> tuple[dict, list]:
    from khipu import ledger

    purged: list = []
    monkeypatch.setattr(ledger, "_connect", lambda: type("C", (), {"close": lambda self: None})())
    monkeypatch.setattr(ledger, "purge", lambda conn, project, r: purged.append((project, r)) or {"ok": True})
    words = f"purge {row} from the atlas ledger"
    rc, out = world.run(["ledger", "purge", row, "--project", "atlas", "--json"], words, world.say(words))
    assert rc == 0 and out["state"] == "scheduled", out
    return owner_direction.actions()[-1], purged


def test_a_due_purge_can_not_be_cancelled_even_before_it_ran(world, monkeypatch):
    line, purged = _scheduled_purge(world, monkeypatch)
    monkeypatch.setattr(owner_direction, "_clock", lambda: time.time() + owner_direction.PURGE_DELAY_S + 60)
    with pytest.raises(ValueError, match="deadline"):
        owner_direction.undo(line["id"], "direction")
    assert owner_direction.line_state(line["id"]) == "scheduled" and purged == []


def test_undo_says_a_purge_already_ran_once_it_ran(world, monkeypatch):
    line, purged = _scheduled_purge(world, monkeypatch)
    owner_direction.run_due(now=time.time() + owner_direction.PURGE_DELAY_S + 60)
    assert purged == [("atlas", "T-12")]
    with pytest.raises(ValueError, match="already ran"):
        owner_direction.undo(line["id"], "direction")


def test_an_undo_command_for_a_due_purge_is_refused_with_the_deadline(world, monkeypatch):
    line, _purged = _scheduled_purge(world, monkeypatch)
    monkeypatch.setattr(owner_direction, "_clock", lambda: time.time() + owner_direction.PURGE_DELAY_S + 60)
    words = f"undo the purge {line['id']}"
    rc, out = world.run(["owner-direction", "undo", line["id"]], words, world.say(words))
    assert rc == 2 and out["code"] == "refused" and "deadline" in out["error"], out


def test_an_overdue_purge_runs_from_the_replica_refresh_timer(world, monkeypatch):
    from khipu import cli, replica_heal

    line, purged = _scheduled_purge(world, monkeypatch)
    monkeypatch.setattr(owner_direction, "_clock", lambda: time.time() + owner_direction.PURGE_DELAY_S + 60)
    monkeypatch.setattr(replica_heal, "run", lambda: {"ok": True, "action": "none"})
    with mock.patch("sys.stdout"):
        assert cli.main(["snapshot", "heal"]) == 0
    assert purged == [("atlas", "T-12")]
    assert owner_direction.line_state(line["id"]) == "done"
    assert json.loads(json.dumps(owner_direction.verify_log()))["ok"] is True


# ---- the second review's probes: every file a turn or an action wrote goes -------------------


HOOK = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}


def test_forget_removes_a_session_turn_file_even_if_a_crash_left_a_bad_line(folders):
    session = "forgotten-session"
    path = owner_turns.session_file(session)
    owner_turns._ensure_folder()
    path.write_text('{truncated raw owner words', encoding="utf-8")
    owner_turns.forget_sessions({session})
    assert not path.exists(), "forget left raw owner words in its matching turn file"


def _crash_temps(words: str, session: str) -> list:
    owner_turns._ensure_folder()
    brief_tmp = owner_turns.folder() / ".briefs.jsonl.123.tmp"
    brief_tmp.write_text(json.dumps({"parent": session, "head": words, "sha": "digest"}) + "\n", encoding="utf-8")
    turn_tmp = owner_turns.folder() / ".other-session.jsonl.77.tmp"
    turn_tmp.write_text('{"session": "other-session", "text": "' + words[:12], encoding="utf-8")
    pending_tmp = owner_direction._pending_path().with_name(".owner-direction-pending.json.123.tmp")
    pending_tmp.parent.mkdir(parents=True, exist_ok=True)
    pending_tmp.write_text(json.dumps([{"session": session, "words": words}]) + "\n", encoding="utf-8")
    action_tmp = owner_direction.log_path().with_name(".owner-actions.jsonl.123.tmp")
    action_tmp.write_text(json.dumps({"session": session, "words": words}) + "\n", encoding="utf-8")
    return [brief_tmp, turn_tmp, pending_tmp, action_tmp]


def _age(paths, seconds: float) -> None:
    then = time.time() - seconds
    for path in paths:
        os.utime(path, (then, then))


def test_forget_removes_crash_temps_for_briefs_pending_and_action_words(folders):
    session = "forgotten-session"
    words = "raw owner words from an interrupted private rewrite"
    temps = _crash_temps(words, session)
    _age(temps, 3600)
    out = forget._owner_words_scan(session, gateway=False)
    left = [p.name for p in temps if p.exists()]
    assert out["ok"] is True and not left, (out, left)


def test_a_fresh_temp_that_may_hold_the_words_keeps_the_forget_open(folders):
    session = "forgotten-session"
    owner_turns._ensure_folder()
    temp = owner_turns.folder() / ".briefs.jsonl.999.tmp"
    temp.write_text('{"parent": "forg', encoding="utf-8")  # cut before it names a session
    out = forget._owner_words_scan(session, gateway=False)
    assert out["ok"] is False or not temp.exists(), out


def test_the_expiry_sweep_removes_crash_temps_and_truncated_files(folders):
    words = "raw owner words from an interrupted private rewrite"
    temps = _crash_temps(words, "some-session")
    _age(temps, owner_turns.RETENTION_S + 10)
    truncated = owner_turns.session_file("crashed")
    truncated.write_text('{"ts": 1, "text": "raw owner words', encoding="utf-8")
    out = owner_turns.sweep()
    assert out["ok"] is True, out
    left = [p.name for p in temps + [truncated] if p.exists()]
    assert not left, left


def test_every_file_owner_direction_writes_is_in_the_inventory(folders, monkeypatch):
    """Every file the recorder, the check, the log and the widenings write
    matches one of ``owner_files.PATTERNS``, so forget and the sweep (which
    list by those patterns) can never miss one by name."""
    from khipu import owner_files

    session = "inventory-session"
    owner_turns.record({"session_id": session, "prompt": "pause Codex for review", "cwd": "/w"}, HOOK)
    owner_turns.note_brief("a delegated brief with the owner's words in it", parent=session, harness="claude_code")
    owner_turns.write_settings({"enabled": True})
    owner_direction._save_pending([{"session": session, "words": "x y z", "ts": time.time()}])
    owner_direction._append({"kind": "action", "id": "inv-1", "session": session, "action": "route.pause",
                             "target": "codex|", "words": "pause Codex for review"})
    owner_direction.scrub_words({session})
    _crash_temps("words", session)
    roots = [owner_turns.folder(), owner_direction.log_path().parent]
    written = sorted({str(p) for root in roots for p in root.rglob("*") if p.is_file()
                      and (root == owner_turns.folder() or p.name.lstrip(".").startswith(("owner-", "owner_")))})
    listed = {str(p) for p in owner_files.listed()}
    assert written and set(written) <= listed, sorted(set(written) - listed)
    kinds = {owner_files.kind_of(p) for p in owner_files.listed()}
    assert {"turns", "briefs", "settings", "pending", "log", "head", "lock", "temp"} <= kinds, kinds


def test_normal_forget_scrubs_words_and_keeps_the_action_log_chain_and_undo_state(folders):
    session = "normal-forget"
    words = "pause Codex for review"
    owner_turns.record({"session_id": session, "prompt": words, "cwd": str(folders)}, HOOK, now=1700000000)
    owner_turns.note_brief("a delegated brief containing the owner words", parent=session, harness="claude_code",
                           now=1700000000)
    owner_direction._save_pending([{"session": session, "words": words, "ts": 1700000000}])
    owner_direction._append({"kind": "action", "id": "forget-log", "session": session,
                             "action": "route.pause", "target": "codex|", "state": "done", "words": words})
    owner_turns.forget_sessions({session})
    owner_direction.forget_pending({session})
    owner_direction.scrub_words({session})
    assert not owner_turns.session_file(session).exists()
    assert words not in (owner_turns.folder() / "briefs.jsonl").read_text(encoding="utf-8")
    assert words not in owner_direction.log_path().read_text(encoding="utf-8")
    assert owner_direction.verify_log()["first_bad"] is None
    line = owner_direction.actions()[0]
    assert line["words"] is None and line["words_sha"]


def test_retention_read_prunes_expired_words_but_keeps_fresh_words(folders):
    at = 1700000000
    session = "mixed-retention"
    owner_turns.record({"session_id": session, "prompt": "expired owner words", "cwd": str(folders)}, HOOK, now=at)
    owner_turns.record({"session_id": session, "prompt": "fresh owner words", "cwd": str(folders)}, HOOK,
                       now=at + owner_turns.RETENTION_S - 1)
    got = owner_turns.turns(session, now=at + owner_turns.RETENTION_S + 1)
    raw = owner_turns.session_file(session).read_text(encoding="utf-8")
    assert [turn["text"] for turn in got] == ["fresh owner words"]
    assert "expired owner words" not in raw and "fresh owner words" in raw


def test_purge_cannot_run_early_or_be_cancelled_after_deadline(folders, monkeypatch):
    now = 1700000000.0
    monkeypatch.setattr(owner_direction, "_clock", lambda: now)
    line_id = "purge-1"
    owner_direction._append({"kind": "action", "id": line_id, "action": "ledger.purge",
                             "target": "project#row-1", "state": "scheduled",
                             "runs_at_ts": now + owner_direction.PURGE_DELAY_S})
    assert owner_direction.run_due(now=now + owner_direction.PURGE_DELAY_S - 1) == []
    monkeypatch.setattr(owner_direction, "_clock", lambda: now + owner_direction.PURGE_DELAY_S + 1)
    with pytest.raises(ValueError, match="deadline"):
        owner_direction.undo(line_id, "direction")
