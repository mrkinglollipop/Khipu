"""Future topic event dates must not exceed the documented recency ceiling."""
import json
from datetime import datetime, timedelta, timezone

from khipu import db, embed, features, hub_snapshot
from khipu.recency import RECENCY_WEIGHT, apply_recency
from tests.hermetic_pg import hub, pg_cluster


def test_future_date_recency_bonus_is_bounded():
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    rows = [
        {"id": "relevant-current", "score": .15, "ts": now},
        {"id": "weak-future", "score": .01, "ts": now + timedelta(days=365)},
    ]
    out = apply_recency(rows, now=now)
    print(json.dumps(out, default=str))
    assert max(r["recency"] for r in out) <= RECENCY_WEIGHT + 0.000001


def test_explicit_hub_future_event_exceeds_recency_ceiling(hub, monkeypatch):
    monkeypatch.setattr(db, "connect", lambda **kw: hub.connect())
    monkeypatch.setattr(hub_snapshot, "try_hub_connect", lambda **kw: hub.connect())
    monkeypatch.setattr(features, "enabled", lambda _: False)
    conn = hub.connect()
    conn.execute("INSERT INTO topics(slug,title,body,event_at) VALUES "
                 "('future-note','Zephyr','zephyr synthetic event',now() + interval '365 days')")
    conn.commit()
    out = embed.hybrid_search("zephyr", mode="literal", kind="topic", include_libraries=False)
    print(json.dumps(out, default=str))
    assert len(out["results"]) == 1
    assert out["results"][0]["recency"] <= RECENCY_WEIGHT + 0.000001
