"""route --json carries what slices 17a, 17c and 4P already own for the app's
pace group: lead-conversation advice, the owner's cap confirmation time and
the newest reading's time. The push and gateway projections stay as they were.
Hermetic: fake samples only."""
from __future__ import annotations

import json
from datetime import timedelta

from khipu import routing
from tests.test_routing_s17c_advise import _DISPATCH_MARKERS, _pace_target, _sample  # noqa: F401
from tests.test_routing_s17c_advise import _outside_a_job  # noqa: F401
from tests.test_routing_acceptance import AUTHOR  # noqa: F401

pytest_plugins = ("tests.test_routing_acceptance",)


def _claude(view):
    return next(a for a in view["accounts"] if a["label"] == "Claude")


def test_route_json_carries_lead_conversation_advice_beside_job_advice(replay):
    _pace_target(replay, used=18)
    out = routing.public_view(replay.view(), replay.now)
    claude = _claude(out)
    assert claude["advice"]["purpose"] == "job" and claude["advice"]["verdict"] == "hold_back"
    assert claude["lead_advice"]["purpose"] == "orchestration"
    assert claude["lead_advice"]["verdict"] == "go"
    assert set(claude["lead_advice"]) >= {"verdict", "reason", "evidence", "numbers", "label"}


def test_cap_status_carries_the_owners_confirm_time(replay):
    _pace_target(replay, used=18)
    status = next(s for s in _claude(routing.public_view(replay.view(), replay.now))["cap_statuses"]
                  if s["kind"] == "pace_target")
    confirmed = routing._parse(status["confirmed_at"])
    assert confirmed is not None and abs((replay.now - confirmed).total_seconds()) < 3600


def test_a_reset_cap_carries_its_new_confirm_time(replay):
    _pace_target(replay, used=18)
    first = next(s for s in _claude(routing.public_view(replay.view(), replay.now))["cap_statuses"]
                 if s["kind"] == "pace_target")["confirmed_at"]
    replay.advance(2 * 3600)
    routing.set_cap("Claude", window="weekly", kind="pace_target", pct=70, slack=5,
                    revision="pt2", via="app")
    second = next(s for s in _claude(routing.public_view(replay.view(), replay.now))["cap_statuses"]
                  if s["kind"] == "pace_target")["confirmed_at"]
    assert routing._parse(second) > routing._parse(first)


def test_advice_carries_the_newest_reading_time(replay):
    routing.report(account="Claude", state="open", author=AUTHOR)
    old = replay.now - timedelta(minutes=50)
    replay.write(_sample(replay, used=18, at=old))
    replay.write(_sample(replay, used=19, at=replay.now - timedelta(minutes=7)))
    claude = _claude(routing.public_view(replay.view(), replay.now))
    want = replay.now - timedelta(minutes=7)
    for key in ("advice", "lead_advice"):
        assert routing._parse(claude[key]["newest_reading_at"]) == want


def test_an_account_with_no_readings_has_no_reading_time(replay):
    routing.report(account="Claude", state="open", author=AUTHOR)
    claude = _claude(routing.public_view(replay.view(), replay.now))
    assert claude["advice"]["newest_reading_at"] is None


def test_push_and_gateway_projections_do_not_carry_the_additions(replay):
    _pace_target(replay, used=18)
    view = replay.view()
    gateway = json.dumps(routing.gateway_projection(view, now=replay.now), sort_keys=True)
    conv = json.dumps(routing.conversation_view(now=replay.now), sort_keys=True, default=str)
    line = routing.account_line(_claude(view), replay.now, push=True)
    for text in (gateway, conv, line):
        for word in ("newest_reading_at", "lead_advice", "cap_confirmed"):
            assert word not in text, word
    # An account's own `confirmed_at` is an older field; a cap's is the new one.
    assert all("confirmed_at" not in c for c in _claude(view)["caps"])
    for row in routing.conversation_view(now=replay.now)["accounts"]:
        assert all("confirmed_at" not in c for c in row.get("cap_statuses") or [])
    assert routing.cap_statuses(view, _claude(view), replay.now)[0].keys() == \
        {"kind", "revision", "window", "pct", "slack", "allowance_pct", "used_pct", "verdict", "why"}
