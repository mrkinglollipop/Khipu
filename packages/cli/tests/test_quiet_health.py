"""Health that is quiet unless something is wrong (review finding 9, 2026-11-05): doctor's false reds, the delivery
check's hour, the capture queue's "is a capture at risk", and the five-line summary."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from khipu import cli, embed, recall_prompt, routing, routing_health as health
from khipu.doctor_summary import health_summary
from tests import test_routing_acceptance as acceptance

replay = acceptance.replay


# ---- embed_coverage_ok ------------------------------------------------------------------------


def _embed_check(**coverage):
    base = {"episodes": {"missing": 0}, "topics": {"missing": 0}, "missing": 0, "stale": 0}
    with mock.patch("khipu.embed.coverage", return_value={**base, **coverage}), \
            mock.patch("khipu.embed.topics_embed_lag_minutes", return_value={"ok": True, "lag_minutes": 0}):
        return cli._doctor_embed_checks()[1]


def test_an_episode_with_no_text_is_not_a_missing_embedding():
    # What the real hub showed: one empty-summary episode has no vector and no chunk to embed.
    assert _embed_check(episodes={"missing": 1}, missing=0, stale=0) is True


def test_a_chunk_captured_minutes_ago_is_not_behind():
    assert _embed_check(missing=1, oldest_gap_minutes=4) is True
    assert _embed_check(stale=2, oldest_gap_minutes=60) is True


def test_a_chunk_unembedded_for_over_an_hour_is_red_and_an_unknown_age_is_never_fresh():
    assert _embed_check(missing=1, oldest_gap_minutes=61) is False
    assert _embed_check(missing=1, oldest_gap_minutes=None) is False
    assert _embed_check(stale=1) is False


class _Cursor:
    def __init__(self, oldest):
        self.oldest, self.args = oldest, None

    def execute(self, sql, args):
        self.args = args

    def fetchone(self):
        return (self.oldest,)


def test_the_age_of_the_oldest_gap_is_read_from_its_rows():
    now = datetime.now(timezone.utc)
    cur = _Cursor(now - timedelta(minutes=7))
    assert embed._oldest_gap_minutes(cur, [("episode", "5", 0)], [("topic", "slug", 1)]) == 7
    assert cur.args == (["5"], ["slug"])
    assert embed._oldest_gap_minutes(_Cursor(None), [], []) == 0  # no gap, no query
    assert embed._oldest_gap_minutes(_Cursor(None), [("episode", "5", 0)], []) is None  # gap, row not found


# ---- outbox_ok and at_risk --------------------------------------------------------------------


@pytest.mark.parametrize("box,ok,at_risk", [
    ({}, True, False),
    # A capture queued two minutes ago is on its way: neither red nor at risk.
    ({"pending": 3, "oldest_age_s": 120}, True, False),
    # Stuck for hours: red (behind), but safe on this Mac and retried, so not at risk.
    ({"pending": 3, "oldest_age_s": 7200}, False, False),
    # Held where nothing retries it: at risk.
    ({"dead": 2, "dead_waiting": 2}, False, True),
    ({"pending": 1, "oldest_age_s": 5, "unreadable": 1}, False, True),
    ({"pending": -1, "error": "OSError: no"}, False, True),
])
def test_the_queue_is_red_for_a_stuck_capture_and_says_whether_one_is_at_risk(box, ok, at_risk):
    base = {"pending": 0, "oldest_age_s": None, "unreadable": 0, "dead": 0, "dead_waiting": 0}
    with mock.patch("khipu.outbox.status", return_value={**base, **box}):
        local = cli._doctor_local_checks(None)
    assert local["outbox_ok"] is ok and local["outbox"]["at_risk"] is at_risk


# ---- routing delivery -------------------------------------------------------------------------


def _log(rows):
    path = recall_prompt._log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{routing._iso(at)} [khipu-prompt-recall] session={s} reason=no_hits ms=1 hits=[] "
                            f"legs=[] degraded=None routing={st}\n" for at, s, st in rows))


def test_skips_from_before_the_last_hour_no_longer_count(replay):
    now = acceptance.T0
    _log([(now - timedelta(hours=5, seconds=i), f"s{i}", "skipped:budget") for i in range(20)]
         + [(now - timedelta(minutes=10, seconds=i), f"t{i}", "none") for i in range(20)])
    result = health.prompt_health(now, 1.0)
    assert result["status"] == "green" and result["count"] == 20 and result["out_of_time"] == 0


def test_three_skips_in_the_last_hour_still_show(replay):
    now = acceptance.T0
    _log([(now - timedelta(minutes=10, seconds=i), f"t{i}", "skipped:budget" if i in (3, 6, 9) else "none") for i in range(20)])
    assert health.prompt_health(now, 1.0)["reasons"] == ["prompt-skips"]


def test_a_job_slot_with_no_account_is_not_a_delivery_fault(replay):
    lane = {"limited_since": routing._iso(replay.now - timedelta(hours=3))}
    result = health.doctor_block({"unmatched": [lane]}, replay.now, routing.settings())
    assert "unmatched-limit" not in result["reasons"]


# ---- the whole report -------------------------------------------------------------------------


@pytest.fixture
def green_doctor(tmp_path):
    from tests.test_cli_recall_quality import _green_doctor_patches

    patches = [p for p in _green_doctor_patches() if p.attribute != "path_setting"]
    patches += [mock.patch("khipu.config.path_setting", side_effect=lambda *a, **k: None),
                mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}),
                mock.patch("khipu.drift.recall_quality", return_value={})]
    for p in patches:
        p.start()
    yield tmp_path
    for p in patches:
        p.stop()


def _doctor(root, delivery):
    args = cli.build_parser().parse_args(["doctor", "--memory-root", str(root)])
    with mock.patch("khipu.routing.doctor_block", return_value={"warnings": [], "routing_delivery": delivery}), \
            redirect_stdout(io.StringIO()) as out:
        cli.cmd_doctor(args)
    return json.loads(out.getvalue())


def test_a_delivery_check_with_nothing_to_read_is_not_red_and_a_real_failure_is(green_doctor):
    quiet = _doctor(green_doctor, {"status": "unknown", "missing": ["recent hook outcomes"], "reasons": []})
    assert quiet["routing_delivery_ok"] is True
    failing = _doctor(green_doctor, {"status": "red", "reasons": ["prompt-skips"],
                                     "prompts": {"count": 10, "out_of_time": 4}})
    assert failing["routing_delivery_ok"] is False
    assert failing["summary"][2].startswith("Routing: ") and "4 of the last hour's 10 prompts" in failing["summary"][2]


def test_the_report_opens_with_five_lines_and_a_capture_at_risk_shows(green_doctor):
    out = _doctor(green_doctor, {"status": "green"})
    assert len(out["summary"]) == 5 and list(out)[0] == "summary"
    assert out["summary"][0] == "Saving memory: fine." and out["summary"][2] == "Routing: fine."
    with mock.patch("khipu.outbox.status", return_value={"pending": 0, "oldest_age_s": None, "unreadable": 0, "dead": 4,
                                                          "dead_waiting": 4, "fix": "Run `khipu outbox retry-dead`."}):
        risky = _doctor(green_doctor, {"status": "green"})
    assert risky["ok"] is False and risky["outbox"]["at_risk"] is True
    assert risky["summary"][0].startswith("Saving memory: 4 captures sit held on this Mac, safe but not in memory")
    assert "Fix: run `khipu outbox retry-dead`." in risky["summary"][0]


def test_each_summary_line_names_what_is_wrong_and_what_would_fix_it():
    lines = health_summary({"hub_ok": False, "backup_ok": False, "jobs_ok": False, "job_failures": ["nightly"],
                            "embed_coverage_ok": False, "embed_coverage": {"missing": 3, "stale": 1}})
    assert lines[1] == "Finding things: 4 search chunks unembedded for over an hour. Fix: run `khipu embed backfill`."
    assert lines[3].startswith("Database and disk: The database does not answer. Fix: ")
    assert lines[4] == "Jobs and other checks: Failing: nightly. Fix: run `khipu jobs status`."
    assert len(lines) == 5


# ---- review round: unreadable delivery state is never fine ------------------------------------


def test_idle_delivery_is_fine_but_anything_unreadable_is_not():
    idle = {"status": "unknown", "missing": ["recent hook outcomes", "live delivery identities"], "reasons": []}
    assert health.delivery_ok(idle) is True
    assert health.delivery_ok({"status": "not_applicable", "missing": []}) is True
    for why in ("delivery ledger unreadable", "delivery ledger malformed", "cross-Mac caught-up cursor",
                "gateway publication failed; retry pending", "hook outcome log unreadable",
                "delivery point is ahead of the clock", "hook outcome dated ahead of the clock"):
        assert health.delivery_ok({"status": "unknown", "missing": ["recent hook outcomes", why]}) is False, why
    assert health.delivery_ok({"status": "unknown", "missing": ["routing check"]}) is False
    assert health.delivery_ok({"status": "red", "reasons": ["prompt-skips"]}) is False


def test_a_mac_whose_every_identity_is_idle_is_not_unchecked_but_a_live_one_is():
    """An idle Mac (all identities green and idle, no hook outcomes in the hour) reads fine; one live identity
    without hook outcomes stays unknown."""
    idle_row = {"identity": "a", "status": "green", "idle": True}
    live_row = {"identity": "b", "status": "green"}
    base = {"status": "unknown", "missing": ["recent hook outcomes"], "reasons": []}
    assert health.delivery_ok(dict(base, identities=[idle_row, idle_row])) is True
    assert health.delivery_ok(dict(base, identities=[idle_row, live_row])) is False
    assert health.delivery_ok(dict(base, identities=[live_row])) is False
    other = dict(base, missing=["recent hook outcomes", "delivery ledger unreadable"], identities=[idle_row])
    assert health.delivery_ok(other) is False


def test_a_malformed_delivery_ledger_is_red_and_the_summary_says_it_could_not_be_checked(replay):
    folder = recall_prompt._dedup_path("ledger").parent
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "recall--x.routing.json").write_text("{not json")
    block = health.doctor_block({"unmatched": []}, replay.now, routing.settings())
    assert "delivery ledger unreadable" in block["missing"] and health.delivery_ok(block) is False
    line = health_summary({"routing_delivery": block, "routing_delivery_ok": False})[2]
    assert "could not confirm that conversations received the latest account advice" in line


def test_a_future_dated_hook_outcome_is_not_read_as_idle(replay):
    now = acceptance.T0
    _log([(now + timedelta(minutes=30), "s1", "none")])
    result = health.prompt_health(now, 1.0)
    assert "hook outcome dated ahead of the clock" in result["missing"] and result["status"] == "unknown"
    assert health.delivery_ok(result) is False
    _log([])
    assert health.delivery_ok(health.prompt_health(now, 1.0)) is True  # no prompts at all: idle


def test_an_unreadable_hook_log_is_not_idle(replay, monkeypatch):
    def fail(*a, **k):
        raise OSError("denied")

    monkeypatch.setattr(recall_prompt, "_tail_text", fail)
    result = health.prompt_health(acceptance.T0, 1.0)
    assert result["missing"] == ["hook outcome log unreadable"] and health.delivery_ok(result) is False


# ---- review round: a held capture is at risk, whatever its age --------------------------------


def test_an_old_refused_capture_is_at_risk_and_the_summary_says_so_without_changing_outbox_ok(green_doctor):
    with mock.patch("khipu.outbox.status", return_value={
            "pending": 0, "oldest_age_s": None, "unreadable": 0, "dead": 1, "dead_waiting": 0, "announce_refused_old": 1}):
        out = _doctor(green_doctor, {"status": "green"})
    assert out["outbox_ok"] is True  # unchanged: the promote gate reads it
    assert out["outbox"]["at_risk"] is True
    assert out["summary"][0].startswith("Saving memory: 1 capture sits held on this Mac, safe but not in memory")


# ---- review round: the five lines cannot all say fine while doctor fails ----------------------


def _all_good():
    return {**{k: True for k in cli.OK_GATES}, "routing_delivery_ok": True, "outbox": {}}


@pytest.mark.parametrize("key", cli.OK_GATES)
def test_every_check_that_feeds_ok_shows_in_the_summary_when_it_fails(key):
    report = _all_good()
    assert health_summary(report) == [f"{a}: fine." for a in
                                      ("Saving memory", "Finding things", "Routing", "Database and disk", "Jobs and other checks")]
    report[key] = False
    lines = health_summary(report)
    assert any(not line.endswith("fine.") for line in lines), key
    assert "Fix:" in "".join(lines) and key not in "".join(lines)  # plain words, not the key


def test_every_gate_has_words_and_every_ok_key_in_a_real_report_is_a_gate_or_named_as_not_one(green_doctor):
    from khipu.doctor_summary import WORDS

    assert set(cli.OK_GATES) <= set(WORDS), set(cli.OK_GATES) - set(WORDS)
    out = _doctor(green_doctor, {"status": "green"})
    not_gates = {"routing_delivery_ok", "notes_reconcile_ok", "embed_provider_ok", "commitments_hygiene_ok", "mark_stale_ok"}
    keys = {k for k in out if k.endswith("_ok")}
    assert keys <= set(cli.OK_GATES) | not_gates, keys - set(cli.OK_GATES) - not_gates
    assert set(cli.OK_GATES) <= keys
    assert out["ok"] == all(out[k] for k in cli.OK_GATES)
    if not out["ok"]:
        assert any(not line.endswith("fine.") for line in out["summary"])


# ---- last round --------------------------------------------------------------------------------


def test_a_cursor_or_publication_dated_in_the_future_could_not_be_checked(replay, monkeypatch):
    from khipu import routing_gateway_copy, routing_replication

    future = routing._iso(replay.now + timedelta(hours=2))
    monkeypatch.setattr(routing, "_replication_live", lambda: True)
    monkeypatch.setattr(routing_replication, "status",
                        lambda now=None: {"caught_up_at": future, "age_seconds": 0, "stale": False})
    block = health.doctor_block({"unmatched": []}, replay.now, routing.settings())
    assert "cross-Mac caught-up cursor" in block["missing"] and block["replication"]["status"] == "unknown"
    monkeypatch.setattr(routing, "_replication_live", lambda: False)
    monkeypatch.setattr(routing_gateway_copy, "status", lambda: {"gateway_rows": [], "published_at": future,
                                                                 "health_refreshed_at": routing._iso(replay.now)})
    block = health.doctor_block({"unmatched": []}, replay.now, dict(routing.settings(), routing_cloud_copy=True))
    assert "gateway publication dated ahead of the clock" in block["missing"]
    assert health.delivery_ok(block) is False


def test_no_hook_outcomes_is_only_idle_when_no_conversation_is_live():
    idle = {"status": "unknown", "missing": ["recent hook outcomes", "live delivery identities"], "identities": []}
    assert health.delivery_ok(idle) is True
    live = {"status": "unknown", "missing": ["recent hook outcomes"], "identities": [{"identity": "x", "status": "green"}]}
    assert health.delivery_ok(live) is False
    assert "could not confirm that conversations received the latest account advice" in health_summary({"routing_delivery": live, "routing_delivery_ok": False})[2]


def test_a_blank_queued_at_is_unreadable_even_beside_a_young_job(tmp_path, monkeypatch):
    from khipu import outbox

    monkeypatch.setenv("KHIPU_OUTBOX", str(tmp_path))
    outbox.enqueue({"ts": "2026-08-17T18:00:00Z", "summary": "a"}, reason="t")
    outbox.enqueue({"ts": "2026-08-17T19:00:00Z", "summary": "b"}, reason="t")
    blank = outbox.jobs()[0]
    job = json.loads(blank.read_text())
    job["queued_at"] = ""
    blank.write_text(json.dumps(job))
    status = outbox.status()
    assert status["pending"] == 2 and status["unreadable"] == 1


def test_the_summary_names_skips_and_unreadable_evidence_together():
    block = {"status": "red", "reasons": ["prompt-skips"], "prompts": {"count": 10, "out_of_time": 4},
             "missing": ["delivery ledger unreadable"]}
    line = health_summary({"routing_delivery": block, "routing_delivery_ok": False})[2]
    assert "4 of the last hour's 10 prompts" in line and "could not confirm that conversations received the latest account advice" in line


def test_one_late_prompt_out_of_thirteen_is_not_red(replay):
    _log([(acceptance.T0, f"s{i}", "skipped:budget" if i == 0 else "none") for i in range(13)])
    result = health.prompt_health(acceptance.T0)
    assert result["out_of_time"] == 1 and result["count"] == 13
    assert result["status"] == "green"


def test_delivery_words_explain_the_effect_without_internal_names():
    block = {"status": "red", "reasons": ["cross-mac-stale", "gateway-stale"],
             "missing": ["gateway delivered copy revision"]}
    line = health_summary({"routing_delivery": block, "routing_delivery_ok": False})[2]
    assert "other Mac may still be using older account advice" in line
    assert "cloud conversations received" in line
    assert "Settings" not in line and "cross-mac-stale" not in line and "gateway" not in line


def test_routing_fix_requires_no_owner_chore():
    line = health_summary({"routing_delivery_ok": False,
                           "routing_delivery": {"reasons": ["prompt-skips"],
                                                "prompts": {"count": 13, "out_of_time": 3}}})[2]
    assert ("nothing for you to do; Khipu tries again on later prompts. "
            "If this is still here tomorrow, tell a conversation") in line
    assert "ask the conversation to check" not in line


# ---- the notes watcher is the only way an edited note is picked up -------------------------------


def test_a_mac_without_the_notes_watcher_says_so_and_names_the_install_command(monkeypatch):
    from khipu import jobs
    from khipu.doctor_summary import failing

    monkeypatch.setattr(jobs, "_job_entry", lambda name: {"plist_loaded": False})
    row = jobs.notes_watch_health()
    assert row["ok"] is False and "not installed" in row["error"]
    assert row["fix"] == "run `khipu jobs install notes_watch`"
    report = {"nightly_steps": {"notes_reconcile_ok": row}, "nightly_steps_ok": False}
    got = failing(report)
    assert any("notes_watch (not installed)" in what and "khipu jobs install notes_watch" in fix
               for _area, what, fix in got), got


def test_a_mac_with_the_notes_watcher_loaded_is_green(monkeypatch):
    from khipu import jobs

    from khipu import launchd_gen

    monkeypatch.setattr(jobs, "_job_entry", lambda name: {"plist_loaded": True})
    monkeypatch.setattr(jobs, "launchd_state", lambda label: {"loaded": True, "spawn_failed": False, "last_exit": 0})
    monkeypatch.setattr(launchd_gen, "watch_list_drift", lambda: {"installed": ["/m/a"], "unwatched": [], "stale": []})
    assert jobs.notes_watch_health()["ok"] is True


def test_the_health_check_names_a_notes_folder_that_exists_but_is_not_watched(monkeypatch):
    from khipu import jobs, launchd_gen

    monkeypatch.setattr(jobs, "_job_entry", lambda name: {"plist_loaded": True})
    monkeypatch.setattr(jobs, "launchd_state", lambda label: {"loaded": True, "spawn_failed": False, "last_exit": 0})
    monkeypatch.setattr(launchd_gen, "watch_list_drift",
                        lambda: {"installed": ["/m/a"], "unwatched": ["/m/b"], "stale": []})
    row = jobs.notes_watch_health()
    assert row["ok"] is False and "/m/b" in row["error"] and "not watching 1 notes folder" in row["error"]
    assert row["fix"] == "run `khipu jobs install notes_watch`"
