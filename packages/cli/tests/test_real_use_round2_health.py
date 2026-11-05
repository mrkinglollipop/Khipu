"""Health reads from the second real-use round: a Mac with the routing push off,
an outcome log that was just started afresh, and where the dead captures are."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from khipu import outbox, recall_prompt, routing, routing_health as health, session_capture
from tests import test_routing_acceptance as acceptance

replay = acceptance.replay


def test_a_mac_with_the_routing_push_off_has_nothing_to_deliver_and_is_not_unknown(replay, monkeypatch):
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "0")
    result = health.doctor_block({"unmatched": []}, replay.now, routing.settings())
    assert result["status"] == "not_applicable" and result["missing"] == [] and result["reasons"] == []
    assert "switched off" in result["words"]


def test_a_mac_with_the_push_on_and_no_evidence_is_still_unknown(replay):
    result = health.doctor_block({"unmatched": []}, replay.now, routing.settings())
    assert result["status"] == "unknown" and "today's hook outcomes" in result["missing"]


def test_a_real_delivery_problem_is_not_hidden_by_the_push_being_off(replay, monkeypatch):
    monkeypatch.setenv("KHIPU_FEATURE_ROUTING_PUSH", "0")
    result = health.doctor_block({"unmatched": [{"limited_since": routing._iso(replay.now - timedelta(hours=3))}]},
                                 replay.now, routing.settings())
    assert result["status"] == "red" and "unmatched-limit" in result["reasons"]


def _recall_line(when: datetime, n: int) -> str:
    return (f"{when.strftime('%Y-%m-%dT%H:%M:%SZ')} [khipu-prompt-recall] session=s{n} reason=ok ms=120.0 "
            "hits=[1] legs=['lexical'] degraded=None\n")


def test_the_week_the_doctor_reads_does_not_stop_when_the_log_is_started_afresh(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path))
    log = recall_prompt._log_path()
    log.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    older = log.with_name(log.name + ".1")
    older.write_text("".join(_recall_line(now - timedelta(minutes=30), i) for i in range(25)))
    log.write_text(f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')} [khipu-delegation-hook] session=x event=PreToolUse "
                   "tool=Bash outcome=pass ms=20.0\n")
    result = recall_prompt.prompt_recall_outcomes(now=now)
    assert result["count"] == 25 and result["ok"] is True, result


def test_a_check_that_finds_nothing_says_where_it_looked(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path))
    log = recall_prompt._log_path()
    log.parent.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    log.write_text("".join(f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')} [khipu-delegation-hook] session=x "
                           "event=PreToolUse tool=Bash outcome=pass ms=20.0\n" for _ in range(3)))
    result = recall_prompt.prompt_recall_outcomes(now=now)
    assert result["ok"] is False and result["error"].startswith("no recall calls")
    assert result["log"]["path"] == str(log) and result["log"]["lines_by_writer"] == {"khipu-delegation-hook": 3}


def test_dead_captures_say_where_they_are_from(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    queue = session_capture.queue_dir()
    queue.mkdir(parents=True)
    monkeypatch.setattr(session_capture, "held_jobs", lambda min_age_s=None: [queue / "x-gateway-1.json", queue / "x-aegis-2.json"])
    (queue / "x-gateway-1.json").write_text(json.dumps({"harness": "gateway"}))
    (queue / "x-aegis-2.json").write_text(json.dumps({}))
    monkeypatch.setattr(session_capture, "_retry_state", lambda p: {"disposition": "held", "last_error": "hub said no"})
    status = outbox.status()
    detail = status["dead_detail"]
    assert status["dead"] == 2 and detail["native_held"] == 2
    assert set(detail["native_held_by_harness"]) == {"gateway", "aegis"}
    assert detail["native_held_by_harness"]["gateway"]["errors"] == {"hub said no": 1}
