"""Optional alias enrichment must recover without losing the caller's writes."""
from __future__ import annotations

from contextlib import ExitStack, nullcontext
from unittest import mock

import pytest
from psycopg.errors import InFailedSqlTransaction, InsufficientPrivilege, QueryCanceled

from khipu import capture, embed, identity, project_group


ERRORS = (QueryCanceled, InsufficientPrivilege, InFailedSqlTransaction)
INSERT = "INSERT INTO alias_fallback_writes (label) VALUES (%s)"


class TransactionCursor:
    """Track abort state and savepoint rollback, including earlier writes."""

    def __init__(self, error_type=QueryCanceled):
        self.error_type = error_type
        self.aborted = False
        self.writes = []
        self.savepoints = []
        self.calls = []

    def execute(self, sql, params=()):
        self.calls.append(sql)
        if sql.startswith("ROLLBACK TO SAVEPOINT "):
            name = sql.split()[-1]
            index = max(i for i, (saved, _) in enumerate(self.savepoints) if saved == name)
            self.writes = self.writes[:self.savepoints[index][1]]
            self.savepoints = self.savepoints[:index + 1]
            self.aborted = False
            return
        if self.aborted:
            raise InFailedSqlTransaction("synthetic aborted transaction")
        if sql.startswith("SAVEPOINT "):
            self.savepoints.append((sql.split()[-1], len(self.writes)))
        elif sql.startswith("RELEASE SAVEPOINT "):
            name = sql.split()[-1]
            index = max(i for i, (saved, _) in enumerate(self.savepoints) if saved == name)
            self.savepoints = self.savepoints[:index]
        elif sql == INSERT:
            self.writes.append(params[0])
        elif "project_aliases" in sql:
            self.fail_lookup()
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fail_lookup(self, *args):
        # A helper may fail without establishing/recovering its own savepoint.
        # Its partial work must not become part of the surrounding capture.
        self.execute(INSERT, ("lookup partial write",))
        self.aborted = True
        raise self.error_type("synthetic alias failure")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TransactionConn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0
        self.committed = []

    def cursor(self):
        return self.cur

    def commit(self):
        assert not self.cur.aborted, "cannot commit an aborted transaction"
        self.committed = list(self.cur.writes)
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def write_capture(conn):
    """Exercise write_pg itself; isolate unrelated enrichment/provider steps."""
    def upsert(cur, payload, **kwargs):
        cur.execute(INSERT, (payload["project"],))
        return True

    with ExitStack() as patches:
        for target, value in (
            ("khipu.db.connect", conn),
            ("khipu.memory_lock.shared", nullcontext()),
            ("khipu.tombstones.hub_source_forgotten", False),
            ("khipu.capture._inherit_project", None),
            ("khipu.capture._observe_checkout", None),
            ("khipu.capture._note_project_root", None),
            ("khipu.capture.dedup_before_insert", {"action": "none"}),
            ("khipu.db.has_columns", False),
            ("khipu.capture._stamp_origin", None),
            ("khipu.topic_graph.persist_capture_graph", {}),
            ("khipu.config.path_setting", None),
        ):
            patches.enter_context(mock.patch(target, return_value=value))
        patches.enter_context(mock.patch("khipu.mirror._upsert_episode", side_effect=upsert))
        return capture.write_pg({"ts": "2026-10-22T12:00:00Z", "summary": "fake capture",
                                 "project": "acme/original", "session_id": "gw:fake"})


@pytest.mark.parametrize("error_type", (*ERRORS, RuntimeError))
@pytest.mark.parametrize("helper_recovers", (False, True), ids=("caller-savepoint", "identity-savepoint"))
def test_capture_retains_project_and_commits_after_alias_failure(error_type, helper_recovers):
    cur = TransactionCursor(error_type)
    conn = TransactionConn(cur)
    cur.execute(INSERT, ("before lookup",))
    helper = nullcontext() if helper_recovers else mock.patch.object(
        identity, "_resolve_project_alias", side_effect=cur.fail_lookup)
    with helper, mock.patch.object(capture, "_log") as log:
        result = write_capture(conn)
    assert result["episode_inserted"] is True
    assert conn.commits == 1
    assert conn.committed == ["before lookup", "acme/original"]
    assert not cur.aborted
    assert all(name != "capture_project_alias" for name, _ in cur.savepoints)
    assert any(error_type.__name__ in call.args[0] for call in log.call_args_list)


@pytest.mark.parametrize("error_type", (*ERRORS, RuntimeError))
@pytest.mark.parametrize("helper_recovers", (False, True), ids=("caller-savepoint", "identity-savepoint"))
def test_search_keeps_original_filter_and_transaction_after_alias_failure(error_type, helper_recovers):
    cur = TransactionCursor(error_type)
    cur.execute(INSERT, ("before lookup",))
    filters = embed._SearchFilters(project="acme/original")
    filters.projects = ["acme/stale"]  # a retry must discard an earlier expansion
    helper = nullcontext() if helper_recovers else mock.patch.object(
        identity, "project_group", side_effect=cur.fail_lookup)
    with helper, mock.patch.object(embed, "_log") as log:
        filters.expand_project(cur)
    cur.execute(INSERT, ("after lookup",))
    assert filters.projects == ["acme/original"]
    assert filters.params["kf_project"] == "%acme/original%"
    assert cur.writes == ["before lookup", "after lookup"]
    assert not cur.aborted
    assert cur.savepoints == []
    assert any(error_type.__name__ in call.args[0] for call in log.call_args_list)


def test_successful_alias_lookup_still_enriches_capture_and_search():
    cur = TransactionCursor()
    conn = TransactionConn(cur)
    with mock.patch.object(identity, "_resolve_project_alias", return_value="acme/canonical"):
        write_capture(conn)
    assert conn.committed == ["acme/canonical"]
    filters = embed._SearchFilters(project="acme/original")
    with mock.patch.object(identity, "project_group", return_value=["acme/canonical", "acme/original"]):
        filters.expand_project(cur)
    assert filters.projects == ["acme/canonical", "acme/original"]


@pytest.mark.parametrize("error_type", ERRORS)
@pytest.mark.parametrize("lookup", (
    lambda cur: identity._resolve_project_alias(cur, "acme/original"),
    lambda cur: identity.project_canonical_group(cur, "acme/original"),
    lambda cur: project_group._savepoint(cur, "strict_lookup", "SELECT * FROM project_aliases", ()),
), ids=("alias", "canonical-group", "project-group-read"))
def test_identity_and_owner_group_helpers_remain_strict(error_type, lookup):
    cur = TransactionCursor(error_type)
    with pytest.raises(error_type):
        lookup(cur)
    cur.execute(INSERT, ("after strict failure",))
    assert cur.writes == ["after strict failure"]
