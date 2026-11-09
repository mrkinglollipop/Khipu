"""Doctor warns only while there is something for the owner to do.

Each warning changed in "quiet, truthful doctor" is pinned here for when it
shows and when it does not: a line dated off its neighbours (only the last
week), a job runner silent for more than a week (one quiet note, never a
warning), a priority line that repeats the plan warning, and the routing
record that cannot shrink (a note, a warning only past a size that hurts).
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import cli, routing

FIXTURES = Path(__file__).parent / "fixtures" / "routing"
T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"},
            {"label": "Muse", "harness": "muse"}]
LANES_A = {"codex": "Codex", "main": "Claude", "muse": "Muse"}
AUTHOR = {"kind": "conversation", "harness": "claude_code"}


class Clock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    runners = tmp_path / "runners"
    shutil.copytree(FIXTURES / "lanes-a", runners / "project-a")
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    clock = Clock(T0)
    monkeypatch.setattr(routing, "_now", clock)
    return {"data": data, "clock": clock, "a": runners / "project-a"}


def _config(home, **values) -> None:
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def _beat(folder: Path, at: datetime, interval: float = 20.0) -> None:
    (folder / "state" / "heartbeat.json").write_text(json.dumps(
        {"pid": 1, "time": at.timestamp(), "at": routing._iso(at), "interval": interval, "error": None}))


# ---- a line dated well off its neighbours: only while it is recent ----------------------


def _record_with_a_2001_line(home):
    clock = home["clock"]
    routing.report(account="Codex", state="open", author=AUTHOR)
    clock.now = datetime(2001, 1, 1, tzinfo=timezone.utc)
    routing.report(account="Muse", state="open", author=AUTHOR)
    clock.now = T0 + timedelta(minutes=10)
    routing.report(account="Claude", state="open", author=AUTHOR)


def test_a_stray_dated_line_warns_while_it_is_recent(home):
    _record_with_a_2001_line(home)
    home["clock"].now = T0 + timedelta(days=6)
    warnings = routing.doctor_block()["warnings"]
    assert any("1 line dated well before" in w and "2001-01-01T00:00:00Z" in w for w in warnings), warnings


def test_a_stray_dated_line_stops_warning_after_a_week(home):
    _record_with_a_2001_line(home)
    home["clock"].now = T0 + timedelta(days=8)
    warnings = routing.doctor_block()["warnings"]
    assert not any("dated well" in w for w in warnings), warnings


# ---- a job runner silent for more than a week: one quiet note, not a warning -------------


def _register(home, *others) -> None:
    sources = [{"name": "project A", "kind": "lanes", "path": str(home["a"])}, *others]
    _config(home, routing_sources=sources, routing_lane_map={"project A": LANES_A})


def test_a_runner_silent_a_day_is_a_warning_and_has_no_note(home):
    _register(home)
    _beat(home["a"], T0 - timedelta(seconds=10))
    home["clock"].now = T0 + timedelta(days=1)
    block = routing.doctor_block()
    assert any("Your project A job runner stopped reporting" in w for w in block["warnings"]), block["warnings"]
    assert block["notes"] == []


def test_a_runner_silent_past_a_week_is_one_quiet_note_with_the_exact_way_out(home):
    _register(home)
    _beat(home["a"], T0 - timedelta(seconds=10))
    home["clock"].now = T0 + timedelta(days=8)
    block = routing.doctor_block()
    assert not any("job runner" in w for w in block["warnings"]), block["warnings"]
    assert len(block["notes"]) == 1
    note = block["notes"][0]
    assert "project A job runner has not reported since 2026-10-14T14:59:50Z" in note
    assert "Settings → Routing → Job runner folders → Remove project A" in note
    assert "khipu config --unset routing_sources" in note  # the only source: nothing is left to keep


def test_the_note_keeps_the_other_sources_in_its_command(home):
    other = {"name": "project B", "kind": "lanes", "path": str(home["a"].parent / "elsewhere")}
    _register(home, other)
    _beat(home["a"], T0 - timedelta(seconds=10))
    home["clock"].now = T0 + timedelta(days=8)
    note = routing.doctor_block()["notes"][0]
    assert "khipu config --set routing_sources " in note and "project B" in note
    assert "--unset" not in note and "\"project A\"" not in note.split("or run")[1]


def test_a_runner_that_is_reporting_has_neither(home):
    _register(home)
    _beat(home["a"], T0 - timedelta(seconds=10))
    home["clock"].now = T0 + timedelta(seconds=30)
    block = routing.doctor_block()
    assert block["notes"] == [] and not any("job runner" in w for w in block["warnings"])


# ---- the priority line that repeats the plan warning --------------------------------------

FIRST = "The first account in your priority, Codex, is limited until 06:53Z."
OTHER = "Your priority names Gemini, which isn't tracked in Accounts and routing."


def test_the_priority_line_is_dropped_when_the_plan_warning_says_it():
    plans = {"accounts": [{"account": "Codex", "used_pct": 100.0}, {"account": "Claude", "used_pct": 12.0}]}
    assert cli._without_repeated_limit([FIRST, OTHER], plans) == [OTHER]


def test_the_priority_line_stays_when_the_plan_is_not_nearly_used_up():
    plans = {"accounts": [{"account": "Codex", "used_pct": 40.0}]}
    assert cli._without_repeated_limit([FIRST, OTHER], plans) == [FIRST, OTHER]
    assert cli._without_repeated_limit([FIRST], {"accounts": [], "error": "unreadable"}) == [FIRST]


def test_the_priority_line_stays_for_another_account():
    plans = {"accounts": [{"account": "Claude", "used_pct": 99.0}]}
    assert cli._without_repeated_limit([FIRST], plans) == [FIRST]
