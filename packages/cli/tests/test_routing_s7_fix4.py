"""Routing slice 7 fix round 4: strict-scrub cost shape, discovery-store
completeness, and child capability."""
from __future__ import annotations

import json
import os
from datetime import timedelta

import pytest

from khipu import agent_view, discover, routing, routing_delivery
from tests.test_routing_s7_fix1 import _configure, _route, home  # noqa: F401

NAME = "codex-OutsideLL"
AUTH = {"kind": "conversation", "harness": "codex", "session": "s"}


def _seed_lane(home):
    _configure(home, routing_lane_map={"runnerx": {NAME: "NoSuch"}})
    routing.report(state="limited", runner="runnerx", lane=NAME, until="1h", author=AUTH)


def _write_store(outside):
    path = discover.store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"id": "c1", "cli": "codex", "identity": f"path:{outside}",
           "sources": ["names"], "abs": str(outside)}
    path.write_text(json.dumps({"version": discover.STORE_VERSION, "candidates": {"c1": rec}}))
    return path


@pytest.mark.parametrize("mode", ["valid", "corrupt", "unreadable", "missing"])
def test_discovery_store_completeness_gates_the_pull(home, monkeypatch, mode):
    monkeypatch.setenv("CODEX_THREAD_ID", "s")
    outside = home["tmp"] / "elsewhere" / ".codex-OutsideLL"
    outside.mkdir(parents=True)
    path = _write_store(outside)
    try:
        if mode == "corrupt":
            path.write_text("{not json")
        elif mode == "unreadable":
            os.chmod(path, 0)
        elif mode == "missing":
            path.unlink()
        _seed_lane(home)
        body = _route()
        blob = json.dumps({"routing_changes": body.get("routing_changes"),
                           "delivery": body.get("delivery")})
        if mode in ("corrupt", "unreadable"):
            # Never read as an empty complete store: the pull is withheld.
            assert body.get("available") is False, body
        elif mode == "valid":
            assert body.get("available") is not False, body
            assert NAME not in blob, blob
        else:
            # No store file at all is a legitimate empty store.
            assert body.get("available") is not False, body
            assert NAME in blob, blob
    finally:
        if path.exists():
            os.chmod(path, 0o600)


def test_child_of_a_hooked_root_is_pull_only():
    env = {"CLAUDE_CODE_SESSION_ID": "abc", "CLAUDECODE": "1"}
    child = routing_delivery.canonical_identity(env, explicit="claude:abc/c")
    assert child["capability"] == "pull_only" and child["child"] is True
    root = routing_delivery.canonical_identity(env)
    assert root["capability"] == "hooked" and root["child"] is False


def test_stalled_child_pull_health_turns_red(home, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("CLAUDECODE", "1")
    routing.add_rule("owner debt", via="app")
    health = []
    for _ in range(4):
        health.append(_route(session_id="claude:abc/c")["delivery"]["health"])
        home["clock"].now += timedelta(hours=2)
    assert health[-1] != health[0] or health[-1] == "red", health


def test_scrub_strict_still_withholds_secrets_and_keys():
    secret = "sk-proj-" + "A" * 32
    note = "owner words " + "n" * 200
    assert agent_view.scrub_strict({"t": f"use {secret} now"}, keep=[note]) == {"t": agent_view.WITHHELD}
    assert agent_view.scrub_strict({"t": "codex|/some/home x"}) == {"t": agent_view.WITHHELD}
    # A secret inside a complete owner phrase is cut, the words stay.
    owned = f"Use key {secret} for nothing"
    out = agent_view.scrub_strict({"t": owned}, keep=[owned])
    assert secret not in out["t"] and "for nothing" in out["t"], out


def test_scrub_strict_owner_replacements_are_cached_per_word_set():
    words = ["owner words " + "n" * 500]
    first = agent_view._owner_token_replacements(words)
    assert agent_view._owner_token_replacements(list(words)) is first
