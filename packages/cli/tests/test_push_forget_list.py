"""A forget reaches this Mac's forget list before the replica's next rewrite
tombstones the row. Every automatic push, and the offline search, reads the
forget list (and the erasure queue) at the time it answers: a forgotten
episode the replica still holds live is never pushed or found, and an
unreadable forget list pushes nothing from the replica."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from khipu import hub_snapshot as hs
from khipu import recall_prompt as rp
from khipu import tombstones

PROMPT = "zephyrine importer canary"


def _ts(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture
def replica(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.setenv("KHIPU_DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none")
    now = datetime.now(timezone.utc)
    snap = tmp_path / "hub_snapshot.sqlite"
    con = sqlite3.connect(snap)
    hs._create_schema(con)
    for eid, word in ((902, "reverted"), (903, "queued"), (904, "kept")):
        con.execute("INSERT INTO episodes (id, summary, ingested_at) VALUES (?, ?, ?)",
                    (eid, f"{PROMPT} fix {word}", _ts(now - timedelta(hours=2))))
    con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('refreshed_at', ?)", (_ts(now),))
    con.commit()
    con.close()
    (tmp_path / "meta.json").write_text(json.dumps({"refreshed_at": _ts(now), "hub_dumped_at": _ts(now)}))
    monkeypatch.setattr(hs, "snapshot_path", lambda: snap)
    monkeypatch.setattr(hs, "meta_path", lambda: tmp_path / "meta.json")
    monkeypatch.setattr(hs, "freshness_path", lambda: tmp_path / "freshness.json")
    monkeypatch.setattr(hs, "snapshot_is_fresh", lambda: (True, {"exists": True, "age_seconds": 5}))

    def no_hub(*_a, **_k):
        pytest.fail("the hub was reached")

    monkeypatch.setattr("khipu.embed.hybrid_search", no_hub)
    monkeypatch.setattr(hs, "try_hub_connect", no_hub)
    monkeypatch.setattr("khipu.db.connect", no_hub)
    monkeypatch.setattr(rp, "_cached_query_embed", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("offline")))
    return snap


def _pushed(**kw) -> list[str]:
    return [h["id"] for h in rp.prior_work_for_prompt(PROMPT, **kw)["hits"]]


def _found() -> list[str]:
    out = hs.search_stale_payload(PROMPT, 10)
    return [str(r["id"]) for r in out["results"] if r.get("kind") == "episode"]


def test_the_replica_still_holds_all_three_live(replica):
    assert set(_pushed()) == {"902", "903", "904"}
    assert set(_found()) == {"902", "903", "904"}


def test_a_forget_not_yet_tombstoned_in_the_replica_is_never_pushed_or_found(replica):
    tombstones.publish_episode_ids({902})
    assert sqlite3.connect(replica).execute(
        "SELECT deleted_at FROM episodes WHERE id = 902").fetchone() == (None,)
    pushed = _pushed()
    assert "902" not in pushed and "904" in pushed
    budgeted = _pushed(budget_ms=2000)
    assert "902" not in budgeted and "904" in budgeted
    found = _found()
    assert "902" not in found and "904" in found


def test_an_erasure_queued_episode_is_never_pushed_or_found(replica):
    assert hs._queue_erasure(903)
    assert "903" not in _pushed() and "904" in _pushed()
    assert "903" not in _found()


def test_an_unreadable_forget_list_pushes_nothing_from_the_replica(replica):
    path = tombstones.registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert _pushed() == []
    assert _pushed(budget_ms=2000) == []
    # Explicit search keeps its contract (test_forget_replica_reads): an
    # unreadable registry hides pending captures, not the replica's rows.


def test_the_forget_list_is_reread_after_every_publication(replica):
    tombstones.publish_episode_ids({903})
    assert tombstones.hidden_episode_ids() == frozenset({903})
    tombstones.publish_episode_ids({904})
    assert tombstones.hidden_episode_ids() == frozenset({903, 904})
    assert _pushed() == ["902"]


def test_a_row_reached_through_a_withheld_episode_is_dropped(replica):
    tombstones.publish_episode_ids({902})
    rows = [{"kind": "topic", "id": "t", "via": "episode:902"}, {"kind": "topic", "id": "u", "via": "episode:904"}]
    assert [r["id"] for r in hs.without_withheld(rows)] == ["u"]


def test_the_replica_deliverable_fence_reads_the_forget_list(replica):
    con = sqlite3.connect(replica)
    tombstones.publish_episode_ids({902})
    assert "902" in hs.forget_fence_sql(con, "deliverables", "episode_id")
    tombstones.registry_path().write_text("{not json")
    assert hs.forget_fence_sql(con, "deliverables", "episode_id") == " AND 0"
