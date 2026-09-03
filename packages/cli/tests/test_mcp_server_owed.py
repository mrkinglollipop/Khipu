"""Tests for the khipu_owed MCP tool (W3.4) and khipu_capture's new `project`
passthrough. Separate file from test_mcp_server.py (a concurrent change's
territory) to avoid colliding edits.
"""
from __future__ import annotations

import unittest
from unittest import mock

from khipu import mcp_server as srv


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

        def fake_list_owed(cur, *, project=None, status="open", limit=50):
            called["project"] = project
            called["status"] = status
            return []

        with mock.patch("khipu.db.connect", return_value=_FakeConn(_FakeCur([]))), \
                mock.patch("khipu.commitments.list_owed", fake_list_owed):
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

        def fake_capture(payload, mode=None):
            seen.update(payload)
            return 0

        with mock.patch.object(srv, "_capture_mode", return_value="hub"), \
                mock.patch.object(srv, "_stdio_hook_owns_capture", return_value=False), \
                mock.patch("khipu.capture.capture", fake_capture):
            srv._tool_capture({"summary": "did a thing", "project": "acme/widget"})
        self.assertEqual(seen.get("project"), "acme/widget")


if __name__ == "__main__":
    unittest.main()
