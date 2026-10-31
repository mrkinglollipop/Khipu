"""The forget fence (``tombstones.withhold``) on the explicit readers of
episode-derived rows that trusted only the hub's ``deleted_at``: owed and
decisions (MCP, CLI, the Work view), ``khipu activity``, and briefs (MCP,
CLI, reflect, topic detail). A forget lands on this Mac's list before the hub
records it, so each reader drops the rows of a listed episode; with an
unreadable list each drops every row tied to an episode. Kimi's round-2
probes, 2026-10-30."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
from unittest import mock

import pytest

from khipu import activity, briefs, cli, commitments, decisions, mcp_server, tombstones

FORGOT = 902
LIVE = 904


@pytest.fixture
def forgotten():
    tombstones.publish_episode_ids({FORGOT})
    return FORGOT


def _corrupt_list() -> None:
    path = tombstones.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")


class _Cursor:
    """Answers each query by the table it reads; anything else fails."""

    def __init__(self, rows: dict[str, list[tuple]]):
        self.rows, self.sql = rows, ""

    def execute(self, sql, *args):
        self.sql = sql
        return self

    def _match(self):
        for marker, rows in self.rows.items():
            if marker in self.sql:
                return rows
        raise AssertionError(self.sql)

    def fetchone(self):
        rows = self._match()
        return rows[0] if rows else None

    def fetchall(self):
        return list(self._match())


class _Connection:
    def __init__(self, cur=None):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cursor(self):
        ctx = mock.MagicMock()
        ctx.__enter__.return_value = self.cur or _Cursor({})
        return ctx


# ---- khipu_decisions and khipu_owed (gateway and MCP) ----------------------

DECISION = {"id": 5, "episode_id": FORGOT, "text": "FORGOTTEN-DECISION"}
LIVE_DECISION = {"id": 7, "episode_id": LIVE, "text": "LIVE-DECISION"}
COMMITMENT = {"id": 6, "opened_episode": FORGOT, "text": "FORGOTTEN-COMMITMENT"}
LIVE_COMMITMENT = {"id": 8, "opened_episode": LIVE, "text": "LIVE-COMMITMENT"}


def _gateway_outputs():
    with mock.patch.object(mcp_server, "_ensure_path"), \
         mock.patch("khipu.db.connect", return_value=_Connection()), \
         mock.patch("khipu.offline.setting", return_value=1), \
         mock.patch("khipu.decisions.list_decisions", return_value=[dict(DECISION), dict(LIVE_DECISION)]), \
         mock.patch("khipu.owner_writes.annotate", return_value={}), \
         mock.patch("khipu.commitments.fetch_owed_rows",
                    return_value=[dict(COMMITMENT), dict(LIVE_COMMITMENT)]), \
         mock.patch("khipu.commitments.page_owed", side_effect=lambda rows, **kw: {"results": rows}), \
         mock.patch.object(mcp_server, "_routing_direction_items", return_value=[]), \
         mock.patch.object(mcp_server, "_routing_direction_rows", return_value=[]), \
         mock.patch.object(mcp_server, "owed_page_limit", return_value=(50, None)):
        return (mcp_server._tool_decisions({})["results"],
                mcp_server.owed_listing({})["results"])


def test_khipu_decisions_and_owed_hide_a_forgotten_episodes_rows(forgotten):
    found, owed = _gateway_outputs()
    assert [d["text"] for d in found] == ["LIVE-DECISION"]
    assert [c["text"] for c in owed] == ["LIVE-COMMITMENT"]


def test_khipu_decisions_and_owed_show_no_episode_row_when_the_forget_list_is_unreadable():
    _corrupt_list()
    found, owed = _gateway_outputs()
    assert found == [] and owed == []


def test_list_decisions_drops_a_forgotten_episodes_decision(forgotten):
    cols = (5, "p", "FORGOTTEN-DECISION", None, None, FORGOT, "s", None, None)
    live = (7, "p", "LIVE-DECISION", None, None, LIVE, "s", None, None)
    cur = _Cursor({"FROM decisions": [cols, live]})
    with mock.patch.object(decisions, "_evidence_ready", return_value=False), \
         mock.patch.object(decisions, "_not_forgotten_clause", return_value=""):
        rows = decisions.list_decisions(cur)
    assert [r["text"] for r in rows] == ["LIVE-DECISION"]


def test_cli_decisions_list_never_prints_a_forgotten_episodes_decision(forgotten):
    cur = _Cursor({"FROM decisions": [(5, "p", "FORGOTTEN-DECISION", None, None, FORGOT, "s", None, None)]})
    args = argparse.Namespace(decisions_cmd="list", since=None, project=None, limit=5)
    with mock.patch("khipu.db.connect", return_value=_Connection(cur)), \
         mock.patch.object(decisions, "_evidence_ready", return_value=False), \
         mock.patch.object(decisions, "_not_forgotten_clause", return_value=""), \
         contextlib.redirect_stdout(io.StringIO()) as out:
        cli.cmd_decisions(args)
    assert "FORGOTTEN-DECISION" not in out.getvalue()


def test_decision_conflict_links_hide_a_forgotten_episodes_decision(forgotten):
    link = (1, 5, 7, "reversal", 0.9, "auto", "r", "candidate", None, None,
            "FORGOTTEN-DECISION", "LIVE-DECISION", FORGOT, LIVE)
    cur = _Cursor({"FROM decision_links": [link]})
    with mock.patch.object(decisions, "_links_ready", return_value=True), \
         mock.patch.object(decisions, "_not_forgotten_clause", return_value=""):
        rows = decisions.list_links(cur)
    assert rows == []


def test_owed_rows_drop_a_forgotten_episodes_commitment_and_its_suggestion(forgotten):
    row = (6, "FORGOTTEN-COMMITMENT", "p", "user", "followup", FORGOT, None, None, "open", None, None, None)
    live = (8, "LIVE-COMMITMENT", "p", "user", "followup", LIVE, None, None, "open", None, None, None)
    cur = _Cursor({"FROM commitments": [row, live]})
    with mock.patch.multiple(commitments, _seen_columns_ready=lambda cur: False,
                             _future_trigger_ready=lambda cur: False,
                             _owner_columns_ready=lambda cur: False,
                             _close_columns_ready=lambda cur: False,
                             _reassigned_column_ready=lambda cur: False,
                             _lineage_columns_ready=lambda cur: False,
                             _not_forgotten_clause=lambda cur: "1=1",
                             close_reason_sql=lambda cur: "close_reason"):
        rows = commitments.fetch_owed_rows(cur, status="open")
    assert [r["text"] for r in rows] == ["LIVE-COMMITMENT"]

    suggested = [{"id": 9, "opened_episode": LIVE, "settle_suggestion": {"episode": FORGOT, "reason": "x"}}]
    assert commitments._fenced_owed_rows(suggested)[0]["settle_suggestion"] is None


def test_an_item_closed_by_a_forgotten_episode_stays_with_its_reason_forgotten(forgotten):
    closed = [{"id": 10, "opened_episode": LIVE, "closed_episode": FORGOT, "status": "closed",
               "close_reason": "settled by decision 5: FORGOTTEN-DECISION"}]
    assert commitments._fenced_owed_rows(closed) == [{**closed[0], "close_reason": "forgotten"}]


# ---- khipu activity --------------------------------------------------------

def _activity():
    cur = _Cursor({"COUNT(*) FROM episodes": [(1,)], "MAX(ts)": [(None, None, None)],
                   "to_regclass": [(False,)]})
    with mock.patch.object(activity, "connect", return_value=_Connection(cur)), \
         mock.patch.object(activity, "_live_where", return_value=""), \
         mock.patch.object(activity, "_recent_rows",
                           return_value=[{"id": FORGOT, "summary": "FORGOTTEN-ACTIVITY"},
                                         {"id": LIVE, "summary": "LIVE-ACTIVITY"}]), \
         mock.patch("khipu.keychain.secrets_status", return_value={}):
        return activity.activity_payload()["recent"]


def test_khipu_activity_hides_a_forgotten_episode(forgotten):
    assert [r["summary"] for r in _activity()] == ["LIVE-ACTIVITY"]


def test_khipu_activity_shows_no_episode_when_the_forget_list_is_unreadable():
    _corrupt_list()
    assert _activity() == []


# ---- briefs ----------------------------------------------------------------

def _brief_show():
    cur = _Cursor({"FROM briefs": [(1, "FORGOTTEN-BRIEF", [], [FORGOT], "current",
                                    briefs.DERIVATION_VERSION, "fake", None, 0)]})
    with mock.patch.object(briefs, "_ready", return_value=True), \
         mock.patch.object(briefs, "_has", return_value=False), \
         mock.patch.object(briefs, "_current_row", return_value=None), \
         mock.patch("khipu.db.connect", return_value=_Connection(cur)), \
         contextlib.redirect_stdout(io.StringIO()) as out:
        briefs.cli_main(argparse.Namespace(briefs_cmd="show", slug="secret-topic", limit=1))
    return json.loads(out.getvalue())


def test_a_brief_citing_a_forgotten_episode_is_withheld(forgotten):
    shown = _brief_show()
    assert shown["body"] == "" and shown["claims"] == [] and "withheld" in shown


def test_a_brief_is_withheld_when_the_forget_list_is_unreadable():
    _corrupt_list()
    assert _brief_show()["body"] == ""
