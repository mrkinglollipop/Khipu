"""Running now (`khipu running --json`, app slice B4): the work view's jobs joined with the runners' heartbeat,
shared between Macs through the B3 status row."""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from khipu import db, macs, running_now, work
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_macs import ME, YOU, put_other, world  # noqa: F401
from tests.test_work import ACCT_ID, NOW, _t, entry, job, put, summary, view_for

BEAT = "2026-10-15T19:59:50Z"


def status(name="lanes", **over):
    base = {"name": name, "heartbeat_at": BEAT, "silent": False, "unreadable": False}
    base.update(over)
    return base


def runners_for(tmp_path, jobs, statuses, runner="lanes", **cfg_over):
    path = put(tmp_path, summary(jobs=jobs, prs=[], trains=[], runner={"name": runner, "interval_s": 20}))
    view = view_for(path, work_job_quiet_after=300, work_finished_for=600, **cfg_over)
    return running_now.build_runners(view, statuses, NOW), view


def test_one_mac_with_jobs_joined_to_its_runner_heartbeat(tmp_path):
    runners, view = runners_for(tmp_path, [job("a", model="Sonnet 5.5"),
                                           job("b", state="waiting", summary="Waiting for a review", now=[])],
                                [status()])
    assert [r["name"] for r in runners] == ["lanes"]
    [r] = runners
    assert r["heartbeat_at"] == BEAT and r["silent"] is False and r["silent_since"] is None
    first, second = r["jobs"]
    assert first == {"summary": "Fixing the two review findings", "now_line": "Running the full test suite",
                     "now_at": _t(30), "state": "running", "quiet": False, "quiet_for_s": None, "account_key": ACCT_ID,
                     "account_label": "Claude second", "model": "Sonnet 5.5", "started_at": _t(600),
                     "updated_at": _t(30), "ended_at": None}
    assert second["state"] == "waiting" and second["now_line"] is None and second["now_at"] is None
    assert list(first) == list(running_now.JOB_FIELDS) and list(r) == list(running_now.RUNNER_FIELDS)
    # not pull requests, trains or the ledger
    assert "prs" not in json.dumps(runners) and view["counts"]["running"] == 1


def test_a_runner_with_no_heartbeat_match_shows_no_report_yet(tmp_path):
    runners, _ = runners_for(tmp_path, [job()], [status("someone-else")], runner="lanes")
    r = next(x for x in runners if x["name"] == "lanes")
    assert [x["name"] for x in runners] == ["lanes", "someone-else"] and r["heartbeat_at"] is None and r["silent"] is False
    assert r["jobs"][0]["state"] == "running"
    names = [x["name"] for x in running_now.build_runners({"projects": [], "live": []}, [status("someone-else")], NOW)]
    assert names == ["someone-else"]  # a runner that reports but has no jobs is still listed, empty


def test_a_silent_runner_shows_its_jobs_as_unknown_with_what_it_last_said(tmp_path):
    silent = status(heartbeat_at="2026-10-15T19:40:00Z", silent=True)
    [r], _ = runners_for(tmp_path, [job("a"), job("b", state="done", ended_at=_t(60), updated_at=_t(60))], [silent])
    assert r["silent"] is True and r["silent_since"] == "2026-10-15T19:40:00Z"
    live, ended = r["jobs"]
    assert live["state"] == "unknown" and live["now_line"] == "Running the full test suite" and live["now_at"] == _t(30)
    assert live["quiet"] is False
    assert ended["state"] == "done"  # a finished job is still finished
    assert r["last_finished_at"] == _t(60)
    unreadable = status(unreadable=True)
    [u], _ = runners_for(tmp_path, [job()], [unreadable])
    assert u["jobs"][0]["state"] == "unknown"


def test_a_quiet_job_says_how_long(tmp_path):
    [r], _ = runners_for(tmp_path, [job("a", updated_at=_t(900), now=[{"at": _t(900), "text": "Still compiling"}])],
                         [status()])
    [j] = r["jobs"]
    assert j["state"] == "running" and j["quiet"] is True and j["quiet_for_s"] == 900 and j["now_line"] == "Still compiling"
    counts = running_now._counts([{"stale": False, "runners": [r]}])
    assert counts == {"running": 1, "waiting": 0}


def test_an_ended_job_is_kept_for_work_finished_for_then_goes(tmp_path):
    fresh = job("a", state="done", ended_at=_t(300), updated_at=_t(300))
    old = job("b", state="failed", ended_at=_t(1200), updated_at=_t(1200))
    [r], _ = runners_for(tmp_path, [fresh, old], [status()])
    assert [j["state"] for j in r["jobs"]] == ["done"] and r["jobs"][0]["ended_at"] == _t(300)
    assert r["last_finished_at"] == _t(300)
    [gone], _ = runners_for(tmp_path, [old], [status()])
    assert gone["jobs"] == [] and gone["last_finished_at"] is None


def test_the_projection_keeps_only_the_listed_fields_and_no_path():
    dirty = [{"name": "lanes", "heartbeat_at": BEAT, "silent": False, "home": "/Users/matt", "transcript": "secret words",
              "jobs": [{"summary": "Fix the thing in /Users/matt/Code/x/secret.py now", "now_line": "x" * 300,
                        "now_at": "2026-10-15T19:59:00Z", "state": "running", "prompt": "do the private thing",
                        "account_key": "claude|/Users/matt/.claude", "account_label": "/Users/matt",
                        "model": "Opus", "cwd": "/Users/matt"},
                       {"summary": "bad state", "state": "exploding"}, {"state": "running"}, "nonsense"]},
             {"name": "/Users/matt/runner", "jobs": []}, "nonsense"]
    [runner] = running_now.project_runners(dirty)
    assert list(runner) == list(running_now.RUNNER_FIELDS)
    [j] = runner["jobs"]
    assert list(j) == list(running_now.JOB_FIELDS)
    assert len(j["now_line"]) <= running_now.NOW_CHARS and j["account_key"] is None and j["account_label"] is None
    blob = json.dumps(runner)
    for leak in ("/Users", "secret", "private", "transcript", "cwd", "home", "claude|"):
        assert leak not in blob
    assert running_now.project_runners("nonsense") is None and running_now.project_runners([]) == []


def test_the_row_carries_the_running_part_as_a_closed_list(world):  # noqa: F811
    runners = [{"name": "lanes", "heartbeat_at": BEAT, "silent": False, "silent_since": None, "last_finished_at": None,
                "jobs": [{k: None for k in running_now.JOB_FIELDS} | {"summary": "Fixing", "state": "running"}]}]
    row = macs.project({"name": "M1", "running": {"count": 1, "silent": [], "runners": runners, "extra": "x"}})
    assert list(row["running"]) == list(macs.RUNNING_FIELDS) and row["running"]["runners"][0]["jobs"][0]["summary"] == "Fixing"
    assert macs.project({"running": {"count": 1, "silent": []}})["running"]["runners"] is None
    macs.publish({"name": "M1", "running": {"count": 1, "silent": [], "runners": runners}})
    world.execute("UPDATE mac_status SET running = jsonb_set(running, '{runners,0,jobs,0,summary}', "
                  "'\"Fix /Users/matt/x/y.py\"'::jsonb) || '{\"cwd\": \"/Users/matt\"}'::jsonb WHERE mac = %s", (ME,))
    world.commit()
    [back], how = macs.read_rows()
    assert how == "ok" and "/Users" not in json.dumps(back) and "cwd" not in back["running"]


def test_this_macs_row_publishes_the_running_jobs_khipu_running_last_kept(world, monkeypatch):  # noqa: F811
    """G7: the timer and health publish what `khipu running` kept; they never build the work view themselves."""
    runners = [{"name": "lanes", "heartbeat_at": BEAT, "silent": False, "silent_since": None, "last_finished_at": None,
                "jobs": []}]
    built = []
    # nobody has looked yet: the row has no running jobs, and nothing built the view to find some
    monkeypatch.setattr(running_now, "this_mac_runners", lambda *a, **k: built.append("runners") or runners)
    monkeypatch.setattr(work, "build_view", lambda *a, **k: built.append("view") or {"projects": [], "live": []})
    assert macs.publish()["ok"]
    [mine] = [r for r in macs.read_rows()[0] if r["mac"] == ME]
    assert (mine["running"] or {}).get("runners") is None and built == []
    # someone looks (`khipu running`): the runners are kept ...
    out = running_now.view()
    assert out["macs"][0]["runners"][0]["name"] == "lanes"
    built.clear()
    # ... and the next publish carries them, still without building anything
    assert macs.publish()["ok"]
    [mine] = [r for r in macs.read_rows()[0] if r["mac"] == ME]
    assert mine["running"]["runners"][0]["name"] == "lanes" and built == []
    assert macs.local_row()["running"]["runners"][0]["name"] == "lanes"
    # runners kept more than 15 minutes ago are not published as if current
    kept = macs._remembered()
    macs._write_cache(dict(kept, runners_at="2026-01-01T00:00:00Z"))
    assert macs.publish()["ok"]
    [mine] = [r for r in macs.read_rows()[0] if r["mac"] == ME]
    assert (mine["running"] or {}).get("runners") is None and built == []


def test_health_publishes_the_kept_running_part_and_builds_no_work_view(world, monkeypatch, capsys):  # noqa: F811
    from khipu import cli, health_five
    from tests.test_health_five import report

    runners = [{"name": "lanes", "heartbeat_at": BEAT, "silent": False, "silent_since": None, "last_finished_at": None,
                "jobs": []}]
    monkeypatch.setattr(running_now, "this_mac_runners", lambda *a, **k: runners)
    running_now.view()
    built = []
    monkeypatch.setattr(running_now, "this_mac_runners", lambda *a, **k: built.append("runners") or runners)
    monkeypatch.setattr(work, "build_view", lambda *a, **k: built.append("view") or {"projects": [], "live": []})
    monkeypatch.setattr(health_five, "run_doctor", lambda: (report(), ""))
    assert cli.main(["health", "--json"]) == 0
    capsys.readouterr()
    [mine] = [r for r in macs.read_rows()[0] if r["mac"] == ME]
    assert mine["running"]["runners"][0]["name"] == "lanes" and built == []
    # health's own count/silent part is refreshed, the kept runners stay (and survive remember_health)
    assert mine["running"]["count"] == 2 and macs._remembered()["running"]["runners"][0]["name"] == "lanes"


def test_a_publish_with_the_hub_known_down_builds_nothing(world, monkeypatch):  # noqa: F811
    called = []
    monkeypatch.setattr("khipu.hub_reach.known_unreachable", lambda: {"since": "x"})
    monkeypatch.setattr(macs, "local_row", lambda *a, **k: called.append("row") or {})
    monkeypatch.setattr(running_now, "this_mac_runners", lambda *a, **k: called.append("runners"))
    monkeypatch.setattr(db, "connect", lambda **kw: called.append("connect"))
    assert macs.publish() == {"ok": False, "why": "hub_unreachable"} and called == []


def other_runners():
    return [{"name": "codex", "heartbeat_at": "2026-11-10T12:00:00Z", "silent": False, "silent_since": None,
             "last_finished_at": None,
             "jobs": [{"summary": "Reviewing the pull request", "now_line": "Reading the diff", "now_at": "2026-11-10T12:00:00Z",
                       "state": "running", "quiet": False, "quiet_for_s": None, "account_key": "acct-aaaaaaaaaaaa",
                       "account_label": "Codex", "model": "gpt-6.1-sol", "started_at": "2026-11-10T11:00:00Z",
                       "updated_at": "2026-11-10T12:00:00Z", "ended_at": None}]}]


def mine_runners(*a, **k):
    return running_now.build_runners(
        {"projects": [{"jobs": [dict(job("a"), project="atlas", runner="lanes",
                                      account_ref={"id": ACCT_ID, "label": "Claude second", "matched": True})]}],
         "live": [{"project": "atlas", "id": "a"}]}, [status()], NOW)


def test_two_macs_are_merged_this_one_first(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(macs, "mac_label", lambda: "M1")
    monkeypatch.setattr(running_now, "this_mac_runners", mine_runners)
    assert put_other(running={"count": 1, "silent": [], "runners": other_runners()})["ok"]
    out = running_now.view(now=NOW)
    assert [m["name"] for m in out["macs"]] == ["M1", "M5"] and [m["this_mac"] for m in out["macs"]] == [True, False]
    me, other = out["macs"]
    assert me["mac"] == ME and me["stale"] is False and me["as_of"] == "2026-10-15T20:00:00Z"
    assert other["mac"] == YOU and other["stale"] is False and other["runners"][0]["jobs"][0]["model"] == "gpt-6.1-sol"
    assert out["counts"] == {"running": 2, "waiting": 0} and out["shared"] is True and "note" not in out
    assert set(out) == {"updated_at", "macs", "counts", "shared"}
    # a stale row is shown, flagged, and not counted as running now
    world.execute("UPDATE mac_status SET seen_at = clock_timestamp() - interval '20 minutes' WHERE mac = %s", (YOU,))
    world.commit()
    stale = running_now.view(now=NOW)
    assert stale["macs"][1]["stale"] is True and stale["counts"] == {"running": 1, "waiting": 0}


def test_without_the_other_macs_part_this_mac_is_shown_alone_labelled(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(running_now, "this_mac_runners", mine_runners)
    # no other row at all
    out = running_now.view(now=NOW)
    assert len(out["macs"]) == 1 and out["shared"] is False and "No other Mac has reported yet" in out["note"]
    # an older build's row: running without the runners part
    put_other(running={"count": 2, "silent": []})
    old = running_now.view(now=NOW)
    assert len(old["macs"]) == 1 and "older Khipu" in old["note"] and old["counts"]["running"] == 1
    # a row with no running at all
    put_other(running=None)
    assert len(running_now.view(now=NOW)["macs"]) == 1
    # the table missing
    world.execute("DROP TABLE mac_status")
    world.commit()
    gone = running_now.view(now=NOW)
    assert len(gone["macs"]) == 1 and "not on the hub yet" in gone["note"]


def test_the_command_always_exits_zero_even_with_no_hub_and_a_broken_work_view(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")

    def down(**kw):
        raise OSError("hub down")

    monkeypatch.setattr(db, "connect", down)
    monkeypatch.setattr(work, "build_view", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope")))
    assert running_now.cmd_running(None) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["macs"][0]["runners"] == [] and out["macs"][0]["this_mac"] is True and out["shared"] is False
    assert "could not be read" in out["note"]
    monkeypatch.setattr(running_now, "view", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert running_now.cmd_running(None) == 0
    assert json.loads(capsys.readouterr().out)["macs"] == []


def test_a_job_with_no_runner_name_is_grouped_under_an_unnamed_runner(tmp_path):
    runners, _ = runners_for(tmp_path, [job()], [], runner=None)
    assert [r["name"] for r in runners] == [running_now.UNNAMED] and runners[0]["heartbeat_at"] is None


# ---- the sweep's last stage keeps this Mac's running part fresh itself -----------------------------------------------


def _sweep(monkeypatch, capsys, calls):
    import argparse

    from khipu import cli, jobs, owner_direction, replica_heal, routing, storage

    monkeypatch.setattr(storage, "sweep_if_due", lambda: calls.append("storage") or {"ok": True})
    monkeypatch.setattr(replica_heal, "run", lambda: calls.append("replica") or {"ok": True, "action": "none"})
    monkeypatch.setattr(owner_direction, "timer", lambda: calls.append("owner") or {"ok": True})
    monkeypatch.setattr(jobs, "_write_job_state", lambda *a: calls.append("job_state"))
    monkeypatch.setattr(routing, "poll_refold", lambda: calls.append("routing_copy") or {"ok": True})
    rc = cli.cmd_snapshot(argparse.Namespace(snapshot_cmd="heal"))
    return rc, json.loads(capsys.readouterr().out)


def _lanes():
    return [{"name": "lanes", "heartbeat_at": BEAT, "silent": False, "silent_since": None, "last_finished_at": None,
             "jobs": []}]


def _mine(rows=None):
    return [r for r in macs.read_rows()[0] if r["mac"] == ME]


def test_the_sweep_refreshes_and_publishes_the_running_jobs_with_no_khipu_running_before(world, monkeypatch, capsys):  # noqa: F811
    calls: list = []
    monkeypatch.setattr(running_now, "this_mac_runners", lambda *a, **k: calls.append("runners") or _lanes())
    assert macs._remembered() == {}  # nobody ran `khipu running`
    rc, out = _sweep(monkeypatch, capsys, calls)
    assert rc == 0 and out["mac_status"] == {"ok": True, "running": "fresh"}
    assert calls == ["storage", "replica", "owner", "job_state", "routing_copy", "runners"]  # routing copy first
    [mine] = _mine()
    assert mine["running"]["runners"][0]["name"] == "lanes"


def test_a_failing_build_still_publishes_the_rest_of_the_row_and_the_earlier_stages_ran_first(world, monkeypatch, capsys):  # noqa: F811
    calls: list = []
    monkeypatch.setattr(running_now, "this_mac_runners", lambda *a, **k: calls.append("runners") or None)
    rc, out = _sweep(monkeypatch, capsys, calls)
    assert rc == 0 and out["mac_status"] == {"ok": True, "running": "failed"}
    assert calls.index("routing_copy") < calls.index("runners")
    [mine] = _mine()
    assert mine["version"] == "0.4.15 (aaaa1111)" and (mine["running"] or {}).get("runners") is None
    # a kept part under 15 minutes old is still published; a build that raises counts as failed too
    macs.remember_runners(_lanes())
    monkeypatch.setattr(running_now, "this_mac_runners", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    rc, out = _sweep(monkeypatch, capsys, [])
    assert rc == 0 and out["mac_status"] == {"ok": True, "running": "failed"}
    assert _mine()[0]["running"]["runners"][0]["name"] == "lanes"


def test_a_slow_build_is_cut_off_and_the_row_is_still_published(world, monkeypatch, capsys):  # noqa: F811
    import time

    from khipu import cli

    calls: list = []
    monkeypatch.setattr(cli, "MAC_STATUS_MAX_S", 1.0)
    monkeypatch.setattr(running_now, "this_mac_runners", lambda *a, **k: calls.append("runners") or time.sleep(60))
    started = time.monotonic()
    rc, out = _sweep(monkeypatch, capsys, calls)
    assert time.monotonic() - started < 10
    assert rc == 0 and out["mac_status"] == {"ok": True, "running": "failed"}
    assert calls.index("routing_copy") < calls.index("runners") and _mine()


def test_with_the_hub_known_down_the_sweep_builds_nothing(world, monkeypatch, capsys):  # noqa: F811
    calls: list = []
    monkeypatch.setattr("khipu.hub_reach.known_unreachable", lambda: {"since": "x"})
    monkeypatch.setattr(running_now, "this_mac_runners", lambda *a, **k: calls.append("runners") or _lanes())
    monkeypatch.setattr(work, "build_view", lambda *a, **k: calls.append("view"))
    monkeypatch.setattr(db, "connect", lambda **kw: calls.append("connect"))
    rc, out = _sweep(monkeypatch, capsys, calls)
    assert rc == 0 and out["mac_status"] == {"ok": False, "why": "hub_unreachable", "running": "skipped"}
    assert "runners" not in calls and "view" not in calls and "connect" not in calls
