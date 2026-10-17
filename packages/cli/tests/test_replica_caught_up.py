"""How current the replica is has one source, ``hub_snapshot.caught_up_at``:
the later of the last full dump and the last incremental pull that finished
with every part ok, including one that found nothing new. A copy dumped
hours ago and pulled a minute ago is a minute old to every reader: the stale
marker, doctor, snapshot status, the app and the trust rule
(docs/plans/2026-10-16-offline-and-self-healing.md, slices 2 to 4)."""
from __future__ import annotations

import ast
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from khipu import hub_snapshot as hs
from khipu import recall_prompt as rp
from khipu import replica_heal as rh
from tests.test_replica_pull_integrity import replica  # noqa: F401 - the fixture

PACKAGE = Path(hs.__file__).resolve().parent

# Every function that reads a replica time key, and why. "age" readers say how
# current the copy is and go through caught_up_at or snapshot_health's age;
# the rest name the dump itself (which dump a measurement or error belongs
# to, or when the next dump is due) or write it.
READERS = {
    ("hub_snapshot.py", "meta"): "dump: reads the dump's own stamps from the image",
    ("hub_snapshot.py", "caught_up_at"): "age: the one source",
    ("hub_snapshot.py", "_refresh_locked"): "dump: writes the dump's stamp",
    ("hub_snapshot.py", "snapshot_health"): "age: age_seconds from caught_up_at",
    ("hub_snapshot.py", "measure_lag"): "dump: needs a full dump on record",
    ("hub_snapshot.py", "_local_state"): "dump: a failed dump after the last one",
    ("hub_snapshot.py", "snapshot_freshness"): "dump: names the dump a measurement was of",
    ("hub_snapshot.py", "stale_fields"): "age: the offline reads' stamp, from caught_up_at",
    ("hub_snapshot.py", "_recent_measurement"): "dump: a measurement of this same dump",
    ("recall_eval.py", "_stamp"): "dump: which replica an eval ran against",
    ("recall_prompt.py", "_stale_marker"): "age: the marker, from caught_up_at",
    ("replica_heal.py", "last_update_at"): "age: from caught_up_at",
    ("replica_heal.py", "plan"): "dump: a failed dump, a measurement of this dump",
    ("cli.py", "cmd_owed"): "age: passes stale_fields through",
}

_KEYS = {"refreshed_at", "snapshot_refreshed_at"}


def _readers():
    found = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            nested = {id(n) for f in ast.walk(fn) if f is not fn
                      and isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(f)}
            if any(id(n) not in nested and isinstance(n, ast.Constant) and n.value in _KEYS for n in ast.walk(fn)):
                found.add((str(path.relative_to(PACKAGE)), fn.name))
    return found


def test_every_reader_of_the_replica_time_is_listed():
    assert _readers() == set(READERS), "a reader of the replica's time is not listed with how it reads it"


def _set_dump(hours_ago: float) -> datetime:
    """Move the fixture's dump back: both the image and the sidecar."""
    dump = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    with sqlite3.connect(hs.snapshot_path()) as con:
        for key in ("refreshed_at", "hub_dumped_at"):
            con.execute("INSERT OR REPLACE INTO snapshot_meta(key,value) VALUES (?,?)", (key, dump.isoformat()))
    hs._write_meta({"refreshed_at": dump.isoformat(), "hub_dumped_at": dump.isoformat()})
    return dump


def _minutes(health) -> float:
    return health["age_seconds"] / 60


def test_a_pull_makes_an_old_dump_current_for_every_reader(replica):  # noqa: F811
    dump = _set_dump(30)
    assert hs.snapshot_health()["age_seconds"] > 29 * 3600
    assert hs.pull_changes()["ok"]

    caught = hs.caught_up_at()
    assert caught is not None and hs._utc(caught) > dump
    health = hs.snapshot_health()
    assert health["caught_up_at"] == caught
    assert _minutes(health) < 2, health
    assert health["refreshed_at"] == dump.isoformat()
    assert health["dump_age_seconds"] > 29 * 3600

    line = rp._stale_line(rp._stale_marker(health))
    assert "minutes old" in line and "hours" not in line, line
    assert rp._stale_marker(health)["refreshed_at"] == caught
    assert hs.stale_fields()["snapshot_refreshed_at"] == caught

    fresh, why = hs.snapshot_is_fresh()
    assert fresh, why
    assert hs.prompt_recall_snapshot_status()["fresh"] is True
    assert hs.snapshot_freshness(None).get("state") != "stale"

    assert rh.last_update_at({}, health) == hs._utc(caught)
    # A pull leaves the graph tables as the dump wrote them: the job still
    # asks for a dump a day after the last one.
    action, reason = rh.plan(now=datetime.now(timezone.utc), state={}, health=health, sidecar=hs._read_sidecar(),
                             last_known=None, refresh_after=600.0)
    assert (action, reason) == ("dump", "overdue")


def test_a_pull_that_found_nothing_new_still_counts(replica):  # noqa: F811
    _set_dump(3)
    assert hs.pull_changes()["ok"]
    first = hs.caught_up_at()
    out = hs.pull_changes()
    assert out["ok"] and out["episodes"] == 0, out
    assert hs._utc(hs.caught_up_at()) >= hs._utc(first)
    assert _minutes(hs.snapshot_health()) < 2


def test_a_pull_with_a_failed_part_does_not_count(replica, monkeypatch):  # noqa: F811
    dump = _set_dump(3)
    monkeypatch.setattr(hs, "sync_decision_changes", lambda: {"ok": False, "error": "hub connection lost"})
    assert not hs.pull_changes()["ok"]
    assert hs.caught_up_at() == dump.isoformat()
    assert hs.snapshot_health()["age_seconds"] > 2 * 3600


def test_a_pull_made_against_another_dump_does_not_count(replica):  # noqa: F811
    dump = _set_dump(3)
    side = hs._read_sidecar()
    side.update({"pull_dump": "2026-01-01T00:00:00+00:00",
                 hs.PULL_CAUGHT_UP_KEY: datetime.now(timezone.utc).isoformat()})
    hs.meta_path().write_text(json.dumps(side), encoding="utf-8")
    assert hs.caught_up_at() == dump.isoformat()


def test_the_app_shows_the_catch_up_time():
    app = (PACKAGE.parents[2] / "apps" / "desktop" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "hubSnapshotHealth.caught_up_at" in app
