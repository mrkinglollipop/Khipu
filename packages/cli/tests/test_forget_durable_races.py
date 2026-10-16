"""Failing-first reproductions for forget completing ahead of local writers."""
from __future__ import annotations

from contextlib import contextmanager
from unittest import mock

import pytest

from khipu import capture as cap, outbox, tombstones
from tests import test_forget_ownership as fo
from tests.test_capture import _FakeConn
from tests.test_capture_identity import _SourcesFakeCursor


@pytest.mark.parametrize("mode", ["dual", "hub"])
def test_forget_between_pg_commit_and_file_write(tmp_path, mode):
    payload = {"ts": "2026-10-01T09:00:00Z", "summary": "R6-INFLIGHT-CAPTURE-TEXT",
               "harness": "codex", "session_id": "codex:r6", "transcript_range": "0:10"}
    cur = _SourcesFakeCursor()
    (tmp_path / "replica").mkdir()
    fo.write_lines(tmp_path, [])
    observed = {}

    def concurrent_forget(value, *, episode_id):
        ep = cur.episodes[episode_id]
        world = {"episodes": {episode_id: dict(id=episode_id, ts=fo.instant(ep["ts"]),
                                                 summary=ep["summary"], session_id=payload["session_id"],
                                                 raw=ep["raw"], deleted=False)},
                 "sources": [dict(s, source_key=k) for k, s in cur.sources.items()],
                 "sources_enabled": True}
        observed["forget"] = fo.run_forget(world, episode_id, tmp_path)
        assert observed["forget"]["complete"], observed["forget"]
        return False

    child = tmp_path / "file_writer.py"
    child.write_text("import json,sys,pathlib\np=json.load(sys.stdin)\nwith pathlib.Path(" +
                     repr(str(tmp_path / "episodes.jsonl")) +
                     ").open('a') as f:f.write(json.dumps(p)+'\\n')\n")
    result = {}
    with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
            mock.patch("khipu.config.path_setting", return_value=None), \
            mock.patch("khipu.t3.thread_for_session", return_value=None), \
            mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}), \
            mock.patch("khipu.embed.embed_one", side_effect=RuntimeError("no provider")), \
            mock.patch("khipu.embed.embed_on_capture", side_effect=concurrent_forget), \
            mock.patch("khipu.hub_snapshot.snapshot_path", return_value=tmp_path / "replica/hub_snapshot.sqlite"), \
            mock.patch.dict("os.environ", {"KHIPU_OUTBOX": str(tmp_path / "outbox")}), \
            mock.patch.object(cap, "_capture_v2", return_value=child), \
            mock.patch("khipu.paths.isolated_dir", return_value=None):
        assert cap.capture(dict(payload), mode=mode, result=result) == 0
    assert payload["summary"] not in (tmp_path / "episodes.jsonl").read_text(), (
        "capture appended forgotten plaintext after forget returned complete=True")


def test_waiting_enqueue_before_forget_returns(tmp_path):
    payload = {"ts": "2026-10-01T09:00:00Z", "summary": "R6-OUTBOX-SENTINEL",
               "session_id": "codex:r6queue", "harness": "codex", "transcript_range": "0:10"}
    world = {"episodes": {1: dict(payload, id=1, ts=fo.BASE, raw=dict(payload), deleted=False)},
             "sources": [fo.ForgetOwnershipTest._source(None, 1, payload)], "sources_enabled": True}
    fo.write_lines(tmp_path, [payload])
    (tmp_path / "replica").mkdir()
    original_lock = outbox._locked_outbox
    state = {"calls": 0}

    @contextmanager
    def scheduler():
        state["calls"] += 1
        nth = state["calls"]
        with original_lock() as directory:
            yield directory
        if nth == 2:
            try:
                state["job"] = outbox.enqueue(dict(payload), reason="delayed PG failure")
            except tombstones.ForgottenSource:
                state["refused"] = True

    with mock.patch("khipu.outbox._locked_outbox", scheduler), \
            mock.patch("khipu.outbox._queued_origin", return_value={"origin": None}), \
            mock.patch("khipu.hub_snapshot.snapshot_path", return_value=tmp_path / "replica/hub_snapshot.sqlite"):
        result = fo.run_forget(world, 1, tmp_path)
    assert result["complete"], result
    assert state.get("refused") is True
    assert outbox.jobs() == []
