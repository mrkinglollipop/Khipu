"""Compaction keeps every usage sample the pace calculation reads, however old
(2026-11-03: on this Mac's real log the 3-day cut dropped the only readings an
account's five-hour window had, and the compacted log folded without it).
Hermetic: a synthetic log with a weekly and a five-hour window and a reset."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests import test_routing_audit_fold as audit

world = audit.world
NOW = datetime(2026, 11, 3, 13, tzinfo=timezone.utc)
ACCOUNT = "claude|"
ISO = routing._iso


class _Log:
    def __init__(self):
        self.rows: list[dict] = []

    def sample(self, window, at, reset=None, used=10.0, source="probe", mac="m1", **extra):
        n = len(self.rows) + 1
        row = {"type": "usage_sample", "account": ACCOUNT, "window": window, "witness_at": ISO(at),
               "used_pct": used, "t_err": 60, "source": source, "mac": mac, "id": f"u{n}", "seq": n,
               "written_at": ISO(at), "written_by": {"kind": "probe"}, "via": "probe", **extra}
        if reset is not None:
            row["resets_at"] = ISO(reset)
        self.rows.append(row)
        return n - 1

    def planned(self, profile=None):
        synth.write(routing.events_file(), self.rows)
        rows = rc._rows(routing.events_file().read_bytes())
        drop = rc.plan(rows, NOW, profile or rc.PROFILES[0])
        return rows, drop

    def agrees(self, drop):
        rows = [(json_line, e) for json_line, e in rc._rows(routing.events_file().read_bytes())]
        inputs = routing._inputs()
        kept = [line for i, (line, _e) in enumerate(rows) if i not in drop]
        return rc.agree(rc._Log([line for line, _e in rows], inputs), rc._Log(kept, inputs), NOW)


def _build():
    """Five-hour readings that stopped 3.5 days ago (the account has not
    reported the window since; its later probes carry no reset time and the
    fold ignores them), a week-long cycle that reset 2.5 days ago and the one
    after it, with a witness that skews its reset estimate by half an hour
    now and then."""
    log = _Log()
    ids: dict[str, list[int]] = {}
    five_reset = NOW - timedelta(days=3, hours=14)
    for k in range(20):
        at = NOW - timedelta(days=4) + timedelta(minutes=10 * k)
        ids.setdefault("five_valid", []).append(log.sample("five_hour", at, five_reset, used=k))
    for k in range(12):
        # newer lines for the same window with no reset time: no reading at all
        ids.setdefault("five_ignored", []).append(
            log.sample("five_hour", NOW - timedelta(days=3, hours=12) + timedelta(hours=3 * k), None, used=0))
    old_reset, new_reset = NOW - timedelta(days=2, hours=12), NOW + timedelta(days=4, hours=12)
    for k in range(40):  # the cycle that ended: readings 9..2.5 days back
        at = NOW - timedelta(days=9) + timedelta(hours=4 * k)
        ids.setdefault("weekly_old_cycle", []).append(log.sample("weekly", at, old_reset, used=k * 2))
    for k in range(60):  # the current cycle, from its start until now
        at = NOW - timedelta(days=2, hours=11) + timedelta(hours=k)
        reset = new_reset - (timedelta(minutes=30) if k == 3 else timedelta(0))
        ids.setdefault("weekly_new_cycle", []).append(log.sample("weekly", at, reset, used=k % 40))
    return log, ids


def test_readings_pace_reads_are_not_dropped_for_being_old(world):
    log, ids = _build()
    rows, drop = log.planned()
    # The five-hour window's only readings are the ones with a reset time:
    # the newest of them is the reading pace falls back on after its reset.
    assert ids["five_valid"][-1] not in drop
    # The previous weekly cycle's newest reading anchors the interval that
    # straddles the trailing window's start.
    assert ids["weekly_old_cycle"][-1] not in drop
    # The earlier reset estimate (the only reading carrying it) is the next
    # change the fold schedules; it must stay.
    assert ids["weekly_new_cycle"][3] not in drop
    # Lines the fold ignores are not readings; the newest of them is the
    # writer's cadence and stays.
    assert ids["five_ignored"][-1] not in drop
    assert ids["five_ignored"][0] in drop
    ok, _moments, differs = log.agrees(drop)
    assert ok, differs


def test_the_old_cut_would_have_changed_the_fold(world):
    """The regression: drop what the 3-day, newest-four rule dropped and the
    compacted log folds without the five-hour window."""
    log, ids = _build()
    rows, _drop = log.planned()
    old_rule = set()
    cut = NOW - timedelta(days=3)
    for window in ("five_hour", "weekly"):
        group = sorted(((rc._when(e, "witness_at", "written_at"), i) for i, (_l, e) in enumerate(rows)
                        if e["type"] == "usage_sample" and e["window"] == window), reverse=True)
        old_rule.update(i for at, i in group[4:] if at < cut)
    ok, _moments, differs = log.agrees(old_rule)
    assert not ok and differs is not None
    # ... and the derived rule drops a good part of the same lines without that.
    _rows, drop = log.planned()
    assert drop and log.agrees(drop)[0]


def test_every_profile_keeps_the_fold_on_the_pace_log(world):
    log, _ids = _build()
    for profile in rc.PROFILES:
        _rows, drop = log.planned(profile)
        assert drop
        ok, _moments, differs = log.agrees(drop)
        assert ok, (profile, differs)


def test_the_reset_estimates_of_one_cycle_stay_linked(world):
    """Estimates 40 minutes apart link into one cycle (under an hour); a
    drifting chain of them must not be thinned into two."""
    log = _Log()
    base = NOW - timedelta(days=6)
    for k in range(30):
        at = base + timedelta(hours=2 * k)
        log.sample("weekly", at, NOW - timedelta(days=1) + timedelta(minutes=40 * k) - timedelta(minutes=40 * 15),
                   used=k)
    rows, drop = log.planned()
    kept = [i for i in range(len(rows)) if i not in drop]
    resets = [routing._parse(rows[i][1]["resets_at"]) for i in kept]
    resets.sort()
    assert all((b - a).total_seconds() <= rc.CYCLE_LINK_S for a, b in zip(resets, resets[1:]))
    assert log.agrees(drop)[0]
