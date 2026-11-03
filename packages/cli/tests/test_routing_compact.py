"""The routing log is bounded (khipu.routing_compact): compaction keeps the
fold, the version and what ``since`` answers; a crash leaves a readable log;
archives go only when past both their count and their age; doctor goes yellow
and then red; and it runs by itself over the bound and nightly. Hermetic."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from khipu import jobs, routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests import test_routing_audit_fold as audit

world = audit.world
END = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
KEYS = ["claude|", "codex|"]
WHENS = [END, END + timedelta(hours=2), END + timedelta(days=1), END + timedelta(days=9), END + timedelta(days=60)]


def _log(seed=3, days=100, step=120, end=END):
    return synth.synth(KEYS, end, days=days, step_min=step, seed=seed)


def _install(rows):
    synth.write(routing.events_file(), rows)


def _bodies(whens):
    events, _bad = routing.read_events()
    return [rc._normal(routing.fold(events, now=w, cfg=routing.settings(), known=routing.accounts()))
            for w in whens]


def _lines():
    return rc._content_lines(routing.events_file().read_bytes())


def _types(lines):
    return [json.loads(l)["type"] for l in lines]


# ---- the property: the compacted log folds as the full one ---------------------------


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_compaction_keeps_the_fold(world, seed):
    _install(_log(seed=seed))
    before_lines = _lines()
    before = _bodies(WHENS)
    out = rc.compact(END)
    assert out["result"] == "compacted", out
    after_lines = _lines()
    assert len(after_lines) < len(before_lines) * 0.5
    # No line was rewritten: what is kept is the full log's lines, in order.
    it = iter(before_lines)
    assert all(any(line == other for other in it) for line in after_lines)
    # Every owner line is still there (for observations, test_routing_compact_observations).
    for kind in ("note_set", "priority_set", "lift", "clear"):
        assert _types(after_lines).count(kind) == _types(before_lines).count(kind)
    assert _bodies(WHENS) == before


def test_old_evidence_goes_too_and_the_fold_is_unchanged(world):
    _install(_log(days=220, step=240, seed=7))
    before = _bodies(WHENS)
    n_evidence = _types(_lines()).count("evidence")
    out = rc.compact(END)
    assert out["result"] == "compacted", out
    assert _types(_lines()).count("evidence") < n_evidence
    assert _bodies(WHENS) == before


def _record(steps, when, tail_rows):
    """Append ``tail_rows`` as lines and read the view at ``when``: its version
    and what it stands on, and what ``since`` answers for the first."""
    synth.append(routing.events_file(), tail_rows)
    view = routing.read_view(when)
    since = routing.since(steps[0]["version"], epoch=steps[0]["epoch"]) if steps else None
    steps.append({"version": view["version"], "epoch": view["epoch"], "effective": view["effective"],
                  "accounts": [(a["key"], a["state"], a["until"]) for a in view["accounts"]],
                  "pace": [a.get("pace") for a in view["accounts"]],
                  "priority": view.get("priority"), "rules": view.get("rules"), "note": view.get("note"),
                  "valid_until": view.get("valid_until"),
                  "since": None if since is None else {k: since[k] for k in ("version", "complete", "entities")}})


def _reset_state():
    for name in ("view.json", rc.STATE_NAME):
        (routing.routing_dir() / name).unlink(missing_ok=True)


def _tail(seed, step, later):
    at = routing._iso(later + timedelta(minutes=30 * step))
    return [{"type": "usage_sample", "account": KEYS[step % 2], "window": "weekly", "id": f"t{seed}-{step}",
             "seq": 90000 + step, "witness_at": at, "written_at": at,
             "resets_at": routing._iso(END + timedelta(days=3)), "used_pct": 40 + step, "t_err": 60,
             "source": "probe", "written_by": {"kind": "probe"}, "via": "probe"},
            {"type": "evidence", "account": KEYS[step % 2], "state": "limited" if step % 3 == 0 else "open",
             "basis": "estimated", "source": "report", "scope": "all", "id": f"ev{seed}-{step}",
             "seq": 91000 + step, "witness_at": at, "written_at": at,
             "until": routing._iso(later + timedelta(hours=2 + step)),
             "written_by": {"kind": "conversation", "harness": "codex"}, "via": "mcp"}]


@pytest.mark.parametrize("seed", [2, 5])
def test_version_epoch_and_since_behave_as_if_never_compacted(world, seed):
    """Two runs over the same log and the same later lines, one compacted
    half way: every step's version, effective view and ``since`` answer match;
    the epoch stays through the compaction."""
    prefix = _log(seed=seed, days=60)
    later = END + timedelta(hours=1)
    runs = {}
    for compacted in (False, True):
        _install(prefix)
        _reset_state()
        steps: list = []
        _record(steps, END, [])
        if compacted:
            epoch, version = steps[0]["epoch"], steps[0]["version"]
            assert rc.compact(END)["result"] == "compacted"
            view = routing.read_view(END)
            assert (view["epoch"], view["version"]) == (epoch, version)
        for step in range(1, 9):
            _record(steps, later + timedelta(minutes=30 * step + (5 if step % 2 else 0)), _tail(seed, step, later))
        runs[compacted] = steps
    for a, b in zip(runs[False], runs[True]):
        assert {k: v for k, v in a.items() if k != "epoch"} == {k: v for k, v in b.items() if k != "epoch"}
    assert len({s["epoch"] for s in runs[True]}) == 1
    assert runs[True][-1]["version"] > runs[True][0]["version"]


def test_the_owners_history_is_the_same_lines_after_compaction(world):
    """The owner's lines are never touched, so what an undo or a restore
    reads from the log is the same lines."""
    old = _log(seed=9, days=100)
    owner = [{"type": "priority_set", "id": f"p{i}", "seq": len(old) + 1 + i, "written_at": routing._iso(END),
              "entries": [{"account": KEYS[i % 2], "label": "A", "role": f"role {i}", "until": None}], "until": None,
              "written_by": {"kind": "owner"}, "via": "app", "auth": "app"} for i in range(3)]
    _install(old + owner)
    before = [l for l in _lines() if json.loads(l)["type"].startswith("priority")]
    out = rc.compact(END)
    assert out["result"] == "compacted", out
    assert [l for l in _lines() if json.loads(l)["type"].startswith("priority")] == before


# ---- a fold that would differ is refused ---------------------------------------------


def test_a_plan_that_changes_the_fold_writes_nothing(world, monkeypatch):
    _install(_log())
    original = routing.events_file().read_bytes()
    monkeypatch.setattr(rc, "plan", lambda rows, now, profile, **_kw: {
        i for i, (_l, e) in enumerate(rows) if e and e["type"] in ("usage_sample", "clear", "lift")})
    out = rc.compact(END)
    assert out["result"] == "skipped" and "fold differently" in out["why"]
    assert routing.events_file().read_bytes() == original
    assert not rc.archive_dir().exists() or not list(rc.archive_dir().iterdir())


def test_unreadable_lines_and_a_torn_last_line_survive(world):
    _install(_log(days=60))
    with routing.events_file().open("ab") as f:
        f.write(b"not json at all\n\xff\xfe broken\n{\"type\": \"note_set\", \"te")
    bad_before = routing.read_events()[1]
    assert bad_before >= 3
    assert rc.compact(END)["result"] == "compacted"
    assert routing.read_events()[1] == bad_before
    assert _lines()[-1].startswith(b'{"type": "note_set", "te')


# ---- a crash leaves a readable log -----------------------------------------------------


@pytest.mark.parametrize("where", ["replace", "archive", "write"])
def test_a_crash_mid_compaction_leaves_a_readable_log(world, monkeypatch, where):
    _install(_log(days=60))
    original = routing.events_file().read_bytes()
    before = _bodies(WHENS[:2])

    def boom(*a, **k):
        raise OSError("disk went away")

    with monkeypatch.context() as broken:
        if where == "replace":
            broken.setattr(rc.os, "replace", boom)
        elif where == "archive":
            broken.setattr(rc, "_write_archive", boom)
        else:
            broken.setattr(rc.os, "fsync", boom)
        assert rc.compact(END)["result"] == "skipped"
    assert routing.events_file().read_bytes() == original
    assert _bodies(WHENS[:2]) == before
    assert not list(routing.routing_dir().glob(".events-compact-*")), "the temp file is cleaned up"
    assert rc.compact(END)["result"] == "compacted"  # and the next attempt works


def test_lines_written_while_it_checked_are_carried_over(world, monkeypatch):
    _install(_log(days=60))
    late = {"type": "note_set", "text": "late", "id": "late1", "seq": 99999, "written_at": routing._iso(END),
            "written_by": {"kind": "owner"}, "via": "app", "auth": "app"}
    real = rc._choose

    def choose_then_write(data, now):
        out = real(data, now)
        synth.append(routing.events_file(), [late])  # a writer appends between the check and the swap
        return out

    monkeypatch.setattr(rc, "_choose", choose_then_write)
    assert rc.compact(END)["result"] == "compacted"
    assert json.loads(_lines()[-1])["id"] == "late1"
    assert routing.read_view(END)["note"]["text"] == "late"


def test_a_line_half_written_at_the_read_survives_once_and_whole(world, monkeypatch):
    _install(_log(days=60))
    line = json.dumps({"type": "note_set", "text": "half", "id": "half1", "seq": 99998,
                       "written_at": routing._iso(END), "written_by": {"kind": "owner"}, "via": "app",
                       "auth": "app"}, sort_keys=True).encode()
    with routing.events_file().open("ab") as f:
        f.write(line[:40])  # a writer's line, half down when the log is read
    bad_before = routing.read_events()[1]
    real = rc._choose

    def finish_the_line(data, now):
        out = real(data, now)
        with routing.events_file().open("ab") as f:
            f.write(line[40:] + b"\n")  # the writer finishes before the swap
        return out

    monkeypatch.setattr(rc, "_choose", finish_the_line)
    assert rc.compact(END)["result"] == "compacted"
    data = routing.events_file().read_bytes()
    assert data.count(line + b"\n") == 1 and data.endswith(line + b"\n")
    assert routing.read_events()[1] == bad_before - 1 if bad_before else True
    assert routing.read_view(END)["note"]["text"] == "half"


def test_protected_history_keeps_absolute_red_severity(world, monkeypatch):
    monkeypatch.setattr(rc, "LINES_YELLOW", 50)
    monkeypatch.setattr(rc, "LINES_RED", 100)
    monkeypatch.setattr(rc, "BYTES_YELLOW", 10 ** 9)
    monkeypatch.setattr(rc, "BYTES_RED", 10 ** 9)
    owner = [{"type": "note_set", "text": f"n{i}", "id": f"n{i}", "seq": i + 1,
              "written_at": routing._iso(END - timedelta(minutes=300 - i)),
              "written_by": {"kind": "owner"}, "via": "app", "auth": "app"} for i in range(150)]
    _install(owner)
    assert rc.health()["level"] == "red"  # not tried yet: the plain row
    assert rc.compact(END)["result"] == "skipped"
    row = rc.health()
    assert row["level"] == "red" and "can't shrink" in row["warning"] and "150 of its lines" in row["warning"]
    assert "protected history" in row["warning"] and "nothing is broken" in row["warning"]


def test_protected_history_keeps_absolute_byte_severity(world, monkeypatch):
    _install(_log(days=1))
    monkeypatch.setattr(rc, "BYTES_RED", 1)
    monkeypatch.setattr(rc, "_read_state", lambda: {"attempted_at": routing._iso(END)})
    monkeypatch.setattr(rc, "_droppable", lambda: (0, 100))
    assert rc.health()["level"] == "red"


def test_a_rewritten_log_is_not_replaced(world, monkeypatch):
    _install(_log(days=60))
    real = rc._choose

    def choose_then_rewrite(data, now):
        out = real(data, now)
        routing.events_file().write_bytes(b"")  # something else reset it meanwhile
        return out

    monkeypatch.setattr(rc, "_choose", choose_then_rewrite)
    out = rc.compact(END)
    assert out["result"] == "skipped" and "changed" in out["why"]
    assert routing.events_file().read_bytes() == b""


# ---- archives ---------------------------------------------------------------------------


def _archive(day: datetime) -> str:
    folder = rc.archive_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"events-{day.strftime('%Y%m%dT%H%M%SZ')}-aaaaaa.jsonl"
    path.write_text("{}\n")
    return path.name


def test_archives_go_only_when_past_both_count_and_age(world):
    names = [_archive(END - timedelta(days=d)) for d in (1, 2, 3, 4, 5, 6, 40, 50, 60)]
    # Nine archives: the newest four are inside the count; of the rest, only those older than 30 days go.
    assert rc.prune_archives(END) == 3
    assert sorted(p.name for p in rc.archive_dir().iterdir()) == sorted(names[:6])
    for p in rc.archive_dir().iterdir():
        p.unlink()
    for d in (100, 200, 300):
        _archive(END - timedelta(days=d))
    assert rc.prune_archives(END) == 0  # inside the count, however old
    assert len(list(rc.archive_dir().iterdir())) == 3


def test_compaction_archives_what_it_leaves_out(world):
    _install(_log(days=60))
    full = _lines()
    out = rc.compact(END)
    archived = rc.archive_dir() / out["archive"]
    assert rc.full_log() == full
    assert oct(archived.stat().st_mode & 0o777) == "0o600"
    assert rc.health()["archives"] == 1


# ---- doctor ----------------------------------------------------------------------------


def test_doctor_row_is_green_then_yellow_then_red(world, monkeypatch):
    monkeypatch.setattr(rc, "LINES_YELLOW", 50)
    monkeypatch.setattr(rc, "LINES_RED", 120)
    monkeypatch.setattr(rc, "BYTES_YELLOW", 10 ** 9)
    monkeypatch.setattr(rc, "BYTES_RED", 10 ** 9)
    rows = _log(days=5, step=30)
    _install(rows[:30])
    assert routing.doctor_block(END)["log"]["level"] == "green"
    _install(rows[:80])
    block = routing.doctor_block(END)
    assert block["log"]["level"] == "yellow" and block["log"]["lines"] == 80 and block["log"]["bytes"] > 0
    assert any("80 lines" in w for w in block["warnings"])
    _install(rows[:200])
    block = routing.doctor_block(END)
    assert block["log"]["level"] == "red" and block["log"]["lines"] == 200
    assert any("past 120 lines" in w for w in block["warnings"])


def test_bytes_alone_turn_the_row_yellow(world, monkeypatch):
    monkeypatch.setattr(rc, "BYTES_YELLOW", 5000)
    monkeypatch.setattr(rc, "BYTES_RED", 10 ** 9)
    _install(_log(days=3, step=60)[:60])
    assert rc.health()["level"] == "yellow"


# ---- when it runs --------------------------------------------------------------------


def test_it_runs_by_itself_over_the_line_and_not_under_it(world, monkeypatch):
    monkeypatch.setattr(rc, "LINES_TRIGGER", 500)
    monkeypatch.setattr(rc, "BYTES_TRIGGER", 10 ** 9)
    _install(_log(days=100)[:300])
    assert rc.maybe_compact(END) == {"result": "not due"}
    _install(_log(days=100))
    out = rc.maybe_compact(END)
    assert out["result"] == "compacted" and out["lines_after"] < out["lines_before"]
    _install(_log(days=100))  # still over, but inside the interval
    assert rc.maybe_compact(END + timedelta(hours=1)) == {"result": "not due"}
    assert rc.maybe_compact(END + timedelta(hours=7))["result"] == "compacted"


def test_the_plan_figures_refresh_and_the_nightly_both_compact(world, monkeypatch):
    from khipu import features, usage

    monkeypatch.setattr(rc, "LINES_TRIGGER", 500)
    monkeypatch.setattr(rc, "BYTES_TRIGGER", 10 ** 9)
    monkeypatch.setattr(features, "enabled", lambda *a, **k: True)
    monkeypatch.setattr(usage, "refresh_due", lambda: {"refreshed": 0})
    monkeypatch.setattr(routing, "record_pace_samples", lambda *a, **k: 0)
    monkeypatch.setattr(routing, "_now", lambda: END)
    _install(_log(days=100))
    assert routing.refresh_plan_figures() == {"refreshed": 0}
    assert len(_lines()) < 2000
    _install(_log(days=100))  # the nightly takes no interval
    monkeypatch.setattr("khipu.routing_ledger_prune.prune", lambda: {"ok": True})
    assert jobs._prune_routing_ledgers()["log"]["result"] == "compacted"


def test_a_compaction_that_gains_little_is_not_made(world):
    _install(_log(days=1, step=60)[:200])
    original = routing.events_file().read_bytes()
    assert rc.compact(END)["result"] == "skipped"
    assert routing.events_file().read_bytes() == original
    assert not rc.archive_dir().exists() or not list(rc.archive_dir().iterdir())
