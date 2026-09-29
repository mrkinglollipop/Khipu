# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Unit tests for khipu.timeparse (Phase 3, session A). Every grammar
production, both directions of each boundary, zone handling, ambiguity
returning None, and the boost preference (never an exclusion)."""
from __future__ import annotations

from datetime import datetime, timezone

from khipu import timeparse as tp

NOW = datetime(2026, 9, 28, 15, 30, tzinfo=timezone.utc)  # a Monday


def test_today():
    r = tp.interpret("what happened today", NOW)
    assert r["phrase"].lower() == "today"
    since = datetime.fromisoformat(r["since"])
    until = datetime.fromisoformat(r["until"])
    assert since == datetime(2026, 9, 28, 0, 0, 0, tzinfo=timezone.utc)
    assert until.date() == since.date()
    assert until.hour == 23 and until.minute == 59


def test_yesterday():
    r = tp.interpret("what happened yesterday", NOW)
    since = datetime.fromisoformat(r["since"])
    assert since.date().isoformat() == "2026-09-27"


def test_this_week_starts_monday():
    r = tp.interpret("what happened this week", NOW)
    since = datetime.fromisoformat(r["since"])
    until = datetime.fromisoformat(r["until"])
    assert since.weekday() == 0
    assert (until - since).days == 6
    assert since.date().isoformat() == "2026-09-28"  # NOW is itself a Monday


def test_last_week():
    r = tp.interpret("what happened last week", NOW)
    since = datetime.fromisoformat(r["since"])
    assert since.weekday() == 0
    assert since.date().isoformat() == "2026-09-21"


def test_this_month():
    r = tp.interpret("what happened this month", NOW)
    since = datetime.fromisoformat(r["since"])
    until = datetime.fromisoformat(r["until"])
    assert since.day == 1 and since.month == 9
    assert until.month == 9 and until.day == 30


def test_last_month_crosses_year_boundary():
    jan_now = datetime(2026, 1, 15, tzinfo=timezone.utc)
    r = tp.interpret("what happened last month", jan_now)
    since = datetime.fromisoformat(r["since"])
    until = datetime.fromisoformat(r["until"])
    assert since.year == 2025 and since.month == 12 and since.day == 1
    assert until.year == 2025 and until.month == 12 and until.day == 31


def test_n_days_ago():
    r = tp.interpret("3 days ago", NOW)
    since = datetime.fromisoformat(r["since"])
    assert since.date().isoformat() == "2026-09-25"


def test_n_weeks_ago():
    r = tp.interpret("2 weeks ago", NOW)
    since = datetime.fromisoformat(r["since"])
    assert since.weekday() == 0
    assert since.date().isoformat() == "2026-09-14"


def test_n_months_ago():
    r = tp.interpret("1 month ago", NOW)
    since = datetime.fromisoformat(r["since"])
    assert since.month == 8 and since.day == 1


def test_in_the_last_n_days_is_a_rolling_window_to_now():
    r = tp.interpret("in the last 5 days", NOW)
    since = datetime.fromisoformat(r["since"])
    until = datetime.fromisoformat(r["until"])
    assert since.date().isoformat() == "2026-09-23"
    assert until.date().isoformat() == "2026-09-28"


def test_in_the_past_n_weeks():
    r = tp.interpret("in the past 2 weeks", NOW)
    since = datetime.fromisoformat(r["since"])
    assert since.date().isoformat() == "2026-09-14"


def test_since_weekday_most_recent_occurrence():
    r = tp.interpret("since Wednesday", NOW)
    since = datetime.fromisoformat(r["since"])
    assert since.weekday() == 2  # Wednesday
    assert since <= NOW
    assert r["until"] is None


def test_since_month_name_rolls_back_a_year_when_future():
    # NOW is September 2026; "since November" must mean November 2025.
    r = tp.interpret("since November", NOW)
    since = datetime.fromisoformat(r["since"])
    assert (since.year, since.month) == (2025, 11)


def test_since_iso_date():
    r = tp.interpret("since 2026-01-15", NOW)
    since = datetime.fromisoformat(r["since"])
    assert since.date().isoformat() == "2026-01-15"
    assert r["until"] is None


def test_before_iso_date_is_exclusive_of_that_day():
    r = tp.interpret("before 2026-01-15", NOW)
    assert r["since"] is None
    until = datetime.fromisoformat(r["until"])
    assert until.date().isoformat() == "2026-01-14"


def test_after_iso_date_starts_the_next_day():
    r = tp.interpret("after 2026-01-15", NOW)
    assert r["until"] is None
    since = datetime.fromisoformat(r["since"])
    assert since.date().isoformat() == "2026-01-16"


def test_on_iso_date_is_that_whole_day():
    r = tp.interpret("on 2026-01-15", NOW)
    since = datetime.fromisoformat(r["since"])
    until = datetime.fromisoformat(r["until"])
    assert since.date().isoformat() == until.date().isoformat() == "2026-01-15"


def test_in_month_with_explicit_year():
    r = tp.interpret("in March 2025", NOW)
    since = datetime.fromisoformat(r["since"])
    until = datetime.fromisoformat(r["until"])
    assert (since.year, since.month, since.day) == (2025, 3, 1)
    assert (until.year, until.month, until.day) == (2025, 3, 31)


def test_in_month_without_year_uses_now():
    r = tp.interpret("in March", NOW)
    since = datetime.fromisoformat(r["since"])
    assert since.year == NOW.year


def test_as_of_month_year_is_the_same_production_as_in_month_year():
    r = tp.interpret("what was the rocket launch fee as of September 2026", NOW)
    assert r is not None
    assert r["as_of"].startswith("2026-09")
    since = datetime.fromisoformat(r["since"])
    assert (since.year, since.month, since.day) == (2026, 9, 1)


def test_as_of_is_since_when_since_is_present():
    r = tp.interpret("today", NOW)
    assert r["as_of"] == r["since"]


def test_as_of_falls_back_to_until_when_there_is_no_since():
    r = tp.interpret("before 2026-01-15", NOW)
    assert r["since"] is None
    assert r["as_of"] == r["until"]


# ---- zone handling ----------------------------------------------------------

def test_named_zone_shifts_the_day_boundary():
    # 2026-09-28T02:00:00Z is still 2026-09-27 in America/Los_Angeles.
    early_utc = datetime(2026, 9, 28, 2, 0, tzinfo=timezone.utc)
    r = tp.interpret("today", early_utc, tz="America/Los_Angeles")
    assert r["tz"] == "America/Los_Angeles"
    since = datetime.fromisoformat(r["since"])
    assert since.astimezone(timezone.utc).date().isoformat() == "2026-09-27"


def test_default_zone_is_utc():
    r = tp.interpret("today", NOW)
    assert r["tz"] == "UTC"
    assert "tz_note" not in r


def test_unknown_zone_falls_back_to_utc_and_says_so():
    r = tp.interpret("today", NOW, tz="Nowhere/Fake")
    assert r["tz"] == "UTC"
    assert "tz_note" in r and "Nowhere/Fake" in r["tz_note"]


# ---- ambiguity: never resolved by guessing -----------------------------------

def test_two_distinct_phrases_in_one_query_is_ambiguous():
    assert tp.interpret("since Monday before 2026-01-01", NOW) is None


def test_repeated_phrase_is_also_ambiguous():
    assert tp.interpret("today or yesterday, whichever", NOW) is None


def test_an_unplaceable_phrase_is_none():
    assert tp.interpret("recently", NOW) is None
    assert tp.interpret("a while ago", NOW) is None


def test_empty_query_is_none():
    assert tp.interpret("", NOW) is None
    assert tp.interpret("   ", NOW) is None


def test_no_recognized_phrase_at_all_is_none():
    assert tp.interpret("what is the widget-batching endpoint", NOW) is None


# ---- apply_time_boost: a preference, never an exclusion ----------------------

def test_boost_multiplies_rows_inside_the_window_only():
    interp = tp.interpret("today", NOW)
    rows = [
        {"kind": "episode", "id": "1", "score": 0.5, "ts": "2026-09-28T10:00:00+00:00"},
        {"kind": "episode", "id": "2", "score": 0.9, "ts": "2026-09-01T10:00:00+00:00"},
        {"kind": "episode", "id": "3", "score": 0.4, "ts": None},
    ]
    out = tp.apply_time_boost(rows, interp)
    by_id = {r["id"]: r for r in out}
    assert by_id["1"]["score"] == round(0.5 * tp.TIME_BOOST, 6)
    assert by_id["2"]["score"] == 0.9  # outside the window: untouched
    assert by_id["3"]["score"] == 0.4  # no usable ts: untouched
    assert len(out) == 3  # nothing excluded


def test_boost_no_interpretation_is_a_no_op():
    rows = [{"kind": "episode", "id": "1", "score": 0.5, "ts": "2026-09-28T10:00:00+00:00"}]
    assert tp.apply_time_boost(rows, None) == rows


def test_boost_open_ended_since_still_boosts_within_range():
    interp = tp.interpret("since 2026-09-01", NOW)
    rows = [
        {"kind": "episode", "id": "1", "score": 1.0, "ts": "2026-09-15T00:00:00+00:00"},
        {"kind": "episode", "id": "2", "score": 1.0, "ts": "2026-08-15T00:00:00+00:00"},
    ]
    out = tp.apply_time_boost(rows, interp)
    by_id = {r["id"]: r for r in out}
    assert by_id["1"]["score"] > by_id["2"]["score"]
