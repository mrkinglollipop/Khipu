"""Routing directions the owner is shown: only words he typed himself, joined
to the recorder's count, not older than a week; the start-of-session line names
only the starting conversation's own. The stored row is never deleted."""
from __future__ import annotations

import contextvars
import time

from khipu import owner_turns, recall_rule, routing_directions
from tests.test_routing_s19a_directions import GLM, SESSION, scan, say, world  # noqa: F401 - the fixture

BRIEF = "Use GLM only one job at a time."  # words an agent wrote into a brief: no hook recorded them


def test_words_no_hook_recorded_as_his_are_stored_but_never_listed(world):
    (row,) = scan([BRIEF])  # a transcript turn the recorder never saw (a brief, an agent's text)
    assert row["status"] == "open" and row["typed"] is False
    assert routing_directions.owed_items() == []
    assert routing_directions.pushed_slice_ids("proj", session=SESSION) == []
    assert len(routing_directions.directions()) == 1  # kept, only unlisted


def test_words_he_typed_are_listed(world):
    say(GLM)
    (row,) = scan([GLM])
    assert row["typed"] is True and row["join"] == "joined" and isinstance(row["seen_at"], float)
    assert [i["words"] for i in routing_directions.owed_items()] == [GLM]


def test_an_unjoined_direction_is_stored_but_not_listed(world):
    say("a different prompt entirely")
    (row,) = scan([GLM])
    assert row["join"] == "unjoined" and row["status"] == "open"
    assert routing_directions.owed_items() == []


def test_an_unsettled_direction_stops_being_listed_after_a_week_and_stays_stored(world):
    say(GLM)
    scan([GLM])
    seen = routing_directions.directions()[0]["seen_at"]
    assert len(routing_directions.owed_items(seen + 6 * 86400)) == 1
    assert routing_directions.owed_items(seen + 8 * 86400) == []
    assert len(routing_directions.directions()) == 1 and routing_directions.directions()[0]["status"] == "open"


def test_a_row_stored_before_the_mark_is_not_listed_until_his_recorded_turn_is_seen(world):
    say(GLM)
    scan([GLM])
    data = routing_directions._load()
    (row,) = data["directions"].values()
    row.pop("typed")
    routing_directions._save(data)
    assert routing_directions.owed_items() == []
    scan([GLM])  # his recorded turn is still there: the next capture marks the row
    assert len(routing_directions.owed_items()) == 1


def test_a_row_with_no_time_at_all_is_not_listed():
    row = {"status": "open", "join": "joined", "typed": True}
    assert routing_directions.listable(row) is False
    assert routing_directions.listable(dict(row, seen_at=time.time())) is True


def test_the_start_line_names_only_the_starting_conversations_own(world, monkeypatch):
    from khipu import activity, identity

    monkeypatch.setattr(identity, "resolve_repo_root", lambda cwd: {"repo_root": "/work", "project": "proj"})
    monkeypatch.setattr(activity, "project_slice",
                        lambda **kw: {"commitments": [], "episodes": [{"id": 5, "summary": "x"}],
                                      "topics": [], "decisions": []})
    say(GLM)
    (row,) = scan([GLM])

    def start(session):
        monkeypatch.setattr(recall_rule, "_START_SESSION", contextvars.ContextVar("s", default=session))
        return recall_rule._pushed_memory_slice("/work")

    assert row["id"] in start(SESSION)
    assert "routing direction" not in start("another-conversation").lower()
    assert "routing direction" not in start("").lower()
