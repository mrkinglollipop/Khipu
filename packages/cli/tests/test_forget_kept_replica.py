"""Consumed replica cursors must discover kept facts reparented by forget."""
from datetime import datetime, timezone

import psycopg
import pytest

from khipu import db, forget, hub_snapshot as hs, offline_reads
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_forget_kept_support import PROJECT, seed


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for key, folder in (("KHIPU_DATA_DIR", "local"), ("KHIPU_CAPTURE_HOME", "capture"),
                        ("KHIPU_OUTBOX", "outbox")):
        monkeypatch.setenv(key, str(tmp_path / folder))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    db._TABLE_COLUMNS_CACHE.clear()


@pytest.mark.parametrize("local_erase", [True, False])
def test_consumed_replica_pull_discovers_kept_reparented_facts(hub, tmp_path, monkeypatch, local_erase):
    conn, cur, ids = seed(hub)
    monkeypatch.setattr(hs, "try_hub_connect", lambda **kw: psycopg.Connection.connect(hub.dsn, **kw))
    # These source rows are outside the pull overlap and their cursors have
    # already been consumed. A kept source needs a genuine new change stamp.
    cur.execute("ALTER TABLE episodes DISABLE TRIGGER khipu_episodes_changed_at")
    cur.execute("UPDATE episodes SET ingested_at=now()-interval '1 hour', changed_at=now()-interval '1 hour'")
    cur.execute("ALTER TABLE episodes ENABLE TRIGGER khipu_episodes_changed_at")
    conn.commit()
    assert hs.refresh()["ok"]
    assert hs.sync_decision_changes()["ok"]
    assert hs.meta().get("last_decision_sync_at")
    cur.execute("SELECT created_at,superseded_at,retracted_at FROM decisions")
    before_decision_times = cur.fetchone()
    cur.execute("SELECT ts,raw FROM episodes WHERE id=%s", (ids[1],))
    kept_authored = cur.fetchone()
    if not local_erase:
        # The other Mac has its own registry and replica directory.
        monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "forgetting-machine"))
    forget.forget_episode(cur, ids[0], memory_root=tmp_path / "memory")
    conn.commit()
    if not local_erase:
        monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "local"))
        from khipu.tombstones import forgotten_episode_ids

        assert ids[0] not in forgotten_episode_ids()
    if local_erase:
        assert hs.forget_episode_in_snapshot(ids[0])["ok"]
    with hs.open_snapshot() as replica:
        lag = hs._lag_decisions(
            cur, replica, datetime(1970, 1, 1, tzinfo=timezone.utc),
            datetime.now(timezone.utc), 100, hs.meta().get("hub_dumped_at"),
        )
    assert lag["missing"] >= 1 and lag["lag_seconds"] > 0, lag
    monkeypatch.setattr(hs, "refresh", lambda: pytest.fail("a full refresh must not be needed"))
    out = hs.pull_changes(include_ledger=False)
    assert out["ok"], out
    decisions = offline_reads.decisions(PROJECT, None, "all", 10)["results"]
    assert len(decisions) == 1 and decisions[0]["episode_id"] == ids[1], out
    with hs.open_snapshot() as replica:
        deliverables = hs.recent_deliverables_snapshot(replica, project=PROJECT)
    assert len(deliverables) == 1 and deliverables[0]["episode_id"] == ids[1], out
    cur.execute("SELECT created_at,superseded_at,retracted_at FROM decisions")
    assert cur.fetchone() == before_decision_times
    cur.execute("SELECT ts,raw FROM episodes WHERE id=%s", (ids[1],))
    assert cur.fetchone() == kept_authored


def test_decision_change_stamp_tracks_real_edits_without_rewriting_lifecycle(hub):
    conn, cur, _ = seed(hub)
    cur.execute("SELECT changed_at,created_at,superseded_at,retracted_at,decided_at FROM decisions")
    before = cur.fetchone()
    assert before[0] is not None
    cur.execute("UPDATE decisions SET text=text RETURNING changed_at")
    assert cur.fetchone()[0] == before[0]
    cur.execute(
        "UPDATE decisions SET text='Owner-authored replacement' "
        "RETURNING changed_at,created_at,superseded_at,retracted_at,decided_at"
    )
    after = cur.fetchone()
    assert after[0] > before[0]
    assert after[1:] == before[1:]
    conn.commit()
