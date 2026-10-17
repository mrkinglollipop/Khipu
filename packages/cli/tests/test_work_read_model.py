"""The Work read model's speed and freshness (slice 3 of the Work sync fixes):
"Looks like" without comparing every pair in full, one owed read, the three
sources read at once, one projection per answer, caches that never go back
in time, a file read that never mixes two versions, offline and queue state on
every ledger read, project aliases, and summary ids that repeat.

Hermetic: no hub, no database; every input is synthetic.
"""
from __future__ import annotations

import argparse
import difflib
import io
import json
import os
import random
import threading
import time
from contextlib import redirect_stdout
from datetime import timedelta

import pytest

from khipu import ledger, safe_read, work
from tests.test_work import (NO_LEDGER, NO_OWED, NOW, ROUTING, VOCAB, cfg, entry, job, put, row,
                             summary, _t)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    (tmp_path / "data").mkdir(exist_ok=True)


# ---- "Looks like": the same answers, far fewer full comparisons -------------------

_WORDS = ("check the ledger fix review build release sign merge desktop app settings route account capture "
          "recall summary doctor nightly backup restore graph replica outbox drain hub snapshot install update "
          "notarize credentials token gateway prompt hook test suite oracle branch worktree commit verify").split()


def _sentence(rnd):
    return " ".join(rnd.choice(_WORDS) for _ in range(rnd.randint(6, 20))).capitalize()


def _synthetic(seed=3, needs=30, owed=600, projects=4):
    rnd = random.Random(seed)
    names = [f"org/p{i}" for i in range(projects)]
    titles = []
    data = {}
    for p in names:
        data[p] = {"name": p, "hidden": False, "vocab": VOCAB, "entered": {},
                   "rows": [row(f"r{i}", _sentence(rnd), seq=i) for i in range(5)]}
    for k in range(needs):
        p = names[k % projects]
        title = _sentence(rnd)
        titles.append((p, title))
        data[p]["rows"].append(row(f"n{k}", title, state="NEEDS OWNER", kind="needs-owner", seq=100 + k))
    items = []
    for i in range(owed):
        p, text = rnd.choice(names), _sentence(rnd)
        roll = rnd.random()
        if roll < 0.03:
            p, text = rnd.choice(titles)
        elif roll < 0.10:
            p, base = rnd.choice(titles)
            words = base.split()
            words[rnd.randrange(len(words))] = rnd.choice(_WORDS)
            text = " ".join(words)
        elif roll < 0.12:
            p, base = rnd.choice(titles)
            text = base + " " + rnd.choice(_WORDS)
        items.append({"id": 1000 + i, "text": text, "project": p, "owner": "user", "kind": "followup",
                      "opened_at": _t(rnd.randint(60, 86400 * 20))})
    return ({"projects": data, "offline": False, "as_of": None, "error": None}, {"items": items, "error": None})


def _reference_looks_like(ledger_data, owed):
    """Every unlinked commitment's "Looks like" row as the first reader found
    it: the first needs-you row of its project, in order, whose title is
    alike in full (difflib ratio >= 0.75)."""
    needs = [(p, r) for p, d in ledger_data["projects"].items() for r in d["rows"] if r["state"] == "NEEDS OWNER"]
    out = {}
    for c in owed["items"]:
        text = work._norm(c["text"])
        out[str(c["id"])] = next((r["row_id"] for p, r in needs if p == c["project"]
                                  and difflib.SequenceMatcher(None, work._norm(r["title"]), text).ratio() >= 0.75),
                                 None)
    return out


def test_looks_like_answers_are_unchanged():
    ledger_data, owed = _synthetic()
    waiting = work._waiting(ledger_data, owed, ROUTING, NOW)
    got = {w["id"]: (w["looks_like"] or {}).get("id") for w in waiting if w["source"] == "commitment"}
    want = _reference_looks_like(ledger_data, owed)
    linked = {w["also_owed"] for w in waiting if w["source"] == "ledger" and w["also_owed"]}
    assert set(got) | linked == set(want)
    assert {k: v for k, v in want.items() if k not in linked} == got
    assert sum(1 for v in got.values() if v) > 10


def test_looks_like_compares_few_pairs_in_full(monkeypatch):
    ledger_data, owed = _synthetic()
    calls = []
    real = difflib.SequenceMatcher.ratio
    monkeypatch.setattr(difflib.SequenceMatcher, "ratio", lambda self: calls.append(1) or real(self))
    work._waiting(ledger_data, owed, ROUTING, NOW)
    pairs = sum(1 for c in owed["items"] for p, d in ledger_data["projects"].items() if p == c["project"]
                for r in d["rows"] if r["state"] == "NEEDS OWNER")
    # The quadratic reader compared every pair in full (4,500 here).
    assert len(calls) < pairs // 10, (len(calls), pairs)


def test_lcs_bound_is_an_upper_bound_on_ratio():
    rnd = random.Random(11)
    for _ in range(400):
        a, b = _sentence(rnd).lower(), _sentence(rnd).lower()
        if rnd.random() < 0.3:
            b = a[: rnd.randrange(len(a) + 1)] + _sentence(rnd).lower()
        bound = work._ratio_bound(a, b)
        assert difflib.SequenceMatcher(None, a, b).ratio() <= bound + 1e-12
    assert work._ratio_bound("", "") == 0.0


# ---- one owed read; the three sources at once; one projection ----------------------


class _OwedCur:
    def __init__(self, rows):
        self.rows, self.queries = rows, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _OwedConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def close(self):
        pass


def test_read_owed_reads_the_commitments_once(monkeypatch):
    from khipu import commitments, hub_snapshot

    later = NOW + timedelta(hours=5)
    rows = [{"id": 1, "text": "Sign the agreement", "project": "atlas", "owner": "user", "due_after": None},
            {"id": 2, "text": "Snoozed one", "project": "atlas", "owner": "user", "due_after": later},
            {"id": 3, "text": "An agent's", "project": "atlas", "owner": "agent", "due_after": None}]
    cur = _OwedCur(rows)

    def fake_list(c, *, status, limit, hide_snoozed=False, **kw):
        c.queries += 1
        got = [dict(r) for r in c.rows]
        if hide_snoozed:
            got = [r for r in got if r["due_after"] is None or r["due_after"] <= NOW]
        return got

    monkeypatch.setattr(hub_snapshot, "try_hub_connect", lambda **kw: _OwedConn(cur))
    monkeypatch.setattr(commitments, "list_owed", fake_list)
    got = work.read_owed(NOW)
    assert cur.queries == 1
    assert [r["id"] for r in got["items"]] == [1]
    assert got["valid_until"] == later and got["error"] is None


def test_build_view_reads_its_three_sources_at_once(monkeypatch):
    def slow(value):
        def read(*a, **kw):
            time.sleep(0.4)
            return value
        return read

    monkeypatch.setattr(work, "read_ledger", slow(NO_LEDGER))
    monkeypatch.setattr(work, "read_owed", slow(NO_OWED))
    monkeypatch.setattr(work, "read_routing", slow(ROUTING))
    started = time.monotonic()
    view = work.build_view(NOW, cfg=cfg())
    took = time.monotonic() - started
    assert view["counts"]["waiting"] == 0
    assert took < 1.0, took  # one after another took 1.2 s


def _work_json(monkeypatch):
    from khipu import owner_direction

    monkeypatch.setattr(ledger, "require", lambda *a, **kw: None)
    monkeypatch.setattr(owner_direction, "run_due", lambda: None)
    monkeypatch.setattr(work, "read_ledger", lambda: NO_LEDGER)
    monkeypatch.setattr(work, "read_owed", lambda *a: NO_OWED)
    monkeypatch.setattr(work, "read_routing", lambda now: ROUTING)


def test_khipu_work_projects_its_answer_once(monkeypatch):
    from khipu import agent_view

    _work_json(monkeypatch)
    calls = []
    real = agent_view.project
    monkeypatch.setattr(agent_view, "project", lambda v: calls.append(1) or real(v))
    out = io.StringIO()
    with redirect_stdout(out):
        rc = work.cmd(argparse.Namespace(work_cmd=None, json=True, by_direction=False))
    assert rc == 0
    printed = json.loads(out.getvalue())
    assert printed["format"] == work.VIEW_FORMAT
    assert json.loads(work.view_file().read_text()) == printed
    assert len(calls) == 1


def test_a_forget_between_build_and_save_leaves_no_saved_view(monkeypatch):
    _work_json(monkeypatch)
    real = work.build_view

    def build_then_forget(*a, **kw):
        view = real(*a, **kw)
        work.invalidate_view()
        return view

    monkeypatch.setattr(work, "build_view", build_then_forget)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = work.cmd(argparse.Namespace(work_cmd=None, json=True, by_direction=False))
    assert rc == 0 and json.loads(out.getvalue())["format"] == work.VIEW_FORMAT
    assert not work.view_file().exists()


# ---- caches never go back in time (V4) -----------------------------------------------


def test_an_older_summary_read_never_replaces_a_newer_cache(monkeypatch, tmp_path):
    old = summary(written_at=_t(60))
    new = summary(written_at=_t(0))
    path = put(tmp_path, old)
    parsed, release = threading.Event(), threading.Event()
    real = work.parse_summary

    def paused(data, *a, **kw):
        out = real(data, *a, **kw)
        if out["written_at"] == old["written_at"] and threading.current_thread().name == "older":
            parsed.set()
            assert release.wait(5)
        return out

    monkeypatch.setattr(work, "parse_summary", paused)
    got = {}
    t = threading.Thread(target=lambda: got.update(r=work.read_source(entry(path), cfg(), NOW)), name="older")
    t.start()
    assert parsed.wait(5)
    path.write_text(json.dumps(new))
    assert work.read_source(entry(path), cfg(), NOW)["contents"]["written_at"] == new["written_at"]
    release.set()
    t.join(5)
    held = json.loads(work._last_file("atlas").read_text())["contents"]["written_at"]
    assert held == new["written_at"]
    assert got["r"]["contents"]["written_at"] == new["written_at"]


def test_an_older_view_never_replaces_a_newer_saved_view():
    a = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER, owed=NO_OWED, routing_info=ROUTING)
    b = work.build_view(NOW + timedelta(seconds=10), cfg=cfg(), ledger_data=NO_LEDGER, owed=NO_OWED,
                        routing_info=ROUTING)
    work.write_view(b)
    work.write_view(a)
    assert json.loads(work.view_file().read_text())["generated_at"] == b["generated_at"]
    # Forget still fences both, and a view built after it publishes.
    work.invalidate_view()
    c = work.build_view(NOW + timedelta(seconds=20), cfg=cfg(), ledger_data=NO_LEDGER, owed=NO_OWED,
                        routing_info=ROUTING)
    work.write_view(b)
    assert json.loads(work.view_file().read_text())["generated_at"] == b["generated_at"]
    work.write_view(c)
    assert json.loads(work.view_file().read_text())["generated_at"] == c["generated_at"]


# ---- a file read never mixes two versions (V5) ---------------------------------------


def test_a_rewrite_during_the_read_is_read_again(monkeypatch, tmp_path):
    path = tmp_path / "mixed.json"
    old, new = b'{"a":1,"b":1}', b'{"a":2,"b":2}'
    path.write_bytes(old)
    real = safe_read.Opened.read
    reads = []

    def mixed(opened, most=0):
        reads.append(1)
        if len(reads) > 1:
            return real(opened, most)
        first = os.read(opened.fd, 7)
        time.sleep(0.01)
        path.write_bytes(new)
        return first + os.read(opened.fd, 100)

    monkeypatch.setattr(safe_read.Opened, "read", mixed)
    got, _ = safe_read.read_file(str(path))
    assert json.loads(got) == {"a": 2, "b": 2}
    assert len(reads) == 2


def test_a_file_that_never_holds_still_is_not_read(monkeypatch, tmp_path):
    path = tmp_path / "busy.json"
    path.write_bytes(b'{"a":1}')

    def always(opened, most=0):
        data = os.read(opened.fd, 100)
        time.sleep(0.01)
        path.write_bytes(b'{"a":1}')
        return data

    monkeypatch.setattr(safe_read.Opened, "read", always)
    with pytest.raises(safe_read.Refused):
        safe_read.read_file(str(path))


# ---- offline state and queue health on every read (V6, V7) ---------------------------


def _offline_load(monkeypatch):
    from khipu import owner_direction

    monkeypatch.setattr(ledger, "require", lambda *a, **kw: None)
    monkeypatch.setattr(owner_direction, "run_due", lambda: None)
    monkeypatch.setattr(ledger, "load", lambda p: {"vocab": ledger.default_vocab(), "rows": [], "offline": True,
                                                   "as_of": "2020-01-01T00:00:00Z", "pending": 0})


@pytest.mark.parametrize("verb", ["list", "loose-ends", "needs-owner", "render", "check"])
def test_every_json_read_says_when_it_is_offline(monkeypatch, verb):
    from khipu import cli

    _offline_load(monkeypatch)
    args = cli.build_parser().parse_args(["ledger", verb, "--project", "atlas", "--json"])
    out = io.StringIO()
    with redirect_stdout(out):
        ledger.cmd(args)
    got = json.loads(out.getvalue())
    assert got["offline"] is True and got["as_of"] == "2020-01-01T00:00:00Z", got
    assert "pending" in got and "queue" in got


def test_ledger_get_says_offline_or_live(monkeypatch):
    from khipu import cli, owner_direction

    monkeypatch.setattr(ledger, "require", lambda *a, **kw: None)
    monkeypatch.setattr(owner_direction, "run_due", lambda: None)
    one = row("one", "Synthetic")
    monkeypatch.setattr(ledger, "get", lambda p, i: {"row": one, "history": [], "offline": False})
    args = cli.build_parser().parse_args(["ledger", "get", "one", "--project", "atlas", "--json"])
    out = io.StringIO()
    with redirect_stdout(out):
        ledger.cmd(args)
    got = json.loads(out.getvalue())
    assert got["offline"] is False and "as_of" in got


class _Conn:
    def close(self):
        pass

    def rollback(self):
        pass


def _queued(project="atlas", row_id="q1"):
    return {"verb": "add", "project": project, "row_id": row_id, "at": _t(30),
            "fields": {"title": "Queued while busy", "kind": "slice"}}


def test_an_online_read_shows_writes_still_waiting(monkeypatch):
    monkeypatch.setattr(ledger, "_connect", lambda: _Conn())
    monkeypatch.setattr(ledger, "drain", lambda conn: {"jobs": 1, "landed": 0, "refused": 0,
                                                       "stopped_early": False, "busy": True})
    monkeypatch.setattr(ledger, "replica_catch_up", lambda conn: {})
    monkeypatch.setattr(ledger, "_hub_view", lambda conn, project: {
        "rows": [dict(row("one", "Landed"), created_at=NOW, version=1)], "vocab": ledger.default_vocab(),
        "offline": False, "as_of": None})
    monkeypatch.setattr(ledger, "queued_ops", lambda **kw: [_queued()])
    got = ledger.load("atlas")
    assert got["offline"] is False
    assert [(r["row_id"], bool(r.get("queued"))) for r in got["rows"]] == [("one", False), ("q1", True)]
    assert got["pending"] == 1 and got["queue"]["busy"] is True


def test_an_online_read_with_an_empty_line_is_unchanged(monkeypatch):
    monkeypatch.setattr(ledger, "_connect", lambda: _Conn())
    monkeypatch.setattr(ledger, "drain", lambda conn: {"jobs": 0, "landed": 0, "refused": 0,
                                                       "stopped_early": False})
    monkeypatch.setattr(ledger, "replica_catch_up", lambda conn: {})
    monkeypatch.setattr(ledger, "_hub_view", lambda conn, project: {
        "rows": [], "vocab": ledger.default_vocab(), "offline": False, "as_of": None})
    monkeypatch.setattr(ledger, "queued_ops", lambda **kw: [])
    got = ledger.load("atlas")
    assert got["rows"] == [] and got["pending"] == 0 and got["queue"]["busy"] is False


def test_work_says_when_the_line_cannot_be_read(monkeypatch):
    monkeypatch.setattr(ledger, "outbox_status", lambda: {"pending": None, "refused": None,
                                                          "unreadable": "synthetic unreadable"})
    got = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER, owed=NO_OWED, routing_info=ROUTING)
    assert "outbox" in got["problems"]


def test_work_says_when_writes_were_refused(monkeypatch):
    monkeypatch.setattr(ledger, "outbox_status", lambda: {"pending": 0, "refused": 2, "oldest_age_s": None})
    got = work.build_view(NOW, cfg=cfg(), ledger_data=NO_LEDGER, owed=NO_OWED, routing_info=ROUTING)
    assert got["counts"]["ledger_refused"] == 2 and "outbox" in got["problems"]


def test_work_shows_writes_still_waiting_on_an_online_read(monkeypatch):
    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            self.got = [("atlas", "Atlas", False, NOW)] if "ledger_projects" in a[0] else []

        def fetchall(self):
            return self.got

    class Conn(_Conn):
        def cursor(self):
            return Cur()

    monkeypatch.setattr(ledger, "_connect", lambda: Conn())
    monkeypatch.setattr(ledger, "drain", lambda conn: {"busy": True})
    monkeypatch.setattr(ledger, "_hub_view", lambda conn, project: {
        "rows": [], "vocab": ledger.default_vocab(), "offline": False, "as_of": None})
    monkeypatch.setattr(ledger, "queued_ops", lambda **kw: [_queued()])
    got = work.read_ledger()
    assert [r["row_id"] for r in got["projects"]["atlas"]["rows"]] == ["q1"]


# ---- one project name for the ledger and capture (V8) --------------------------------


def test_a_ledger_project_from_the_folder_uses_the_alias(monkeypatch):
    from khipu import identity

    monkeypatch.setattr(os, "getcwd", lambda: "/synthetic/repo")

    def git(cwd, args):
        return {"rev-parse --show-toplevel": "/synthetic/repo", "rev-parse --git-common-dir": ".git",
                "remote get-url origin": "https://github.com/oldorg/repo.git"}[" ".join(args)]

    monkeypatch.setattr(identity, "_git", git)

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a):
            pass

        def fetchall(self):
            return [("oldorg/repo", "neworg/repo")]

    class Conn(_Conn):
        def cursor(self):
            return Cur()

    monkeypatch.setattr(ledger, "_connect", lambda: Conn())
    assert ledger._project_for(argparse.Namespace(project=None), canonical=True) == "neworg/repo"
    # Before the owner check (no canonical) it never reaches the hub.
    assert ledger._project_for(argparse.Namespace(project=None)) == "oldorg/repo"
    # A name given outright is used as given, and an away hub keeps the table read before.
    assert ledger._project_for(argparse.Namespace(project="oldorg/repo"), canonical=True) == "oldorg/repo"

    def away():
        raise ledger.HubAway("synthetic")

    monkeypatch.setattr(ledger, "_connect", away)
    assert ledger._project_for(argparse.Namespace(project=None), canonical=True) == "neworg/repo"


# ---- summary ids that repeat (V9) ----------------------------------------------------


def test_a_repeated_job_id_keeps_the_newest_once():
    doc = summary(jobs=[job("dup", updated_at=_t(10), summary="Newer words"),
                        job("dup", updated_at=_t(300), state="queued", summary="Older words"),
                        job("other")])
    got = work.parse_summary(json.dumps(doc).encode(), "atlas", cfg(), NOW)
    assert [(j["id"], j["summary"]) for j in got["jobs"]] == [("dup", "Newer words"),
                                                             ("other", "Fixing the two review findings")]
    assert got["dropped"] == 1 and got["duplicates"] == 1


def test_a_repeated_id_with_the_same_time_keeps_the_later_one():
    doc = summary(prs=[{"id": "41", "project": "atlas", "title": "First", "stage": "built", "stage_at": _t(60)},
                       {"id": "41", "project": "atlas", "title": "Second", "stage": "fixing", "stage_at": _t(60)}])
    got = work.parse_summary(json.dumps(doc).encode(), "atlas", cfg(), NOW)
    assert [p["title"] for p in got["prs"]] == ["Second"]


def test_check_names_repeated_ids(tmp_path):
    path = put(tmp_path, summary(jobs=[job("dup"), job("dup")]))
    ok, problems, _parsed = work.check_file(str(path))
    assert not ok and any("same id" in p for p in problems)
    assert not any("couldn't be read" in p for p in problems)


# ---- round 2 of the review of 5415342 -----------------------------------------------


def _built(mark, at):
    view = work.build_view(at, cfg=cfg(), ledger_data=NO_LEDGER, owed=NO_OWED, routing_info=ROUTING)
    view["review_marker"] = mark
    return view


def test_a_view_read_earlier_in_the_same_second_never_replaces_a_later_one():
    old = _built("old", NOW + timedelta(microseconds=100000))
    new = _built("new", NOW + timedelta(microseconds=900000))
    work.write_view(new)
    work.write_view(old)
    assert json.loads(work.view_file().read_text())["review_marker"] == "new"


def test_a_view_read_after_a_clock_rollback_still_publishes():
    old = _built("old", NOW)
    new = _built("new", NOW - timedelta(minutes=10))
    work.write_view(old)
    work.write_view(new)
    assert json.loads(work.view_file().read_text())["review_marker"] == "new"


def test_forget_keeps_the_refresh_order():
    a = _built("a", NOW)
    work.invalidate_view()
    b = _built("b", NOW)
    work.write_view(b)
    work.write_view(a)  # fenced by Forget, and older
    assert json.loads(work.view_file().read_text())["review_marker"] == "b"
    c = _built("c", NOW)
    work.write_view(c)
    assert json.loads(work.view_file().read_text())["review_marker"] == "c"


class _AliasCur:
    def __init__(self, table):
        self.table = table

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, *a):
        self.sql = sql

    def fetchall(self):
        return list(self.table.items())

    def fetchone(self):
        return None


def _folder(monkeypatch, project="oldorg/repo"):
    from khipu import identity

    monkeypatch.setattr(identity, "resolve_repo_root", lambda _cwd, **kw: {"project": project})


def test_a_renamed_repo_keeps_one_project_key_offline(monkeypatch):
    _folder(monkeypatch)
    calls = []

    class Conn(_Conn):
        def cursor(self):
            return _AliasCur({"oldorg/repo": "neworg/repo"})

    def connect():
        calls.append(1)
        return Conn()

    monkeypatch.setattr(ledger, "_connect", connect)
    args = argparse.Namespace(project=None)
    assert ledger._project_for(args, canonical=True) == "neworg/repo"
    # The table is kept: the next command does not reach the hub again.
    assert ledger._project_for(args, canonical=True) == "neworg/repo"
    assert calls == [1]

    def away():
        raise ledger.HubAway("synthetic")

    monkeypatch.setattr(ledger, "_connect", away)
    monkeypatch.setattr(ledger, "_ALIAS_FRESH_S", 0)
    assert ledger._project_for(args, canonical=True) == "neworg/repo"


def test_with_no_alias_known_the_folder_name_is_used_both_ways(monkeypatch):
    _folder(monkeypatch)

    def away():
        raise ledger.HubAway("synthetic")

    monkeypatch.setattr(ledger, "_connect", away)
    args = argparse.Namespace(project=None)
    assert ledger._project_for(args, canonical=True) == "oldorg/repo"

    class Conn(_Conn):
        def cursor(self):
            return _AliasCur({})

    monkeypatch.setattr(ledger, "_connect", lambda: Conn())
    assert ledger._project_for(args, canonical=True) == "oldorg/repo"


def _online_get(monkeypatch, *, hub_row, queued, drained=None):
    import copy

    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, *a):
            self.sql = sql

        def fetchone(self):
            return ("exists",) if hub_row is not None and "ledger_rows" in self.sql else None

        def fetchall(self):
            return []

    class Conn(_Conn):
        def cursor(self):
            return Cur()

    monkeypatch.setattr(ledger, "_connect", lambda: Conn())
    monkeypatch.setattr(ledger, "drain", lambda conn: drained or {"busy": True})
    monkeypatch.setattr(ledger, "replica_catch_up", lambda conn: None)
    monkeypatch.setattr(ledger, "queued_ops", lambda **kw: list(queued))
    monkeypatch.setattr(ledger, "_marker", lambda cur, p, r: None)
    monkeypatch.setattr(ledger, "_hub_view", lambda conn, project: {
        "vocab": ledger.default_vocab(), "rows": [copy.deepcopy(hub_row)] if hub_row else [], "offline": False,
        "as_of": None})
    monkeypatch.setattr(ledger, "_row_dict", lambda got: copy.deepcopy(hub_row))


def test_an_online_get_shows_a_write_still_waiting_as_the_list_does(monkeypatch):
    base = dict(row("one", "Landed", seq=1), created_at=NOW, version=1, removed_at=None)
    pending = {"verb": "set", "project": "atlas", "row_id": "one", "at": work._z(NOW),
               "fields": {"title": "Unsaved locally"}}
    _online_get(monkeypatch, hub_row=base, queued=[pending])
    listed = ledger.load("atlas")
    detail = ledger.get("atlas", "one")
    assert listed["rows"][0]["title"] == detail["row"]["title"] == "Unsaved locally"
    assert detail["row"]["queued"] is True
    assert detail["pending"] == 1 and detail["queue"]["busy"] is True


def test_an_online_get_finds_a_queued_addition(monkeypatch):
    _online_get(monkeypatch, hub_row=None, queued=[_queued()])
    detail = ledger.get("atlas", "q1")
    assert detail["row"]["title"] == "Queued while busy" and detail["row"]["queued"] is True
    assert detail["offline"] is False and detail["pending"] == 1


def test_ledger_get_json_says_how_the_line_stands(monkeypatch):
    from khipu import cli, owner_direction

    monkeypatch.setattr(ledger, "require", lambda *a, **kw: None)
    monkeypatch.setattr(owner_direction, "run_due", lambda: None)
    one = row("one", "Synthetic")
    monkeypatch.setattr(ledger, "get", lambda p, i: {"row": one, "history": [], "offline": False, "pending": 1,
                                                     "queue": {"pending": 1, "busy": True}})
    args = cli.build_parser().parse_args(["ledger", "get", "one", "--project", "atlas", "--json"])
    out = io.StringIO()
    with redirect_stdout(out):
        ledger.cmd(args)
    got = json.loads(out.getvalue())
    assert got["pending"] == 1 and got["queue"]["busy"] is True


def test_an_offline_read_with_an_unreadable_waiting_write_is_not_a_healthy_zero(monkeypatch):
    def away():
        raise ledger.HubAway("synthetic")

    monkeypatch.setattr(ledger, "_connect", away)
    monkeypatch.setattr(ledger, "_offline_view", lambda p: None)
    ledger.outbox_dir().mkdir(parents=True)
    (ledger.outbox_dir() / "broken.json").write_text("{")
    got = ledger.load("atlas")
    assert got["queue"]["unreadable"]
    assert got["queue"]["pending"] is None


def test_earlier_refusals_stay_in_the_next_reads_health(monkeypatch):
    monkeypatch.setattr(ledger, "_connect", lambda: _Conn())
    monkeypatch.setattr(ledger, "replica_catch_up", lambda c: None)
    monkeypatch.setattr(ledger, "_hub_view", lambda c, p: {"rows": [], "vocab": ledger.default_vocab(),
                                                          "offline": False})
    ledger._refused_dir().mkdir(parents=True)
    (ledger._refused_dir() / "refused.json").write_text("{}")
    got = ledger.load("atlas")
    assert got["queue"]["refused"] == 1 and got["queue"]["unreadable"] is None
