"""Prompt-time recall's keyword leg stays on the trigram index when the prompt
holds a two-digit number (a date or a time). One such token used to send the
whole search to a LIKE scan of every row, which stalled the lane past its
1.2 s deadline in the keyword stage with no legs run."""
from __future__ import annotations

import sqlite3
from unittest import mock

import pytest

from khipu import hub_snapshot, recall_prompt


@pytest.fixture()
def replica(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    con = sqlite3.connect(str(hub_snapshot.snapshot_path()))
    hub_snapshot._create_schema(con)
    cols = hub_snapshot._snapshot_table_columns(con, "episodes")
    for i, summary in enumerate(["the replica schema stalled at 16:05", "lunch on the 16th", "nothing here"], 1):
        row = {k: v for k, v in {"id": i, "session_id": f"s:{i}", "ts": "2026-10-16T00:00:00Z",
                                 "summary": summary, "scope": "p"}.items() if k in cols}
        con.execute(f"INSERT INTO episodes ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                    list(row.values()))
    con.commit()
    con.close()
    hub_snapshot._FTS_STATE.update(key=None, ready=False)
    statements: list[str] = []
    real_open = hub_snapshot.open_snapshot

    def traced():
        c = real_open()
        c.set_trace_callback(statements.append)
        return c

    monkeypatch.setattr(hub_snapshot, "open_snapshot", traced)
    return statements


def _like_scans(statements):
    return [s for s in statements if "FROM episodes" in s and " LIKE " in s and "MATCH" not in s]


def test_a_two_digit_number_keeps_the_prompt_lane_on_the_index(replica):
    rows = hub_snapshot.search_snapshot("why did the replica stall at 16", 5, kind="episode",
                                        indexed_tokens_only=True)
    assert [r["id"] for r in rows] == ["1"]
    assert not _like_scans(replica), _like_scans(replica)


def test_other_callers_keep_matching_every_token(replica):
    rows = hub_snapshot.search_snapshot("why did the replica stall at 16", 5, kind="episode")
    assert {r["id"] for r in rows} == {"1", "2"}
    assert _like_scans(replica)


def test_a_prompt_of_only_short_numbers_still_searches(replica):
    rows = hub_snapshot.search_snapshot("16 05", 5, kind="episode", indexed_tokens_only=True)
    assert "1" in {r["id"] for r in rows}


def test_the_prompt_lane_asks_for_indexed_tokens_only():
    with mock.patch("khipu.hub_snapshot.snapshot_is_fresh", return_value=(True, {"exists": True})), \
            mock.patch("khipu.hub_snapshot.active_snapshot_profile", return_value=None), \
            mock.patch("khipu.hub_snapshot.search_snapshot", return_value=[]) as search:
        recall_prompt._snapshot_search_hits("why did the replica stall at 16", project=None)
    assert search.call_args.kwargs.get("indexed_tokens_only") is True
