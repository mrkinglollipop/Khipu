"""`khipu health --json`: five plain rows over the doctor report."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from khipu import cli, doctor_summary, health_five

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
GB = 1000 ** 3


def report(**over) -> dict:
    base = {k: True for k in health_five.every_key()}
    base.update({
        "routing": {"record": True, "folded_at": "2026-11-10T11:58:00Z", "version": 7,
                    "sources": {"runners": [{"name": "codex", "heartbeat_at": "2026-11-10T11:50:00Z", "silent": False},
                                            {"name": "claude", "heartbeat_at": "2026-11-10T11:55:00Z", "silent": False}]}},
        "disk_space": {"volumes": [{"free_bytes": 120 * GB}, {"free_bytes": 80 * GB}]},
        "backup": {"pg_dump": {"status": "ok", "created_at": "2026-11-10T03:00:00+00:00"}},
    })
    base.update(over)
    return base


def doc(**over) -> dict:
    return health_five.build(report(**over), now=NOW, mac="M1")


def test_shape_and_healthy_example():
    d = doc()
    assert list(d) == ["checked_at", "mac", "headline", "areas", "jobs_running", "silent_runners", "free_gb",
                       "last_backup_at", "agent_note"]
    assert d["headline"] == "Everything is working."
    assert [a["name"] for a in d["areas"]] == list(doctor_summary.AREAS)
    assert all(set(a) == {"key", "name", "state", "line", "khipu_doing", "you_do", "link"} for a in d["areas"])
    assert {a["state"] for a in d["areas"]} == {"ok"}
    assert (d["jobs_running"], d["free_gb"], d["last_backup_at"], d["mac"]) == (2, 80.0, "2026-11-10T03:00:00Z", "M1")
    assert d["areas"][4]["line"] == "2 jobs are running."
    json.dumps(d)


def test_every_gate_lands_in_exactly_one_area_and_every_area_is_reachable():
    keys = health_five.every_key()
    assert set(cli.OK_GATES) <= set(keys)
    assert len(set(keys)) == len(keys)
    for key in keys:
        assert health_five.area_of(key) in range(5)
        d = doc(**{key: False}) if key != "routing_delivery_ok" else doc(routing_delivery_ok=False)
        if key == "routing_delivery_ok":
            assert {a["state"] for a in d["areas"]} == {"ok"}  # not read
            continue
        bad = [i for i, a in enumerate(d["areas"]) if a["state"] != "ok"]
        assert bad == [health_five.area_of(key)], key
    assert {health_five.area_of(k) for k in keys} == set(range(5))


def test_a_problem_says_what_khipu_is_doing_and_what_you_do():
    d = doc(hub_ok=False, disk_space_ok=False)
    row = d["areas"][3]
    assert row["state"] == "err"
    assert "database" in row["line"].lower() and row["khipu_doing"]
    assert "Free up some space on this Mac." in row["you_do"] and "internet connection" in row["you_do"]
    assert d["headline"].startswith("Database and disk: ")
    assert "hub_ok" in d["agent_note"] and "khipu status" in d["agent_note"]


def test_you_do_never_names_a_command():
    for key in health_five.every_key():
        for d in (doc(**{key: False}), doc(**{key: False, "outbox": {"dead": 2, "at_risk": True}})):
            for a in d["areas"]:
                assert "`" not in a["you_do"] + a["line"] + a["khipu_doing"]
                assert not re.search(r"\bkhipu \w+|\bsudo\b|--\w", a["you_do"]), a["you_do"]
    # commands live in the note
    assert "`khipu embed backfill`" in doc(embed_coverage_ok=False, embed_coverage={"missing": 3})["agent_note"]


def test_a_failing_check_with_no_wording_lands_in_the_fifth_area_named(monkeypatch):
    monkeypatch.setattr(cli, "OK_GATES", (*cli.OK_GATES, "mystery_thing_ok"))
    assert health_five.area_of("mystery_thing_ok") == 4
    d = doc(mystery_thing_ok=False)
    assert d["areas"][4]["state"] == "warn"
    assert "Mystery thing is failing" in d["areas"][4]["line"]
    assert "mystery_thing_ok" in d["agent_note"]
    assert "agent" in d["areas"][4]["you_do"] and "`" not in d["areas"][4]["you_do"]


def test_routing_reads_the_record_not_the_delivery_ledger():
    d = doc(routing_delivery_ok=False, routing_delivery={"status": "red", "reasons": ["prompt-skips"]})
    assert d["areas"][2]["state"] == "ok"
    assert "routing_delivery" not in json.dumps(d)
    assert doc(routing={"record": False, "sources": {}})["areas"][2]["state"] == "warn"
    old = doc(routing={"record": True, "folded_at": "2026-11-08T00:00:00Z", "sources": {"runners": []}})
    assert old["areas"][2]["state"] == "warn" and "out of date" in old["areas"][2]["line"]
    broken = doc(routing={"error": "OSError: nope"})
    assert broken["areas"][2]["state"] == "err" and "OSError" in broken["agent_note"]


def test_jobs_names_a_silent_runner_and_a_week_old_silence_stays_quiet():
    runners = [{"name": "codex", "heartbeat_at": "2026-11-10T09:00:00Z", "silent": True},
               {"name": "grok", "heartbeat_at": "2026-10-01T09:00:00Z", "silent": True},
               {"name": "claude", "heartbeat_at": "2026-11-10T11:55:00Z", "silent": False}]
    d = doc(routing={"record": True, "folded_at": "2026-11-10T11:58:00Z", "sources": {"runners": runners}})
    assert d["jobs_running"] == 1
    assert [r["name"] for r in d["silent_runners"]] == ["codex", "grok"]
    assert d["silent_runners"][0] == {"name": "codex", "mac": "M1", "since": "2026-11-10T09:00:00Z"}
    assert d["areas"][4]["state"] == "warn" and "codex" in d["areas"][4]["line"] and "grok" not in d["areas"][4]["line"]
    only_old = doc(routing={"record": True, "folded_at": "2026-11-10T11:58:00Z", "sources": {"runners": runners[1:]}})
    assert only_old["areas"][4]["state"] == "ok"


def test_the_note_is_redacted():
    d = doc(hub_ok=False, status={"dsn": "postgres://u:hunter2@h/db"})
    assert "hunter2" not in d["agent_note"]
    secret = "sk-ant-api03-" + "A" * 40
    assert secret not in health_five.build(report(synth_budget_ok=False, drift_ok=False,
                                                  outbox={"error": secret, "at_risk": True}), now=NOW, mac="M1")["agent_note"]


def test_no_report_is_unknown_not_fine():
    d = health_five.build({}, now=NOW, mac="M1")
    assert {a["state"] for a in d["areas"]} == {"unknown"}
    assert d["headline"] != "Everything is working."
    assert d["free_gb"] is None and d["jobs_running"] is None


def test_doctor_summary_is_unchanged_by_the_keyed_view():
    bad = report(jobs_ok=False, nightly_steps_ok=False, outbox_ok=False, outbox={"dead": 1, "at_risk": True})
    assert doctor_summary.failing(bad) == list(dict.fromkeys((a, w, f) for _k, a, w, f in doctor_summary.failing_keyed(bad)))
    assert len([x for x in doctor_summary.failing(bad) if x[0] == 4]) == 1  # the two job flags still read as one line


def test_health_parses_and_prints_json(monkeypatch, capsys):
    monkeypatch.setattr(health_five, "run_doctor", lambda: (report(), ""))
    monkeypatch.setattr(health_five, "mac_label", lambda: "M1")
    assert cli.main(["health", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["headline"] == "Everything is working." and len(out["areas"]) == 5
