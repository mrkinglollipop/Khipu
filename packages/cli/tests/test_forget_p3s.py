"""#342 review P3 follow-ups: replica forget removes commitments and their
vectors, a purge marker compares hub time to hub time, and one malformed
routing-direction row never blocks a forget. Fake data, no network."""
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
    return {"id": key, "session": session, "words": words, "status": "open", "target": None, "occurrence": 1}


def _write(rows):
    rd._save({"directions": rows})


def test_a_malformed_row_blocks_nothing_valid_is_still_scrubbed_and_the_error_names_it(store):
    _write({"a": _row("a", "forgotten"), "b": _row("b", "kept"), "bad": {"id": "bad", "words": 5}})
    with pytest.raises(ValueError) as err:
        rd.forget_sessions(["forgotten"])
    assert "bad" in str(err.value)
    left = rd._load()["directions"]
    assert set(left) == {"b"}
    raw = json.loads(rd.store_path().read_text(encoding="utf-8"))["directions"]
    assert "a" not in raw and "b" in raw and "bad" in raw  # the bad row is kept for repair, not lost


def test_a_malformed_row_that_names_the_forgotten_session_is_scrubbed(store):
    _write({"bad": {"id": "bad", "session": "forgotten", "words": ["x"], "status": 3}})
    rd.forget_sessions(["forgotten"])
    assert rd.malformed_rows() == []
    assert "forgotten" not in rd.store_path().read_text(encoding="utf-8")


def test_repair_moves_malformed_rows_aside_and_forget_then_completes(store):
    _write({"b": _row("b", "kept"), "bad": {"id": "bad", "words": 5}})
    assert [m["key"] for m in rd.malformed_rows()] == ["bad"]
    assert rd.repair_store() == ["bad"]
    assert rd.malformed_rows() == []
    assert set(rd._load()["directions"]) == {"b"}
    rd.forget_sessions(["kept"])
    assert rd._load()["directions"] == {}


def test_doctor_reports_a_malformed_row_and_sweep_repairs_it(store):
    _write({"bad": {"id": "bad", "words": 5}})
    block = owner_turns.doctor_block(installed=[])
    assert any("bad" in w for w in block["warnings"]) and not block["ok"]
    owner_turns.sweep()
    assert rd.malformed_rows() == []
    block = owner_turns.doctor_block(installed=[])
    assert not block["ok"] and any("held" in w and "bad" in w for w in block["warnings"])


def test_a_quarantined_row_survives_the_sweep_whole_and_doctor_stays_red(store):
    import os

    words = "route the fake builds to the fake lane"
    row = _row("rd-1", "kept-session", words)
    row["occurrence"] = "1"  # a schema-incompatible field
    _write({"rd-1": row})
    assert rd.repair_store() == ["rd-1"]
    path = rd.store_path().with_name(rd.QUARANTINE_NAME)
    then = time.time() - 61
    os.utime(path, (then, then))
    out = owner_turns.sweep()
    assert out["ok"], out
    assert path.exists() and words in path.read_text(encoding="utf-8")
    assert not owner_turns.doctor_block(installed=[])["ok"]


def test_a_quarantined_row_without_a_session_keeps_its_words_until_that_session_is_forgotten(store):
    _write({"x": {"id": "x", "words": "fake words naming gone-session", "status": 3}})
    rd.repair_store()
    path = rd.store_path().with_name(rd.QUARANTINE_NAME)
    assert "fake words" in path.read_text(encoding="utf-8")
    rd.forget_sessions(["other"])
    assert path.exists()
    rd.forget_sessions(["gone-session"])
    assert not path.exists()
