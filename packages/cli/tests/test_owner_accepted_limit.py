"""Owner direction: the same-user limit Matt accepted (docs/plans/2026-10-16-owner-direction.md,
section 2.6). The check stops ordinary jobs, each running its own harness, from making an owner
change: a ``claude -p`` job, a ``codex exec`` job, a T3 delegate and a launchd job are refused
whether they carry their own session id or kept the owner's. A same-user process that forges a
harness (the sixth review's Node issuer and borrowed ancestry) is outside what the check can
tell apart, so its change must always be visible: a log line with its Undo, a row in the app's
Owner actions, and a line in the next routing block.

Hermetic: the ``world`` fixture from tests/test_owner_direction.py; process ancestry is faked
through ``owner_direction._caller_chain``."""
from __future__ import annotations

import pytest

from khipu import owner_direction, owner_turns, routing
from tests import test_owner_direction as base
from tests.conftest import FAKE_HOOK_ISSUER

world = base.world  # the fixture, shared
_state = base._state

# The owner's conversation: the harness whose prompt hook recorded his turn.
OWNER_HARNESS = (FAKE_HOOK_ISSUER["pid"], FAKE_HOOK_ISSUER["start"], FAKE_HOOK_ISSUER["exe"])

# Each ordinary job's ancestry, nearest first: its own harness sits between its
# command and anything the owner's conversation started.
JOBS = {
    "claude -p": [(5000, 2.0, "zsh"), (5001, 2.0, "claude"), OWNER_HARNESS],
    "codex exec": [(6000, 3.0, "bash"), (6001, 3.0, "codex"), (6002, 3.0, "node"), OWNER_HARNESS],
    "T3 delegate": [(7000, 4.0, "sh"), (7001, 4.0, "claude"), (7002, 4.0, "node"), (7003, 4.0, "T3 Code")],
    "launchd job": [(8000, 5.0, "sh"), (8001, 5.0, "python3.12"), (1, 0.5, "launchd")],
}
# The id each job's own harness gives it (a launchd job has none).
OWN_ID = {"claude -p": ("CLAUDE_CODE_SESSION_ID", "s-job"), "codex exec": ("CODEX_THREAD_ID", "t-job"),
          "T3 delegate": ("CLAUDE_CODE_SESSION_ID", "s-t3-delegate"), "launchd job": None}


def _nothing_changed() -> None:
    assert _state("codex|") != "paused"
    assert not [x for x in owner_direction.actions() if x["state"] in ("applied", "unconfirmed")]


@pytest.mark.parametrize("job", sorted(JOBS))
def test_an_ordinary_job_with_its_own_session_is_refused(world, job):
    world.say("pause Codex for now")
    world.mp.setattr(owner_direction, "_caller_chain", lambda: JOBS[job])
    if OWN_ID[job]:
        var, sid = OWN_ID[job]
        world.be(sid, var=var)
    else:
        world.be(None)
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 2 and out["code"] == "no_recorded_turn", out
    _nothing_changed()


@pytest.mark.parametrize("job", sorted(JOBS))
def test_an_ordinary_job_that_kept_the_owners_session_id_is_refused(world, job):
    world.say("pause Codex for now")
    world.mp.setattr(owner_direction, "_caller_chain", lambda: JOBS[job])
    world.be(base.OWNER, var="KHIPU_SESSION_ID")
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 2 and out["code"] == "not_from_the_hook", out
    _nothing_changed()


def test_a_claude_p_job_turn_is_never_eligible(world):
    """A ``claude -p`` job's own prompt hook records its brief under the
    ``sdk-cli`` entrypoint, which is not a trusted front end."""
    world.say("pause Codex for now", session="s-job", entry="sdk-cli")
    world.mp.setattr(owner_direction, "_caller_chain", lambda: [OWNER_HARNESS])
    world.be("s-job")
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 2 and out["code"] == "no_recorded_turn", out
    _nothing_changed()


def test_a_forged_harness_change_is_always_visible_and_undoable(world, khipu_app):
    """The sixth review's P1 probes, as the accepted limit: a same-user Node
    process runs the stock shim with invented words, so the turn names Node
    as its issuer and Node is the nearest harness above the call. The check
    can't refuse it; the change is in the log with its Undo, in the app's
    Owner actions, and in the next routing block, and Undo reverses it."""
    marker = base._marker()
    node = {"pid": 9100, "start": 6.0, "exe": "node"}
    owner_turns.record({"session_id": "node-unattended", "prompt": "pause Codex", "cwd": str(world.tmp)},
                       {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}, issuer=node)
    world.mp.setattr(owner_direction, "_caller_chain", lambda: [(9101, 6.0, "sh"), (9100, 6.0, "node")])
    world.be("node-unattended")
    rc, out = world.run(["route", "pause", "Codex"], True)
    assert rc == 0 and _state("codex|") == "paused", out
    # The log, with its Undo.
    (line,) = owner_direction.actions()
    assert (line["action"], line["auth"], line["state"], line["session"]) == (
        "route.pause", "direction", "applied", "node-unattended")
    assert not line.get("no_undo") and line["before"] == {"paused": False, "until": None}
    assert owner_direction.verify_log()["ok"] is True
    # The app's Owner actions list (what Settings reads), Undo offered.
    rc, listed = world.run(["owner-direction", "actions", "--json"])
    assert rc == 0 and [(x["id"], x.get("no_undo")) for x in listed["actions"]] == [(line["id"], None)]
    # The next routing block.
    push = routing.prompt_push(marker, budget=5.0)
    assert push["kind"] == "changes" and "Paused Codex by your direction" in push["text"]
    assert "(undo in Settings)" in push["text"]
    # The owner's Undo in the app reverses it.
    khipu_app.as_app()
    rc, undone = world.run(["owner-direction", "undo", "--from-app", line["id"]])
    assert rc == 0 and _state("codex|") != "paused", undone
