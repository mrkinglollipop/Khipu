"""A synthetic routing log for the compaction tests and the fold-time measurement:
probe samples at a steady cadence on two windows per account, runner sightings,
conversation reports, reset checks and a trickle of owner lines. Raw lines, so a
10,000-line log is built in a moment."""
import json, random
from datetime import datetime, timedelta, timezone
from khipu import routing

ISO = routing._iso

def synth(keys, end, *, days=45, step_min=30, seed=1, owner=True):
    """(lines) a varied log ending at `end`. keys: account keys."""
    rnd = random.Random(seed)
    start = end - timedelta(days=days)
    rows = []
    n = [0]
    def add(row, at):
        n[0] += 1
        rows.append(dict(row, id=f"e{n[0]}", seq=n[0], written_at=ISO(at)))
    t = start
    owner_done = set()
    while t <= end:
        for k, key in enumerate(keys):
            for window, length in (("weekly", 7 * 86400), ("five_hour", 5 * 3600)):
                epoch = int(t.timestamp())
                cycle = epoch // length
                reset = datetime.fromtimestamp((cycle + 1) * length, tz=timezone.utc)
                used = ((epoch % length) / length) * 100 * (0.5 + 0.1 * k) + rnd.random()
                add({"type": "usage_sample", "account": key, "window": window, "witness_at": ISO(t),
                     "resets_at": ISO(reset), "used_pct": round(used, 1), "t_err": 60, "source": "probe",
                     "written_by": {"kind": "probe"}, "via": "probe"}, t)
        if int(t.timestamp()) // 1800 % 12 == 0:
            add({"type": "source_seen", "runner": "r1", "witness_at": ISO(t), "status": "{}",
                 "written_by": {"kind": "runner"}, "via": "runner"}, t)
        if rnd.random() < 0.08:
            key = rnd.choice(keys)
            state = rnd.choice(["limited", "open", "down", "open"])
            row = {"type": "evidence", "account": key, "state": state, "basis": "estimated", "source": "report",
                   "scope": "all", "witness_at": ISO(t), "written_by": {"kind": "conversation", "harness": "codex"},
                   "via": "mcp"}
            if state == "limited":
                row["until"] = ISO(t + timedelta(hours=rnd.randint(1, 60)))
            add(row, t)
        if rnd.random() < 0.03:
            key = rnd.choice(keys)
            add({"type": "reset_check", "account": key, "status": "open", "scope": "all", "checks": 1,
                 "reset_until": ISO(t + timedelta(hours=1)), "written_by": {"kind": "check"}, "via": "check"}, t)
        if rnd.random() < 0.02:
            key = rnd.choice(keys)
            n[0] += 1
            seen_id = f"obs{n[0]}"
            add({"type": "observation", "id": seen_id, "account": key, "state": rnd.choice(["open", "limited"]),
                 "basis": "cli", "source": "probe", "scope": "all", "witness_at": ISO(t),
                 "until": ISO(t + timedelta(hours=3)), "source_id": f"src{n[0]}",
                 "written_by": {"kind": "runner"}, "via": "runner"}, t)
            add({"type": "observation_renew", "target": seen_id, "witness_at": ISO(t + timedelta(minutes=20)),
                 "expires_at": ISO(t + timedelta(hours=3)), "written_by": {"kind": "runner"}, "via": "runner"}, t)
        day = (t - start).days
        if owner and rnd.random() < 0.004:
            key = rnd.choice(keys)
            kind = rnd.choice(["pause", "lift", "clear"])
            row = {"type": kind, "account": key, "written_by": {"kind": "owner"}, "via": "app", "auth": "app"}
            if kind == "pause":
                row["until"] = ISO(t + timedelta(days=rnd.randint(1, 4)))
            add(row, t)
        if owner and day % 9 == 3 and day not in owner_done:
            owner_done.add(day)
            owner_row = {"written_by": {"kind": "owner"}, "via": "app", "auth": "app"}
            add(dict(owner_row, type="note_set", text=f"note {day}"), t)
            if day % 18 == 3:
                add(dict(owner_row, type="priority_set", entries=[
                    {"account": keys[0], "label": "A", "role": f"role {day}", "until": None}],
                    until=None), t)
        t += timedelta(minutes=step_min)
    return rows

def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def append(path, rows):
    with path.open("a") as f:
        f.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))


def real_shape(keys, end, *, total=10_000, days=45, seed=1):
    """About ``total`` lines in the mix of the owner's real (compacted) log:
    ~44% observations, ~4% renewals, ~43% usage samples, ~1.4% runner
    sightings and ~3% owner lines. Raw lines, in write order."""
    rnd = random.Random(seed)
    start = end - timedelta(days=days)
    plan = (["observation"] * 4400 + ["observation_renew"] * 420 + ["usage_sample"] * 4300 +
            ["source_seen"] * 140 + ["owner"] * 300)
    plan = plan[:total] if total < len(plan) else plan + ["usage_sample"] * (total - len(plan))
    rnd.shuffle(plan)
    rows, observations = [], []
    span = (end - start).total_seconds()
    for n, kind in enumerate(plan, 1):
        at = start + timedelta(seconds=span * n / (len(plan) + 1))
        key = rnd.choice(keys)
        base = {"id": f"e{n}", "seq": n, "written_at": ISO(at)}
        if kind == "observation":
            observations.append(f"obs{n}")
            row = {"type": "observation", "id": f"obs{n}", "account": key, "state": rnd.choice(["open", "limited"]),
                   "basis": "cli", "source": "probe", "scope": "all", "witness_at": ISO(at),
                   "until": ISO(at + timedelta(hours=3)), "source_id": f"src{n}",
                   "written_by": {"kind": "runner"}, "via": "runner"}
        elif kind == "observation_renew" and observations:
            row = {"type": "observation_renew", "target": rnd.choice(observations),
                   "witness_at": ISO(at), "expires_at": ISO(at + timedelta(hours=3)),
                   "written_by": {"kind": "runner"}, "via": "runner"}
        elif kind == "source_seen":
            row = {"type": "source_seen", "runner": "r1", "witness_at": ISO(at), "status": "{}",
                   "written_by": {"kind": "runner"}, "via": "runner"}
        elif kind == "owner":
            what = rnd.choice(["pause", "lift", "clear", "note_set"])
            row = {"type": what, "written_by": {"kind": "owner"}, "via": "app", "auth": "app"}
            if what == "note_set":
                row["text"] = f"note {n}"
            else:
                row["account"] = key
            if what == "pause":
                row["until"] = ISO(at + timedelta(days=2))
        else:
            window, length = rnd.choice([("weekly", 7 * 86400), ("five_hour", 5 * 3600)])
            cycle = int(at.timestamp()) // length
            reset = datetime.fromtimestamp((cycle + 1) * length, tz=timezone.utc)
            row = {"type": "usage_sample", "account": key, "window": window, "witness_at": ISO(at),
                   "resets_at": ISO(reset), "used_pct": round(rnd.random() * 100, 1), "t_err": 60,
                   "source": "probe", "written_by": {"kind": "probe"}, "via": "probe"}
        rows.append({**base, **row})
    return rows


class Stream:
    """The owner's log as it grows, in the shape Khipu writes it (stamped, each
    observation with its scope, expiry and salted-looking ``source_id``): about
    a line a minute, some 60% of them observations. Probes read each account
    and model every few minutes and write a new observation when what they
    read changes (its reset time moves, mostly); runners report limits on their
    lanes, some of which no account answers for. ``owner_per_day`` lines are
    the owner's, which never go. Call ``until(at)`` for the lines up to ``at``."""

    MODELS = ("all", "model-a")
    SAMPLE_WINDOWS = (("weekly", 7 * 86400), ("five_hour", 5 * 3600))

    def __init__(self, keys, start, *, seed=1, mac="a" * 32, runners=2, lanes=6, owner_per_day=3.0,
                 observations_per_min=0.6, samples_per_min=0.3):
        self.keys, self.mac, self.rnd = list(keys), mac, random.Random(seed)
        self.t, self.n = start, 0
        self.owner_per_day = owner_per_day
        self.rates = observations_per_min, samples_per_min
        self.lanes = [(f"runner{r}", f"lane{l}", self.keys[l % len(self.keys)] if l % 3 else None)
                      for r in range(runners) for l in range(lanes)]
        self.last_obs: list[tuple[str, str]] = []

    def _line(self, row, at):
        self.n += 1
        stamp = {"mac": self.mac, "seq": self.n, "written_at": ISO(at),
                 "hlc": {"wall": int(at.timestamp() * 1000), "counter": 0, "mac": self.mac}}
        return {"id": row.pop("id", f"{self.n:032x}"), **stamp, **row}

    def _observation(self, at):
        rnd = self.rnd
        by = {"kind": "probe"}
        if rnd.random() < 0.8:
            account, scope = rnd.choice(self.keys), rnd.choice(self.MODELS)
            runner, lane = None, None
            state = "limited" if rnd.random() < 0.08 else ("down" if rnd.random() < 0.02 else "open")
            source = "probe"
        else:
            runner, lane, account = rnd.choice(self.lanes)
            scope, state, source, by = "all", "limited", "runner", {"kind": "runner", "name": runner}
        limited = state == "limited"
        until = at + timedelta(minutes=rnd.choice([30, 90, 180, 300, 300, 600])) if limited else None
        row = {"type": "observation", "kind": "observation", "state": state,
               "basis": "cli" if limited else "checked", "scope": scope, "source": source,
               "witness_at": ISO(at), "until": ISO(until), "written_by": by, "via": source,
               "observation_scope": {"kind": "account_quota"},
               "expires_at": ISO(until if limited else at + timedelta(hours=12)),
               "source_id": f"{rnd.getrandbits(48):012x}", "observed_turn": False,
               "unmatched": bool(runner and not account),
               "evidence": f"{source}: synthetic", "reason": None, "message_time": limited}
        if account:
            row["account"] = account
        if runner:
            row.update(runner=runner, lane=lane)
        if source == "probe":
            row["reset_at"] = ISO(at + timedelta(hours=rnd.randint(1, 5)))
            row["used_pct"] = round(rnd.random() * 100, 1)
        return row

    def _sample(self, at):
        rnd = self.rnd
        key = rnd.choice(self.keys)
        window, length = rnd.choice(self.SAMPLE_WINDOWS)
        cycle = int(at.timestamp()) // length
        reset = datetime.fromtimestamp((cycle + 1) * length, tz=timezone.utc)
        return {"type": "usage_sample", "account": key, "window": window, "witness_at": ISO(at),
                "resets_at": ISO(reset), "used_pct": round(((int(at.timestamp()) % length) / length) * 90, 1),
                "t_err": 60, "source": "probe", "written_by": {"kind": "probe"}, "via": "probe"}

    def until(self, end):
        rnd, out = self.rnd, []
        obs_rate, sample_rate = self.rates
        while self.t < end:
            self.t += timedelta(minutes=1)
            at = self.t
            if rnd.random() < obs_rate:
                row = self._observation(at)
                line = self._line(row, at)
                self.last_obs.append((line["id"], at))
                self.last_obs = self.last_obs[-12:]
                out.append(line)
            if self.last_obs and rnd.random() < 0.05:
                target, base = rnd.choice(self.last_obs)
                out.append(self._line({"type": "observation_renew", "target": target, "witness_at": ISO(at),
                                       "expires_at": ISO(at + timedelta(hours=12)), "source_id": f"{rnd.getrandbits(48):012x}",
                                       "written_by": {"kind": "probe"}, "via": "probe"}, at))
            if rnd.random() < sample_rate:
                out.append(self._line(self._sample(at), at))
            if rnd.random() < 0.002:
                out.append(self._line({"type": "source_seen", "runner": "runner0", "witness_at": ISO(at), "status": "{}",
                                       "written_by": {"kind": "runner"}, "via": "runner"}, at))
            if rnd.random() < self.owner_per_day / 1440:
                what = rnd.choice(["pause", "lift", "clear", "note_set"])
                row = {"type": what, "written_by": {"kind": "owner"}, "via": "app", "auth": "app"}
                if what == "note_set":
                    row["text"] = f"note {self.n}"
                else:
                    row["account"] = rnd.choice(self.keys)
                if what == "pause":
                    row["until"] = ISO(at + timedelta(days=1))
                out.append(self._line(row, at))
        return out


def stream_log(keys, end, *, others=1300, observations=4800, owner=150, seed=1):
    """About ``observations`` + ``others`` + ``owner`` lines of a ``Stream``
    ending at ``end``: the log doctor said could go no lower. The sample days
    a compaction keeps are the youngest, so the others stay in the last days."""
    days = observations / (0.6 * 1440)
    stream = Stream(keys, end - timedelta(days=days), seed=seed, owner_per_day=owner / days,
                    samples_per_min=min(1.0, max(0.0, (others - owner) / (days * 1440))))
    return stream.until(end)
