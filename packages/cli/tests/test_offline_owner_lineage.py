"""The replica carries what decides an item's owner (third cross-family
review): origin, label, and who reassigned it, why and from whom, so an
offline read shows the same owner as the hub. No hub: fake cursors and an
in-memory replica."""
from __future__ import annotations

import copy
import sqlite3

from khipu import commitments as co
from khipu import hub_snapshot as hs
from khipu import offline_reads
from tests.replica_copy import certify


def _replica(monkeypatch, rows):
    """``rows`` written through the pull's own path into a fresh replica."""
    class Cursor:
        def execute(self, sql, params=()):
            self.cols = sql.removeprefix("SELECT ").split(" FROM commitments")[0].split(", ")

        def fetchall(self):
            return [tuple(row.get(col) for col in self.cols) for row in rows]

    hub_cols = set().union(*rows)
    monkeypatch.setattr(hs, "_pg_columns", lambda cur, table: hub_cols if table == "commitments" else set())
    cols, dumped = hs._hub_commitments(Cursor())
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    hs._create_schema(con)
    certify(con)
    hs._write_commitment_rows(con, cols, dumped)
    con.commit()
    monkeypatch.setattr(offline_reads, "_open", lambda: con)
    return con


def test_replica_keeps_an_explicit_owner_assignment(monkeypatch):
    row = dict(id=7, text="Should we enable desktop notifications?", project="acme/widget",
               owner="assistant", kind="question", status="open", opened_episode=None,
               opened_at="2026-10-16T12:00:00+00:00", origin="conversation", owner_label=None,
               owner_reassigned_at="2026-10-16T13:00:00+00:00", future_trigger=False)
    assert co.row_owner(row) == ("assistant", None)
    _replica(monkeypatch, [row])
    offline = offline_reads.owed_rows("acme/widget", "open")
    assert len(offline) == 1
    page = co.page_owed(offline, owner="assistant")
    assert len(page["results"]) == 1, "The real hub assignment became user-owned again in the replica"


_HUB_ROWS = [
    # A question moved to the assistant by hand, with who, why and from whom.
    dict(id=1, text="Should we enable desktop notifications?", owner="assistant", kind="question",
         origin="conversation", owner_label=None, owner_reassigned_at="2026-10-16T13:00:00+00:00",
         owner_reassigned_by="a conversation", owner_reassign_reason="the assistant decides this",
         owner_prior="user", owner_prior_label=None, linked_ids=[], latest_text=None),
    # A job's ask, the orchestrator's by lineage.
    dict(id=2, text="Matt to decide the rollout order", owner="agent", kind="followup", origin="job",
         owner_label="orchestrator", owner_reassigned_at=None, owner_reassigned_by=None,
         owner_reassign_reason=None, owner_prior=None, owner_prior_label=None, linked_ids=[3],
         latest_text=None),
    # The owner's own promise, linked to a similar item and restated.
    dict(id=3, text="I will review the deployment checklist", owner="user", kind="promise",
         origin="conversation", owner_label=None, owner_reassigned_at=None, owner_reassigned_by=None,
         owner_reassign_reason=None, owner_prior=None, owner_prior_label=None, linked_ids=[2],
         latest_text="I will review the deployment checklist before release"),
    # Moved to a reviewer by hand, from the assistant.
    dict(id=4, text="Rebase the release branch", owner="agent", kind="followup", origin="conversation",
         owner_label="reviewer", owner_reassigned_at="2026-10-16T14:00:00+00:00",
         owner_reassigned_by="you, in the app", owner_reassign_reason=None, owner_prior="assistant",
         owner_prior_label=None, linked_ids=[], latest_text=None),
]
_COMPARED = ("owner", "owner_label", "origin", "owner_reassigned_by", "owner_reassign_reason", "owner_prior",
             "owner_prior_label", "linked_ids", "latest_text")


def test_offline_owners_equal_online_owners(monkeypatch):
    base = dict(project="acme/widget", status="open", opened_episode=None, future_trigger=False,
                opened_at="2026-10-16T12:00:00+00:00")
    rows = [dict(base, **r) for r in _HUB_ROWS]
    online = {r["id"]: r for r in co.resolve_owed_rows(copy.deepcopy(rows))}
    _replica(monkeypatch, rows)
    offline = {r["id"]: r for r in co.resolve_owed_rows(offline_reads.owed_rows("acme/widget", "open"))}
    assert set(offline) == set(online)
    for cid, row in online.items():
        assert {k: offline[cid].get(k) for k in _COMPARED} == {k: row.get(k) for k in _COMPARED}, cid
    assert (offline[1]["owner"], offline[4]["owner_label"], offline[2]["owner_label"]) == (
        "assistant", "reviewer", "orchestrator")


def test_an_older_replica_gains_the_lineage_columns_on_its_next_pull(monkeypatch):
    con = sqlite3.connect(":memory:")
    hs._create_schema(con, version=6)
    certify(con)
    assert "owner_reassigned_at" not in hs._snapshot_table_columns(con, "commitments")
    cols = ["id", "text", "status", "owner", "owner_reassigned_at", "owner_prior"]
    hs._write_commitment_rows(con, cols, [(1, "Rebase the branch", "open", "assistant",
                                           "2026-10-16T13:00:00+00:00", "user")])
    got = con.execute("SELECT owner, owner_reassigned_at, owner_prior FROM commitments").fetchone()
    assert got == ("assistant", "2026-10-16T13:00:00+00:00", "user")
