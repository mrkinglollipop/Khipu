"""khipu_owed_update and khipu_forget MCP tools (audit 2026-09-04: cloud
agents could open commitments they could never close; forget stopped at
vectors)."""
from __future__ import annotations

import unittest
from unittest import mock

from khipu import mcp_server as srv


class _FakeCur:
    def __init__(self, rowcount=1, one=None):
        self.rowcount = rowcount
        self.one = one
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur):
        self._cur = cur

    def cursor(self):
        return self._cur

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ToolOwedUpdateTest(unittest.TestCase):
    def test_close_and_reopen_go_through_set_status(self):
        cur = _FakeCur()
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)):
            out = srv._tool_owed_update({"id": 7, "action": "close"})
        self.assertEqual(out, {"ok": True, "id": 7, "action": "close"})
        self.assertTrue(any("status = %s, closed_at = now()" in s for s in cur.sql))

        cur2 = _FakeCur()
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur2)):
            out = srv._tool_owed_update({"id": "7", "action": "reopen"})
        self.assertEqual(out["action"], "reopen")
        self.assertTrue(any("status = 'open'" in s for s in cur2.sql))

    def test_snooze_needs_a_date_and_bad_actions_are_refused(self):
        cur = _FakeCur()
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)):
            with self.assertRaises(ValueError):
                srv._tool_owed_update({"id": 7, "action": "snooze", "until": "someday"})

        cur2 = _FakeCur()
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur2)):
            out = srv._tool_owed_update({"id": 7, "action": "snooze", "until": "7d"})
        self.assertEqual(out["until"], "7d")
        self.assertTrue(any("due_after" in s for s in cur2.sql))

        with self.assertRaises(ValueError):
            srv._tool_owed_update({"id": 7, "action": "delete"})

    def test_unknown_id_is_an_error(self):
        cur = _FakeCur(rowcount=0)
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch("khipu.commitments.set_status", return_value=False):
            with self.assertRaises(ValueError):
                srv._tool_owed_update({"id": 999, "action": "close"})


class ToolForgetTest(unittest.TestCase):
    def test_gateway_refuses_a_local_capture_and_allows_a_cloud_one(self):
        cur = _FakeCur(one=("claude_code:f916790e",))
        with mock.patch.object(srv, "_via_https_gateway", return_value=True), \
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch("khipu.forget.forget_everywhere") as m_forget:
            with self.assertRaises(ValueError) as ctx:
                srv._tool_forget({"id": 11617})
        self.assertIn("claude_code", str(ctx.exception))
        m_forget.assert_not_called()

        cur2 = _FakeCur(one=("grokbot:x-intern:pack",))
        with mock.patch.object(srv, "_via_https_gateway", return_value=True), \
                mock.patch("khipu.db.connect", return_value=_FakeConn(cur2)), \
                mock.patch("khipu.forget.forget_everywhere",
                           return_value={"ok": True, "id": 5, "identity": {"ts": "t"}}) as m_forget:
            out = srv._tool_forget({"id": 5})
        m_forget.assert_called_once_with(5)
        self.assertNotIn("identity", out)

    def test_local_stdio_forgets_anything(self):
        with mock.patch.object(srv, "_via_https_gateway", return_value=False), \
                mock.patch("khipu.forget.forget_everywhere", return_value={"ok": True, "id": 5}):
            out = srv._tool_forget({"id": 5})
        self.assertEqual(out["id"], 5)


if __name__ == "__main__":
    unittest.main()
