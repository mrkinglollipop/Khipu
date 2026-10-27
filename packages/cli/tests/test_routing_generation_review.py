"""Advice freshness regressions from the RDF review, using synthetic local data."""
from __future__ import annotations

import copy
import json
import socket
import time
from datetime import timedelta

import pytest

from khipu import recall_prompt, routing, routing_advice, routing_generation
from tests.test_routing_acceptance import AUTHOR, replay  # noqa: F401
from tests.test_routing_push_prompt import _fake_reader
from tests.test_routing_s17c_advise import MAIN, NOW, _coverage, _runner, _spend, mac  # noqa: F401


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("generation regression attempted a real connection")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def _telemetry(kind, value):
    if kind == "concurrency":
        return {"active": [{"lane": "main-lane", "account": MAIN, "jobs": value,
                            "unit": "job", "at": NOW.timestamp()}]}
    return {"spend": [_spend("e1", 1, NOW - timedelta(hours=1)),
                      *([_spend("e2", value, NOW - timedelta(minutes=1))] if value else [])],
            "spend_coverage": [_coverage()]}


def _seed_cap(mac, kind):
    folder = _runner(mac, _telemetry(kind, 0))
    args = {"n": 2} if kind == "concurrency" else {"usd": 35, "days": 7}
    routing.set_cap("Main", kind=kind, revision="r1", via="app", **args)
    routing.set_priority([{"account": "Main", "role": "first"}],
                         via="app", expect=routing.priority_version())
    return folder, routing.read_view(NOW)


def _update_runner(folder, kind, value):
    (folder / "state" / "heartbeat.json").write_text(json.dumps(
        {"time": NOW.timestamp(), "interval": 20, **_telemetry(kind, value)}))


@pytest.mark.parametrize("kind,value", [("concurrency", 2), ("spend", 35)])
def test_changed_cap_verdict_invalidates_revision_and_buffered_go(mac, kind, value):
    folder, before = _seed_cap(mac, kind)
    generation = routing._generation(before, routing.settings())
    buffered = routing._advice_push_adjust({"kind": "none"}, before, NOW, "review", "main")
    assert "go (" in buffered["text"]
    _update_runner(folder, kind, value)
    routing._poll_refold(NOW)
    after = routing._load_view()
    assert routing_advice.account_advice(after, after["accounts"][0], NOW)["verdict"] == "hold_back"
    assert before["version"] == after["version"]
    buffered.update(version=before["version"], epoch=before["epoch"],
                    generation=generation, done=time.monotonic())
    at, writer = _fake_reader([(0.0, json.dumps(buffered).encode() + b"\n")])
    writer.join(5)
    assert at.collect() is None
    assert routing_generation.revision(before) != routing_generation.revision(after)
    # Check the semantic fallback itself, independently of heartbeat stats.
    assert not routing_generation.matches(str(routing.view_file()), routing_generation.revision(before))


@pytest.mark.parametrize("kind", ["concurrency", "spend"])
def test_runner_change_invalidates_generation_before_refold(mac, kind):
    folder, before = _seed_cap(mac, kind)
    generation = routing._generation(before, routing.settings())
    heartbeat = str(folder / "state" / "heartbeat.json")
    assert recall_prompt._routing_generation_current(generation)
    _update_runner(folder, kind, 35)
    assert not recall_prompt._routing_generation_current(generation)
    assert heartbeat in {item[0] for item in generation}


@pytest.mark.parametrize("kind", ["concurrency", "spend"])
def test_same_verdict_telemetry_keeps_semantic_revision(mac, kind):
    folder, before = _seed_cap(mac, kind)
    generation = routing._generation(before, routing.settings())
    buffered = routing._advice_push_adjust({"kind": "none"}, before, NOW, "review", "main")
    assert "go (" in buffered["text"]
    buffered.update(version=before["version"], epoch=before["epoch"],
                    generation=generation, done=time.monotonic())
    _update_runner(folder, kind, 1)
    routing._poll_refold(NOW)
    after = routing._load_view()
    assert routing_advice.account_advice(before, before["accounts"][0], NOW)["verdict"] == "go"
    assert routing_advice.account_advice(after, after["accounts"][0], NOW)["verdict"] == "go"
    assert routing_generation.revision(before) == routing_generation.revision(after)
    assert routing_generation.matches(str(routing.view_file()), routing_generation.revision(before))
    at, writer = _fake_reader([(0.0, json.dumps(buffered).encode() + b"\n")])
    writer.join(5)
    assert at.collect() == buffered
    assert recall_prompt._routing_generation_current(generation)


@pytest.mark.parametrize("kind", ["concurrency", "spend"])
def test_harmless_telemetry_without_refold_still_invalidates_delivery(mac, kind):
    folder, before = _seed_cap(mac, kind)
    generation = routing._generation(before, routing.settings())
    _update_runner(folder, kind, 1)
    assert not recall_prompt._routing_generation_current(generation)


@pytest.mark.parametrize("kind", ["concurrency", "spend"])
@pytest.mark.parametrize("damage", ["schema", "digest", "missing_fingerprint", "old_fingerprint",
                                   "new_heartbeat", "events", "config"])
def test_heartbeat_fallback_requires_valid_current_replacement(mac, kind, damage):
    folder, before = _seed_cap(mac, kind)
    generation = routing._generation(before, routing.settings())
    _update_runner(folder, kind, 1)
    routing._poll_refold(NOW)
    after = routing._load_view()
    assert routing_generation.revision(before) == routing_generation.revision(after)
    if damage == "new_heartbeat":
        # Even equivalent telemetry needs a fold that has actually read it.
        _update_runner(folder, kind, 1)
    elif damage in {"events", "config"}:
        # Semantic fallback is limited to heartbeats; other input edits
        # cannot be excused by a view that predates them.
        index = 0 if damage == "events" else 1
        with open(generation[index][0], "a", encoding="utf-8") as fh:
            fh.write("\n")
    else:
        if damage == "schema":
            after["schema"] = 999
        elif damage == "missing_fingerprint":
            after["fingerprint"].pop("sources")
        elif damage == "old_fingerprint":
            after["fingerprint"] = before["fingerprint"]
        routing._save_view(after)
        if damage == "digest":
            after["digest"] = "invalid"
            routing.view_file().write_text(json.dumps(after))
    assert not recall_prompt._routing_generation_current(generation)


def _forecast(replay, name="plan", minutes=120):
    routing.report(account="Claude", state="open", author=AUTHOR)
    view = replay.view()
    entry = next(a for a in view["accounts"] if a["key"] == "claude|")
    field = "runs_out_at" if name == "plan" else "reaches_cap_at"
    entry["pace"] = [{"window": "weekly", "cycle_resets_at": routing._iso(replay.now + timedelta(days=1)),
                      "headline": "plan", "used_pct": 70, "buckets": 6, "why": None,
                      name: {"verdict": "outpacing", field: routing._iso(replay.now + timedelta(minutes=minutes))}}]
    routing._save_view(view)
    # Pin the file stat to the saved synthetic view, as a real read does.
    return routing._load_view()


@pytest.mark.parametrize("name", ["plan", "share_cap", "day_cap"])
def test_forecast_entering_hour_invalidates_semantic_fallback(replay, name):
    before = _forecast(replay, name)
    generation = routing._generation(before, routing.settings())
    after = copy.deepcopy(before)
    entry = next(a for a in after["accounts"] if a["key"] == "claude|")
    field = "runs_out_at" if name == "plan" else "reaches_cap_at"
    entry["pace"][0][name][field] = routing._iso(replay.now + timedelta(minutes=30))
    routing._save_view(after)
    old = next(a for a in before["accounts"] if a["key"] == "claude|")
    assert routing_advice.account_advice(before, old, replay.now)["verdict"] == "go"
    assert routing_advice.account_advice(after, entry, replay.now)["verdict"] == "hold_back"
    assert not recall_prompt._routing_generation_current(generation)


@pytest.mark.parametrize("minutes,changed", [(121, False), (60, True), (30, True), (0, False), (-1, False)])
def test_forecast_revision_tracks_hour_condition_not_raw_eta(replay, minutes, changed):
    before = _forecast(replay)
    after = copy.deepcopy(before)
    entry = next(a for a in after["accounts"] if a["key"] == "claude|")
    entry["pace"][0]["plan"]["runs_out_at"] = routing._iso(
        replay.now + timedelta(minutes=minutes))
    assert (routing_generation.revision(before) != routing_generation.revision(after)) is changed


def test_clock_crossing_forecast_hour_changes_revision(replay):
    before = _forecast(replay)
    after = copy.deepcopy(before)
    after["folded_at"] = after["checked_at"] = routing._iso(replay.now + timedelta(minutes=90))
    assert routing_generation.revision(before) != routing_generation.revision(after)


def test_checked_clock_crossing_forecast_hour_changes_revision(replay):
    before = _forecast(replay)
    after = copy.deepcopy(before)
    # A cache hit updates checked_at without rewriting the original fold.
    after["checked_at"] = routing._iso(replay.now + timedelta(minutes=90))
    assert routing_generation.revision(before) != routing_generation.revision(after)


def test_forecast_remaining_within_hour_keeps_revision(replay):
    before = _forecast(replay, minutes=30)
    after = copy.deepcopy(before)
    entry = next(a for a in after["accounts"] if a["key"] == "claude|")
    entry["pace"][0]["plan"]["runs_out_at"] = routing._iso(replay.now + timedelta(minutes=45))
    assert routing_generation.revision(before) == routing_generation.revision(after)


@pytest.mark.parametrize("schema", [None, 999, "1", 1])
def test_semantic_fallback_requires_loader_supported_schema(replay, schema):
    routing.report(account="Claude", state="open", author=AUTHOR)
    before = replay.view()
    generation = routing._generation(before, routing.settings())
    after = copy.deepcopy(before)
    if schema is None:
        after.pop("schema")
    else:
        after["schema"] = schema
    routing._save_view(after)
    supported = schema == routing.SCHEMA
    assert (routing._load_view() is not None) is supported
    assert recall_prompt._routing_generation_current(generation) is supported


def test_legacy_view_without_cap_decisions_is_refolded(mac):
    _, before = _seed_cap(mac, "concurrency")
    legacy = copy.deepcopy(before)
    legacy.pop("advice_cap_decisions", None)
    routing._save_view(legacy)
    current = routing.read_view(NOW)
    assert "advice_cap_decisions" in current
    assert "advice_cap_decisions" not in routing.public_view(current, NOW)
