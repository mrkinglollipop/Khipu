"""Owner-local Checks data must never become routing delivery data.

Only disposable homes and synthetic owner words; every outward projection is
scanned recursively, including strings nested in lists and dictionary keys.
"""
from __future__ import annotations

import copy
import json

import pytest

from khipu import cli, routing, routing_delivery, routing_directions
from tests.test_routing_acceptance import replay  # noqa: F401

FIELD = "unreached_directions"
WORDS = "Use Codex for QuasarSentinel JuniperSentinel builds."
PROJECT = "synthetic-unreached-project"
SESSION = "claude:synthetic-unreached-session"
PRIVATE_PATH = "/private/synthetic-unreached-folder/transcript.jsonl"


def strings(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from strings(inner)
    elif isinstance(value, (list, tuple)):
        for inner in value:
            yield from strings(inner)
    elif isinstance(value, str):
        yield value


@pytest.fixture
def directions(replay):
    replay.configure(routing_cloud_copy=True, routing_cloud_copy_rules=True,
                     routing_cloud_copy_hold_words=True, routing_child_chars=400)
    rows = [
        {"id": "rd-synthetic-open", "status": "open", "words": WORDS,
         "session": SESSION, "project": PROJECT, "join": "unjoined",
         "reason": "occurrence mismatch", "path": PRIVATE_PATH,
         "prompt_sha256": "synthetic-hash", "target": {"entity": "Codex"}},
        {"id": "rd-synthetic-dismissed", "status": "dismissed", "words": "DismissedSentinel",
         "session": SESSION},
        {"id": "rd-synthetic-applied", "status": "applied", "words": "AppliedSentinel",
         "session": SESSION},
    ]
    routing_directions._save({"directions": {r["id"]: r for r in rows}})
    return replay


def test_public_view_carries_only_the_open_owed_display_fields(directions, capsys):
    view = directions.view()
    before = copy.deepcopy(view)
    owed, = routing_directions.owed_items()
    public = routing.public_view(view, directions.now)
    assert public[FIELD] == [{key: owed[key] for key in ("id", "words", "project", "join", "reason")}]
    assert view == before
    assert FIELD not in view
    assert not any(SESSION in s or PRIVATE_PATH in s for s in strings(public[FIELD]))
    assert cli.main(["route", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[FIELD] == public[FIELD]
    routing_directions.dismiss(owed["id"])
    assert routing.public_view(directions.view(), directions.now)[FIELD] == []


def test_empty_directions_are_an_explicit_empty_list(replay):
    assert routing.public_view(replay.view(), replay.now)[FIELD] == []


@pytest.mark.parametrize("surface", ["push", "child", "mcp", "replication", "gateway"])
def test_owner_local_directions_never_reach_other_surfaces(directions, monkeypatch, surface):
    # Seed actual routing content, so a missing/empty projection cannot pass.
    for i in range(3):
        routing.add_rule(f"Synthetic ordinary rule {i} " + "x" * 900, via="app")
    view = directions.view()
    public = routing.public_view(view, directions.now)
    assert public[FIELD][0]["words"] == WORDS
    # Even an accidentally enriched fold must stay behind closed projections.
    enriched = dict(view, **{FIELD: public[FIELD]})
    monkeypatch.setattr(routing, "read_view", lambda *a, **kw: enriched)
    monkeypatch.setattr(routing, "_start_view", lambda *a, **kw: enriched)
    if surface == "push":
        output = routing.start_push(budget=2, session_id="synthetic-recipient")
        assert output and output["text"]
    elif surface == "child":
        start = routing.start_push(budget=2, moment="delegation",
                                   child_component="agent:synthetic", child_session="synthetic-recipient")
        assert routing_delivery.acknowledge("synthetic-recipient", start["routing_changes"], now=directions.now)
        pages = []
        for _ in range(20):
            page = routing._child_page_push_inline("synthetic-recipient", "synthetic", directions.now)
            if not page:
                break
            pages.append(page)
            assert routing_delivery.acknowledge("synthetic-recipient", page["routing_changes"], now=directions.now)
        assert start and start["text"] and pages
        output = [start, pages, routing_delivery.snapshot("synthetic-recipient", "agent:synthetic")]
    elif surface == "mcp":
        error, output = directions.call(session_id="synthetic-recipient")
        assert not error and output["accounts"]
    elif surface == "replication":
        events = routing._read_all_events()[0]
        # Unknown owner-local fields on even a valid event are not replicated.
        events = [dict(e, **{FIELD: public[FIELD]}) for e in events]
        output = routing.replication_payload(events, bindings={})
        assert output
    else:
        output = routing.gateway_projection(public, now=directions.now)
        assert output and output["accounts"] and output["rules"]
    forbidden = (FIELD, WORDS, "QuasarSentinel", "JuniperSentinel", PROJECT, SESSION, PRIVATE_PATH,
                 "DismissedSentinel", "AppliedSentinel")
    for leaf in strings(output):
        assert not any(secret in leaf for secret in forbidden), (surface, leaf)


def test_a_damaged_directions_store_never_breaks_the_route_view(replay, monkeypatch):  # noqa: F811
    from datetime import datetime, timezone

    from khipu import routing, routing_directions

    def broken():
        raise KeyError("words")

    monkeypatch.setattr(routing_directions, "owed_items", broken)
    view = routing.read_view()
    out = routing.public_view(view, datetime.now(timezone.utc))
    assert out["unreached_directions"] == []
