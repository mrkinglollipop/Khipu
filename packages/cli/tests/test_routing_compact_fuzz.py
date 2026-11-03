"""Fuzz: compaction never changes the fold, however noisy the log.

Each seed builds a noisy synthetic log (several Macs, skewed clocks, jittered
reset estimates, twins, quiet spells, missing reset times) and checks that the
compacted file folds exactly like the full one, now and every five hours for
the next fourteen days. A second log of sightings does the same for what reads
observations.

The seeds are a sample of a much longer run, so this stays minutes shorter than
that: ``FAILED_BEFORE`` are the ones that failed on the retention before
every cycle kept an anchor (14a6f14c), and ``OBSERVATION_SEEDS`` holds the ones
that found a hole while the observation rule was written.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests.test_routing_audit_fold import world  # noqa: F401  (fixture)
from tests.test_routing_compact_pace import _Log, NOW, ISO

FAILED_BEFORE = [9, 29, 46, 49, 50, 63, 72, 101, 103, 107, 111, 114]
SAMPLE_SEEDS = sorted(FAILED_BEFORE + [0, 1, 2, 3, 7, 13, 21, 34, 40, 58, 66, 77, 85, 90, 96, 105, 110, 119])
OBSERVATION_SEEDS = [*range(12), 27, 28, 224, 545, 816]

# (name, window_mins in the sample, window length in seconds)
WINDOWS = [
    ("weekly", None, 7 * 86400),
    ("five_hour", None, 18000),
    ("monthly", 43200, 30 * 86400),
    ("90_min", 90, 5400),
    ("foo", None, 60),
    ("foo", 600, 36000),
    ("weekly_opus", None, 7 * 86400),
    ("daily", None, 86400),
]


def _build(seed: int) -> _Log:
    rnd = random.Random(seed)
    log = _Log()
    name, mins, wlen = rnd.choice(WINDOWS)
    macs = rnd.sample(["m1", "m2", "m3"], rnd.randint(1, 3))
    skew = {m: timedelta(minutes=rnd.choice([0, 0, -90, 45, 300, -600])) for m in macs}
    quiet = {m: timedelta(days=rnd.choice([0, 0, 3, 6, 12])) for m in macs}
    span = rnd.choice([5, 12, 25])
    step = timedelta(minutes=rnd.choice([10, 30, 120, 600]))
    t = NOW - timedelta(days=span)
    while t <= NOW:
        for m in macs:
            if t > NOW - quiet[m] or rnd.random() < 0.15:
                continue
            cycle = int(t.timestamp() // wlen)
            reset = routing._parse(ISO(datetime.fromtimestamp((cycle + 1) * wlen, tz=timezone.utc)))
            reset += timedelta(minutes=rnd.choice([0, 0, 0, 20, -25, 40, 90]))
            if rnd.random() < 0.12:
                reset = None
            extra = {}
            if mins and name in ("monthly", "90_min", "foo"):
                extra["window_mins"] = mins
            if rnd.random() < 0.1:
                extra["cycle"] = f"c{cycle}"
            used = (t.timestamp() % wlen) / wlen * 100 + rnd.random() * 3
            if rnd.random() < 0.05:
                used = max(0, used - 30)
            used = round(used, 1)
            source = rnd.choice(["probe", "probe", "report"])
            log.sample(name, t + skew[m], reset, used=used, source=source, mac=m,
                       t_err=rnd.choice([60, 60, 60, 900, 3000]), **extra)
            if rnd.random() < 0.1:  # twin
                log.sample(name, t + skew[m] + timedelta(seconds=30), reset, used=used,
                           source=source, mac=m, t_err=60, **extra)
        t += step
    return log


@pytest.mark.parametrize("seed", SAMPLE_SEEDS)
def test_compaction_keeps_the_fold(world, seed):
    log = _build(seed)
    if not log.rows:
        pytest.skip("empty log")
    rows, drop = log.planned()
    kept = [line for i, (line, _e) in enumerate(rows) if i not in drop]
    inputs = routing._inputs()
    full = rc._Log([line for line, _ in rows], inputs)
    comp = rc._Log(kept, inputs)
    ok, _n, differs = rc.agree(full, comp, NOW)
    assert ok, (seed, len(rows), len(drop), differs)  # keeping anchors is the fallback: it must always pass
    # What compaction would actually write, old cycles' anchors gone if it can spare them.
    chosen = rc._choose(routing.events_file().read_bytes(), NOW)
    if "kept" in chosen:
        comp = rc._Log(chosen["kept"], inputs)
        rc._replace(routing.events_file(), b"".join(line + b"\n" for line in chosen["kept"]),
                    "events-20261103T120000Z-f00000.jsonl.gz", rc._archive_bytes(chosen, NOW))
        assert rc.full_log() == [line for line, _ in rows]
    for h in range(1, 24 * 14, 5):
        when = NOW + timedelta(hours=h)
        assert rc._normal(full.at(when)) == rc._normal(comp.at(when)), (seed, h)


# ---- observations: every reader of them, on noisy logs -------------------------------


def _observations(seed: int) -> tuple[list[dict], list[dict]]:
    """A noisy log of sightings: several Macs with skewed clocks, renewals by
    the wrong author, limits that overlap on lanes no account answers for, the
    owner's clears, dismissals and account changes, a priority level, a restore
    batch; and what the sources offer now, some of it already in the log."""
    rnd = random.Random(10_000 + seed)
    macs = rnd.sample(["a" * 32, "b" * 32, "c" * 32], rnd.randint(1, 3))
    skew = {m: timedelta(minutes=rnd.choice([0, 0, -90, 45, 300])) for m in macs}
    keys = ["claude|", "codex|", "gone|"]
    lanes = [("r1", "l1"), ("r1", "l2"), ("r2", "l1")]
    span, step = rnd.choice([3, 9, 20]), timedelta(minutes=rnd.choice([20, 45, 120]))
    rows: list[dict] = []
    seen: list[dict] = []
    offered: list[dict] = []
    salt = routing.entry_model.source_id

    def add(row, at, mac=None):
        mac = mac or rnd.choice(macs)
        stamped = at + skew[mac]
        out = dict(row, seq=len(rows) + 1, mac=mac, written_at=ISO(stamped))
        out.setdefault("id", f"e{len(rows) + 1}")
        out["hlc"] = {"wall": int(stamped.timestamp() * 1000), "counter": 0, "mac": mac}
        rows.append(out)
        return out

    t = NOW - timedelta(days=span)
    while t <= NOW:
        t += step * rnd.choice([0.5, 1, 1, 2])
        at = min(t, NOW)
        limited = rnd.random() < 0.3
        until = at + timedelta(minutes=rnd.choice([10, 60, 400, 5000])) if limited else None
        runner = rnd.random() < 0.3
        runner_name, lane = rnd.choice(lanes) if runner else (None, None)
        account = (rnd.choice(keys) if not runner or rnd.random() < 0.4 else None)
        by = {"kind": "runner", "name": runner_name} if runner else {"kind": "probe"}
        source_key = f"s{len(rows)}"
        state = "limited" if limited else rnd.choice(["open", "open", "down"])
        row = {"type": "observation", "kind": "observation", "state": state, "basis": rnd.choice(["cli", "estimated"]) if limited else "checked",
               "scope": rnd.choice(["all", "all", "m1"]), "source": "runner" if runner else "probe", "witness_at": ISO(at),
               "until": ISO(until), "written_by": by, "via": "probe", "observation_scope": {"kind": "account_quota"},
               "expires_at": ISO(until if limited else at + timedelta(hours=rnd.choice([12, 12, 12, 200]))),
               "source_id": salt(source_key), "unmatched": bool(runner and not account), "evidence": "x"}
        if account:
            row["account"] = account
        if runner:
            row.update(runner=runner_name, lane=lane)
        if rnd.random() < 0.3:
            row["reset_at"] = ISO(at + timedelta(hours=rnd.randint(1, 50)))
        if rnd.random() < 0.15:
            row["window"] = rnd.choice(["weekly", "five_hour"])
        line = add(row, at)
        seen.append(line)
        if rnd.random() < 0.08:
            offered.append(dict(id=source_key, account=account, runner=runner_name, lane=lane, state=state,
                                until=until, basis=row["basis"], scope=row["scope"], window=row.get("window"),
                                used_pct=None, source=row["source"], written_by=by, via="probe", at=at, reason=None,
                                evidence="x", message_time=False, silent=False))
        if rnd.random() < 0.2 and seen:
            target = rnd.choice(seen[-8:])
            add({"type": "observation_renew", "target": target["id"], "witness_at": ISO(at + timedelta(minutes=rnd.randint(1, 90))),
                 "expires_at": ISO(at + timedelta(hours=12)), "used_pct": 5.0, "source_id": salt(f"renew{len(rows)}"),
                 "written_by": target["written_by"] if rnd.random() < 0.85 else {"kind": "probe", "harness": "other"},
                 "via": "probe"}, at, target["mac"])
        owner = {"written_by": {"kind": "owner"}, "via": "app", "auth": "app"}
        draw = rnd.random()
        if draw < 0.015:
            add(dict(owner, type="clear", account=rnd.choice(keys)), at)
        elif draw < 0.03 and seen:
            add(dict(owner, type="dismiss", account=rnd.choice(keys),
                     target=rnd.choice(seen)[rnd.choice(["id", "id", "source_id"])]), at)
        elif draw < 0.035:
            add(dict(owner, type="account_note", account=rnd.choice(keys), state=rnd.choice(["tracked", "untracked"]),
                     lanes_cut=[list(rnd.choice(lanes))]), at)
        elif draw < 0.04:
            add(dict(owner, type="account_move", account=rnd.choice(keys), target=rnd.choice(keys)), at)
        elif draw < 0.045:
            as_of = at - timedelta(hours=rnd.randint(1, 30))
            add(dict(owner, type="priority_set", until=None, entries=[
                {"account": rnd.choice(keys[:2]), "label": "A", "role": "r", "until": None,
                 "level": {"claim": "low", "as_of": ISO(as_of), "until": ISO(as_of + timedelta(days=rnd.randint(1, 40)))}}]), at)
        elif draw < 0.05 and seen:
            batch = f"b{len(rows)}"
            add(dict(rnd.choice(seen), id=f"bm{len(rows)}", batch=batch, source_id=salt(f"bm{len(rows)}")), at)
    # Sources also offer what no line holds yet.
    for lane_pair in lanes[:rnd.randint(0, 2)]:
        offered.append(dict(id=f"new{lane_pair}", account=None, runner=lane_pair[0], lane=lane_pair[1], state="limited",
                            until=NOW + timedelta(hours=2), basis="cli", scope="all", window=None, used_pct=None,
                            source="runner", written_by={"kind": "runner", "name": lane_pair[0]}, via="runner",
                            at=NOW - timedelta(minutes=5), reason=None, evidence="x", message_time=False, silent=False))
    return rows, offered


@pytest.mark.parametrize("seed", OBSERVATION_SEEDS)
def test_compaction_keeps_every_reader_of_observations(world, seed):
    rows, offered = _observations(seed)
    synth.write(routing.events_file(), rows)
    parsed = rc._rows(routing.events_file().read_bytes())
    inputs = routing._inputs()
    sightings = (offered, None)
    emitted = rc._emitted(sightings)
    full = rc._Log([line for line, _e in parsed], inputs, sightings)
    gone = 0
    for profile in rc.PROFILES:
        drop = rc.plan(parsed, NOW, profile, emitted=emitted)
        gone = max(gone, len(drop))
        comp = rc._Log([line for i, (line, _e) in enumerate(parsed) if i not in drop], inputs, sightings)
        ok, _n, differs = rc.agree(full, comp, NOW)
        assert ok, (seed, profile, len(rows), len(drop), differs)
    assert gone or len(rows) < 150, (seed, len(rows))  # the check is not idle
