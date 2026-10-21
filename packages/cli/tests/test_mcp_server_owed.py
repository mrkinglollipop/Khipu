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
                    "VALUES (%s, %s, 'user', %s)", (f"ship it for {ALIAS}", ALIAS, "h-alias"))
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


if __name__ == "__main__":
    unittest.main()
