"""Partial money evidence is owner-display data, never a cap input or delivery payload."""
from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest

from khipu import cli, routing, routing_advice, routing_delivery, routing_pace, routing_sources
from tests.test_routing_acceptance import replay  # noqa: F401
from tests.test_routing_unreached_directions import strings
from tests.test_routing_s17t_telemetry import MAIN, NOW, coverage, mac, runner, spend  # noqa: F401


def stream(now, runner="project-c", usd=21.40):
    start = now - timedelta(days=7)
    gap_start, gap_end = now - timedelta(hours=6), now - timedelta(hours=3)
    return {"runner": runner, "account": "codex|", "silent": False, "incomplete": False,
            "accumulating_since": start - timedelta(days=1),
            "buckets": [{"at": now - timedelta(hours=1), "usd": usd, "n": 1, "ids": ["fake-event"]}],
            "corrections": [], "dropped": [], "active": [], "launches": [],
            "coverage": [(start, gap_start), (gap_end, now)]}


@pytest.mark.parametrize("usd", [0, 21.40, 35, 35000])
def test_partial_never_judges_a_cap_or_advice(replay, monkeypatch, usd):
    rows = [stream(replay.now, usd=usd)]
    monkeypatch.setattr(routing, "_telemetry_streams", lambda view: rows)
    routing.set_cap("Codex", kind="spend", usd=35, days=7, revision="fake-cap", via="app")
    view = replay.view()
    before = copy.deepcopy(view)
    entry = next(a for a in view["accounts"] if a["key"] == "codex|")
    total = routing_sources.spend_total(rows, "codex|", replay.now - timedelta(days=7), replay.now)
    assert total["usd"] is None and total["status"] == "unknown"
    assert "reported" not in total
    ordinary, = routing.cap_statuses(view, entry, replay.now)
    assert ordinary["usd"] is None and ordinary["verdict"] == "unknown"
    assert "reported" not in ordinary
    advice = routing_advice.account_advice(view, entry, replay.now)
    public = routing.public_view(view, replay.now, "codex|")
    status, = public["accounts"][0]["cap_statuses"]
    assert status["reported"] == {
        "usd": usd, "events": 1,
        "gaps": [{"runner": "project-c", "from": routing._iso(replay.now - timedelta(hours=6)),
                  "to": routing._iso(replay.now - timedelta(hours=3)), "seconds": 10800}],
    }
    assert status["usd"] is None and status["verdict"] == "unknown"
    assert public["accounts"][0]["advice"] == dict(advice, newest_reading_at=entry.get("newest_reading_at"))
    assert routing_pace.spend_verdict(dict(total, reported=status["reported"]), 35) == "unknown"
    assert advice["evidence"] == "unknown" and advice["verdict"] == "go"
    assert view == before


def test_gaps_are_clipped_merged_and_keep_each_runner_label(replay):
    now, start = replay.now, replay.now - timedelta(days=7)
    a, b = stream(now, "Runner A", 10), stream(now, "Runner B", 11.40)
    a["coverage"] += [(start - timedelta(days=1), start + timedelta(hours=2)),
                      (start + timedelta(hours=1), now - timedelta(hours=6))]
    b["coverage"] = [(start - timedelta(days=1), now - timedelta(hours=1))]
    b["launches"] = [now]
    a["corrections"] = [{"at": now, "event": "correction", "corrects": "fake-event", "usd": -2}]
    report = routing_sources.spend_report([a, b], "codex|", start, now)
    assert report["usd"] == 19.40 and report["events"] == 3
    assert [(g["runner"], g["seconds"]) for g in report["gaps"]] == [("Runner A", 10800), ("Runner B", 3600)]
    assert all(start <= routing._parse(g["from"]) < routing._parse(g["to"]) <= now for g in report["gaps"])
    assert routing_sources.spend_report([a, b], "claude|", start, now) is None


def test_spend_report_caps_gaps_to_the_three_largest_with_a_more_count(replay):
    now, start = replay.now, replay.now - timedelta(days=7)
    row = stream(now)
    marks = [start, start + timedelta(hours=1), start + timedelta(hours=2),
             start + timedelta(hours=3), start + timedelta(hours=5),
             start + timedelta(hours=6), start + timedelta(hours=9),
             start + timedelta(hours=10), start + timedelta(hours=14),
             start + timedelta(hours=15), start + timedelta(hours=20), now]
    row["coverage"] = list(zip(marks[::2], marks[1::2]))
    report = routing_sources.spend_report([row], "codex|", start, now)
    assert [gap["seconds"] for gap in report["gaps"]] == [18000, 14400, 10800]
    assert report["gaps_more"] == 2
    small = routing_sources.spend_report([stream(now)], "codex|", start, now)
    assert len(small["gaps"]) == 1 and "gaps_more" not in small


def test_complete_and_no_records_do_not_invent_partial_numbers(replay):
    now, start = replay.now, replay.now - timedelta(days=7)
    row = stream(now)
    row["coverage"] = [(start, now)]
    assert routing_sources.spend_report([row], "codex|", start, now) is None
    row["coverage"] = []
    row["buckets"] = []
    assert routing_sources.spend_report([row], "codex|", start, now) is None


def test_real_fake_runner_read_keeps_partial_out_of_persisted_view(mac):  # noqa: F811
    gap_start, gap_end = NOW - timedelta(hours=6), NOW - timedelta(hours=3)
    runner(mac, "project-c", {"spend": [spend("e1", 21.40, NOW - timedelta(hours=1))],
                              "spend_coverage": [coverage(end=gap_start), coverage(start=gap_end)]})
    routing.set_cap("Main", kind="spend", usd=35, days=7, revision="fake-cap", via="app")
    view = routing.read_view(now=NOW)
    entry = next(a for a in view["accounts"] if a["key"] == MAIN)
    ordinary, = routing.cap_statuses(view, entry, NOW)
    assert ordinary["usd"] is None and ordinary["verdict"] == "unknown"
    owner = routing.public_view(view, NOW, MAIN)["accounts"][0]["cap_statuses"][0]
    assert owner["reported"]["usd"] == 21.40
    assert owner["reported"]["gaps"][0]["runner"] == "project-c"
    assert owner["reported"]["gaps"][0]["seconds"] == 10800
    assert "reported" not in strings(json.loads(routing.view_file().read_text()))


@pytest.mark.parametrize("cause", ["silent", "incomplete", "since", "correction"])
def test_other_incompleteness_keeps_reported_separate_from_unknown(replay, cause):
    now, start = replay.now, replay.now - timedelta(days=7)
    row = stream(now)
    row["coverage"] = [(start, now)]
    if cause in ("silent", "incomplete"):
        row[cause] = True
    elif cause == "since":
        row["accumulating_since"] = now - timedelta(days=1)
    else:
        row["corrections"] = [{"at": now, "event": "orphan", "corrects": "missing", "usd": -8}]
    assert routing_sources.spend_total([row], "codex|", start, now)["usd"] is None
    assert routing_sources.spend_report([row], "codex|", start, now)["usd"] == 21.40


def test_failed_new_read_is_empty_and_cli_prints_owner_report(replay, monkeypatch, capsys):
    monkeypatch.setattr(routing, "_telemetry_streams", lambda view: [stream(replay.now)])
    routing.set_cap("Codex", kind="spend", usd=35, days=7, revision="fake-cap", via="app")
    assert cli.main(["route", "--json"]) == 0
    public = json.loads(capsys.readouterr().out)
    assert next(a for a in public["accounts"] if a["key"] == "codex|")["cap_statuses"][0]["reported"]["usd"] == 21.40

    def broken(*args):
        raise OSError("synthetic read failure")

    monkeypatch.setattr(routing_sources, "spend_report", broken)
    public = routing.public_view(replay.view(), replay.now, "codex|")
    assert public["accounts"][0]["cap_statuses"][0]["reported"] is None


def test_partial_report_never_reaches_any_non_owner_surface(replay, monkeypatch):
    replay.configure(routing_cloud_copy=True, routing_cloud_copy_rules=True,
                     routing_cloud_copy_hold_words=True, routing_child_chars=400)
    for i in range(3):
        routing.add_rule(f"Ordinary synthetic rule {i} " + "x" * 900, via="app")
    routing.set_cap("Codex", kind="spend", usd=35, days=7, revision="fake-cap", via="app")
    monkeypatch.setattr(routing, "_telemetry_streams", lambda view: [stream(replay.now)])
    words, path = "PartialQuasarSentinel JuniperSentinel", "/private/synthetic-partial-spend/transcript.jsonl"
    session, project = "codex:synthetic-partial-session", "synthetic-partial-project"
    planted = {"usd": 21.40, "events": 1, "gaps": [{"runner": words, "from": path, "to": session,
                                                   "seconds": 10800, "project": project}]}
    monkeypatch.setattr(routing_sources, "spend_report", lambda *args: planted)
    view = replay.view()
    public = routing.public_view(view, replay.now)
    assert next(a for a in public["accounts"] if a["key"] == "codex|")["cap_statuses"][0]["reported"] == planted
    enriched = dict(view, accounts=[dict(a, reported=planted, cap_statuses=[{"reported": planted}]) for a in view["accounts"]])
    monkeypatch.setattr(routing, "read_view", lambda *a, **kw: enriched)
    monkeypatch.setattr(routing, "_start_view", lambda *a, **kw: enriched)
    outputs = {"push": routing.start_push(budget=2, session_id="synthetic-recipient")}
    start = routing.start_push(budget=2, moment="delegation", child_component="agent:synthetic", child_session="synthetic-child")
    assert start and start["text"]
    assert routing_delivery.acknowledge("synthetic-child", start["routing_changes"], now=replay.now)
    pages = []
    for _ in range(20):
        page = routing._child_page_push_inline("synthetic-child", "synthetic", replay.now)
        if not page:
            break
        pages.append(page)
        assert routing_delivery.acknowledge("synthetic-child", page["routing_changes"], now=replay.now)
    assert pages
    outputs["child"] = [start, pages, routing_delivery.snapshot("synthetic-child", "agent:synthetic")]
    error, outputs["mcp"] = replay.call(session_id="synthetic-mcp")
    assert not error and outputs["mcp"]["accounts"]
    outputs["replication"] = routing.replication_payload(
        [dict(e, reported=planted) for e in routing._read_all_events()[0]], bindings={})
    outputs["gateway"] = routing.gateway_projection(public, now=replay.now)
    outputs["doctor"] = routing.doctor_block(now=replay.now)
    # Only synthetic stores under the disposable replay root are inspected.
    outputs["logs"] = [p.read_text() for p in replay.root.rglob("*") if p.is_file() and p.suffix in (".jsonl", ".log")]
    assert outputs["push"]["text"] and outputs["replication"] and outputs["gateway"]["accounts"] and outputs["logs"]
    for surface, output in outputs.items():
        for leaf in strings(output):
            assert leaf != "reported", (surface, leaf)
            assert not any(secret in leaf for secret in (words, "PartialQuasarSentinel", "JuniperSentinel", path, session, project)), (surface, leaf)
