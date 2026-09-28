# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""``khipu decisions ...`` CLI dispatch (Phase 2, session A additions:
restore/retract/links/confirm/reject, and --standing/--superseded/
--retracted/--all on list). Mocks khipu.db.connect and khipu.decisions
directly — this is a dispatch/wiring test, not a decisions.py behavior
test (that's tests/test_decisions.py)."""
from __future__ import annotations

import unittest
from unittest import mock

from khipu.cli import build_parser, cmd_decisions


class _FakeCur:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.committed = 0

    def cursor(self):
        return _FakeCur()

    def commit(self):
        self.committed += 1

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CmdDecisionsTest(unittest.TestCase):
    def _parse(self, argv):
        return build_parser().parse_args(["decisions", *argv])

    def test_list_forwards_status_flags(self):
        for flag, expected in (("--standing", "standing"), ("--superseded", "superseded"),
                                ("--retracted", "retracted"), ("--all", "all")):
            with self.subTest(flag=flag):
                args = self._parse(["list", "--project", "acme/widget", flag])
                with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                        mock.patch("khipu.decisions.list_decisions", return_value=[]) as m:
                    rc = cmd_decisions(args)
                self.assertEqual(rc, 0)
                self.assertEqual(m.call_args.kwargs["status"], expected)
                self.assertEqual(m.call_args.kwargs["project"], "acme/widget")

    def test_list_with_no_flags_passes_status_none(self):
        args = self._parse(["list"])
        with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                mock.patch("khipu.decisions.list_decisions", return_value=[]) as m:
            cmd_decisions(args)
        self.assertIsNone(m.call_args.kwargs["status"])

    def test_supersede_success_commits_and_returns_zero(self):
        args = self._parse(["supersede", "1", "2", "--reason", "why", "--force"])
        conn = _FakeConn()
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.decisions.supersede", return_value=True) as m:
            rc = cmd_decisions(args)
        self.assertEqual(rc, 0)
        m.assert_called_once()
        self.assertEqual(m.call_args.args[1:], (1, 2))
        self.assertEqual(m.call_args.kwargs, {"reason": "why", "force": True})
        self.assertEqual(conn.committed, 1)

    def test_supersede_refusal_is_reported_not_raised(self):
        args = self._parse(["supersede", "1", "2"])
        with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                mock.patch("khipu.decisions.supersede", side_effect=ValueError("already superseded")):
            rc = cmd_decisions(args)
        self.assertEqual(rc, 1)

    def test_restore(self):
        args = self._parse(["restore", "9"])
        with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                mock.patch("khipu.decisions.restore", return_value=True) as m:
            rc = cmd_decisions(args)
        self.assertEqual(rc, 0)
        m.assert_called_once_with(mock.ANY, 9)

    def test_retract_forwards_reason(self):
        args = self._parse(["retract", "9", "--reason", "wrong"])
        with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                mock.patch("khipu.decisions.retract", return_value=True) as m:
            rc = cmd_decisions(args)
        self.assertEqual(rc, 0)
        m.assert_called_once_with(mock.ANY, 9, "wrong")

    def test_links_lists_with_state(self):
        args = self._parse(["links", "--state", "candidate"])
        with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                mock.patch("khipu.decisions.list_links", return_value=[]) as m:
            rc = cmd_decisions(args)
        self.assertEqual(rc, 0)
        self.assertEqual(m.call_args.kwargs["state"], "candidate")

    def test_confirm_and_reject(self):
        for sub in ("confirm", "reject"):
            with self.subTest(sub=sub):
                args = self._parse([sub, "3"])
                with mock.patch("khipu.db.connect", return_value=_FakeConn()), \
                        mock.patch("khipu.decisions.resolve_link", return_value={"ok": True}) as m:
                    rc = cmd_decisions(args)
                self.assertEqual(rc, 0)
                m.assert_called_once_with(mock.ANY, 3, sub)


if __name__ == "__main__":
    unittest.main()
