"""Old observations leave the live log (khipu.routing_compact): only what nothing
can read goes, every reader answers alike after, and the archive holds what left.
Hermetic."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests import test_routing_audit_fold as audit

world = audit.world
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
KEY = "claude|"
MAC = "a" * 32
ISO = routing._iso
STRONGEST = rc.PROFILES[-1]


class _Log:
    """Hand-written lines, named so a test can say which stay."""

    def __init__(self):
        self.rows: list[dict] = []
        self.names: dict[str, int] = {}

    def add(self, name, row, age_h):
        at = NOW - timedelta(hours=age_h)
        n = len(self.rows) + 1
        self.rows.append({"id": name, "seq": n, "mac": MAC, "written_at": ISO(at),
                          "hlc": {"wall": int(at.timestamp() * 1000), "counter": 0, "mac": MAC}, **row})
        self.names[name] = n - 1

    def seen(self, name, age_h, state="open", account=KEY, until_h=None, **extra):
        """An observation ``age_h`` hours old; a limit ends ``until_h`` after it."""
        at = NOW - timedelta(hours=age_h)
        until = at + timedelta(hours=until_h) if state == "limited" else None
        row = {"type": "observation", "kind": "observation", "state": state, "scope": "all",
               "basis": "cli" if state == "limited" else "checked", "source": "probe", "witness_at": ISO(at),
               "until": ISO(until), "expires_at": ISO(until if until else at + timedelta(hours=12)),
               "observation_scope": {"kind": "account_quota"}, "source_id": f"s-{name}",
               "written_by": {"kind": "probe"}, "via": "probe", **extra}
        if account:
            row["account"] = account
        self.add(name, row, age_h)

    def renew(self, name, target, age_h, by=None):
        at = NOW - timedelta(hours=age_h)
        self.add(name, {"type": "observation_renew", "target": target, "witness_at": ISO(at),
                        "expires_at": ISO(at + timedelta(hours=12)), "used_pct": float(age_h), "source_id": f"s-{name}",
                        "written_by": by or {"kind": "probe"}, "via": "probe"}, age_h)

    def owner(self, name, kind, age_h, **row):
        self.add(name, {"type": kind, "written_by": {"kind": "owner"}, "via": "app", "auth": "app", **row}, age_h)

    def padding(self):
        """A newest line, so nothing the tests look at is the last of the log."""
        self.owner("pad", "note_set", 0.01, text="now")

    def planned(self, emitted=frozenset(), profile=STRONGEST):
        synth.write(routing.events_file(), self.rows)
        rows = rc._rows(routing.events_file().read_bytes())
        return rows, rc.plan(rows, NOW, profile, emitted=emitted if emitted is None else set(emitted))

    def gone(self, emitted=frozenset(), profile=STRONGEST):
        rows, drop = self.planned(emitted, profile)
        return {name for name, i in self.names.items() if i in drop}


def _agree(rows, drop, sightings=None):
    inputs = routing._inputs()
    return rc.agree(rc._Log([line for line, _e in rows], inputs, sightings),
                    rc._Log([line for i, (line, _e) in enumerate(rows) if i not in drop], inputs, sightings), NOW)


# ---- what stays ------------------------------------------------------------------------


def test_only_old_over_and_superseded_sightings_go(world):
    log = _Log()
    log.seen("first", 100)
    log.seen("old", 90)
    log.seen("older-state", 80, state="limited", until_h=1)
    log.seen("old-limit", 70, state="limited", until_h=1)
    log.seen("newest-limit", 60, state="limited", until_h=1)
    log.seen("old-open", 50)
    log.seen("newest-open", 40)
    log.padding()
    assert log.gone() == {"old", "older-state", "old-limit", "old-open"}
    rows, drop = log.planned()
    assert _agree(rows, drop)[0]


def test_a_sighting_that_still_counts_stays_however_superseded(world):
    log = _Log()
    log.seen("limit-ahead", 100, state="limited", until_h=200)
    log.seen("no-end", 90, expires_at=None)
    log.seen("reading-fresh", 11)
    log.seen("limit-just-over", 80, state="limited", until_h=79.9)  # past by less than the margin
    log.seen("newer-limit", 70, state="limited", until_h=1)
    log.seen("reading-newest", 10)
    log.padding()
    assert not log.gone() & {"limit-ahead", "no-end", "reading-fresh", "limit-just-over", "reading-newest"}


def test_every_kind_keeps_its_newest_apart(world):
    """Newest of a scope, a window, a source, a basis, a state and a Mac: what
    ``_fold_account`` asks of old evidence, never answered for by another kind."""
    log = _Log()
    variants = {"scope": {"scope": "m1"}, "window": {"window": "weekly"}, "source": {"source": "check"},
                "basis": {"basis": "estimated"}, "other": {"account": "codex|"}}
    for name, extra in variants.items():
        log.seen(f"{name}-old", 90, **extra)
        log.seen(f"{name}-mid", 85, **extra)
        log.seen(f"{name}-new", 80, **extra)
    log.padding()
    gone = log.gone()
    assert not gone & {f"{name}-new" for name in variants}
    assert {f"{name}-mid" for name in variants} <= gone
    rows, drop = log.planned()
    assert _agree(rows, drop)[0]


def test_an_account_change_splits_the_kinds_of_what_it_names(world):
    log = _Log()
    log.seen("first", 100, account="codex|")
    log.seen("before-1", 90, account="codex|")
    log.seen("before-2", 85, account="codex|")
    log.owner("move", "account_move", 80, account="codex|", target=KEY)
    log.seen("after-1", 70, account="codex|")
    log.seen("after-2", 60, account="codex|")
    log.padding()
    gone = log.gone()
    assert "before-2" not in gone and "before-1" in gone and "after-1" in gone and "after-2" not in gone
    rows, drop = log.planned()
    assert _agree(rows, drop)[0]


def test_a_dismissed_report_stays_and_answers_for_no_other(world):
    log = _Log()
    log.seen("first", 100)
    log.seen("older", 90)
    log.seen("dismissed", 80)
    log.seen("by-source", 70)
    log.owner("no", "dismiss", 60, account=KEY, target="dismissed")
    log.owner("no2", "dismiss", 59, account=KEY, target="s-by-source")
    log.padding()
    assert not log.gone() & {"dismissed", "by-source"}
    # With the newest of its kind dismissed, the one before it is what counts.
    assert "older" not in log.gone()


def test_a_part_of_a_batch_stays(world):
    log = _Log()
    log.seen("first", 100)
    log.seen("in-batch", 90, batch="b1")
    log.seen("in-txn", 80, txn="t1")
    log.seen("newest", 70)
    log.padding()
    assert not log.gone() & {"in-batch", "in-txn"}
    # A batch counts only whole: one part gone voids the rest, and the guard sees it.
    log = _Log()
    log.seen("first", 100)
    log.seen("part-a", 90, batch="b2", batch_n=2, state="limited", until_h=200)
    log.seen("part-b", 89, batch="b2", batch_n=2)
    log.seen("newest", 80)
    log.padding()
    rows, drop = log.planned()
    assert not {log.names["part-a"], log.names["part-b"]} & drop
    assert not _agree(rows, drop | {log.names["part-b"]})[0]


def test_an_unmatched_lanes_current_run_stays_for_limited_since(world):
    log = _Log()
    runner = dict(account=None, runner="r1", lane="l1", source="runner", unmatched=True)
    log.seen("run0", 100, state="limited", until_h=1, **runner)
    log.seen("gap", 90, state="limited", until_h=1, **runner)
    log.seen("run-start", 80, state="limited", until_h=75, **runner)  # it overlaps all that follows
    log.seen("run-mid", 40, state="limited", until_h=39.5, **runner)
    log.seen("run-now", 1, state="limited", until_h=40, **runner)
    log.padding()
    assert not log.gone() & {"run-start", "run-mid", "run-now"}
    assert "gap" in log.gone()
    rows, drop = log.planned()
    assert _agree(rows, drop)[0]
    view = rc._Log([line for i, (line, _e) in enumerate(rows) if i not in drop], routing._inputs()).at(NOW)
    assert view["unmatched"][0]["limited_since"] == ISO(NOW - timedelta(hours=80))
    assert not _agree(rows, drop | {log.names["run-start"]})[0]  # the guard sees it too


def test_a_priority_levels_earliest_reset_stays(world):
    log = _Log()
    as_of = NOW - timedelta(hours=95)
    log.seen("before", 100, reset_at=ISO(as_of - timedelta(hours=1)))
    log.seen("ends-the-level", 90, reset_at=ISO(NOW + timedelta(hours=4)))
    log.seen("later", 80, reset_at=ISO(NOW + timedelta(hours=9)))
    log.seen("newest", 70, reset_at=ISO(NOW + timedelta(hours=20)))
    log.owner("p", "priority_set", 60, until=None, entries=[
        {"account": KEY, "label": "Claude", "role": "r", "until": None,
         "level": {"claim": "low", "as_of": ISO(as_of), "until": ISO(NOW + timedelta(days=30))}}])
    log.padding()
    gone = log.gone()
    assert "ends-the-level" not in gone and "later" in gone
    rows, drop = log.planned()
    assert _agree(rows, drop)[0]
    body = rc._Log([line for i, (line, _e) in enumerate(rows) if i not in drop], routing._inputs()).at(NOW)
    assert body["priority"]["entries"][0]["level"]["until"] == ISO(NOW + timedelta(hours=4))


def test_only_the_renewal_that_counts_stays_and_goes_with_its_line(world):
    log = _Log()
    log.seen("kept", 100)
    log.renew("r-old", "kept", 90)
    log.renew("r-other-author", "kept", 89, by={"kind": "probe", "harness": "h"})
    log.renew("r-new", "kept", 80)
    log.seen("goes", 70)
    log.renew("r-gone", "goes", 69)
    log.seen("newest", 60)
    log.padding()
    gone = log.gone()
    assert {"r-old", "goes", "r-gone"} <= gone
    assert not gone & {"kept", "r-new", "r-other-author", "newest"}
    rows, drop = log.planned()
    assert _agree(rows, drop)[0]


def test_a_sighting_a_source_still_offers_stays_on_its_line_or_renewal(world):
    log = _Log()
    log.seen("first", 100)
    log.seen("offered", 90)
    log.seen("renewed-as-offered", 80)
    log.renew("r-offered", "renewed-as-offered", 79)
    log.seen("newest", 70)
    log.padding()
    assert {"offered", "renewed-as-offered", "r-offered"} <= log.gone()
    gone = log.gone(emitted={"s-offered", "s-r-offered"})
    assert not gone & {"offered", "renewed-as-offered", "r-offered"}


def test_sources_that_cannot_be_read_keep_every_observation(world):
    log = _Log()
    for age in range(100, 40, -5):
        log.seen(f"o{age}", age)
    log.padding()
    assert log.gone(emitted=None) == set()


def test_the_newest_hours_stay_by_profile(world):
    log = _Log()
    log.seen("first", 100)
    log.seen("inside-mildest", 30, state="limited", until_h=1)
    log.seen("newest", 2, state="limited", until_h=1)
    log.padding()
    assert "inside-mildest" not in log.gone(profile=rc.PROFILES[0])
    assert "inside-mildest" in log.gone(profile=rc.PROFILES[-1])


def test_a_line_not_yet_uploaded_and_each_macs_last_never_go(world, monkeypatch):
    log = _Log()
    log.seen("first", 100)
    log.seen("sent", 90)
    log.seen("unsent", 80)
    log.seen("newest", 70)
    log.padding()
    assert {"sent", "unsent"} <= log.gone()
    monkeypatch.setattr(routing, "_replication_live", lambda: True)
    (routing.routing_dir() / "mac-id").parent.mkdir(parents=True, exist_ok=True)
    (routing.routing_dir() / "mac-id").write_text(MAC + "\n")
    (routing.routing_dir() / "replication.json").write_text(json.dumps(
        {"mac": MAC, "uploaded": {MAC: log.names["sent"] + 1}, "deferred": {}}))
    gone = log.gone()
    assert "sent" in gone and "unsent" not in gone


# ---- the guard -------------------------------------------------------------------------


def _offer(source_key, at, **extra):
    return {"id": source_key, "account": KEY, "runner": None, "lane": None, "state": "open", "until": None,
            "basis": "checked", "scope": "all", "window": None, "used_pct": None, "source": "probe",
            "written_by": {"kind": "probe"}, "via": "probe", "at": at, "reason": None, "evidence": "x",
            "message_time": False, **extra}


def _salted_log():
    """Two sightings of the kind, the newer one a source still offers."""
    log = _Log()
    salt = routing.entry_model.source_id
    log.seen("first", 100, source_id=salt("older-key"))
    log.seen("last", 8, source_id=salt("offered-key"))
    log.padding()
    return log


def test_the_guard_refuses_a_line_a_source_still_offers(world):
    log = _salted_log()
    sightings = ([_offer("offered-key", NOW - timedelta(hours=8))], None)
    rows, _drop = log.planned()
    same, _n, differs = _agree(rows, {log.names["last"]}, sightings)
    assert not same and differs
    assert _agree(rows, set(), sightings)[0]


def test_the_guard_refuses_what_changes_the_sources_next_lines(world):
    """The fold can't see this one: only the sightings' own batch can. The
    slot's last line goes, so a sighting older than it would be written again."""
    log = _Log()
    log.seen("first", 90, state="limited", until_h=1)
    log.seen("only", 80)
    log.padding()
    (routing.routing_dir() / "mac-id").parent.mkdir(parents=True, exist_ok=True)
    (routing.routing_dir() / "mac-id").write_text(MAC + "\n")
    sightings = ([_offer("late-key", NOW - timedelta(hours=85), state="limited", basis="cli",
                         until=NOW - timedelta(hours=84))], None)
    rows, _drop = log.planned()
    full = rc._Log([line for line, _e in rows], routing._inputs(), sightings)
    kept = rc._Log([line for i, (line, _e) in enumerate(rows) if i != log.names["only"]], routing._inputs(), sightings)
    assert full.would_write() != kept.would_write()
    assert full.would_write() == rc._Log([line for line, _e in rows], routing._inputs(), sightings).would_write()
    assert not rc.agree(full, kept, NOW)[0]


def test_a_rule_that_breaks_the_fold_costs_only_the_observations(world, monkeypatch):
    """Their proposal failing the guard falls back to keeping them all."""
    original = rc.plan

    def bad_observations(rows, now, profile, emitted=None):
        drop = original(rows, now, profile, emitted=None)
        if emitted is not None:
            drop |= {i for i, (_l, e) in enumerate(rows) if e and e.get("type") == "observation"}
        return drop

    monkeypatch.setattr(rc, "plan", bad_observations)
    rows = synth.stream_log(["claude|", "codex|"], NOW, seed=2, others=4000)
    synth.write(routing.events_file(), rows)
    out = rc.compact(NOW)
    assert out["result"] == "compacted"
    assert "observation" not in out["dropped_by_type"]


def test_a_compaction_with_sources_answers_alike_and_keeps_what_they_offer(world, monkeypatch):
    log = _salted_log()
    log.rows = [*synth.stream_log(["claude|", "codex|"], NOW - timedelta(minutes=2), seed=4), *log.rows]
    for i, row in enumerate(log.rows, 1):
        row["seq"] = i
    salt = routing.entry_model.source_id
    offered = next(r for r in log.rows[:2000] if r["type"] == "observation")
    offered["source_id"] = salt("still-in-the-tail")
    monkeypatch.setattr(routing, "_sources", lambda cfg, now, prev: (
        [_offer("still-in-the-tail", routing._parse(offered["witness_at"]))], None, {}))
    synth.write(routing.events_file(), log.rows)
    out = rc.compact(NOW)
    assert out["result"] == "compacted" and out["dropped_by_type"]["observation"] > 1000
    assert offered["id"].encode() in routing.events_file().read_bytes()


# ---- the archive -----------------------------------------------------------------------


def test_every_line_left_out_is_in_the_archive_byte_for_byte(world):
    rows = synth.stream_log(["claude|", "codex|"], NOW, seed=5)
    synth.write(routing.events_file(), rows)
    before = routing.events_file().read_bytes()
    out = rc.compact(NOW)
    assert out["result"] == "compacted" and out["dropped_by_type"]["observation"] > 1000
    archive = rc.archive_dir() / out["archive"]
    assert archive.read_bytes() == before
    live = routing.events_file().read_bytes().split(b"\n")
    assert set(live) <= set(before.split(b"\n"))
    # The live log is the archive's lines in the archive's order.
    lines = iter(before.split(b"\n"))
    assert all(any(line == other for other in lines) for line in live if line)
    assert rc.prune_archives(NOW + timedelta(days=1)) == 0 and archive.exists()


# ---- where the lines go ----------------------------------------------------------------


def test_the_log_doctor_said_could_not_shrink_comes_under_its_bound(world):
    rows = synth.stream_log(["claude|", "codex|", "muse|", "glm|"], NOW, seed=6)
    assert sum(r["type"] == "observation" for r in rows) > 4000
    synth.write(routing.events_file(), rows)
    out = rc.compact(NOW)
    assert out["result"] == "compacted"
    assert out["lines_after"] <= rc.LINES_YELLOW
    assert rc.health()["level"] == "green"
    assert "warning" not in rc.health()


def test_the_log_stays_near_its_bound_over_a_week_of_growth(world):
    """A week, not for good: each usage cycle keeps an anchor per witness however
    old (``_sample_drops``), so samples still add a few lines a day."""
    stream = synth.Stream(["claude|", "codex|"], NOW, seed=7, runners=1, lanes=4, owner_per_day=2)
    highest, compactions, at = 0, 0, NOW
    for _hour in range(0, 8 * 24, 3):
        at += timedelta(hours=3)
        synth.append(routing.events_file(), stream.until(at)) if routing.events_file().exists() else \
            synth.write(routing.events_file(), stream.until(at))
        world[0] = at
        if rc.maybe_compact(at)["result"] == "compacted":
            compactions += 1
            highest = max(highest, rc.log_size()["lines"])
    assert compactions >= 2
    assert highest <= rc.LINES_YELLOW
    assert rc.log_size()["lines"] <= rc.LINES_YELLOW + 300


def test_doctor_names_what_it_keeps_when_it_cannot_shrink(world, monkeypatch):
    monkeypatch.setattr(rc, "LINES_YELLOW", 20)
    monkeypatch.setattr(rc, "LINES_RED", 40)
    log = _Log()
    for n in range(30):
        log.owner(f"n{n}", "note_set", 40 - n, text=f"n{n}")
    synth.write(routing.events_file(), log.rows)
    world[0] = NOW
    assert rc.compact(NOW)["result"] == "skipped"
    row = rc.health()
    assert "can't shrink" in row["warning"] and "still counted" in row["warning"]
    assert "observations or others" not in row["warning"]
