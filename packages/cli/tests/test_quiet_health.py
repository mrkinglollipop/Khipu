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


def test_a_skip_in_the_last_hour_still_shows(replay):
    now = acceptance.T0
    _log([(now - timedelta(minutes=10, seconds=i), f"t{i}", "skipped:budget" if i == 3 else "none") for i in range(20)])
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
    assert lines[4] == "Background jobs: Failing: nightly. Fix: run `khipu jobs status`."
    assert len(lines) == 5
