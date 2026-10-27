"""Tests for the khipu_owed MCP tool (W3.4) and khipu_capture's new `project`
passthrough. Separate file from test_mcp_server.py (a concurrent change's
territory) to avoid colliding edits.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

import pytest

from khipu import mcp_server as srv
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests import fact_seed


class _FakeCur:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ToolOwedTest(unittest.TestCase):
    def test_lists_open_by_default(self):
        rows = [(1, "follow up", "acme/widget", None, "followup", 10, "t0", None,
                  "open", None, None, None)]
        with mock.patch("khipu.db.connect", return_value=_FakeConn(_FakeCur(rows))):
            out = srv._tool_owed({})
        self.assertEqual(out["status"], "open")
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(out["results"][0]["text"], "follow up")

    def test_rejects_bad_status(self):
        with self.assertRaises(ValueError):
            srv._tool_owed({"status": "bogus"})

    def test_project_filter_is_forwarded(self):
        called = {}

        def fake_fetch(cur, *, project=None, status="open"):
            called["project"] = project
            called["status"] = status
            return []

        with mock.patch("khipu.db.connect", return_value=_FakeConn(_FakeCur([]))), \
                mock.patch("khipu.commitments.fetch_owed_rows", fake_fetch):
            srv._tool_owed({"project": "acme/widget", "status": "stale"})
        self.assertEqual(called["project"], "acme/widget")
        self.assertEqual(called["status"], "stale")

    def test_registered_in_tool_funcs_and_schema(self):
        self.assertIn("khipu_owed", srv.TOOL_FUNCS)
        names = {t["name"] for t in srv.TOOLS}
        self.assertIn("khipu_owed", names)


class CaptureProjectFieldTest(unittest.TestCase):
    def test_project_is_in_the_khipu_capture_schema(self):
        tool = next(t for t in srv.TOOLS if t["name"] == "khipu_capture")
        self.assertIn("project", tool["inputSchema"]["properties"])

    def test_project_rides_through_to_the_payload(self):
        seen = {}

        def fake_capture(payload, mode=None, result=None):
            seen.update(payload)
            return 0

        with mock.patch.object(srv, "_capture_mode", return_value="hub"), \
                mock.patch.object(srv, "_stdio_hook_owns_capture", return_value=False), \
                mock.patch("khipu.capture.capture", fake_capture):
            srv._tool_capture({"summary": "did a thing", "project": "acme/widget"})
        self.assertEqual(seen.get("project"), "acme/widget")


CANON = "owner/olivewood"
ALIAS = "owner/church-harness"


@pytest.fixture
def aliased_hub(hub, tmp_path, monkeypatch):  # noqa: F811
    """A hub where ``ALIAS`` reads as ``CANON``, with one row under the alias."""
    from khipu import db

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    conn = hub.connect()
    hub.migrate(conn=conn)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s)", (ALIAS, CANON))
        cur.execute("INSERT INTO commitments (text, project, owner, content_hash) "
                    "VALUES (%s, %s, 'user', %s) RETURNING id", (f"ship it for {ALIAS}", ALIAS, "h-alias"))
        fact_seed.adopt(cur, "commitment", cur.fetchone()[0])
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    monkeypatch.setattr(db, "connect", lambda *a, **k: hub.connect())
    return conn


def test_owed_project_field_echoes_the_canonical_name_not_the_alias(aliased_hub, capsys):
    out = srv._tool_owed({"project": ALIAS})
    assert out["project"] == CANON
    assert out["asked"] == ALIAS
    assert [r["project"] for r in out["results"]] == [CANON]

    # Asking by the canonical name already is no alias: no asked field.
    canonical_out = srv._tool_owed({"project": CANON})
    assert canonical_out["project"] == CANON
    assert "asked" not in canonical_out

    # `khipu owed --json` is the same listing, so the same two fields.
    from khipu import cli

    args = cli.build_parser().parse_args(["owed", "--project", ALIAS, "--json"])
    assert args.func(args) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["project"] == CANON
    assert printed["asked"] == ALIAS


def _add_commitment(hub_conn, project, text, h):
    with hub_conn.cursor() as cur:
        cur.execute("INSERT INTO commitments (text, project, owner, content_hash) "
                    "VALUES (%s, %s, 'user', %s) RETURNING id", (text, project, h))
        fact_seed.adopt(cur, "commitment", cur.fetchone()[0])
    hub_conn.commit()


def test_owed_short_repo_name_resolves_to_the_one_stored_project(aliased_hub):
    # "olivewood" is neither an alias nor a stored key: it is the last segment of CANON.
    out = srv._tool_owed({"project": "olivewood"})
    assert out["project"] == CANON
    assert out["asked"] == "olivewood"
    assert [r["project"] for r in out["results"]] == [CANON]
    # The case-insensitive full name reads the same way.
    assert srv._tool_owed({"project": CANON.upper()})["project"] == CANON


def test_owed_short_name_fitting_two_projects_names_the_candidates(aliased_hub):
    _add_commitment(aliased_hub, "someone/olivewood", "other repo's work", "h-other")
    with pytest.raises(ValueError) as err:
        srv._tool_owed({"project": "olivewood"})
    assert CANON in str(err.value) and "someone/olivewood" in str(err.value)


def test_owed_exact_stored_name_beats_a_short_name_match(aliased_hub):
    _add_commitment(aliased_hub, "olivewood", "bare-named project", "h-bare")
    _add_commitment(aliased_hub, "someone/olivewood", "other repo's work", "h-other")
    out = srv._tool_owed({"project": "olivewood"})
    assert out["project"] == "olivewood"
    assert [r["text"] for r in out["results"]] == ["bare-named project"]


def test_owed_cli_ambiguous_short_name_is_an_error_not_an_empty_list(aliased_hub, capsys):
    from khipu import cli

    _add_commitment(aliased_hub, "someone/olivewood", "other repo's work", "h-other")
    args = cli.build_parser().parse_args(["owed", "--project", "olivewood", "--json"])
    assert args.func(args) == 2
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is False and CANON in printed["error"]


def test_owed_short_name_ignores_projects_of_forgotten_episodes(aliased_hub):
    with aliased_hub.cursor() as cur:
        fact_seed.announce(cur)
        cur.execute("INSERT INTO episodes (ts, session_id, summary, project) "
                    "VALUES (now(), 'owed-forgotten', 'gone', %s) RETURNING id", ("someone/olivewood",))
        ep = cur.fetchone()[0]
        cur.execute("INSERT INTO commitments (text, project, owner, content_hash, opened_episode) "
                    "VALUES ('x', %s, 'user', 'h-forgot', %s) RETURNING id", ("someone/olivewood", ep))
        cid = cur.fetchone()[0]
        fact_seed.adopt(cur, "commitment", cid)
        # Forgetting retires the fact's claims that name the episode first.
        cur.execute("DELETE FROM fact_claims WHERE kind = 'commitment' AND fact_id = %s", (cid,))
        cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (ep,))
    aliased_hub.commit()
    assert srv._tool_owed({"project": "olivewood"})["project"] == CANON


def test_owed_short_name_resolves_on_the_offline_replica_too(monkeypatch):
    import sqlite3

    from khipu import hub_snapshot, identity

    # A bare sqlite file has no fact stamp, which would hide every row; the
    # forget fence (deleted episodes) is what this test is about.
    monkeypatch.setattr(hub_snapshot, "_fact_epoch_sql", lambda con: "")
    monkeypatch.setattr(hub_snapshot, "_pending_erasure_ids", lambda path=None: set())
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE commitments (id INTEGER, project TEXT, opened_episode INTEGER)")
    con.execute("CREATE TABLE episodes (id INTEGER, deleted_at TEXT)")
    con.execute("CREATE TABLE project_aliases (alias TEXT, project TEXT)")
    con.execute("INSERT INTO commitments VALUES (1, 'owner/olivewood', NULL)")
    con.execute("INSERT INTO project_aliases VALUES ('owner/church-harness', 'owner/olivewood')")
    assert hub_snapshot.snapshot_resolve_project(con, "olivewood") == "owner/olivewood"
    assert hub_snapshot.snapshot_resolve_project(con, "church-harness") == "owner/olivewood"
    assert hub_snapshot.snapshot_resolve_project(con, "nothing-like-it") == "nothing-like-it"
    # A commitment opened by a forgotten episode does not make the name ambiguous.
    con.execute("INSERT INTO episodes VALUES (9, '2026-10-27')")
    con.execute("INSERT INTO commitments VALUES (2, 'someone/olivewood', 9)")
    assert hub_snapshot.snapshot_resolve_project(con, "olivewood") == "owner/olivewood"
    con.execute("INSERT INTO commitments VALUES (3, 'someone/olivewood', NULL)")
    with pytest.raises(identity.AmbiguousProject):
        hub_snapshot.snapshot_resolve_project(con, "olivewood")


if __name__ == "__main__":
    unittest.main()
