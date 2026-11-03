"""Fuzz: compaction never changes the pace fold, however noisy the log.

Each seed builds a noisy synthetic log (several Macs, skewed clocks, jittered
reset estimates, twins, quiet spells, missing reset times) and checks that the
compacted file folds exactly like the full one, now and every five hours for
the next fourteen days.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing, routing_compact as rc
from tests.test_routing_audit_fold import world  # noqa: F401  (fixture)
from tests.test_routing_compact_pace import _Log, NOW, ISO

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


@pytest.mark.parametrize("seed", range(120))
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
    assert ok, (seed, len(rows), len(drop), differs)
    for h in range(1, 24 * 14, 5):
        when = NOW + timedelta(hours=h)
        assert rc._normal(full.at(when)) == rc._normal(comp.at(when)), (seed, h)
