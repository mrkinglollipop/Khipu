"""A stored commitment's owner is a fact of the row: every Mac reads the same
one, whatever aliases that Mac has configured (finding 1 of the 2026-11-05
review: 9,066 versus 3,440 user-owned open rows from one hub, because a read
re-derived each owner from its text with local identity)."""
from __future__ import annotations

import os
import subprocess

import pytest

from khipu import commitments as co

# Rows as the hub stores them. Each text names Matt; the stored owner is what
# capture decided, and no alias set may change it.
ROWS = [
    {"id": 1, "text": "Matt to decide whether 0.3.17 ships with the seal fix", "kind": "question",
     "owner": "user", "origin": "conversation"},
    {"id": 2, "text": "Matthew must merge the stacked PRs", "kind": "followup", "owner": "user",
     "origin": "conversation"},
    {"id": 3, "text": "Matt to decide the rollout order", "kind": "followup", "owner": "agent",
     "owner_label": "orchestrator", "origin": "job"},
    {"id": 4, "text": "Should the cache use a shorter window?", "kind": "question", "owner": "assistant",
     "origin": "conversation"},
    {"id": 5, "text": "Matt reviews the stack when it lands", "kind": "followup", "owner": "assistant",
     "origin": "conversation"},
    {"id": 6, "text": "Rebuild the search index", "kind": "followup", "owner": "user", "origin": "conversation"},
    {"id": 7, "text": "Matt to approve the release", "kind": "followup", "owner": "user",
     "owner_reassigned_at": "2026-10-20T10:00:00+00:00", "origin": "conversation"},
]
STORED = {1: "user", 2: "user", 3: "agent", 4: "assistant", 5: "assistant", 6: "user", 7: "user"}


def _machine(monkeypatch, tmp_path, *, aliases: str, user: str, git_name: str | None) -> None:
    """One Mac's identity: configured aliases, ``$USER`` and the git name."""
    gitconfig = tmp_path / f"gitconfig-{user}-{git_name}"
    gitconfig.write_text(f"[user]\n\tname = {git_name}\n" if git_name else "")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("USER", user)
    monkeypatch.setenv("KHIPU_USER_ALIASES", aliases)
    co.reset_user_patterns()


MACHINES = {
    "main-mac": dict(aliases="matt,matthew", user="matthewschwartz", git_name="Matthew Schwartz"),
    "m1-no-aliases": dict(aliases="", user="schwartz", git_name=None),
    "another-name": dict(aliases="priya", user="priya", git_name="Priya Rao"),
}


@pytest.fixture(autouse=True)
def _reset():
    yield
    co.reset_user_patterns()


@pytest.mark.parametrize("machine", sorted(MACHINES))
def test_a_read_gives_the_stored_owner_on_every_mac(machine, monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path, **MACHINES[machine])
    got = {r["id"]: co.row_owner(dict(r))[0] for r in ROWS}
    assert got == STORED
    assert co.row_owner(dict(ROWS[2])) == ("agent", "orchestrator")


def test_the_owed_list_is_the_same_on_every_mac(monkeypatch, tmp_path):
    seen = []
    for machine in sorted(MACHINES):
        _machine(monkeypatch, tmp_path, **MACHINES[machine])
        rows = co.resolve_owed_rows([dict(r, status="open") for r in ROWS])
        seen.append([(r["id"], r["owner"], r["owner_label"], r["priority"]) for r in rows])
    assert seen[0] == seen[1] == seen[2]
    assert {i: o for i, o, _, _ in seen[0]} == STORED


def test_the_text_never_overrules_the_stored_owner(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path, **MACHINES["main-mac"])
    # Stored for the assistant, worded as a question for Matt: still the assistant's.
    assert co.row_owner(dict(ROWS[3])) == ("assistant", None)
    assert co.row_owner(dict(ROWS[4])) == ("assistant", None)


def test_no_machine_detail_is_a_source_of_the_owner_aliases(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path, aliases="", user="riveras", git_name="Sam Rivera")
    assert set(co.user_aliases()) == {"matt", "matthew", *co._GENERIC_USER_OWNER_WORDS}
    git = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True, text=True).stdout
    assert git.strip() == "Sam Rivera", "the fixture's git name is in place"
    assert "sam" not in co.user_aliases() and "riveras" not in co.user_aliases()


@pytest.mark.parametrize("stored", [None, "", "Peer 1", "matt", "someone"])
def test_a_row_with_no_recognisable_owner_is_the_users_on_every_mac(stored, monkeypatch, tmp_path):
    row = {"id": 9, "text": "Rebuild the search index", "kind": "followup", "owner": stored}
    for machine in sorted(MACHINES):
        _machine(monkeypatch, tmp_path, **MACHINES[machine])
        assert co.row_owner(dict(row)) == ("user", None)


def test_a_legacy_assistant_word_is_the_assistants(monkeypatch, tmp_path):
    _machine(monkeypatch, tmp_path, **MACHINES["m1-no-aliases"])
    assert co.row_owner({"text": "Rebuild the index", "owner": "ASSISTANT"}) == ("assistant", None)
    assert co.row_owner({"text": "Rebuild the index", "owner": "Claude"}) == ("assistant", None)


def test_capture_still_decides_the_owner_from_the_aliases_it_has(monkeypatch, tmp_path):
    """The aliases matter once, when an item is written; the owner it gets is then stored."""
    _machine(monkeypatch, tmp_path, **MACHINES["main-mac"])
    assert co.resolve_owner("Matt to decide whether the release ships") == "user"
    # A configured name adds to Matt's.
    _machine(monkeypatch, tmp_path, **MACHINES["another-name"])
    assert co.resolve_owner("Priya to decide whether the release ships") == "user"
    assert co.resolve_owner("Matt to decide whether the release ships") == "user"


def test_a_close_is_refused_for_an_assistant_row_whose_words_name_the_user_as_the_actor(monkeypatch, tmp_path):
    """The display owner is the stored one, but an automatic close stays conservative,
    the same on every Mac."""
    row = {"text": "Matt reviews and merges the stack himself", "kind": "followup", "owner": "ASSISTANT",
           "origin": "conversation"}
    plain = {"text": "Rebuild the search index", "kind": "followup", "owner": "ASSISTANT",
             "origin": "conversation"}
    for machine in sorted(MACHINES):
        _machine(monkeypatch, tmp_path, **MACHINES[machine])
        assert co.row_owner(dict(row)) == ("assistant", None)
        assert co.row_guard(dict(row), "session-ended")
        assert co.row_guard(dict(plain), "session-ended") is None
        # An owner's own reassignment to the assistant is honoured.
        assert co.row_guard(dict(row, owner_reassigned_at="2026-10-20T10:00:00+00:00"), "session-ended") is None


def test_an_agent_label_is_the_stored_one_never_read_from_the_text():
    row = {"owner": "agent", "owner_label": None, "text": "The reviewer must check the release branch"}
    assert co.row_owner(row) == ("agent", co.ORCHESTRATOR)
    assert co.row_owner(dict(row, owner_label="builder")) == ("agent", "builder")


def test_capture_gives_one_owner_on_every_mac(monkeypatch, tmp_path):
    """Matt's names are built in, so a Mac with no configured aliases writes what the main Mac writes."""
    got = set()
    for machine in sorted(MACHINES):
        _machine(monkeypatch, tmp_path, **dict(MACHINES[machine], aliases=MACHINES[machine]["aliases"]))
        got.add(co.resolve_owner_and_label("Matt to decide whether the release ships", declared="user"))
        got.add(co.resolve_owner_and_label("Matthew to decide whether the release ships", declared="user"))
    assert got == {("user", None)}
