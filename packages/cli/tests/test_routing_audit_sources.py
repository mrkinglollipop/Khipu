"""Offline regressions for independent usage-window constraints."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing, routing_sources, usage


T0 = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)


@pytest.fixture
def windows(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setattr(routing, "_now", lambda: T0)
    return tmp_path


def evidence(model=None):
    rows = {"codex|": {"ok": True, "source": "live", "probed_at": routing._iso(T0), "windows": [
        {"name": "weekly", "used_pct": 100, "resets_at": routing._iso(T0 + timedelta(days=5)),
         "observed_at": routing._iso(T0 - timedelta(minutes=1)), "model": model},
        {"name": "five_hour", "used_pct": 100, "resets_at": routing._iso(T0 + timedelta(hours=1)),
         "observed_at": routing._iso(T0), "model": model}]}}
    path = usage.cache_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": usage.CACHE_VERSION, "rows": rows}))
    return routing_sources.usage_evidence({"codex|"})[0]


def folded(items, when, events=()):
    view = routing.fold(list(events), now=when, cfg=routing.settings({}),
                        known=[{"key": "codex|", "label": "Codex", "harness": "codex", "home": None}],
                        extra=items)
    return view["accounts"][0]


def test_weekly_limit_survives_newer_short_window_reset(windows):
    items = evidence()
    account = folded(items, T0 + timedelta(hours=2))
    assert account["state"] == "limited"
    assert account["until"] == routing._iso(T0 + timedelta(days=5))
    assert {item["window"] for item in items} == {"weekly", "five_hour"}
    assert len({item["id"] for item in items}) == 2


def test_model_weekly_limit_survives_short_window_reset(windows):
    account = folded(evidence("test-model"), T0 + timedelta(hours=2))
    assert account["models"][0]["until"] == routing._iso(T0 + timedelta(days=5))


def test_existing_owner_dismissal_of_weekly_evidence_is_preserved(windows):
    legacy_id = "p" + routing_sources._sid("codex|", "all", "limited",
                                         routing._iso(T0 - timedelta(minutes=1)),
                                         routing._iso(T0 + timedelta(days=5)))
    dismissal = {"id": "owner-dismissal", "type": "dismiss", "account": "codex|", "_seq": 1,
                 "target": legacy_id, "written_at": routing._iso(T0),
                 "written_by": {"kind": "owner"}, "via": "app"}
    account = folded(evidence(), T0 + timedelta(hours=2), [dismissal])
    assert account["state"] == "probably_open"


@pytest.mark.parametrize("clear_minutes, expected", [(-2, "limited"), (-0.5, "probably_open"), (1, "open")])
def test_owner_clear_is_applied_to_each_window_observation(windows, clear_minutes, expected):
    clear = {"id": "owner-clear", "type": "clear", "account": "codex|", "_seq": 1,
             "written_at": routing._iso(T0 + timedelta(minutes=clear_minutes)),
             "written_by": {"kind": "owner"}, "via": "app"}
    account = folded(evidence(), T0 + timedelta(hours=2), [clear])
    assert account["state"] == expected
