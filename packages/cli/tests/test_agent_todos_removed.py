"""Agent to-dos disappear from reads without changing the retained archive."""
import copy
import sqlite3

import pytest

from khipu import commitments as co, mcp_server as srv, cli
from tests.test_commitments import _CommitmentsCursor


def test_owner_lists_and_counts_exclude_agent_rows_without_changing_archive():
    cur = _CommitmentsCursor(migrated=True)
    ids = [cur._seed("Owner decision", owner="user", kind="question")]
    ids += [cur._seed(f"Archived {owner}", owner=owner) for owner in ("assistant", "agent", "CODEX:")]
    legacy = cur._seed("Legacy owner decision", owner=None)
    before = copy.deepcopy(cur.rows)
    assert {r["id"] for r in co.list_owed(cur)} == {ids[0], legacy}
    page = co.page_owed(copy.deepcopy(list(cur.rows.values())))
    assert {r["id"] for r in page["results"]} == {ids[0], legacy}
    assert page["total"] == 2 and page["counts"]["owner"] == {"user": 2}
    assert cur.rows == before


@pytest.mark.parametrize("owner", [None, "", "user", "Matt", "Peer 1", " assistant ", "CODEX:", "AGENT:"])
def test_sql_owner_reads_match_archive_owner_semantics(owner):
    con = sqlite3.connect(":memory:")
    con.create_function("btrim", 1, lambda s: s.strip())
    con.execute("CREATE TABLE commitments (owner TEXT)")
    con.execute("INSERT INTO commitments VALUES (?)", (owner,))
    kept = con.execute("SELECT count(*) FROM commitments WHERE " + co.owner_is_user_sql()).fetchone()[0]
    assert bool(kept) == (co.row_owner({"owner": owner})[0] == "user")
    con.close()


@pytest.mark.parametrize("owner", ["assistant", "agent"])
def test_agent_assignment_refused_before_any_write(owner, monkeypatch, capsys):
    def no_write(*a, **k):
        pytest.fail("Removed assignment reached storage")
    monkeypatch.setattr("khipu.owner_writes.run", no_write)
    with pytest.raises(ValueError, match="To-do lists for agents were removed"):
        srv._tool_owed_update({"id": 7, "action": "reassign", "owner": owner})
    with pytest.raises(ValueError, match="To-do lists for agents were removed"):
        srv._tool_owed_update({"action": "ask", "owner": owner})
    with pytest.raises(ValueError, match="To-do lists for agents were removed"):
        co.reassign(None, 7, owner)
    assert cli.main(["owed", "--reassign", "7", "--to", owner]) == 2
    assert co.AGENT_TODOS_REMOVED in capsys.readouterr().out


def test_owed_owner_filters_never_reintroduce_agent_lists():
    assert srv.owed_filters({})["owner"] == "user"
    assert srv.owed_filters({"owner": "all"})["owner"] == "user"
    for owner in ("assistant", "agent"):
        with pytest.raises(ValueError, match="To-do lists for agents were removed"):
            srv.owed_filters({"owner": owner})
