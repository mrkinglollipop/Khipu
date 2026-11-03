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
