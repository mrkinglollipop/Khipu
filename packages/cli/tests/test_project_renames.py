"""Repo renames, slice B (docs/plans/2026-10-19-repo-renames.md): the newest
name becomes canonical without alias chains, the same checkout reporting a new
name is detected from a local file with no database, the capture write records
it, and the local replica groups aliases the way the hub does."""
from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from khipu import hub_snapshot as hs
from khipu import identity
from tests.hermetic_pg import MIGRATIONS, hub, pg_cluster  # noqa: F401

A, B, C = "o/alpha", "o/beta", "o/gamma"


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


# ---- capture write -------------------------------------------------------------


@pytest.fixture
def writer(hub, monkeypatch):  # noqa: F811
    import psycopg

    _migrate_0039(hub)
    monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
    monkeypatch.setattr(psycopg, "connect", psycopg.Connection.connect)
    monkeypatch.setattr("khipu.db.connect", lambda *a, **k: hub.connect())
    monkeypatch.setattr("khipu.topic_graph.persist_capture_graph", lambda *a, **k: {})
    monkeypatch.setattr("khipu.config.path_setting", lambda *a, **k: None)
    monkeypatch.setattr("khipu.embed.embed_one", mock.Mock(side_effect=RuntimeError("no key")))
    return hub


def _payload(project, n, **extra):
    return {"ts": f"2026-10-19T12:0{n}:00Z", "summary": f"unique zebrafish note {n} {project}", "project": project,
            "harness": "claude_code", "session_id": f"claude_code:s{n}", "transcript_range": f"0:{n}", **extra}


def test_capture_with_a_rename_lands_under_the_new_name(writer):
    from khipu import capture

    rename = {"old": A, "new": B, "root_commit": "r1", "repo_root": "/x"}
    out = capture.write_pg(_payload(B, 1, root_commit="r1", rename=rename))
    assert out["episode_inserted"]
    # A later capture from a checkout still on the old remote lands under the new name too.
    capture.write_pg(_payload(A, 2, root_commit="r1"))
    conn = writer.connect()
    cur = conn.cursor()
    cur.execute("SELECT project FROM episodes ORDER BY id")
    assert [r[0] for r in cur.fetchall()] == [B, B]
    assert identity.project_group(cur, A) == [B, A]
    cur.execute("SELECT source, evidence->>'root_commit' FROM project_aliases WHERE alias = %s", (A,))
    assert cur.fetchone() == ("checkout", "r1")
    cur.execute("SELECT project, root_commit FROM project_roots ORDER BY project")
    assert cur.fetchall() == [(B, "r1")]


def test_capture_without_root_commit_writes_no_root_row(writer):
    from khipu import capture

    capture.write_pg(_payload(A, 1))
    cur = writer.connect().cursor()
    cur.execute("SELECT count(*) FROM project_roots")
    assert cur.fetchone()[0] == 0


# ---- hook-side detection (no database) ------------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)


def _repo(path: Path, remote: str | None) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", "root")
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


def _seen(repo: Path, state: Path) -> dict:
    ident = identity.resolve_repo_root(str(repo))
    return {"project": ident["project"], **identity.checkout_identity(ident["repo_root"], ident["project"], state)}


def test_remote_change_on_the_same_checkout_is_a_rename(tmp_path, no_database):
    repo = _repo(tmp_path / "work", "git@github.com:o/alpha.git")
    state = tmp_path / "state" / "checkouts.json"
    first = _seen(repo, state)
    assert first["project"] == A and first["rename"] is None and first["root_commit"]
    assert _seen(repo, state)["rename"] is None
    _git(repo, "remote", "set-url", "origin", "git@github.com:o/beta.git")
    second = _seen(repo, state)
    assert second["rename"] == {"old": A, "new": B, "root_commit": first["root_commit"],
                                "repo_root": str(repo.resolve())}
    assert _seen(repo, state)["rename"] is None  # reported once, then recorded
    assert json.loads(state.read_text())[str(repo.resolve())]["project"] == B
    no_database.assert_not_called()


def test_folder_rename_of_a_remoteless_checkout_is_a_rename(tmp_path, no_database):
    repo = _repo(tmp_path / "olddir", None)
    state = tmp_path / "checkouts.json"
    first = _seen(repo, state)
    assert first["project"] == "olddir"
    # git's own worktree/common-dir layout is keyed by real path, so a moved folder is a new key;
    # the same checkout is recognised by its root commit through the recorded project name only
    # when the key is unchanged, so rename the project by changing the folder's basename in place.
    moved = tmp_path / "newdir"
    repo.rename(moved)
    rec = json.loads(state.read_text())
    rec[str(moved.resolve())] = rec.pop(str(repo.resolve()))
    state.write_text(json.dumps(rec))
    second = _seen(moved, state)
    assert second["rename"]["old"] == "olddir" and second["rename"]["new"] == "newdir"
    no_database.assert_not_called()


def test_root_commit_is_computed_once_per_checkout(tmp_path):
    repo = _repo(tmp_path / "work", "git@github.com:o/alpha.git")
    state = tmp_path / "checkouts.json"
    calls: list[list[str]] = []
    real = identity._git

    def counting(cwd, args, timeout=identity.GIT_TIMEOUT_S):
        calls.append(args)
        return real(cwd, args, timeout)

    with mock.patch.object(identity, "_git", counting):
        for _ in range(3):
            identity.checkout_identity(str(repo), A, state)
    assert sum(1 for a in calls if a[:1] == ["rev-list"]) == 1


def test_enqueued_job_carries_root_commit_and_rename(tmp_path, monkeypatch, no_database):
    from khipu import session_capture as sc

    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path / "work", "git@github.com:o/alpha.git")
    captured: list[dict] = []
    monkeypatch.setattr(sc, "enqueue", lambda job: captured.append(job) or tmp_path / "job.json")
    monkeypatch.setattr(sc, "save_state", lambda *a, **k: None)

    def window(i):
        sc._enqueue_window(harness="claude_code", sid="s", track_sid="s", cwd=str(repo), event="stop",
                           path=tmp_path / "t.jsonl", st={}, msgs=[("user", f"hello {i}"), ("assistant", "hi")],
                           new_off=10, turns=1)

    window(1)
    _git(repo, "remote", "set-url", "origin", "git@github.com:o/beta.git")
    window(2)
    assert captured[0]["root_commit"] and "rename" not in captured[0]
    assert captured[1]["rename"]["old"] == A and captured[1]["rename"]["new"] == B
    assert captured[1]["root_commit"] == captured[0]["root_commit"]


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
