"""What real use on the second Mac still showed (round 4): health rows that read an
idle or ended conversation as a delivery problem."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from khipu import routing_health

T0 = datetime(2026, 11, 5, 13, 0, 28, tzinfo=timezone.utc)


def _iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def _state(*, expired=2, flight_until=None):
    entry = {"entity": "rule:r", "revision": "r1", "text": "a routing line", "offset": 0, "shown_offset": None,
             "acked": False, "owed_point": 1, "bound": 10, "expired": expired}
    state = {"entries": {"rule:r": entry}, "delivery_points": 0, "last_delivery_point": _iso(T0), "flight": {}}
    if flight_until is not None:
        state["flight"] = {"until": _iso(flight_until),
                           "out": {"routing_changes": {"items": [{"entity": "rule:r", "revision": "r1"}]}}}
    return state


def test_a_conversation_still_prompting_with_lapsed_leases_is_red():
    health = routing_health.component_health(_state(), T0 + timedelta(minutes=2))
    assert health["status"] == "red" and "lease-expired-twice" in health["reasons"], health
    assert not health.get("idle")


def test_the_same_leases_in_a_conversation_quiet_for_hours_are_not_a_problem():
    health = routing_health.component_health(_state(), T0 + timedelta(hours=4))
    assert health["status"] == "green" and health["reasons"] == [], health
    assert health["idle"] is True and health["quiet_reasons"] == ["lease-expired-twice"]
    assert health["owed"] == 1  # what it never got stays visible


def test_a_lease_that_lapsed_after_the_last_prompt_is_not_a_second_failure_once_idle():
    state = _state(expired=1, flight_until=T0 + timedelta(minutes=1))
    assert routing_health.component_health(state, T0 + timedelta(minutes=2))["status"] == "red"
    assert routing_health.component_health(state, T0 + timedelta(hours=2))["status"] == "green"


def test_a_damaged_ledger_stays_unknown_however_idle():
    state = _state()
    state["entries"]["rule:r"]["offset"] = -1
    health = routing_health.component_health(state, T0 + timedelta(hours=4))
    assert health["status"] == "unknown" and health["missing"], health


# ---- the outbox row: old held legacy-key collisions --------------------------------------------


def _held(outbox, name, *, error, age_h, conflict=True):
    import json
    import os
    import time
    path = outbox.dead_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    job = {"payload": {"ts": "2026-10-31T00:00:00Z", "summary": "x", "session_id": "cursor:s"},
           "queued_at": "2026-10-31T00:00:00+00:00", "attempts": 25, "last_error": error}
    if conflict:
        job["evidence_conflict"] = True
    path.write_text(json.dumps(job))
    then = time.time() - age_h * 3600
    os.utime(path, (then, then))


def _outbox_ok(status):
    return status.get("pending") == 0 and not status.get("dead_waiting", status.get("dead"))


LEGACY = "CheckViolation: CaptureEvidenceConflict: legacy episode key conflicts with distinct evidence on episode 93"


def test_old_held_legacy_key_collisions_are_said_plainly_and_do_not_keep_the_outbox_red(tmp_path, monkeypatch):
    from khipu import outbox
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path))
    for i in range(3):
        _held(outbox, f"c{i}.json", error=LEGACY, age_h=40 + i)
    st = outbox.status()
    assert st["dead"] == 3 and st["evidence_conflicts"] == 3 and st["legacy_key_conflicts_old"] == 3
    assert "3 old captures" in st["plain"] and _outbox_ok(st)


def test_a_new_legacy_key_collision_is_red(tmp_path, monkeypatch):
    from khipu import outbox
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path))
    _held(outbox, "new.json", error=LEGACY, age_h=2)
    st = outbox.status()
    assert st["legacy_key_conflicts_old"] == 0 and not _outbox_ok(st)


def test_an_old_legacy_collision_beside_a_newer_death_is_red(tmp_path, monkeypatch):
    from khipu import outbox
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path))
    _held(outbox, "old.json", error=LEGACY, age_h=48)
    _held(outbox, "new.json", error="ValueError: payload rejected", age_h=1, conflict=False)
    assert not _outbox_ok(outbox.status())


def test_any_other_old_evidence_conflict_stays_red(tmp_path, monkeypatch):
    from khipu import outbox
    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path))
    _held(outbox, "other.json", error="held: forget conflict", age_h=100)
    st = outbox.status()
    assert st["legacy_key_conflicts_old"] == 0 and not _outbox_ok(st)


# ---- the owed list: an instruction to run or read something is not the user's ------------------


import pytest

from khipu import commitments as co


@pytest.mark.parametrize("text,kind", [
    ("Run the dry-run on the M1 Mac to verify the breakdown of the 930 unverified `late_repeat` outbox items.", "followup"),
    ("Run `khipu doctor` on the secondary Mac and read `outbox.dead_detail` and the liveness reasons.", "question"),
    ("Look at the 38 routing changes not delivered to subagent conversation agent:a2d (lease expired twice).", "followup"),
    ("Verify against live T3 whether stored prompt text matches the hook's sha256.", "followup"),
])
def test_a_bare_instruction_to_run_or_read_something_is_not_the_users(text, kind):
    assert co.resolve_owner(text, kind=kind, declared="user") == co.OWNER_ASSISTANT


@pytest.mark.parametrize("text,kind", [
    ("Decide whether to hold the M1 install until the swap clears.", "question"),
    ("Provide the second Mac's hostname before the soak can start.", "followup"),
    ("Enable routing copy in the app on each Mac.", "followup"),
    ("Approve the Grok 90% rule", "question"),
    ("Should we run the dry-run on the M1 Mac?", "question"),
    ("Delete /Volumes/KhipuBuild/wt-old; the safety check refused it.", "followup"),
])
def test_what_a_person_must_decide_or_do_stays_theirs(text, kind):
    assert co.resolve_owner(text, kind=kind, declared="user") == co.OWNER_USER


# ---- routing at a prompt: short prompts lost to a 50 ms budget -------------------------------


def _answer_in_the_pipe(read_seconds, *, budget):
    """A routing child that was ready now and took ``read_seconds`` to read, as
    the pipe a prompt with no search of its own (nothing else to wait on) collects."""
    import json
    import os
    import time

    from khipu import recall_prompt

    began = time.monotonic()
    done = began + read_seconds
    answer = {"kind": "none", "version": 1, "text": "", "generation": [], "done": done}
    read_fd, write_fd = os.pipe()
    os.write(write_fd, f"{began!r} {budget!r}\n".encode() + json.dumps(answer).encode() + b"\n")
    os.close(write_fd)
    at = recall_prompt._RoutingAtPrompt()
    at.fd, at.status = read_fd, "skipped:budget"
    at.forked = began
    at.cap = began + 1.0
    return at


def test_the_default_budget_takes_a_read_as_long_as_the_measured_one(tmp_path, monkeypatch):
    from khipu import routing

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    budget = routing.prompt_budget()
    assert budget >= 0.15, "104 ms p50 and 182 ms p90 were measured for the read at load 19-27"
    at = _answer_in_the_pipe(0.12, budget=budget)  # a 120 ms read, no search to hide it behind
    assert at.collect() is not None


def test_a_50_ms_budget_loses_that_same_read(tmp_path, monkeypatch):
    # The old default, kept as evidence of what the test above guards against.
    at = _answer_in_the_pipe(0.12, budget=0.05)
    assert at.collect() is None


# ---- routing at a prompt: no Keychain process per prompt ---------------------------------------


def test_provider_key_presence_is_asked_once_a_minute_inside_a_prompt_read(tmp_path, monkeypatch):
    from khipu import keychain

    monkeypatch.setenv("HOME", str(tmp_path))
    asked = []
    resolve = lambda: asked.append(1) or "k"  # noqa: E731
    with keychain.prompt_scope():
        assert keychain.key_present("glm", resolve) and keychain.key_present("glm", resolve)
    assert len(asked) == 1
    stored = (tmp_path / "Library/Caches/Khipu/login-usage/key-presence.json").read_text()
    assert '"k"' not in stored  # the answer is kept, never the key
    # Anywhere else it is asked every time: the owner's own reads stay exact.
    assert keychain.key_present("glm", resolve) and keychain.key_present("glm", resolve)
    assert len(asked) == 3


def test_a_presence_answer_older_than_a_minute_is_asked_again(tmp_path, monkeypatch):
    import time

    from khipu import keychain

    monkeypatch.setenv("HOME", str(tmp_path))
    asked = []
    with keychain.prompt_scope():
        keychain.key_present("glm", lambda: asked.append(1))
        later = time.time() + keychain.PRESENCE_TTL_S + 1
        monkeypatch.setattr(keychain.time, "time", lambda: later)
        keychain.key_present("glm", lambda: asked.append(1))
    assert len(asked) == 2


def test_storing_a_key_drops_the_remembered_answer(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from khipu import keychain

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(keychain, "keychain_available", lambda: True)
    monkeypatch.setattr(keychain, "_security", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    with keychain.prompt_scope():
        assert not keychain.key_present("glm", lambda: None)
        keychain.set_password("glm_api_key", "secret-value")
        assert keychain.key_present("glm", lambda: "now there")


def test_the_prompt_read_does_not_start_a_security_process_for_provider_keys(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    from khipu import keychain, usage

    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(keychain, "_security", lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
        AssertionError("a Keychain process on the prompt path")))
    for name in ("resolve_glm_key", "resolve_deepseek_key", "resolve_kimi_key"):
        monkeypatch.setattr(keychain, name, lambda: None)
    asked = []
    monkeypatch.setattr(usage, "_glm_key", lambda: asked.append("glm") or None)
    monkeypatch.setattr(usage, "_deepseek_key", lambda: asked.append("deepseek") or None)
    with keychain.prompt_scope():
        for _ in range(3):
            usage.available("glm")
            usage.available("deepseek")
            usage.deepseek_spend_total(datetime.now(timezone.utc), 7, 10.0)
    assert sorted(asked) == ["deepseek", "glm"] and calls == []
