"""Repo renames, slice B (docs/plans/2026-10-19-repo-renames.md): the newest
name becomes canonical without alias chains, the hook only observes a checkout
(device and inode of its git common dir) with no database, the hub decides a
rename from successive observations, and the local replica groups aliases the
way the hub does."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from khipu import hub_snapshot as hs
from khipu import identity
from tests.hermetic_pg import MIGRATIONS, hub, pg_cluster  # noqa: F401

A, B, C = "o/alpha", "o/beta", "o/gamma"


@pytest.fixture(autouse=True)
def _own_capture_home(tmp_path, monkeypatch):
    """The checkout spool lives in the capture state dir: one per test."""
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture-home"))


def _migrate_0039(hub):  # noqa: F811
    """The shared template stops at 0029; this slice's migration goes on top."""
    conn = hub.connect()
    conn.execute((MIGRATIONS / "0039_project_rename_evidence.sql").read_text(encoding="utf-8"))
    conn.commit()
    return conn


@pytest.fixture
def cur(hub):  # noqa: F811
    conn = _migrate_0039(hub)
    yield conn.cursor()
    conn.close()


def _aliases(cur) -> dict[str, str]:
    cur.execute("SELECT alias, project FROM project_aliases")
    return dict(cur.fetchall())


# ---- record_rename -------------------------------------------------------------


def test_rename_between_unknown_names_adds_one_alias(cur):
    out = identity.record_rename(cur, A, B, source="checkout", evidence={"root_commit": "r1"})
    assert out["ok"] and out["changed"] and out["canonical"] == B
    assert _aliases(cur) == {A: B}
    cur.execute("SELECT source, evidence FROM project_aliases WHERE alias = %s", (A,))
    assert cur.fetchone() == ("checkout", {"root_commit": "r1"})
    assert identity.project_canonical_group(cur, A) == (B, [B, A])


def test_same_name_is_a_noop(cur):
    assert identity.record_rename(cur, A, A, source="checkout", evidence=None)["changed"] is False
    assert _aliases(cur) == {}


def test_rename_twice_leaves_no_chain(cur):
    identity.record_rename(cur, A, B, source="checkout", evidence=None)
    out = identity.record_rename(cur, B, C, source="checkout", evidence=None)
    assert out["ok"] and out["repointed"] == [A] and out["added"] == [B]
    assert _aliases(cur) == {A: C, B: C}
    assert identity.project_group(cur, A) == [C, A, B]


def test_old_canonical_with_aliases_repoints_the_group(cur):
    cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s), (%s, %s)", ("o/x", A, "o/y", A))
    identity.record_rename(cur, A, B, source="checkout", evidence=None)
    assert _aliases(cur) == {"o/x": B, "o/y": B, A: B}


def test_old_that_is_an_alias_repoints_its_whole_group(cur):
    cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s), (%s, %s)", (A, B, "o/x", B))
    identity.record_rename(cur, A, C, source="checkout", evidence=None)
    assert _aliases(cur) == {A: C, "o/x": C, B: C}


def test_rename_back_makes_the_old_name_canonical_again(cur):
    identity.record_rename(cur, A, B, source="checkout", evidence=None)
    out = identity.record_rename(cur, B, A, source="checkout", evidence=None)
    assert out["ok"] and out["removed"] == [A]
    assert _aliases(cur) == {B: A}
    assert identity.project_canonical_group(cur, B)[0] == A


def test_owner_alias_keeps_source_owner_and_failure_never_raises(cur):
    identity.add_project_alias(cur, A, B)
    cur.execute("SELECT source FROM project_aliases WHERE alias = %s", (A,))
    assert cur.fetchone()[0] == "owner"
    cur.execute("DROP TABLE project_aliases")
    out = identity.record_rename(cur, B, C, source="checkout", evidence=None)
    assert out["ok"] is False


def test_migration_creates_roots_and_pairs(cur):
    cur.execute("INSERT INTO project_roots (project, root_commit) VALUES ('p', 'r')")
    cur.execute("INSERT INTO project_pairs (a, b, root_commit, state) VALUES ('a', 'b', 'r', 'asked')")
    with pytest.raises(Exception):  # noqa: B017 — a < b
        cur.execute("INSERT INTO project_pairs (a, b, state) VALUES ('b', 'a', 'asked')")


# ---- the hub decides from checkout observations -----------------------------------


def _obs(key, project, kind, at, root="r1"):
    return {"checkout_key": key, "root_commit": root, "project": project, "project_kind": kind,
            "observed_at": float(at)}


def _observe(cur, *observations):
    return [identity.record_checkout_observation(cur, o) for o in observations]


def _pairs(cur):
    cur.execute("SELECT a, b, state FROM project_pairs ORDER BY a, b")
    return cur.fetchall()


def test_reused_path_with_a_fresh_clone_is_not_a_rename(cur):
    # The path is the same, the checkout (device and inode of its git common dir) is not.
    _observe(cur, _obs("1:10", A, "remote", 1), _obs("1:99", B, "remote", 2, root="r2"))
    assert _aliases(cur) == {} and _pairs(cur) == []
    # Even a key that survives, with a different root commit, is never a rename.
    out = identity.record_checkout_observation(cur, _obs("1:10", B, "remote", 3, root="r2"))
    assert out["action"] == "new_root" and _aliases(cur) == {}


def test_fork_switch_without_github_confirmation_asks_and_never_merges(cur):
    with mock.patch.object(identity, "github_confirms_rename", return_value=None) as gh:
        _observe(cur, _obs("1:10", B, "remote", 1), _obs("1:10", A, "remote", 2))
    gh.assert_called_once_with(B, A)
    assert _aliases(cur) == {}
    cur.execute("SELECT a, b, root_commit, state FROM project_pairs")
    assert cur.fetchall() == [(A, B, "r1", "asked")]
    # A third observation of the same state asks nothing new.
    with mock.patch.object(identity, "github_confirms_rename", side_effect=AssertionError("asked again")):
        _observe(cur, _obs("1:10", A, "remote", 3))
    assert len(_pairs(cur)) == 1


def test_github_confirming_a_remote_switch_records_the_alias(cur):
    with mock.patch.object(identity, "github_confirms_rename", return_value=True):
        out = _observe(cur, _obs("1:10", A, "remote", 1), _obs("1:10", B, "remote", 2))
    assert out[1]["action"] == "renamed" and _aliases(cur) == {A: B} and _pairs(cur) == []
    cur.execute("SELECT source, evidence->>'checkout_key' FROM project_aliases")
    assert cur.fetchone() == ("github", "1:10")


def test_github_confirms_rename_runs_gh_without_a_shell():
    def fake(answer, rc=0):
        return mock.Mock(returncode=rc, stdout=answer + "\n", stderr="")

    with (mock.patch("khipu.synth_cli.binary", return_value="/opt/gh"),
          mock.patch("subprocess.run", return_value=fake("O/Beta")) as run):
        assert identity.github_confirms_rename(A, B) is True
    argv = run.call_args.args[0]
    assert argv == ["/opt/gh", "api", f"repos/{A}", "--jq", ".full_name"]
    assert run.call_args.kwargs["timeout"] == 3 and not run.call_args.kwargs.get("shell")
    with mock.patch("subprocess.run", return_value=fake("o/other")):
        assert identity.github_confirms_rename(A, B) is False
    with mock.patch("subprocess.run", return_value=fake("", rc=1)):
        assert identity.github_confirms_rename(A, B) is None
    with mock.patch("subprocess.run", side_effect=FileNotFoundError):
        assert identity.github_confirms_rename(A, B) is None
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("gh", 3)):
        assert identity.github_confirms_rename(A, B) is None
    with (mock.patch("khipu.synth_cli.binary", return_value=None),
          mock.patch("subprocess.run", side_effect=AssertionError("ran without a gh"))):
        assert identity.github_confirms_rename(A, B) is None


def test_gh_is_found_by_the_resolver_under_a_bare_launchd_path(tmp_path, monkeypatch):
    from khipu import synth_cli

    fake = tmp_path / "brew" / "gh"
    fake.parent.mkdir()
    fake.write_text(f"#!/bin/sh\necho {B}\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("KHIPU_GH_CLI", raising=False)
    monkeypatch.setattr(synth_cli, "_chosen", lambda name: None)
    monkeypatch.setitem(synth_cli._CANDIDATES, "gh", (str(fake),))
    assert shutil.which("gh") is None
    assert identity.github_confirms_rename(A, B) is True


def test_folder_to_folder_is_certain(cur):
    _observe(cur, _obs("1:10", "olddir", "folder", 1), _obs("1:10", "newdir", "folder", 2))
    assert _aliases(cur) == {"olddir": "newdir"}
    cur.execute("SELECT source FROM project_aliases")
    assert cur.fetchone()[0] == "checkout"


def test_folder_to_remote_is_certain_and_the_remote_name_is_canonical(cur):
    _observe(cur, _obs("1:10", "olddir", "folder", 1), _obs("1:10", A, "remote", 2))
    assert _aliases(cur) == {"olddir": A}


def test_remote_to_folder_is_ignored_and_does_not_re_arm_a_later_transition(cur):
    _observe(cur, _obs("1:10", A, "remote", 1), _obs("1:10", "work", "folder", 2))
    assert _aliases(cur) == {}
    with mock.patch.object(identity, "github_confirms_rename", side_effect=AssertionError("asked")):
        _observe(cur, _obs("1:10", A, "remote", 3))  # origin put back: still the same project
    assert _aliases(cur) == {} and _pairs(cur) == []


def test_unknown_observations_only_move_last_seen(cur):
    _observe(cur, _obs("1:10", A, "remote", 1), _obs("1:10", "work", "unknown", 5))
    cur.execute("SELECT project, project_kind, extract(epoch FROM last_seen_at) FROM project_checkouts")
    assert cur.fetchall() == [(A, "remote", 5.0)]
    assert _aliases(cur) == {}
    # And an unknown first sighting stores nothing.
    assert identity.record_checkout_observation(cur, _obs("2:20", "x", "unknown", 1))["action"] == "ignored"
    cur.execute("SELECT count(*) FROM project_checkouts")
    assert cur.fetchone()[0] == 1


def test_a_delayed_older_observation_does_not_undo_a_newer_rename(cur):
    _observe(cur, _obs("1:10", "olddir", "folder", 1), _obs("1:10", "newdir", "folder", 3))
    out = identity.record_checkout_observation(cur, _obs("1:10", "olddir", "folder", 2))
    assert out["action"] == "stale" and _aliases(cur) == {"olddir": "newdir"}


def test_a_late_stale_transition_from_another_mac_keeps_the_group_whole(cur):
    # Mac 2 sees B -> C at its 100; Mac 1's A -> B arrives later stamped with its own
    # (behind) clock. Times from two Macs are never compared: nothing is refused and
    # nothing leaves the group, the late event can only relabel it.
    _observe(cur, _obs("2:20", B, "folder", 99), _obs("2:20", C, "folder", 100))
    assert _aliases(cur) == {B: C}
    out = _observe(cur, _obs("1:10", A, "folder", 49), _obs("1:10", B, "folder", 50))
    assert out[1]["action"] == "renamed"
    for name in (A, B, C):
        assert sorted(identity.project_group(cur, name)) == [A, B, C]
    assert set(_aliases(cur).values()) == {B}  # flat, no chain


def test_rename_of_two_separate_groups_merges_them_under_the_new_name(cur):
    cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s), (%s, %s)", (A, B, "o/x", C))
    identity.record_rename(cur, B, "o/x", source="checkout", evidence=None)
    assert _aliases(cur) == {A: "o/x", B: "o/x", C: "o/x"}


def test_recording_failure_is_rolled_back_so_the_next_observation_rederives_it(cur):
    _observe(cur, _obs("1:10", "olddir", "folder", 1))
    cur.execute("ALTER TABLE project_aliases RENAME TO project_aliases_gone")
    out = identity.record_checkout_observation(cur, _obs("1:10", "newdir", "folder", 2))
    assert out["ok"] is False
    cur.execute("ALTER TABLE project_aliases_gone RENAME TO project_aliases")
    cur.execute("SELECT project FROM project_checkouts")
    assert cur.fetchone()[0] == "olddir"  # not advanced
    assert identity.record_checkout_observation(cur, _obs("1:10", "newdir", "folder", 3))["action"] == "renamed"
    assert _aliases(cur) == {"olddir": "newdir"}


def test_concurrent_first_observations_of_one_checkout_both_count(cur, hub):  # noqa: F811
    import threading
    import time

    first, second, watch = hub.connect(), hub.connect(), hub.connect()
    watch.autocommit = True
    try:
        assert identity.record_checkout_observation(first.cursor(), _obs("1:10", "olddir", "folder", 1))["ok"]
        got: dict = {}

        def other():
            got["out"] = identity.record_checkout_observation(second.cursor(), _obs("1:10", "newdir", "folder", 2))
            second.commit()

        t = threading.Thread(target=other)
        t.start()
        deadline = time.time() + 10
        while time.time() < deadline:  # the second waits on the first, then evaluates
            row = watch.execute("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                "AND pid <> pg_backend_pid()").fetchone()
            if row[0]:
                break
            time.sleep(0.05)
        first.commit()
        t.join(10)
        assert got["out"]["action"] == "renamed"
        third = first.cursor()
        assert identity.record_checkout_observation(third, _obs("1:10", "lastdir", "folder", 3))["action"] == "renamed"
        first.commit()
        assert _aliases(cur) == {"olddir": "lastdir", "newdir": "lastdir"}
    finally:
        for conn in (first, second, watch):
            conn.close()


# ---- capture write -------------------------------------------------------------


def _writer(hub, monkeypatch, migrate=True):  # noqa: F811
    import psycopg

    if migrate:
        _migrate_0039(hub)
    monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
    monkeypatch.setattr(psycopg, "connect", psycopg.Connection.connect)
    monkeypatch.setattr("khipu.db.connect", lambda *a, **k: hub.connect())
    monkeypatch.setattr("khipu.topic_graph.persist_capture_graph", lambda *a, **k: {})
    monkeypatch.setattr("khipu.config.path_setting", lambda *a, **k: None)
    monkeypatch.setattr("khipu.embed.embed_one", mock.Mock(side_effect=RuntimeError("no key")))
    return hub


@pytest.fixture
def writer(hub, monkeypatch):  # noqa: F811
    return _writer(hub, monkeypatch)


def _payload(project, n, **extra):
    return {"ts": f"2026-10-19T12:0{n}:00Z", "summary": f"unique zebrafish note {n} {project}", "project": project,
            "harness": "claude_code", "session_id": f"claude_code:s{n}", "transcript_range": f"0:{n}", **extra}


def test_capture_with_a_rename_lands_under_the_new_name(writer):
    from khipu import capture

    capture.write_pg(_payload(A, 1, checkout=_obs("1:10", A, "folder", 1)))
    out = capture.write_pg(_payload(B, 2, checkout=_obs("1:10", B, "folder", 2)))
    assert out["episode_inserted"]
    # A later capture from a checkout still reporting the old name lands under the new name too.
    capture.write_pg(_payload(A, 3, checkout=_obs("2:20", A, "folder", 3)))
    conn = writer.connect()
    cur = conn.cursor()
    cur.execute("SELECT project FROM episodes ORDER BY id")
    assert [r[0] for r in cur.fetchall()] == [A, B, B]
    assert identity.project_group(cur, A) == [B, A]
    cur.execute("SELECT source, evidence->>'root_commit' FROM project_aliases WHERE alias = %s", (A,))
    assert cur.fetchone() == ("checkout", "r1")
    cur.execute("SELECT project, root_commit FROM project_roots ORDER BY project")
    assert cur.fetchall() == [(A, "r1"), (B, "r1")]


def test_capture_without_an_observation_writes_no_root_row(writer):
    from khipu import capture

    capture.write_pg(_payload(A, 1))
    cur = writer.connect().cursor()
    cur.execute("SELECT count(*) FROM project_roots")
    assert cur.fetchone()[0] == 0


def test_capture_still_succeeds_when_the_0039_tables_are_missing(hub, monkeypatch):  # noqa: F811
    from khipu import capture

    _writer(hub, monkeypatch, migrate=False)
    out = capture.write_pg(_payload(A, 1, checkout=_obs("1:10", A, "folder", 1)))
    assert out["episode_inserted"]
    out = capture.write_pg(_payload(B, 2, checkout=_obs("1:10", B, "folder", 2)))
    assert out["episode_inserted"]
    # Neither observation could be recorded: both wait in the spool, in order.
    assert [o["project"] for o in capture._spool_read("1:10")] == [A, B]
    cur = hub.connect().cursor()
    cur.execute("SELECT count(*) FROM episodes")
    assert cur.fetchone()[0] == 2


def test_the_spool_keeps_observation_order_when_drains_finish_out_of_order(tmp_path, monkeypatch):
    from khipu import capture

    from khipu import session_capture

    monkeypatch.setattr(session_capture, "state_dir", lambda: tmp_path)
    # B@2 finishes before A@1 during an outage; the flush must see A first.
    capture._spool_append(_obs("1:10", B, "folder", 2))
    capture._spool_append(_obs("1:10", A, "folder", 1))
    capture._spool_append(_obs("1:10", B, "folder", 3))
    assert [(o["project"], o["observed_at"]) for o in capture._spool_read("1:10")] == [(A, 1), (B, 2)]


def test_an_empty_extraction_job_still_records_its_observation(writer):
    from khipu import capture

    capture.record_checkout_observation(_obs("1:10", "olddir", "folder", 1))
    capture.record_checkout_observation(_obs("1:10", "newdir", "folder", 2))
    cur = writer.connect().cursor()
    assert _aliases(cur) == {"olddir": "newdir"}


def test_a_rolled_back_capture_is_retried_with_its_observation(writer, monkeypatch):
    from khipu import capture

    queued: list[dict] = []
    monkeypatch.setattr("khipu.outbox.enqueue", lambda payload, **k: queued.append(payload))
    monkeypatch.setattr(capture, "run_capture_v2", lambda *a, **k: 0)
    monkeypatch.setattr(capture, "dedup_before_insert", mock.Mock(side_effect=RuntimeError("hub went away")))
    obs = _obs("1:10", A, "folder", 1)
    capture.capture(_payload(A, 1, checkout=dict(obs)), mode="hub")
    assert len(queued) == 1 and queued[0]["checkout"] == obs


def test_failed_empty_job_observations_are_spooled_and_landed_in_order(writer, monkeypatch):
    from khipu import capture
    from khipu import session_capture as sc

    capture.record_checkout_observation(_obs("1:10", "olddir", "folder", 1))
    with monkeypatch.context() as down:
        down.setattr("khipu.db.connect", mock.Mock(side_effect=OSError("hub unreachable")))
        capture.record_checkout_observation(_obs("1:10", "newdir", "folder", 2))
        capture.record_checkout_observation(_obs("1:10", "newdir", "folder", 3))  # same state: kept once
        capture.record_checkout_observation(_obs("1:10", "lastdir", "folder", 4))
    spooled = capture._spool_read("1:10")
    assert [(o["project"], o["observed_at"]) for o in spooled] == [("newdir", 2.0), ("lastdir", 4.0)]
    # The next drain lands the spool oldest first, then the new job's observation.
    monkeypatch.setattr("khipu.config.capture_mode", lambda: "hub")
    monkeypatch.setattr("khipu.extract.extract_memory", lambda *a, **k: None)
    sc.enqueue({"harness": "claude_code", "session_id": "s", "cwd": "/x", "event": "stop",
                "ts": "2026-10-19T12:00:00Z", "turns": 1, "transcript": "hello",
                "checkout": _obs("1:10", "lastdir", "folder", 5)})
    assert sc.drain()["empty"] == 1
    assert capture._spool_read("1:10") == []
    cur = writer.connect().cursor()
    assert _aliases(cur) == {"olddir": "lastdir", "newdir": "lastdir"}


def test_drain_records_the_observation_before_archiving_an_empty_job(tmp_path, monkeypatch):
    from khipu import capture
    from khipu import session_capture as sc

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "home"))
    seen: list[dict] = []
    monkeypatch.setattr(capture, "record_checkout_observation", lambda o: seen.append(o))
    monkeypatch.setattr("khipu.config.capture_mode", lambda: "hub")
    monkeypatch.setattr("khipu.extract.extract_memory", lambda *a, **k: None)
    obs = _obs("1:10", A, "remote", 1)
    sc.enqueue({"harness": "claude_code", "session_id": "s", "cwd": "/x", "event": "stop", "ts": "2026-10-19T12:00:00Z",
                "turns": 1, "transcript": "hello", "checkout": obs})
    out = sc.drain()
    assert out["empty"] == 1 and seen == [obs]


# ---- hook-side observation (no database) -----------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


def _repo(path: Path, remote: str | None, commit: str = "root") -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", commit)
    if remote:
        _git(path, "remote", "add", "origin", remote)
    return path


@pytest.fixture
def no_database(monkeypatch):
    import psycopg

    boom = mock.Mock(side_effect=AssertionError("the hook path opened a database connection"))
    monkeypatch.setattr(psycopg, "connect", boom)
    monkeypatch.setattr("khipu.db.connect", boom)
    return boom


def _seen(repo: Path, cache: Path) -> dict | None:
    ident = identity.resolve_repo_root(str(repo))
    return identity.observe_checkout(str(repo), ident["repo_root"], ident["project"], cache)


def test_observation_reports_key_root_project_and_kind_without_a_database(tmp_path, no_database):
    cache = tmp_path / "cache"
    remote = _seen(_repo(tmp_path / "work", "git@github.com:o/alpha.git"), cache)
    assert remote["project"] == A and remote["project_kind"] == "remote" and remote["root_commit"]
    assert remote["checkout_key"].count(":") == 1
    folder = _seen(_repo(tmp_path / "plain", None), cache)
    assert folder["project"] == "plain" and folder["project_kind"] == "folder"
    no_database.assert_not_called()


def test_the_hook_keeps_no_last_project_state(tmp_path):
    repo = _repo(tmp_path / "work", "git@github.com:o/alpha.git")
    cache = tmp_path / "cache"
    first = _seen(repo, cache)
    _git(repo, "remote", "set-url", "origin", "git@github.com:o/beta.git")
    second = _seen(repo, cache)
    assert first["project"] == A and second["project"] == B
    assert first["checkout_key"] == second["checkout_key"] and "rename" not in second
    for f in cache.iterdir():
        assert set(json.loads(f.read_text())) == {"checkout_key", "root_commit"}


def test_any_git_error_is_unknown_not_folder(tmp_path):
    repo = _repo(tmp_path / "work", "git@github.com:o/alpha.git")
    real = subprocess.run

    def failing(argv, *a, **k):
        if "get-url" in argv:
            return mock.Mock(returncode=128, stdout="", stderr="fatal: not something we can parse")
        return real(argv, *a, **k)

    with mock.patch("subprocess.run", failing):
        assert _seen(repo, tmp_path / "cache")["project_kind"] == "unknown"
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("git", 3)):
        assert identity._origin_kind(str(repo)) == ("unknown", None)


def test_folder_move_of_a_remoteless_repo_keeps_the_key_and_is_detected(tmp_path, cur):
    cache = tmp_path / "cache"
    repo = _repo(tmp_path / "olddir", None)
    first = _seen(repo, cache)
    moved = tmp_path / "newdir"
    os.rename(repo, moved)
    second = _seen(moved, cache)
    assert first["checkout_key"] == second["checkout_key"] and first["root_commit"] == second["root_commit"]
    assert (first["project"], second["project"]) == ("olddir", "newdir")
    _observe(cur, first, second)
    assert _aliases(cur) == {"olddir": "newdir"}


def test_folder_then_remote_is_detected_end_to_end(tmp_path, cur):
    cache = tmp_path / "cache"
    repo = _repo(tmp_path / "work", None)
    first = _seen(repo, cache)
    _git(repo, "remote", "add", "origin", "git@github.com:o/alpha.git")
    second = _seen(repo, cache)
    assert (first["project_kind"], second["project_kind"]) == ("folder", "remote")
    _observe(cur, first, second)
    assert _aliases(cur) == {"work": A}


def test_a_fresh_clone_at_a_reused_path_is_a_different_checkout(tmp_path, cur):
    cache = tmp_path / "cache"
    path = tmp_path / "work"
    first = _seen(_repo(path, "git@github.com:o/alpha.git", "one"), cache)
    shutil.rmtree(path)
    second = _seen(_repo(path, "git@github.com:o/beta.git", "two"), cache)
    assert second["root_commit"] != first["root_commit"]  # the cache is per checkout key, never per path
    _observe(cur, first, second)
    assert _aliases(cur) == {} and _pairs(cur) == []


def test_empty_repo_carries_no_observation(tmp_path):
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q")
    assert identity.observe_checkout(str(repo), str(repo), "empty", tmp_path / "cache") is None


def test_root_commit_is_computed_once_per_checkout_key(tmp_path):
    repo = _repo(tmp_path / "work", "git@github.com:o/alpha.git")
    cache = tmp_path / "cache"
    calls: list[list[str]] = []
    real = identity._git

    def counting(cwd, args, timeout=identity.GIT_TIMEOUT_S):
        calls.append(args)
        return real(cwd, args, timeout)

    with mock.patch.object(identity, "_git", counting):
        for _ in range(3):
            identity.observe_checkout(str(repo), str(repo), A, cache)
        assert sum(1 for a in calls if a[:1] == ["rev-list"]) == 1
        other = _repo(tmp_path / "other", "git@github.com:o/alpha.git")  # a new key always recomputes
        identity.observe_checkout(str(other), str(other), A, cache)
        assert sum(1 for a in calls if a[:1] == ["rev-list"]) == 2


def test_concurrent_hooks_for_different_checkouts_both_persist(tmp_path):
    import threading

    cache = tmp_path / "cache"
    repos = [_repo(tmp_path / f"r{i}", None, f"c{i}") for i in range(6)]
    results: dict[int, dict | None] = {}
    barrier = threading.Barrier(len(repos))

    def hook(i):
        barrier.wait()
        results[i] = _seen(repos[i], cache)

    threads = [threading.Thread(target=hook, args=(i,)) for i in range(len(repos))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results[i] for i in range(len(repos)))
    files = sorted(f for f in cache.iterdir() if f.suffix == ".json")
    assert len(files) == len(repos)
    assert {json.loads(f.read_text())["root_commit"] for f in files} == {results[i]["root_commit"] for i in range(6)}
    assert not [f for f in cache.iterdir() if f.name.endswith(".tmp")]


def test_enqueued_job_carries_the_observation(tmp_path, monkeypatch, no_database):
    from khipu import session_capture as sc

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "work", "git@github.com:o/alpha.git")
    captured: list[dict] = []
    monkeypatch.setattr(sc, "enqueue", lambda job: captured.append(job) or tmp_path / "job.json")
    monkeypatch.setattr(sc, "save_state", lambda *a, **k: None)
    sc._enqueue_window(harness="claude_code", sid="s", track_sid="s", cwd=str(repo), event="stop",
                       path=tmp_path / "t.jsonl", st={}, msgs=[("user", "hello"), ("assistant", "hi")],
                       new_off=10, turns=1)
    obs = captured[0]["checkout"]
    assert obs["project"] == A and obs["project_kind"] == "remote" and obs["root_commit"]
    assert "rename" not in captured[0] and "root_commit" not in captured[0]


# ---- the local replica -----------------------------------------------------------


@pytest.fixture
def replica(tmp_path):
    snap = tmp_path / "hub_snapshot.sqlite"
    con = sqlite3.connect(str(snap))
    hs._create_schema(con)
    con.execute("INSERT INTO project_aliases (alias, project) VALUES (?, ?)", (A, B))
    for i, project in enumerate((A, B, C), start=1):
        con.execute("INSERT INTO episodes (id, ts, session_id, summary, topics, people, decisions, preferences, "
                    "project) VALUES (?, '2026-10-19T00:00:00Z', ?, ?, '[]', '[]', '[]', '[]', ?)",
                    (i, f"s{i}", f"zebrafish rollout for {project}", project))
        con.execute("INSERT INTO commitments (id, text, project, owner, kind, status) "
                    "VALUES (?, ?, ?, 'user', 'followup', 'open')", (i, f"ship zebrafish {project}", project))
        con.execute("INSERT INTO decisions (id, project, text, decided_at) VALUES (?, ?, ?, '2026-10-19T00:00:00Z')",
                    (i, project, f"use zebrafish for {project}"))
    con.commit()
    con.close()
    with (mock.patch.object(hs, "snapshot_path", return_value=snap),
          mock.patch.object(hs, "meta_path", return_value=tmp_path / "meta.json")):
        yield snap


@pytest.mark.parametrize("key", [A, B])
def test_replica_owed_and_decisions_group_by_either_key(replica, key):
    from khipu import offline_reads

    owed = offline_reads.owed(key, "open", 50)
    assert sorted(r["text"] for r in owed["results"]) == [f"ship zebrafish {A}", f"ship zebrafish {B}"]
    assert {r["project"] for r in owed["results"]} == {B}
    dec = offline_reads.decisions(key, None, "all", 50)
    assert sorted(r["text"] for r in dec["results"]) == [f"use zebrafish for {A}", f"use zebrafish for {B}"]
    assert {r["project"] for r in dec["results"]} == {B}


@pytest.mark.parametrize("key", [A, B])
def test_replica_search_groups_by_either_key(replica, key):
    rows = hs.search_snapshot("zebrafish", 20, kind="episode", project=key)
    assert sorted(r["id"] for r in rows) == ["1", "2"]


def test_replica_slice_fallback_reads_the_group(replica):
    from khipu import recall_rule

    con = sqlite3.connect(str(replica))
    con.execute("UPDATE episodes SET raw = json_object('project', project)")
    con.commit()
    con.close()
    text = recall_rule._stale_project_slice(A, None)
    assert "rollout for o/alpha" in text and "rollout for o/beta" in text and "o/gamma" not in text


def test_replica_without_the_table_has_no_groups(replica):
    con = sqlite3.connect(str(replica))
    con.execute("DROP TABLE project_aliases")
    assert hs.snapshot_project_group(con, A) == (A, [A])


def test_dump_copies_project_aliases_from_the_hub(cur, tmp_path):
    identity.record_rename(cur, A, B, source="checkout", evidence={"root_commit": "r1"})
    con = sqlite3.connect(str(tmp_path / "s.sqlite"))
    hs._create_schema(con)
    assert hs._insert_project_aliases(cur, con) == 1
    assert hs.snapshot_project_group(con, A) == (B, [B, A])
    assert con.execute("SELECT source, evidence FROM project_aliases").fetchone() == ("checkout", '{"root_commit": "r1"}')


def test_incremental_pull_carries_an_alias_add_and_an_alias_delete(replica):
    import time

    def sync(rows):
        parts = hs._write_pulled_tables(None, (["alias", "project", "source", "evidence"], rows),
                                        read_at=time.time())
        assert parts["project_aliases"] == {"ok": True, "project_aliases": len(rows)}

    con = sqlite3.connect(str(replica))
    sync([("o/x", B, "checkout", None), (A, B, "owner", '{"k": 1}')])
    assert hs.snapshot_project_group(con, "o/x") == (B, [B, A, "o/x"])
    sync([("o/x", B, "checkout", None)])  # the owner removed A's alias on the hub
    assert hs.snapshot_project_group(con, A) == (A, [A])
    assert hs.snapshot_project_group(con, "o/x") == (B, [B, "o/x"])
    con.close()
    sync([])
    con = sqlite3.connect(str(replica))
    assert con.execute("SELECT count(*) FROM project_aliases").fetchone()[0] == 0
    con.close()


def test_an_older_pull_never_replaces_newer_aliases(replica):
    import time

    now = time.time()
    newer = hs._write_pulled_tables(None, (["alias", "project"], [("o/x", B)]), read_at=now)
    assert newer["project_aliases"] == {"ok": True, "project_aliases": 1}
    older = hs._write_pulled_tables(None, (["alias", "project"], []), read_at=now - 60)
    assert older["project_aliases"]["skipped"]
    con = sqlite3.connect(str(replica))
    assert con.execute("SELECT alias, project FROM project_aliases").fetchall() == [("o/x", B)]
    con.close()


def test_a_kept_separate_pair_is_not_linked_by_the_checkout_path(cur):
    cur.execute("INSERT INTO project_pairs (a, b, state, decided_at) VALUES (%s, %s, 'kept_separate', now())", (A, B))
    cur.execute("INSERT INTO project_pairs (a, b, state, decided_at) VALUES ('newdir', 'olddir', 'kept_separate', now())")
    # GitHub may be asked before the lock; even its yes is refused under it.
    with mock.patch.object(identity, "github_confirms_rename", return_value=True):
        out = _observe(cur, _obs("1:10", B, "remote", 1), _obs("1:10", A, "remote", 2),
                       _obs("2:20", "olddir", "folder", 1), _obs("2:20", "newdir", "folder", 2))
    assert [o["action"] for o in out] == ["first", "kept_separate", "first", "kept_separate"]
    assert _aliases(cur) == {}
    cur.execute("SELECT state FROM project_pairs")
    assert {r[0] for r in cur.fetchall()} == {"kept_separate"}
    cur.execute("SELECT project FROM project_checkouts WHERE checkout_key = '1:10'")
    assert cur.fetchone()[0] == A


def test_an_observation_past_its_lookup_is_refused_when_the_owner_separates_first(hub, cur):  # noqa: F811
    """The GitHub lookup runs before the group lock. The owner's separation,
    on another connection, takes the lock and commits while it runs; the
    observation's write then decides under the lock, and is refused."""
    from khipu import project_group

    cur.execute("INSERT INTO project_aliases (alias, project, source) VALUES (%s, %s, 'github')", (A, B))
    _observe(cur, _obs("1:10", A, "remote", 1))
    cur.connection.commit()
    owner = hub.connect()

    def lookup(old, new):
        with owner.cursor() as ocur:
            assert project_group.apply(ocur, project_group.ACTION_SEPARATE, [A])["ok"]
        owner.commit()
        return True

    with mock.patch.object(identity, "github_confirms_rename", side_effect=lookup):
        [out] = _observe(cur, _obs("1:10", B, "remote", 2))
    cur.connection.commit()
    owner.close()
    assert out["ok"] and out["action"] == "kept_separate" and out["pair"] == [A, B], out
    assert _aliases(cur) == {}
    assert _pairs(cur) == [(A, B, "kept_separate")]
