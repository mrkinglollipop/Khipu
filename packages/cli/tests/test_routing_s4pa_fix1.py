"""Fix round 1 for routing slice 4Pa: regression tests proven before fixes."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import routing_pace
from tests.test_owner_direction_e2e import world  # noqa: F401  (pytest fixture)

T0 = datetime(2026, 10, 20, 14, tzinfo=timezone.utc)
SCRIPT = Path(__file__).resolve().parents[3] / "tools" / "pace_fixture_extract.py"


def samp(t, used, *, reset=604800, mac="m1", source="probe", cycle=None, error=0):
    return {"t": T0 + timedelta(seconds=t), "used": float(used),
            "reset": T0 + timedelta(seconds=reset), "mac": mac, "source": source,
            "cycle": cycle, "t_err": error}


def steady(rate=0.5, *, start=0, end=86400, used=0.0, reset=604800, step=1800, mac="m1"):
    return [samp(t, used + rate * (t - start) / 3600, reset=reset, mac=mac)
            for t in range(start, end + 1, step)]


def pace(samples, *, now=86400, window="weekly", caps=()):
    return routing_pace.compute_pace([dict(s) for s in samples], list(caps),
                                     T0 + timedelta(seconds=now), window)


# ---- P1: refuted possible resets and corrections -------------------------------------


def test_refuted_possible_reset_episode_including_rebound_is_excluded():
    samples = steady(0.5, end=82800, used=80.0)
    samples += [samp(84600, 1), samp(86400, 92)]
    result = pace(samples)
    assert result["rate_pct_per_h"] == pytest.approx(0.5)
    assert result["plan"]["verdict"] != "unknown"


def test_small_correction_and_its_rebound_are_excluded():
    samples = steady()
    samples[20]["used"] = samples[19]["used"] - 1
    assert pace(samples)["rate_pct_per_h"] == pytest.approx(0.5)


# ---- P1: dedup only identical readings from the same Mac/source/reset -----------------


def test_dedup_keeps_disagreeing_reset_from_another_mac_over_hour_span():
    samples = steady(reset=604800)
    # Same value and witness as each other, but different Macs and resets
    # spanning more than an hour: not identical readings, so none may drop.
    samples += [samp(86400, 12, reset=604800 + off, mac=f"m{i + 2}")
                for i, off in enumerate((0, 3000, 6000))]
    assert pace(samples)["why"] == "reset time spread over more than an hour"


def test_dedup_keeps_newest_witness_from_another_mac_with_different_reset():
    samples = steady()
    for s in samples[24:]:
        s["reset"] += timedelta(seconds=5 * 3600)
    samples += [samp(86400, 12, reset=604800, mac="m2")]
    assert pace(samples)["why"] == "reset time disagrees between readings"


def test_readings_disagree_reason_reports_hours():
    first = steady(mac="m1")
    second = steady(mac="m2", end=54000)
    for s in second:
        s["reset"] += timedelta(seconds=5 * 3600)
    result = pace(first + second)
    assert result["why"] is not None and "readings disagree for" in result["why"]
    assert "of the last 24 h" in result["why"]


# ---- P1/P2: extractor ------------------------------------------------------------------


def _block(hh, mm, left, age, reset="Mon 14:00Z"):
    return (f"## Routing at {hh:02d}:{mm:02d}Z (Khipu's routing record)\n"
            f"Codex: open; read from Codex {age} min ago; {left}% of its week left, "
            f"resets {reset} (6 d)\n")


def _run_extractor(tmp_path, rows):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return subprocess.run([sys.executable, str(SCRIPT), str(path)],
                          capture_output=True, text=True, timeout=30)


def test_extractor_keeps_a_real_time_axis_and_reads_attachments(tmp_path):
    rows = []
    for i, (minute, left) in enumerate(((0, 96), (10, 95), (20, 94), (30, 93))):
        rows.append({"type": "user", "timestamp": f"2026-10-20T14:{minute + 1:02d}:00Z",
                     "message": {"role": "user", "content": [
                         {"type": "text", "text": "hi\n" + _block(14, minute, left, 2)}]}})
    rows.append({"type": "attachment", "timestamp": "2026-10-20T14:41:00Z",
                 "attachment": {"type": "hook_additional_context",
                                "content": [_block(14, 41, 91, 2)]}})
    proc = _run_extractor(tmp_path, rows)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    codex = [r for r in out if r["acct"] == "a1"]
    assert [r["t"] for r in codex] == [-120, 480, 1080, 1680, 2340]
    assert [r["used"] for r in codex] == [4.0, 5.0, 6.0, 7.0, 9.0]
    assert codex[0]["reset"] == 6 * 86400
    assert codex[-1]["reset"] == 6 * 86400


@pytest.mark.parametrize("rows", [
    [{"type": "user", "timestamp": 12345, "message": {"content": [
        {"type": "text", "text": _block(14, 0, 96, 2) + "PRIVSENT"}]}}],
    [{"type": "user", "timestamp": "2026-10-20T14:01:00Z", "message": "PRIVSENT"}],
    [{"type": "user", "timestamp": "2026-10-20T14:01:00Z", "message": {"content": [
        {"type": "text", "text": "## Routing at 99:99Z PRIVSENT\nCodex: read from Codex 2 min ago; "
                                  "96% of its week left, resets Mon 14:00Z (6 d)"}]}}],
], ids=["non-string-timestamp", "string-message", "hour-99"])
def test_extractor_malformed_input_exits_2_without_traceback(tmp_path, rows):
    proc = _run_extractor(tmp_path, rows)
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "PRIVSENT" not in proc.stderr and "Traceback" not in proc.stderr
    assert proc.stderr.strip() == "fixture_rejected: malformed transcript"


# ---- P1: pace cost is bounded ------------------------------------------------------------


def test_compute_pace_trims_and_stays_fast_at_18000_samples():
    samples = []
    for i in range(18000):
        t = i * 1800
        samples.append(samp(t, (i * 0.25) % 90, reset=t + 604800))
    now = 18000 * 1800
    start = time.perf_counter()
    result = pace(samples, now=now)
    elapsed = time.perf_counter() - start
    assert result["buckets"] <= 50
    assert elapsed < 1.0, f"compute_pace took {elapsed:.3f}s at 18,000 samples"


# ---- P2: pushes, sampling, caps ----------------------------------------------------------


def _forecast(eta_at, eta_s, **extra):
    base = {"account": "a|", "label": "A", "window": "weekly", "headline": "plan",
            "verdict": "outpacing", "revision": "plan", "cycle": "c1", "availability": "known",
            "eta_at": eta_at, "eta_s": eta_s, "rate_pct_per_h": 1.0, "needed_pct_per_h": 0.5,
            "line_verdict": "outpacing"}
    return {**base, **extra}


def test_movement_push_compares_absolute_runout_not_clock_decay():
    now = T0 + timedelta(minutes=16)
    acked = _forecast(routing_pace.iso(T0 + timedelta(seconds=3500)), 3500)
    current = _forecast(routing_pace.iso(T0 + timedelta(seconds=3500)), 3500 - 16 * 60)
    assert routing_pace.should_push(acked, current, now) is False


def test_sampling_idle_windows_sample_hourly_not_at_fast_cadence():
    base = T0
    hist = [{"window": "five_hour", "t": base + timedelta(seconds=t), "used": 10.0,
             "mac": "m1", "source": "probe"} for t in range(0, 7201, 600)]
    candidate = {"window": "five_hour", "t": base + timedelta(seconds=7200 + 600),
                 "used": 10.0, "mac": "m1", "source": "probe"}
    assert routing_pace.should_sample(hist, candidate) is False
    candidate["t"] = base + timedelta(seconds=7200 + 3600)
    assert routing_pace.should_sample(hist, candidate) is True


def test_sampling_active_window_uses_window_cadence_and_move_rule():
    base = T0
    hist = [{"window": "five_hour", "t": base + timedelta(seconds=t), "used": 10.0 + t / 3600,
             "mac": "m1", "source": "probe"} for t in range(0, 3601, 600)]
    at_cadence = {"window": "five_hour", "t": base + timedelta(seconds=4200), "used": 11.0,
                  "mac": "m1", "source": "probe"}
    assert routing_pace.should_sample(hist, at_cadence) is True
    too_soon = {"window": "five_hour", "t": base + timedelta(seconds=3660), "used": 12.0,
                "mac": "m1", "source": "probe"}
    assert routing_pace.should_sample(hist, too_soon) is False


# ---- P2/P3: pace line ---------------------------------------------------------------------


def test_pace_line_fits_160_chars_and_uses_scope_wording():
    result = pace(steady(reset=900000))
    line = routing_pace.pace_line("Claude", result, T0 + timedelta(seconds=86400))
    assert len(line) <= 160, line
    assert "lands on 100%" in line
    assert "at the trailing average" in line


def test_pace_line_marks_gappy_readings():
    gappy = [s for s in steady() if not 18000 < (s["t"] - T0).total_seconds() < 41400]
    result = pace(gappy)
    assert result["gappy"] is True
    line = routing_pace.pace_line("Claude", result, T0 + timedelta(seconds=86400))
    assert "(gappy readings)" in line and len(line) <= 160


# ---- P2: request_cap cannot be forged as the owner ----------------------------------------

CONV = {"kind": "conversation", "harness": "codex", "project": "p", "session": "s-other"}


def test_request_cap_refuses_a_caller_supplied_owner_author(world):  # noqa: F811
    from khipu import routing
    from tests.test_owner_direction_e2e import _act, _hook  # noqa: F401

    routing.report(account="Claude", state="open", author=CONV)
    out = routing.request_cap("Claude", window="weekly", kind="per_day", pct=50,
                              revision="forged", author={"kind": "owner"})
    view = routing.read_view()
    account = next(a for a in view["accounts"] if a["label"] == "Claude")
    assert not account.get("caps")
    proposals = [p for p in view.get("cap_proposals") or [] if p.get("revision") == "forged"]
    assert proposals and proposals[0]["written_by"]["kind"] == "conversation"
    assert out.get("pending") is True


def test_cap_owner_action_and_undo_round_trip(world, khipu_app):  # noqa: F811
    from khipu import cli, routing
    from tests.test_owner_direction_e2e import _act, _hook, _log

    routing.report(account="Claude", state="open", author=CONV)
    _hook("set Claude weekly share cap 80")
    err, out = _act("route.cap.set", {"account": "Claude", "window": "weekly",
                                      "kind": "share", "pct": 80, "revision": "r1"})
    assert not err, out

    def caps():
        account = next(a for a in routing.read_view()["accounts"] if a["label"] == "Claude")
        return account.get("caps") or []

    assert caps() and caps()[0]["pct"] == 80
    _hook("raise it to 90")
    err, out = _act("route.cap.set", {"account": "Claude", "window": "weekly",
                                      "kind": "share", "pct": 90, "revision": "r2"})
    assert not err, out
    assert caps()[0]["pct"] == 90
    lines = [x for x in _log() if x.get("kind") == "action"]
    khipu_app.as_app()
    assert cli.main(["owner-direction", "undo", "--from-app", "--", lines[-1]["id"]]) == 0
    assert caps() and caps()[0]["pct"] == 80 and caps()[0]["revision"] == "r1"


def test_cap_cli_without_owner_flags_changes_nothing(world):  # noqa: F811
    from khipu import cli, routing

    routing.report(account="Claude", state="open", author=CONV)
    cli.main(["route", "cap", "set", "Claude", "--window", "weekly",
              "--kind", "share", "--pct", "80"])
    account = next(a for a in routing.read_view()["accounts"] if a["label"] == "Claude")
    assert not account.get("caps")
