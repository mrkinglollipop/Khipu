"""Only confirmed resets can shorten an owner priority level. Hermetic."""
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


def test_an_unconfirmed_limit_does_not_shorten_the_level(world):
    assert _level_at([_limit(1, 2, 3)], NOW)["until"] == ISO(NOW + timedelta(days=30))


def test_the_level_survives_an_unconfirmed_limit_end(world):
    assert _level_at([_limit(1, 2, 3)], NOW + timedelta(hours=4))["until"] == ISO(NOW + timedelta(days=30))


def test_a_limit_that_ended_before_as_of_leaves_it(world):
    assert _level_at([_limit(1, 9, -8)], NOW)["until"] == ISO(NOW + timedelta(days=30))


def test_a_lines_reset_time_still_comes_before_its_limit_end(world):
    line = _limit(1, 2, 3, source="probe", reset_at=ISO(NOW + timedelta(hours=8)))
    assert _level_at([line], NOW)["until"] == ISO(NOW + timedelta(hours=8))


def test_another_accounts_limit_leaves_it(world):
    assert _level_at([_limit(1, 2, 3, account="codex|")], NOW)["until"] == ISO(NOW + timedelta(days=30))


def test_default_limit_does_not_end_level(world):
    assert _level_at([_limit(1, 0, .5, until_default=True)], NOW + timedelta(hours=1))["until"] == ISO(NOW + timedelta(days=30))


def test_provider_stated_reset_ends_level(world):
    line = _limit(1, 2, 3, source="report", message_time=True)
    assert _level_at([line], NOW)["until"] == ISO(NOW + timedelta(hours=3))
    assert _level_at([line], NOW + timedelta(hours=4)) is None


def test_estimated_reset_field_is_not_confirmation(world):
    line = _limit(1, 2, 3, basis="estimated", reset_at=ISO(NOW + timedelta(hours=1)))
    assert _level_at([line], NOW)["until"] == ISO(NOW + timedelta(days=30))


def test_cleared_confirmed_limit_does_not_end_level(world):
    clear = {"id": "clear", "seq": 2, "type": "clear", "account": KEY,
             "written_at": ISO(NOW - timedelta(hours=1)), "written_by": {"kind": "owner"},
             "via": "app", "auth": "app"}
    assert _level_at([_limit(1, 2, 3, source="report", message_time=True), clear], NOW)["until"] == ISO(NOW + timedelta(days=30))
