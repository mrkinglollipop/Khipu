"""An override whose review day has come waits in Needs you, in plain words, and
one yes keeps it for another week while one no ends it. Every account is named by
its one id, so an override saved under an older login-folder key still applies."""
import os
import pty
import sys
from datetime import timedelta

import pytest

from khipu import cli, conversation_overrides as overrides, needs_you, routing
from tests.test_t3_owner_confirm import world  # noqa: F401

SUBJECT = {"kind": "session", "id": "review-me", "harness": "codex", "project": "p", "mac": "m1"}


def _due_override(monkeypatch):
    monkeypatch.setattr(overrides, "REVIEW_DAYS", -1)
    row = overrides.grant({"account": "Codex", "indefinite": True, "effect": "ahead_of_pace",
                           "subject": SUBJECT, "request": "run ahead"})
    monkeypatch.setattr(overrides, "REVIEW_DAYS", 7)
    return row


def _review_items():
    return [i for i in needs_you.waiting(record_read=False)["items"] if i["kind"] == "override_review"]


def _terminal(monkeypatch, args):
    main, sub = pty.openpty()
    try:
        os.write(main, b"yes\n")
        with os.fdopen(sub, "r") as terminal:
            monkeypatch.setattr(sys, "stdin", terminal)
            return cli.cmd_needs_you(cli.build_parser().parse_args(["needs-you", *args]))
    finally:
        os.close(main)


def test_a_due_override_appears_in_needs_you_in_plain_words(world, monkeypatch):
    row = _due_override(monkeypatch)
    (item,) = _review_items()
    assert item["sort"] == "confirmation" and item["key"] == f"override-review:{row['id']}"
    assert "run ahead of pace on Codex with no end" in item["words"] and "time to look at it again" in item["words"]
    assert item["settle"]["confirm"]["action"] == "route.override.keep"
    assert item["settle"]["undo"]["action"] == "route.override.end"


def test_an_override_not_yet_due_is_not_listed(world):
    overrides.grant({"account": "Codex", "indefinite": True, "effect": "ahead_of_pace", "subject": SUBJECT, "request": "x"})
    assert _review_items() == []


def test_one_yes_in_the_terminal_keeps_it_for_another_week(world, khipu_app, monkeypatch, capsys):
    row = _due_override(monkeypatch)
    khipu_app.as_shell()
    for key in ("CLAUDE_CODE_SESSION_ID", "KHIPU_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(key, raising=False)
    assert _terminal(monkeypatch, ["--yes", f"override-review:{row['id']}"]) == 0
    (kept,) = overrides.list_overrides()
    assert kept["id"] == row["id"] and overrides._parse(kept["review_at"]) > routing._now() + timedelta(days=6)
    assert _review_items() == []


def test_one_no_in_the_terminal_ends_it(world, khipu_app, monkeypatch, capsys):
    row = _due_override(monkeypatch)
    khipu_app.as_shell()
    for key in ("CLAUDE_CODE_SESSION_ID", "KHIPU_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(key, raising=False)
    assert _terminal(monkeypatch, ["--no", f"override-review:{row['id']}"]) == 0
    assert overrides.list_overrides() == []


def test_an_override_saved_under_the_old_key_applies_to_the_one_account_id(world):
    row = overrides.grant({"account": "Codex", "until": routing._iso(routing._now() + timedelta(hours=3)),
                           "effect": "ahead_of_pace", "subject": SUBJECT, "request": "x"})
    ident = routing.account_id("codex|")
    assert row["account"] == ident and "|" not in ident
    # An older record saved under the login-folder key reads as the same account.
    assert overrides._ident("codex|") == ident
    who = {"subject": SUBJECT, "ancestors": [], "dispatched": False}
    assert overrides.matches(who, routing._now()).get(ident, {}).get("id") == row["id"]
