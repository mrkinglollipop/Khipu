"""``khipu_route`` and ``khipu route --json`` with ``account`` answer that
account's part, not the whole record: its entry, its priority entry, and the
holds and lanes that name it. What is left out is named in ``omitted``."""
from __future__ import annotations

import json

from khipu import cli, routing
from tests.test_routing_mcp import _call, _route, home  # noqa: F401 - the fixture


def _record() -> None:
    routing.add_rule("Every result that is acted on gets a re-check from another model family.", via="app")
    routing.set_note("Builds go to Codex this week.", via="app")
    routing.set_priority([{"account": "Codex", "role": "builds"}, {"account": "Grok", "role": "art"},
                          {"account": "Claude"}], via="app", expect=routing.priority_version())
    routing.add_hold("keep everything calm", reason="other", scope="all", owner=True, via="app")
    routing.add_hold("keep Codex for reviews", reason="other", scope="account", account="Codex", owner=True, via="app")
    routing.add_hold("keep Grok for art", reason="other", scope="account", account="Grok", owner=True, via="app")


def test_account_and_full_answers_that_accounts_part_only(home):
    _record()
    whole = _route(full=True)
    part = _route(account="Codex", full=True)
    assert [a["label"] for a in part["accounts"]] == ["Codex"]
    assert [e["label"] for e in part["priority"]["entries"]] == ["Codex"]
    assert part["priority"]["entries"][0]["role"] == "builds" and "line" not in part["priority"]
    assert sorted(h["text"] for h in part["holds"]) == ["keep Codex for reviews", "keep everything calm"]
    for gone in ("rules", "note", "providers", "sources"):
        assert gone not in part and gone in part["omitted"], gone
    assert part["narrowed_to"] == "Codex" and "view=<section>" in part["omitted_note"]
    assert len(json.dumps(part)) < len(json.dumps(whole)) * 0.6
    assert part["version"] == whole["version"] and "delivery" in part


def test_account_alone_is_the_same_narrow_answer_and_a_view_still_reads_a_whole_section(home):
    _record()
    assert _route(account="Codex")["omitted"] == _route(account="Codex", full=True)["omitted"]
    rules = _route(account="Codex", view="rules")
    assert rules["rules"] and "omitted" not in rules
    assert len(_route(view="priority")["priority"]["entries"]) == 3


def test_a_narrow_read_is_no_receipt_for_the_rules_or_the_whole_priority(home):
    _record()
    seen = []
    real = routing._record_read
    routing._record_read = lambda view, **kw: (seen.append(kw), real(view, **kw))[1]
    try:
        _route(account="Codex")
        _route(full=True)
    finally:
        routing._record_read = real
    assert seen[0] == {"rules": False, "priority": False}
    assert seen[-1] == {"rules": True, "priority": True}


def test_the_command_line_narrows_the_same_way(home, capsys):
    _record()
    assert cli.main(["route", "--json", "--account", "Codex"]) == 0
    part = json.loads(capsys.readouterr().out)
    assert [a["label"] for a in part["accounts"]] == ["Codex"]
    assert [e["account"] for e in part["priority"]["entries"]] == ["codex|"]
    assert sorted(h["text"] for h in part["holds"]) == ["keep Codex for reviews", "keep everything calm"]
    assert "rules" not in part and "rules" in part["omitted"] and "fingerprint" not in part
    assert cli.main(["route", "--json"]) == 0
    whole = json.loads(capsys.readouterr().out)
    assert len(whole["accounts"]) > 1 and whole["rules"]
