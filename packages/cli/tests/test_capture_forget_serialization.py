"""Replays of legacy backfilled sources must stop at their tombstone."""
import json
from unittest import mock

import pytest

from khipu import capture as cap, capture_identity as ci
from tests import test_forget_ownership as fo
from tests.test_capture import _FakeConn
from tests.test_capture_identity import _SourcesFakeCursor


@pytest.mark.parametrize("mode", ["dual", "hub"])
@pytest.mark.parametrize("sources_enabled", [True, False])
def test_legacy_conflict_after_forget_is_terminal(tmp_path, mode, sources_enabled):
    payload = {"ts": "2026-10-01T09:00:00Z", "summary": "legacy forgotten text",
               "session_id": "codex:legacy"}
    source = {"episode_id": 1, "role": "origin", "ts": fo.BASE,
              "summary_md5": fo.md5(payload["summary"]),
              "source_key": ci.episode_key(1), "payload_sha256": None}
    world = {"episodes": {1: dict(payload, id=1, ts=fo.BASE, raw=dict(payload), deleted=False)},
             "sources": [source] if sources_enabled else [], "sources_enabled": sources_enabled}
    (tmp_path / "replica").mkdir()
    fo.write_lines(tmp_path, [payload])
    with mock.patch("khipu.hub_snapshot.snapshot_path",
                    return_value=tmp_path / "replica/hub_snapshot.sqlite"), \
            mock.patch.dict("os.environ", {"KHIPU_OUTBOX": str(tmp_path / "outbox")}):
        assert fo.run_forget(world, 1, tmp_path)["complete"]
    log = tmp_path / "episodes.jsonl"
    assert payload["summary"] not in log.read_text()
    cur = _SourcesFakeCursor({1: dict(payload, id=1, raw=dict(payload), topics=[],
                                    decisions=[], preferences=[], people=[],
                                    deleted_at="2026-10-15T00:00:00Z")}, sources=sources_enabled)
    if sources_enabled:
        cur.sources[source["source_key"]] = dict(source)

    def append(value, **kwargs):
        with log.open("a") as fh:
            fh.write(json.dumps(value) + "\n")
        return 0

    result = {}
    with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
            mock.patch("khipu.config.path_setting", return_value=None), \
            mock.patch("khipu.hygiene.classify_topics", return_value=([], [], False)), \
            mock.patch("khipu.t3.thread_for_session", return_value=None), \
            mock.patch("khipu.topic_graph.persist_capture_graph") as graph, \
            mock.patch.object(cap, "run_capture_v2", side_effect=append) as file_writer, \
            mock.patch("khipu.mirror.mirror_episode") as mirror, \
            mock.patch("khipu.embed.embed_on_capture") as embed:
        assert cap.capture(dict(payload), mode=mode, result=result) == 0
    assert result["dedup"]["action"] == "none"
    assert result.get("forgotten_source") is True
    assert result["episode_id"] == 1
    assert result["episode_inserted"] is False
    assert result["topics_written"] == 0
    file_writer.assert_not_called()
    mirror.assert_not_called()
    embed.assert_not_called()
    graph.assert_not_called()
    assert payload["summary"] not in log.read_text()
