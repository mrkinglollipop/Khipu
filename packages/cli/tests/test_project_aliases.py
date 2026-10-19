"""Aliased project keys read as ONE project in every reader that filters by
project (migration 0021, ``khipu project alias``). Real Postgres from
tests/hermetic_pg.py: rows live under the canonical key and under an alias,
and a query by either key returns both, labelled with the canonical."""
from __future__ import annotations

import json

import pytest

from khipu import identity
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

CANON = "owner/olivewood"
ALIAS = "owner/church-harness"
OTHER = "owner/unrelated"


@pytest.fixture
def seeded(hub, monkeypatch):  # noqa: F811
    import psycopg

    monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
    # conftest forbids psycopg.connect; this hub is the throwaway cluster.
    monkeypatch.setattr(psycopg, "connect", psycopg.Connection.connect)
    conn = hub.connect()
    cur = conn.cursor()
    cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s)", (ALIAS, CANON))
    for project in (CANON, ALIAS, OTHER):
        cur.execute(
            "INSERT INTO episodes (ts, session_id, summary, project) VALUES (now(), %s, %s, %s)",
            (f"claude:{project}", f"zebrafish rollout notes for {project}", project),
        )
        cur.execute(
            "INSERT INTO commitments (text, project, owner, content_hash) VALUES (%s, %s, 'user', %s)",
            (f"ship the zebrafish thing for {project}", project, f"h-{project}"),
        )
        cur.execute("INSERT INTO decisions (project, text) VALUES (%s, %s)",
                    (project, f"use zebrafish for {project}"))
    conn.commit()
    return cur


def test_project_group_is_symmetric_and_fails_open(seeded):
    cur = seeded
    assert identity.project_group(cur, CANON) == [CANON, ALIAS]
    assert identity.project_group(cur, ALIAS) == [CANON, ALIAS]
    assert identity.project_group(cur, OTHER) == [OTHER]
    assert identity.project_canonical_group(cur, ALIAS)[0] == CANON
    cur.execute("DROP TABLE project_aliases")
    assert identity.project_group(cur, ALIAS) == [ALIAS]


@pytest.mark.parametrize("key", [CANON, ALIAS])
def test_owed_rows_by_either_key(seeded, key):
    from khipu import commitments

    rows = commitments.fetch_owed_rows(seeded, project=key)
    assert sorted(r["text"] for r in rows) == [
        f"ship the zebrafish thing for {ALIAS}", f"ship the zebrafish thing for {CANON}"]
    assert {r["project"] for r in rows} == {CANON}


@pytest.mark.parametrize("key", [CANON, ALIAS])
def test_decisions_by_either_key(seeded, key):
    from khipu import decisions

    rows = decisions.list_decisions(seeded, project=key)
    assert sorted(r["text"] for r in rows) == [f"use zebrafish for {ALIAS}", f"use zebrafish for {CANON}"]
    assert {r["project"] for r in rows} == {CANON}


@pytest.mark.parametrize("key", [CANON, ALIAS])
def test_mcp_owed_and_decisions_by_either_key(seeded, key):
    from khipu import mcp_server as srv

    assert len(srv._tool_owed({"project": key})["results"]) == 2
    assert len(srv._tool_decisions({"project": key, "status": "all"})["results"]) == 2


@pytest.mark.parametrize("key", [CANON, ALIAS])
def test_session_start_slice_by_either_key(seeded, key):
    from khipu import activity

    out = activity.project_slice(project=key)
    assert len(out["commitments"]) == 2
    assert len(out["decisions"]) == 2
    assert len(out["episodes"]) == 2


@pytest.mark.parametrize("key", [CANON, ALIAS])
def test_search_filters_by_either_key(seeded, key):
    from khipu import embed
    from khipu.cli import _literal_candidates

    cur = seeded
    filters = embed._SearchFilters(project=key)
    rows = _literal_candidates(cur, "zebrafish", 20, kind="episode", filters=filters)
    assert len(rows) == 2

    # The semantic leg puts the same filter in its SQL.
    cur.execute(f"SELECT project FROM episodes e WHERE {filters.episode_sql(cur, 'e')} ORDER BY project",
                filters.params)
    assert sorted(r[0] for r in cur.fetchall()) == sorted([CANON, ALIAS])

    # The post-fusion pass keeps the same semantics.
    cur.execute("SELECT id::text FROM episodes")
    fused = [{"kind": "episode", "id": r[0]} for r in cur.fetchall()]
    kept = embed._apply_search_filters(cur, fused, project=key, projects=filters.projects)
    assert len(kept) == 2


def test_hybrid_search_literal_by_alias_returns_both(seeded):
    from khipu import embed

    out = embed.hybrid_search("zebrafish", mode="literal", project=ALIAS, include_libraries=False)
    assert {r["project"] for r in out["results"]} == {CANON, ALIAS}


def _alias_cmd(pg, monkeypatch, capsys, *argv):
    from khipu import cli, db, routing

    monkeypatch.setattr(db, "connect", lambda **_kw: pg.connect())
    if argv[0] in ("add", "remove"):
        # The owner's change: a yes typed on a terminal, logged in a temp data dir.
        monkeypatch.setattr(routing, "typed_yes", lambda _prompt: True)
    args = cli.build_parser().parse_args(["project", "alias", *argv])
    code = args.func(args)
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_cli_alias_add_list_remove_and_refusals(hub, monkeypatch, capsys, tmp_path):  # noqa: F811
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    code, out, _ = _alias_cmd(hub, monkeypatch, capsys, "add", ALIAS, CANON)
    assert code == 0 and json.loads(out)["changed"] is True
    code, out, _ = _alias_cmd(hub, monkeypatch, capsys, "add", ALIAS, CANON)
    assert code == 0 and json.loads(out)["changed"] is False  # the same pair is a no-op

    code, out, _ = _alias_cmd(hub, monkeypatch, capsys, "list", "--json")
    assert code == 0 and [(r["alias"], r["canonical"]) for r in json.loads(out)] == [(ALIAS, CANON)]

    for argv, needle in (
        (("add", CANON, CANON), "itself"),
        (("add", "owner/x", ALIAS), "itself an alias"),
        (("add", CANON, "owner/y"), "canonical of other aliases"),
        (("add", ALIAS, "owner/z"), "already an alias"),
        (("remove", "owner/nothing"), "not an alias"),
    ):
        code, _, err = _alias_cmd(hub, monkeypatch, capsys, *argv)
        assert code != 0 and needle in err, (argv, err)

    code, _, _ = _alias_cmd(hub, monkeypatch, capsys, "remove", ALIAS)
    assert code == 0
    code, out, _ = _alias_cmd(hub, monkeypatch, capsys, "list", "--json")
    assert json.loads(out) == []
