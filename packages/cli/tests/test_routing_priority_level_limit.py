"""A priority level ends at the first reset or limit end after its ``as_of``:
the fold read a limit's end through ``_parse``, which takes text and got a
datetime, so a limit never shortened a level. Hermetic."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from khipu import routing
from tests import routing_compact_synth as synth
from tests import test_routing_audit_fold as audit

world = audit.world
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
KEY = "claude|"
ISO = routing._iso


def _limit(n, age_h, until_h, **extra):
    at = NOW - timedelta(hours=age_h)
    return {"id": f"e{n}", "seq": n, "type": "observation", "account": KEY, "state": "limited", "basis": "cli",
            "scope": "all", "source": "runner", "witness_at": ISO(at), "written_at": ISO(at),
            "until": ISO(NOW + timedelta(hours=until_h)), "expires_at": ISO(NOW + timedelta(hours=until_h)),
            "observation_scope": {"kind": "account_quota"}, "written_by": {"kind": "runner", "name": "r"},
            "via": "runner", **extra}


def _level_at(lines, when, as_of_h=5):
    priority = {"id": "p", "seq": 99, "type": "priority_set", "until": None, "written_at": ISO(NOW - timedelta(minutes=1)),
                "written_by": {"kind": "owner"}, "via": "app", "auth": "app",
                "entries": [{"account": KEY, "label": "Claude", "role": "r", "until": None,
                             "level": {"claim": "low", "as_of": ISO(NOW - timedelta(hours=as_of_h)),
                                       "until": ISO(NOW + timedelta(days=30))}}]}
    synth.write(routing.events_file(), [*lines, priority])
    body = routing.fold(routing.read_events()[0], now=when, cfg=routing.settings(), known=routing.accounts())
    return body["priority"]["entries"][0].get("level")


def test_a_limit_ending_after_as_of_shortens_the_level(world):
    assert _level_at([_limit(1, 2, 3)], NOW)["until"] == ISO(NOW + timedelta(hours=3))


def test_the_level_is_gone_once_that_limit_has_ended(world):
    assert _level_at([_limit(1, 2, 3)], NOW + timedelta(hours=4)) is None


def test_a_limit_that_ended_before_as_of_leaves_it(world):
    assert _level_at([_limit(1, 9, -8)], NOW)["until"] == ISO(NOW + timedelta(days=30))


def test_a_lines_reset_time_still_comes_before_its_limit_end(world):
    line = _limit(1, 2, 3, reset_at=ISO(NOW + timedelta(hours=8)))
    assert _level_at([line], NOW)["until"] == ISO(NOW + timedelta(hours=8))


def test_another_accounts_limit_leaves_it(world):
    assert _level_at([_limit(1, 2, 3, account="codex|")], NOW)["until"] == ISO(NOW + timedelta(days=30))
