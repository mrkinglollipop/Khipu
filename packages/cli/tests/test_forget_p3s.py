"""#342 review P3 follow-ups: replica forget removes commitments and their
vectors, a purge marker compares hub time to hub time, and a malformed
routing-direction row is kept as written, never substring-matched, and keeps
a forget incomplete only when its session cannot be read. Fake data, no
network."""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from khipu import hub_snapshot as hs
from khipu import owner_turns
from khipu import routing_directions as rd


@pytest.fixture
def replica(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    import psycopg
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: pytest.fail("Network forbidden"))
    path = hs.snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        hs._create_schema(con)
        con.executemany("INSERT INTO episodes(id,summary) VALUES (?,?)", [(7, "forgotten ep"), (8, "live ep")])
        con.executemany("INSERT INTO commitments(id,text,status,opened_episode) VALUES (?,?,'open',?)",
                        [(70, "FORGOTTEN obligation", 7), (80, "live obligation", 8)])
        con.executemany("INSERT INTO memory_embeddings(profile,kind,ref,chunk_idx,chunk_text) "
                        "VALUES ('p','commitment',?,0,?)", [("70", "FORGOTTEN obligation"), ("80", "live obligation")])
    return path


def _vectors(path):
    with sqlite3.connect(path) as con:
        return [r[0] for r in con.execute("SELECT ref FROM memory_embeddings WHERE kind='commitment' ORDER BY ref")]


def _commitments(path):
    with sqlite3.connect(path) as con:
        return [r[0] for r in con.execute("SELECT id FROM commitments ORDER BY id")]


def test_replica_forget_removes_commitments_and_their_vectors(replica):
    assert hs.forget_episode_in_snapshot(7)["ok"]
    assert _commitments(replica) == [80]
    assert _vectors(replica) == ["80"]


def test_pulled_commitments_replace_drops_vectors_of_commitments_gone_from_the_hub(replica):
    cols = ["id", "text", "status", "opened_episode"]
    parts = hs._write_pulled_tables(None, (cols, [(80, "live obligation", "open", 8)]), None, read_at=time.time())
    assert parts["commitments"]["ok"], parts
    assert _commitments(replica) == [80]
    assert _vectors(replica) == ["80"]


def test_pulled_commitments_replace_keeps_a_standalone_commitments_vector(replica):
    with sqlite3.connect(replica) as con:
        con.execute("INSERT INTO commitments(id,text,status,opened_episode) VALUES (90,'solo','open',NULL)")
        con.execute("INSERT INTO memory_embeddings(profile,kind,ref,chunk_idx,chunk_text) "
                    "VALUES ('p','commitment','90',0,'solo')")
    cols = ["id", "text", "status", "opened_episode"]
    hs._write_pulled_tables(None, (cols, [(80, "x", "open", 8), (90, "solo", "open", None)]), None,
                            read_at=time.time())
    assert _vectors(replica) == ["80", "90"]


def _purged(replica, hub_at):
    assert hs._mark_purged_topic("gone", replica, hub_at=hub_at)


def _topic(stamp):
    return {"slug": "gone", "title": "t", "body": "NEW", "updated_at": stamp}


def test_recreated_topic_clears_marker_by_hub_time_despite_local_clock_ahead(replica):
    hub_at = datetime.now(timezone.utc) - timedelta(hours=2)  # hub clock far behind this Mac
    _purged(replica, hub_at)
    parts = hs._write_pulled_tables([_topic(hub_at + timedelta(seconds=1))], None, None, read_at=time.time())
    assert parts["topics"]["ok"], parts
    assert hs.topic_detail_snapshot("gone")["body"] == "NEW"
    assert "gone" not in hs._purged_topic_slugs(replica)


def test_topic_not_newer_than_hub_purge_time_stays_hidden(replica):
    hub_at = datetime.now(timezone.utc) + timedelta(hours=2)  # hub clock ahead of this Mac
    _purged(replica, hub_at)
    hs._write_pulled_tables([_topic(hub_at - timedelta(seconds=1))], None, None, read_at=time.time())
    assert hs.topic_detail_snapshot("gone") is None
    assert "gone" in hs._purged_topic_slugs(replica)


def test_marker_vanishing_mid_pull_does_not_raise(replica, monkeypatch):
    _purged(replica, datetime.now(timezone.utc) - timedelta(minutes=1))
    real = hs._purged_topic_slugs

    def slugs_then_vanish(path=None):
        out = real(path)
        hs._purged_topic_marker(replica, "gone").unlink()
        return out

    monkeypatch.setattr(hs, "_purged_topic_slugs", slugs_then_vanish)
    parts = hs._write_pulled_tables([_topic(datetime.now(timezone.utc))], None, None, read_at=time.time())
    assert parts["topics"]["ok"], parts
    assert hs._purge_marker_time(replica, "gone") is None


# ---- routing directions ---------------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    owner_turns._ensure_folder()
    return tmp_path


def _row(key, session, words="route the fake builds"):
    return {"id": key, "session": session, "words": words, "status": "open", "target": None, "occurrence": 1,
            "prompt_sha256": "synthetic-hash"}


def _write(rows):
    rd._save({"directions": rows})


def _raw():
    return rd.store_path().read_bytes()


def _rows():
    return json.loads(_raw())["directions"]


def test_a_malformed_row_without_a_session_keeps_the_forget_incomplete_and_is_kept_whole(store):
    bad = {"id": "bad", "words": 5}
    _write({"a": _row("a", "forgotten"), "b": _row("b", "kept"), "bad": bad})
    with pytest.raises(rd.MalformedRows) as err:
        rd.forget_sessions(["forgotten"])
    assert err.value.keys == ["bad"] and "bad" in str(err.value)
    assert set(rd._load()["directions"]) == {"b"}
    raw = _rows()
    assert "a" not in raw and raw["b"] == _row("b", "kept") and raw["bad"] == bad


def test_owner_forget_reports_the_unchecked_row_by_key(store):
    _write({"a": _row("a", "forgotten"), "rd-bad1": {"id": "rd-bad1", "words": "fake words"}})
    with pytest.raises(owner_turns.ForgetCleanupError) as err:
        owner_turns.forget_sessions(["forgotten"])
    assert any("rd-bad1" in e and "routing directions" in e for e in err.value.errors)
    assert not any("fake words" in e for e in err.value.errors)


def test_a_malformed_row_that_names_the_forgotten_session_is_scrubbed(store):
    _write({"bad": {"id": "bad", "session": "forgotten", "words": ["x"], "status": 3}})
    rd.forget_sessions(["forgotten"])
    assert rd.malformed_rows() == []
    assert "forgotten" not in rd.store_path().read_text(encoding="utf-8")


def test_forgetting_a_session_never_touches_a_session_it_prefixes(store):
    gone_live = {"id": "m2", "session": "codex:gone-live", "words": "fake live words", "status": 3}
    _write({"v1": _row("v1", "codex:gone"), "v2": _row("v2", "codex:gone-live"),
            "m1": {"id": "m1", "session": "codex:gone", "words": 7}, "m2": gone_live})
    rd.forget_sessions(["codex:gone"])  # every malformed row has a readable session: complete
    raw = _rows()
    assert set(raw) == {"v2", "m2"}
    assert raw["m2"] == gone_live and raw["v2"] == _row("v2", "codex:gone-live")


def test_words_mentioning_a_forgotten_session_do_not_remove_another_sessions_rows(store):
    live_bad = {"id": "m1", "session": "codex:live", "words": "fake: ask codex:gone again", "status": None}
    live = _row("v1", "codex:live", "fake: route what codex:gone said")
    _write({"v1": live, "m1": live_bad})
    rd.forget_sessions(["codex:gone"])
    assert _rows() == {"v1": live, "m1": live_bad}


@pytest.mark.parametrize("content", [b'{"directions": {"a":', b"[1, 2]", b"\xff\xfe not utf-8", b""])
def test_an_unreadable_store_is_left_byte_identical_by_sweep_forget_and_writers(store, content):
    rd.store_path().write_bytes(content)
    assert owner_turns.sweep()["ok"] is not None
    assert _raw() == content
    with pytest.raises(rd.StoreUnreadable):
        rd.forget_sessions(["codex:gone"])
    assert _raw() == content
    with pytest.raises(owner_turns.ForgetCleanupError) as err:
        owner_turns.forget_sessions(["codex:gone"])
    assert any("routing directions" in e for e in err.value.errors)
    assert _raw() == content
    for write in (lambda: rd.dismiss("rd-x"), lambda: rd.refresh([]),
                  lambda: rd.capture_scan("codex:gone", ["fake turn"])):
        with pytest.raises(rd.StoreUnreadable):
            write()
        assert _raw() == content
    assert rd.owed_items() == [] and rd.directions() == []
    block = owner_turns.doctor_block(installed=[])
    assert not block["ok"] and any("(store)" in w or rd.STORE_NAME in w for w in block["warnings"])
    assert _raw() == content


def test_a_valid_row_next_to_a_malformed_one_is_still_owed_and_delivered(store):
    bad = {"id": "rd-bad", "session": 4, "words": "fake"}
    good = {**_row("rd-good", "codex:live", "fake route words"), "project": "fake-proj", "join": "joined"}
    _write({"rd-good": good, "rd-bad": bad})
    assert [i["id"] for i in rd.owed_items()] == ["rd-good"]
    assert rd.pushed_slice_ids("fake-proj") == ["rd-good"]
    assert rd.dismiss("rd-good")["status"] == "dismissed"
    raw = _rows()
    assert raw["rd-bad"] == bad and raw["rd-good"]["status"] == "dismissed"


def test_doctor_names_a_malformed_row_and_stays_red_after_the_sweep(store):
    import os

    bad = {"id": "rd-bad7", "session": "codex:kept", "words": "fake owner words", "occurrence": "1",
           "status": "open"}
    _write({"rd-bad7": bad})
    block = owner_turns.doctor_block(installed=[])
    assert not block["ok"] and any("rd-bad7" in w for w in block["warnings"])
    assert not any("fake owner words" in w for w in block["warnings"])
    then = time.time() - 3600
    os.utime(rd.store_path(), (then, then))
    owner_turns.sweep()
    assert _rows() == {"rd-bad7": bad}
    assert [m["key"] for m in rd.malformed_rows()] == ["rd-bad7"]
    block = owner_turns.doctor_block(installed=[])
    assert not block["ok"] and any("rd-bad7" in w for w in block["warnings"])
