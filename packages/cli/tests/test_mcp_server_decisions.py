# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Tests for the khipu_decisions / khipu_decisions_update MCP tool pair
(Phase 2, session A) and the tool annotations every entry in TOOLS now
carries. Separate file from test_mcp_server.py (a concurrent phase's
territory) to avoid colliding edits, same posture as
test_mcp_server_owed.py.
"""
from __future__ import annotations

import unittest
from unittest import mock

from khipu import mcp_server as srv


class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, cur=None):
        self._cur = cur or _FakeCur()
        self.committed = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class ToolAnnotationsTest(unittest.TestCase):
    """Part 4: "Every entry in TOOLS gains the MCP annotations object ...
    Add a test that every tool has annotations and that the read/write
    split is exactly this one.\""""

    _EXPECTED_READ_ONLY = {
        "khipu_search", "khipu_get", "khipu_graph", "khipu_status",
        "khipu_owed", "khipu_decisions", "khipu_brief",
    }
    _EXPECTED_WRITE = {
        "khipu_capture", "khipu_owed_update", "khipu_decisions_update", "khipu_forget",
    }

    def test_every_tool_has_annotations(self):
        for tool in srv.TOOLS:
            with self.subTest(tool=tool["name"]):
                self.assertIn("annotations", tool)
                self.assertIn("readOnlyHint", tool["annotations"])

    def test_the_read_write_split_is_exactly_this_one(self):
        names = {t["name"] for t in srv.TOOLS}
        self.assertEqual(names, self._EXPECTED_READ_ONLY | self._EXPECTED_WRITE)
        read_only = {t["name"] for t in srv.TOOLS if t["annotations"]["readOnlyHint"] is True}
        write = {t["name"] for t in srv.TOOLS if t["annotations"]["readOnlyHint"] is False}
        self.assertEqual(read_only, self._EXPECTED_READ_ONLY)
        self.assertEqual(write, self._EXPECTED_WRITE)

    def test_khipu_forget_is_destructive(self):
        tool = next(t for t in srv.TOOLS if t["name"] == "khipu_forget")
        self.assertTrue(tool["annotations"].get("destructiveHint"))

    def test_read_only_tools_declare_a_closed_world(self):
        for name in self._EXPECTED_READ_ONLY:
            tool = next(t for t in srv.TOOLS if t["name"] == name)
            with self.subTest(tool=name):
                self.assertFalse(tool["annotations"].get("openWorldHint", True))


class ToolDecisionsTest(unittest.TestCase):
    def test_default_status_is_standing(self):
        with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                mock.patch("khipu.decisions.list_decisions", return_value=[]) as m:
            out = srv._tool_decisions({"project": "acme/widget"})
        self.assertEqual(out["status"], "standing")
        self.assertEqual(m.call_args.kwargs["status"], "standing")
        self.assertEqual(m.call_args.kwargs["project"], "acme/widget")

    def test_bad_status_rejected(self):
        with self.assertRaises(ValueError):
            srv._tool_decisions({"status": "bogus"})

    def test_conflicts_status_calls_list_links_not_list_decisions(self):
        with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                mock.patch("khipu.decisions.list_links", return_value=[{"id": 1}]) as m_links, \
                mock.patch("khipu.decisions.list_decisions") as m_decisions:
            out = srv._tool_decisions({"status": "conflicts", "project": "acme/widget"})
        m_links.assert_called_once()
        m_decisions.assert_not_called()
        self.assertEqual(out["results"], [{"id": 1}])

    def test_since_is_parsed(self):
        with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                mock.patch("khipu.decisions.list_decisions", return_value=[]) as m:
            srv._tool_decisions({"since": "7d"})
        self.assertIsNotNone(m.call_args.kwargs["since"])

    def test_registered_in_tool_funcs_and_schema(self):
        self.assertIn("khipu_decisions", srv.TOOL_FUNCS)
        names = {t["name"] for t in srv.TOOLS}
        self.assertIn("khipu_decisions", names)


class ToolDecisionsUpdateTest(unittest.TestCase):
    def _cur(self, *, evidence_ready=True):
        cur = _FakeCur()
        return cur, evidence_ready

    def test_bad_action_rejected(self):
        with self.assertRaises(ValueError):
            srv._tool_decisions_update({"action": "bogus"})

    def test_supersede_by_id(self):
        cur, _ = self._cur()
        conn = _FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.decisions._evidence_ready", return_value=True), \
                mock.patch("khipu.decisions.supersede", return_value=True) as m_sup:
            out = srv._tool_decisions_update({"action": "supersede", "id": 1, "by": 2, "reason": "why"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["superseded_by"], 2)
        self.assertNotIn("note", out)
        m_sup.assert_called_once_with(cur, 1, 2, source="agent", reason="why")
        self.assertEqual(conn.committed, 1)

    def test_supersede_with_new_text_creates_a_decision_first(self):
        cur = _FakeCur()
        conn = _FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.decisions._evidence_ready", return_value=True), \
                mock.patch("khipu.decisions.create_decision", return_value=42) as m_create, \
                mock.patch("khipu.decisions.supersede", return_value=True) as m_sup:
            out = srv._tool_decisions_update(
                {"action": "supersede", "id": 1, "new_text": "the replacement",
                 "project": "acme/widget", "source_kind": "user"}
            )
        self.assertEqual(out["superseded_by"], 42)
        m_create.assert_called_once()
        self.assertEqual(m_create.call_args.kwargs["text"], "the replacement")
        self.assertEqual(m_create.call_args.kwargs["source_kind"], "user")
        m_sup.assert_called_once_with(cur, 1, 42, source="agent", reason=None)

    def test_supersede_needs_exactly_one_of_by_or_new_text(self):
        with self.assertRaises(ValueError):
            srv._tool_decisions_update({"action": "supersede", "id": 1})
        with self.assertRaises(ValueError):
            srv._tool_decisions_update({"action": "supersede", "id": 1, "by": 2, "new_text": "x"})

    def test_pre_migration_supersede_carries_a_note(self):
        cur = _FakeCur()
        conn = _FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.decisions._evidence_ready", return_value=False), \
                mock.patch("khipu.decisions.supersede", return_value=True):
            out = srv._tool_decisions_update({"action": "supersede", "id": 1, "by": 2})
        self.assertIn("note", out)

    def test_restore(self):
        cur = _FakeCur()
        conn = _FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.decisions._evidence_ready", return_value=True), \
                mock.patch("khipu.decisions.restore", return_value=True) as m_restore:
            out = srv._tool_decisions_update({"action": "restore", "id": 5})
        self.assertTrue(out["ok"])
        m_restore.assert_called_once_with(cur, 5)

    def test_retract_requires_integer_id(self):
        with self.assertRaises(ValueError):
            srv._tool_decisions_update({"action": "retract", "id": "not-an-int"})

    def test_confirm_reject_use_link_id_not_id(self):
        cur = _FakeCur()
        conn = _FakeConn(cur)
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.decisions._evidence_ready", return_value=True), \
                mock.patch("khipu.decisions.resolve_link", return_value={"ok": True}) as m_resolve:
            srv._tool_decisions_update({"action": "confirm", "link_id": 9})
        m_resolve.assert_called_once_with(cur, 9, "confirm", source="agent")

    def test_registered_in_tool_funcs_and_schema(self):
        self.assertIn("khipu_decisions_update", srv.TOOL_FUNCS)
        names = {t["name"] for t in srv.TOOLS}
        self.assertIn("khipu_decisions_update", names)


class ToolGetDecisionStatesTest(unittest.TestCase):
    def test_episode_payload_gains_decision_states_and_validity(self):
        enrichment = {
            "decision_states": [{"id": 1, "text": "x", "state": "current", "superseded_by": None}],
            "validity": {"current": 1, "superseded": 0, "retracted": 0},
        }
        with mock.patch("khipu.activity.episode_detail",
                         return_value={"id": 1, "summary": "s", "raw": {"secret": True}}), \
                mock.patch("khipu.decisions.decision_states_for_episode", return_value=enrichment):
            out = srv._tool_get({"id": "1", "kind": "episode"})
        self.assertEqual(out["decision_states"], enrichment["decision_states"])
        self.assertEqual(out["validity"], enrichment["validity"])
        self.assertNotIn("raw", out)
        self.assertEqual(out["summary"], "s")


if __name__ == "__main__":
    unittest.main()
