"""The nightly recall measurement: step record, doctor row thresholds,
forgotten-id skipping and ``recall eval --install``. Fake data throughout:
the replica is an in-memory sqlite and the scorer is stubbed. Scoring runs in
a spawned child, so stubs are installed there by module-level ``_child_*``
functions passed as ``child_setup``; what the child saw comes back through a
file."""
from __future__ import annotations

import json
import sqlite3
import stat
import time
from datetime import datetime, timedelta, timezone

import pytest

from khipu import hub_snapshot, recall_eval, recall_eval_nightly as ren


def _replica(live=(1, 2, 3), topics=(), nodes=()):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY)")
    con.execute("CREATE TABLE topics (slug TEXT, deleted_at TEXT)")
    con.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    con.executemany("INSERT INTO episodes VALUES (?)", [(i,) for i in live])
    con.executemany("INSERT INTO topics VALUES (?, ?)", topics)
    con.executemany("INSERT INTO nodes VALUES (?)", [(n,) for n in nodes])
    return con


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(ren, "history_path", lambda: tmp_path / "state" / "hist.json")
    golden = tmp_path / "golden.jsonl"
    monkeypatch.setattr(recall_eval, "default_golden_path", lambda: golden)
    monkeypatch.setattr(hub_snapshot, "open_snapshot", lambda **_k: _replica())
    monkeypatch.setattr("khipu.tombstones.forgotten_episode_ids", lambda: set())
    monkeypatch.setattr("khipu.jobs._is_index_sync_host", lambda: True)
    return tmp_path, golden


def _write_golden(path, entries):
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def _child_fake_scorer(seen_file, misses=(), stale=False, boom=()):
    def fake(entry, path, **_k):
        if entry["query"] in boom:
            raise ValueError("bad entry")
        with open(seen_file, "a", encoding="utf-8") as out:
            out.write(json.dumps([entry["query"], path]) + "\n")
        hit = entry["query"] not in misses
        return {"expect_none": bool(entry.get("expect_none")), "hit": hit,
                "reciprocal_rank": 1.0 if hit else 0.0, "abstain_correct": True,
                "stale_violation": stale, "timeout": False, "error": False, "degraded": None,
                "outcome": "match", "ms": 1.0, "payload_chars": 10}

    recall_eval.eval_one_path = fake


def _stub_scorer(tmp, **kwargs):
    """A child setup that stubs the scorer, and a reader of the (query, path)
    pairs the child scored."""
    seen_file = tmp / f"seen-{time.monotonic_ns()}.jsonl"

    def seen():
        if not seen_file.exists():
            return []
        return [tuple(json.loads(line)) for line in seen_file.read_text().splitlines()]

    args = (str(seen_file), tuple(kwargs.get("misses", ())), bool(kwargs.get("stale")),
            tuple(kwargs.get("boom", ())))
    return (_child_fake_scorer, args), seen


def test_step_records_per_path_figures_and_golden_stat(world):
    tmp, golden = world
    _write_golden(golden, [{"query": f"q{i}", "expect": ["1"]} for i in range(4)]
                  + [{"query": "none", "expect_none": True}])
    setup, seen = _stub_scorer(tmp, misses=("q0",))
    rec = ren.run_nightly_eval(golden=golden, child_setup=setup)
    assert rec["ok"] and rec["entries"] == 5 and rec["scored"] == 5
    assert set(rec["paths"]) == set(ren.MEASURED_PATHS) and "explicit" not in rec["paths"]
    assert rec["not_measured"] == {"explicit": {"needs_hub": 5}}
    assert {p for _, p in seen()} == set(ren.MEASURED_PATHS)  # the search tool is never run
    assert rec["paths"]["prompt"]["hit_rate"] == 0.75
    assert rec["paths"]["prompt"]["abstain_correct"] == 1
    assert rec["golden"]["bytes"] == golden.stat().st_size and rec["golden"]["mtime"]
    assert rec["previous"] is None
    second = ren.run_nightly_eval(golden=golden, child_setup=setup)
    assert second["previous"]["paths"]["prompt"]["hit_rate"] == 0.75


def test_history_keeps_30_records_and_never_holds_a_query(world):
    tmp, golden = world
    _write_golden(golden, [{"query": "very private words", "expect": ["1"]}])
    ren._write_history([{"ts": f"2026-10-{i:02d}T00:00:00+00:00", "ok": True, "paths": {}}
                        for i in range(1, 35)])
    setup, _ = _stub_scorer(tmp)
    rec = ren.run_nightly_eval(golden=golden, child_setup=setup)
    hist = ren.history_path()
    assert rec["scored"] == 1 and len(ren.read_history()) == 30
    assert "very private words" not in hist.read_text()
    assert stat.S_IMODE(hist.stat().st_mode) == 0o600


def _child_real_scorers_with_spies(calls_file):
    """Inside the child, before its guard: the lowest real exits become spies
    that log and raise, and the replica is the fake one."""
    import socket
    import subprocess
    import urllib.request

    import psycopg

    from khipu import embed, hub_reach

    def spy(name):
        def fn(*a, **_k):
            with open(calls_file, "a", encoding="utf-8") as out:
                out.write(name + ":" + repr(a)[:200] + "\n")
            raise AssertionError(f"{name} reached")
        return fn

    psycopg.connect = spy("psycopg.connect")
    socket.socket.connect = spy("socket.connect")
    socket.socket.connect_ex = spy("socket.connect_ex")
    socket.create_connection = spy("create_connection")
    socket.getaddrinfo = spy("getaddrinfo")
    subprocess.Popen = spy("Popen")
    embed._transport = spy("embed._transport")
    embed.embed_one = spy("embed.embed_one")
    urllib.request.urlopen = spy("urlopen")
    hub_reach.note_unreachable = spy("note_unreachable")
    hub_reach.note_reachable = spy("note_reachable")
    hub_snapshot.open_snapshot = lambda **_k: _replica()


def test_whole_step_makes_no_network_hub_or_provider_call(world):
    """The real scorers run end to end in the child against a fake replica;
    the lowest real exits are spies that must never fire, and the query never
    reaches one."""
    tmp, golden = world
    secret = "zebra-private-golden-words"
    _write_golden(golden, [{"query": secret, "expect": ["1"]},
                           {"query": secret + " two", "expect_none": True}])
    calls = tmp / "calls.log"
    rec = ren.run_nightly_eval(golden=golden, budget_s=60,
                               child_setup=(_child_real_scorers_with_spies, (str(calls),)))
    assert not calls.exists(), calls.read_text()
    assert rec["ok"] and rec["scored"] == 2 and not rec["truncated"]
    assert set(rec["paths"]) == set(ren.MEASURED_PATHS)
    assert rec["not_measured"]["explicit"] == {"needs_hub": 2}
    assert secret not in ren.history_path().read_text()


def _child_abandoning_scorer(marker_dir, release_at):
    """Opus's scenario: a lookup that outlives the deadline on its own thread
    and then makes an embedding call with the golden query. The spy stands in
    for the real embed_one; the guard is installed over it afterwards."""
    import pathlib

    from khipu import embed

    marks = pathlib.Path(marker_dir)

    def real_embed(query, **_k):
        (marks / "real-call").write_text(query)
        return [0.0]

    def stuck(query, entry, **_k):
        (marks / "started").write_text("1")
        while time.monotonic() < release_at:
            time.sleep(0.05)
        embed.embed_one(query, input_type="query")
        return {}

    embed.embed_one = real_embed
    recall_eval._call_prompt_or_status = stuck


def test_a_lookup_thread_outliving_the_deadline_never_reaches_a_real_exit(world):
    tmp, golden = world
    _write_golden(golden, [{"query": "abandoned-secret", "expect": ["1"], "paths": ["prompt"]}])
    budget = 1.5
    release_at = time.monotonic() + budget + 0.5
    rec = ren.run_nightly_eval(golden=golden, budget_s=budget,
                               child_setup=(_child_abandoning_scorer, (str(tmp), release_at)))
    assert rec["truncated"] and rec["scored"] == 0
    while time.monotonic() < release_at + 1.5:
        time.sleep(0.1)
    assert (tmp / "started").exists()  # the lookup really ran and was abandoned
    assert not (tmp / "real-call").exists()
    assert "abandoned-secret" not in ren.history_path().read_text()


def _child_raising_thread(query):
    import threading

    def boom():
        raise ValueError(query)

    t = threading.Thread(target=boom)
    t.start()
    t.join()


def test_a_stray_thread_exception_in_the_child_logs_only_its_type(world, capfd):
    tmp, golden = world
    secret = "stderr-secret-golden-words"
    _write_golden(golden, [{"query": "q", "expect": ["1"]}])
    ren.run_nightly_eval(golden=golden, child_setup=(_child_raising_thread, (secret,)))
    err = capfd.readouterr().err
    assert secret not in err
    assert "uncaught ValueError" in err


def _child_blocked_scorer(marker_dir):
    import pathlib

    def blocked(*_a, **_k):
        (pathlib.Path(marker_dir) / "started").write_text("1")
        time.sleep(60)

    recall_eval.eval_one_path = blocked


def test_a_stuck_scorer_is_killed_and_the_step_returns_by_the_deadline(world):
    tmp, golden = world
    _write_golden(golden, [{"query": "q", "expect": ["1"]}])
    budget = 1.5
    t0 = time.monotonic()
    rec = ren.run_nightly_eval(golden=golden, budget_s=budget,
                               child_setup=(_child_blocked_scorer, (str(tmp),)))
    assert time.monotonic() - t0 < budget + 2
    assert (tmp / "started").exists()
    assert rec["truncated"] and rec["stopped_child"] and rec["scored"] == 0
    assert ren.read_history()[-1]["truncated"] is True


def _child_hub_fallback(_unused=None):
    def answer(query, entry, **_k):
        if query == "falls back":
            hub_snapshot.try_hub_connect()  # refused by the guard: raises
        return {"hits": [{"id": "1"}], "legs": ["lexical"]}

    recall_eval._call_prompt_or_status = answer


def test_a_lookup_that_falls_back_to_the_hub_is_not_measured_not_an_error(world):
    tmp, golden = world
    _write_golden(golden, [{"query": "falls back", "expect": ["1"], "paths": ["prompt", "status"]},
                           {"query": "replica", "expect": ["1"], "paths": ["prompt", "status"]}])
    rec = ren.run_nightly_eval(golden=golden, child_setup=(_child_hub_fallback, ()))
    assert rec["scored"] == 2 and rec["offline"]["hub"] == 2
    assert rec["not_measured"] == {"prompt": {"hub_fallback": 1}, "status": {"hub_fallback": 1}}
    for p in ("prompt", "status"):
        assert rec["paths"][p]["total"] == 1 and rec["paths"][p]["errors"] == 0
        assert rec["paths"][p]["hit_rate"] == 1.0


def test_offline_guard_refuses_and_counts_every_exit_then_restores(monkeypatch):
    import socket
    import subprocess
    import urllib.error
    import urllib.request

    from khipu import db, embed

    real = (hub_snapshot.try_hub_connect, db.connect, embed.embed_one, socket.socket.connect,
            socket.getaddrinfo, subprocess.Popen)
    with ren._offline() as guard:
        with pytest.raises(Exception, match="connection refused"):
            hub_snapshot.try_hub_connect()
        with pytest.raises(Exception, match="connection refused"):
            db.connect()
        with pytest.raises(RuntimeError, match="embedding refused"):
            embed.embed_one("x", input_type="query")
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen("http://example.invalid/", timeout=1)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", 9))
        with pytest.raises(PermissionError):
            subprocess.run(["true"], check=False)
        assert guard.refused["hub"] == 2 and guard.refused["embed"] == 1
        assert guard.refused["network"] >= 2 and guard.refused["process"] == 1
    assert (hub_snapshot.try_hub_connect, db.connect, embed.embed_one, socket.socket.connect,
            socket.getaddrinfo, subprocess.Popen) == real


def test_entries_naming_forgotten_erased_or_deleted_ids_are_skipped_not_missed(world, monkeypatch):
    tmp, golden = world
    monkeypatch.setattr(hub_snapshot, "open_snapshot", lambda **_k: _replica(
        live=(1, 2), topics=[("gone-topic", "2026-01-01"), ("live-topic", None)],
        nodes=["wiki:live-node"]))
    monkeypatch.setattr("khipu.tombstones.forgotten_episode_ids", lambda: {2})
    _write_golden(golden, [
        {"query": "alive", "expect": ["1"]},
        {"query": "forgotten", "expect": ["2"]},
        {"query": "erased", "expect": ["99"]},
        {"query": "topic gone", "expect": ["gone-topic"]},
        {"query": "topic erased", "expect": ["erased-topic-slug"]},
        {"query": "partly", "expect": ["99", "1"]},
        {"query": "live topic", "expect": ["live-topic"]},
        {"query": "live node", "expect": ["wiki:live-node"]},
        {"query": "library", "expect": ["library:books:doc#1"]},
    ])
    setup, seen = _stub_scorer(tmp)
    rec = ren.run_nightly_eval(golden=golden, child_setup=setup)
    assert rec["skipped_forgotten"] == 4
    assert rec["skipped_ids"] == 4
    assert {q for q, _ in seen()} == {"alive", "partly", "live topic", "live node", "library"}
    assert rec["paths"]["prompt"]["hit_rate"] == 1.0
    text = ren.history_path().read_text()
    for ident in ("erased-topic-slug", "gone-topic", '"99"'):
        assert ident not in text


def test_an_old_replica_without_topic_tables_judges_no_slug(world, monkeypatch):
    tmp, golden = world

    def bare(**_k):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE episodes (id INTEGER PRIMARY KEY)")
        return con

    monkeypatch.setattr(hub_snapshot, "open_snapshot", bare)
    _write_golden(golden, [{"query": "slug", "expect": ["some-topic"]}])
    setup, _ = _stub_scorer(tmp)
    assert ren.run_nightly_eval(golden=golden, child_setup=setup)["skipped_forgotten"] == 0


def test_step_stops_at_its_time_budget(world):
    tmp, golden = world
    _write_golden(golden, [{"query": f"q{i}", "expect": ["1"]} for i in range(3)])
    setup, seen = _stub_scorer(tmp)
    rec = ren.run_nightly_eval(golden=golden, budget_s=-1, child_setup=setup)
    assert seen() == []
    assert rec["truncated"] and rec["scored"] == 0


def _child_slow_lookup(durations_file):
    def slow(*_a, **_k):
        time.sleep(5.0)
        return {"hits": [{"id": "1"}], "legs": ["lexical"]}

    real = recall_eval.eval_one_path

    def timed(*a, **k):
        t0 = time.monotonic()
        try:
            return real(*a, **k)
        finally:
            with open(durations_file, "a", encoding="utf-8") as out:
                out.write(f"{time.monotonic() - t0}\n")

    recall_eval._call_prompt_or_status = slow
    recall_eval.eval_one_path = timed


def test_a_slow_entry_stops_inside_at_the_deadline(world):
    tmp, golden = world
    _write_golden(golden, [{"query": f"q{i}", "expect": ["1"], "paths": ["prompt"]} for i in range(3)])
    durations = tmp / "durations.txt"
    budget = 1.5
    t0 = time.monotonic()
    rec = ren.run_nightly_eval(golden=golden, budget_s=budget,
                               child_setup=(_child_slow_lookup, (str(durations),)))
    assert time.monotonic() - t0 < budget + 1
    assert rec["truncated"] and rec["scored"] == 0
    got = [float(x) for x in durations.read_text().split()]
    assert got and got[0] < budget  # the entry stopped at the deadline, not after its 5 s lookup
    assert rec["stopped_child"] is False  # the child stopped itself; nothing had to be killed
    assert ren.read_history()[-1]["truncated"] is True


def test_malformed_golden_lines_are_skipped_and_counted_never_raised(world):
    tmp, golden = world
    golden.write_text("\n".join([
        json.dumps({"query": "fine", "expect": ["1"]}),
        json.dumps({"query": "bad k", "expect": ["1"], "k": "not-an-int"}),
        json.dumps({"query": "bad paths", "expect": ["1"], "paths": "prompt"}),
        "{not json",
        json.dumps({"query": "no expect"}),
    ]) + "\n", encoding="utf-8")
    setup, seen = _stub_scorer(tmp)
    rec = ren.run_nightly_eval(golden=golden, child_setup=setup)
    assert rec["ok"] and rec["entries"] == 5 and rec["scored"] == 1
    assert rec["skipped_malformed"] == 4
    assert rec["malformed"] == {"bad_k": 1, "bad_paths": 1, "bad_json": 1, "bad_expect": 1}
    assert {q for q, _ in seen()} == {"fine"}
    assert "bad k" not in ren.history_path().read_text()


def test_a_scorer_crash_skips_the_entry_and_history_is_still_written(world):
    tmp, golden = world
    _write_golden(golden, [{"query": "boom", "expect": ["1"]}, {"query": "fine", "expect": ["1"]}])
    setup, _ = _stub_scorer(tmp, boom=("boom",))
    rec = ren.run_nightly_eval(golden=golden, child_setup=setup)
    assert rec["skipped_malformed"] == 1 and rec["scored"] == 1
    assert len(ren.read_history()) == 1


def test_real_scorer_with_a_non_int_k_does_not_sink_the_run(world):
    tmp, golden = world
    golden.write_text(json.dumps({"query": "q", "expect": ["1"], "k": "x"}) + "\n", encoding="utf-8")
    rec = ren.run_nightly_eval(golden=golden)
    assert rec["skipped_malformed"] == 1 and len(ren.read_history()) == 1


def test_missing_replica_is_a_failed_step_not_a_zero_score(world, monkeypatch):
    tmp, golden = world
    _write_golden(golden, [{"query": "q", "expect": ["1"]}])

    def boom(**_k):
        raise FileNotFoundError("no replica")

    monkeypatch.setattr(hub_snapshot, "open_snapshot", boom)
    rec = ren.run_nightly_eval(golden=golden)
    assert rec["ok"] is False and rec["transient"] and rec["paths"] == {}
    assert len(ren.read_history()) == 1


def test_nightly_step_defers_a_transient_skip_instead_of_failing(world, monkeypatch):
    from khipu import jobs

    tmp, golden = world
    _write_golden(golden, [{"query": "q", "expect": ["1"]}])

    def boom(**_k):
        raise FileNotFoundError("no replica")

    monkeypatch.setattr(hub_snapshot, "open_snapshot", boom)
    out = jobs._recall_eval_step()
    assert out["ok"] is True and "replica unavailable" in out["deferred"] and "error" not in out


# ---- doctor row ---------------------------------------------------------------

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def _record(world, *, rate=0.8, prev=0.8, entries=25, age_h=2, violations=0, ok=True,
            truncated=False, errors=0, timeouts=0):
    prompt = {"total": 10, "hit_rate": rate, "abstain_total": 0, "stale_violations": violations,
              "errors": errors, "timeouts": timeouts}
    rec = {"ts": (NOW - timedelta(hours=age_h)).isoformat(), "ok": ok, "entries": entries,
           "truncated": truncated, "paths": {"prompt": prompt},
           "previous": None if prev is None else {"paths": {"prompt": {"hit_rate": prev}}}}
    ren._write_history([rec])


@pytest.mark.parametrize("kwargs,level", [
    (dict(), "green"),
    (dict(rate=0.76, prev=0.8), "green"),          # a 4-point drop is inside the allowance
    (dict(rate=0.74, prev=0.8), "red"),            # 6 points
    (dict(rate=0.65, prev=0.7), "green"),          # exactly 5 points, above the floor
    (dict(rate=0.55, prev=0.56), "red"),           # under the 0.6 floor
    (dict(violations=1), "red"),
    (dict(entries=19), "yellow"),
    (dict(age_h=49), "yellow"),
    (dict(ok=False), "yellow"),
    (dict(ok=False, rate=0.1, prev=0.9), "yellow"),  # a run that could not finish never reds
    (dict(truncated=True, rate=0.1, prev=0.9), "yellow"),
    (dict(errors=2, timeouts=2, rate=0.4, prev=0.9), "yellow"),  # 4 of 10 lookups failed: transient
    (dict(errors=1, rate=0.4, prev=0.9), "red"),  # 1 of 10 failed: the drop is real
])
def test_doctor_row_levels(world, kwargs, level):
    _record(world, **kwargs)
    row = ren.recall_eval_health(now=NOW)
    assert row["level"] == level
    assert row["ok"] is (level != "red")
    assert row["summary"]


def test_doctor_row_counts_hub_fallbacks_as_not_measured_never_as_failures(world):
    rec = {"ts": (NOW - timedelta(hours=2)).isoformat(), "ok": True, "entries": 25,
           "truncated": False, "previous": {"paths": {"prompt": {"hit_rate": 0.9}}},
           "paths": {"prompt": {"total": 0, "hit_rate": 0.0, "abstain_total": 0,
                                "stale_violations": 0, "errors": 0, "timeouts": 0}},
           "not_measured": {"prompt": {"hub_fallback": 25}, "explicit": {"needs_hub": 25}}}
    ren._write_history([rec])
    row = ren.recall_eval_health(now=NOW)
    assert row["level"] == "yellow" and row["ok"]
    assert "needed the hub" in row["summary"] and "failed" not in row["summary"]


def test_doctor_row_before_any_run_is_yellow(world, monkeypatch):
    monkeypatch.setattr("khipu.jobs._is_index_sync_host", lambda: True)
    row = ren.recall_eval_health(now=NOW)
    assert row["level"] == "yellow" and row["ok"]


def test_doctor_row_is_not_applicable_off_the_nightly_host(world, monkeypatch):
    monkeypatch.setattr("khipu.jobs._is_index_sync_host", lambda: False)
    assert ren.recall_eval_health(now=NOW)["applicable"] is False


def test_doctor_row_is_not_applicable_off_the_host_even_with_red_history(world, monkeypatch):
    _record(world, rate=0.1, prev=0.9)
    monkeypatch.setattr("khipu.jobs._is_index_sync_host", lambda: False)
    row = ren.recall_eval_health(now=NOW)
    assert row["applicable"] is False and row["ok"] is True


def test_a_stopped_or_failed_run_is_never_the_baseline(world, monkeypatch):
    tmp, golden = world
    _write_golden(golden, [{"query": "q", "expect": ["1"]}])
    ren._write_history([
        {"ts": "2026-10-28T00:00:00+00:00", "ok": True, "paths": {"prompt": {"hit_rate": 0.9}}},
        {"ts": "2026-10-29T00:00:00+00:00", "ok": True, "truncated": True,
         "paths": {"prompt": {"hit_rate": 0.1}}},
        {"ts": "2026-10-29T01:00:00+00:00", "ok": False, "paths": {}},
    ])
    setup, _ = _stub_scorer(tmp)
    rec = ren.run_nightly_eval(golden=golden, child_setup=setup)
    assert rec["previous"]["paths"]["prompt"]["hit_rate"] == 0.9


# ---- --install ----------------------------------------------------------------

def test_install_merges_dedupes_validates_and_backs_up(world):
    tmp, golden = world
    _write_golden(golden, [{"query": "old", "expect": ["1"]}])
    before = golden.read_text()
    cand = tmp / "cand.jsonl"
    _write_golden(cand, [
        {"query": "old", "expect": ["2"]},                       # duplicate: existing wins
        {"query": "new", "expect": ["2"], "n": 3, "source": "replay"},
        {"query": "new", "expect": ["3"]},                       # duplicate within candidates
        {"query": "dangling", "expect": ["404"]},                # id not in the replica
        {"query": "abstain", "expect_none": True},
    ])
    result = ren.install_candidates(cand, golden)
    assert result == {"ok": True, "added": 2, "duplicates": 2, "rejected_missing_id": 1,
                      "rejected_malformed": 0, "malformed_reasons": {}, "total": 3}
    merged = [json.loads(line) for line in golden.read_text().splitlines()]
    assert [e["query"] for e in merged] == ["old", "new", "abstain"]
    assert merged[1]["source"] == "replay" and merged[0]["expect"] == ["1"]
    backup = golden.parent / ren.BACKUP_NAME
    assert backup.read_text() == before
    assert stat.S_IMODE(golden.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert "old" not in json.dumps(result) and "new" not in json.dumps(result)


def test_install_rejects_entries_the_scorer_cannot_take(world):
    tmp, golden = world
    cand = tmp / "cand.jsonl"
    cand.write_text("\n".join([
        json.dumps({"query": "secret-k", "expect": ["1"], "k": "not-an-int"}),
        json.dumps({"query": "secret-bool-k", "expect": ["1"], "k": True}),
        json.dumps({"query": "secret-mode", "expect": ["1"], "mode": "fuzzy"}),
        json.dumps({"query": "secret-expect", "expect": "1"}),
        json.dumps({"query": "secret-both", "expect": ["1"], "expect_none": True}),
        json.dumps({"query": "secret-paths", "expect": ["1"], "paths": ["nowhere"]}),
        json.dumps({"query": "secret-project", "expect": ["1"], "project": 7}),
        json.dumps(["secret-list"]),
        "{secret broken",
        json.dumps({"query": "good", "expect": ["1"], "k": 3}),
    ]) + "\n", encoding="utf-8")
    result = ren.install_candidates(cand, golden)
    assert result["ok"] and result["added"] == 1 and result["rejected_malformed"] == 9
    assert result["malformed_reasons"] == {"bad_k": 2, "bad_mode": 1, "bad_expect": 2, "bad_paths": 1,
                                           "bad_project": 1, "not_an_object": 1, "bad_json": 1}
    assert "secret" not in json.dumps(result)
    assert [json.loads(line)["query"] for line in golden.read_text().splitlines()] == ["good"]


def test_install_with_nothing_new_leaves_the_file_alone(world):
    tmp, golden = world
    _write_golden(golden, [{"query": "old", "expect": ["1"]}])
    cand = tmp / "cand.jsonl"
    _write_golden(cand, [{"query": "old", "expect": ["1"]}])
    before = golden.stat().st_mtime_ns
    assert ren.install_candidates(cand, golden)["added"] == 0
    assert golden.stat().st_mtime_ns == before and not (tmp / ren.BACKUP_NAME).exists()


def test_install_without_a_replica_refuses(world, monkeypatch):
    tmp, golden = world
    cand = tmp / "cand.jsonl"
    _write_golden(cand, [{"query": "q", "expect": ["1"]}])

    def boom(**_k):
        raise FileNotFoundError("no replica")

    monkeypatch.setattr(hub_snapshot, "open_snapshot", boom)
    assert ren.install_candidates(cand, golden)["ok"] is False
    assert not golden.exists()


def test_offline_guard_covers_every_listed_exit_and_restores_each():
    import importlib

    for obj_path, name, kind in ren.GUARDED_EXITS:
        obj = ren._resolve(obj_path)
        if not hasattr(obj, name):  # absent on this platform
            continue
        real = getattr(obj, name)
        with ren._offline() as guard:
            patched = getattr(obj, name)
            assert patched is not real, f"{obj_path}.{name} is not guarded"
            before = guard.refused[kind]
            with pytest.raises((OSError, RuntimeError)):
                patched()
            assert guard.refused[kind] == before + 1
        assert getattr(obj, name) is real, f"{obj_path}.{name} not restored"
    importlib.invalidate_caches()


def test_offline_guard_refuses_udp_dns_and_a_spawned_process():
    import os
    import socket

    with ren._offline() as guard:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(ConnectionRefusedError):
                udp.sendto(b"fake-dns", ("127.0.0.1", 9))
        finally:
            udp.close()
        with pytest.raises(PermissionError):
            os.posix_spawn("/usr/bin/true", ["true"], os.environ)
        assert guard.refused["network"] == 1 and guard.refused["process"] == 1
