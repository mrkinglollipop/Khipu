"""The hub's copy of pace readings stays bounded, run for real.

A throwaway PostgreSQL (tests/hermetic_pg.py) through the real ``PostgresHub``:
no fake hub, no live data."""
from __future__ import annotations

import json
from datetime import timedelta

import psycopg
import pytest

from khipu import db, routing, routing_replication as rr
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests import test_routing_s11b_replication as _s11b
from tests.test_routing_acceptance import T0

pair = _s11b.pair
ME, OTHER = "a" * 32, "b" * 32


@pytest.fixture
def pg(hub, monkeypatch):  # noqa: F811
    monkeypatch.setattr(db, "connect", lambda **_kw: psycopg.Connection.connect(hub.dsn))
    conn = hub.connect()
    hub.migrate(conn=conn)
    conn.commit()
    conn.close()
    return hub


def row(event_id, mac, kind, days_old):
    return (event_id, mac, json.dumps({"id": event_id, "type": kind, "mac": mac}), days_old)


def seed(pg, rows):
    with pg.connect() as conn:
        for event_id, mac, payload, days_old in rows:
            conn.execute("INSERT INTO routing_events(event_id, sender_mac, payload, received_at) "
                         "VALUES (%s, %s, %s::jsonb, clock_timestamp() - make_interval(secs => %s))",
                         (event_id, mac, payload, days_old * 86400))
        conn.commit()


def ids(pg):
    with pg.connect() as conn:
        return {r[0] for r in conn.execute("SELECT event_id FROM routing_events").fetchall()}


def test_only_this_senders_own_old_samples_go(pg):
    seed(pg, [row("old-mine", ME, "usage_sample", 4), row("new-mine", ME, "usage_sample", 2),
              row("old-theirs", OTHER, "usage_sample", 9), row("old-rule", ME, "rule_add", 40),
              row("old-cap", ME, "cap_set", 40), row("old-observation", ME, "observation", 40),
              row("old-hold", ME, "hold", 40)])
    assert rr.PostgresHub().prune_samples(ME) == 1
    assert ids(pg) == {"new-mine", "old-theirs", "old-rule", "old-cap", "old-observation", "old-hold"}
    assert rr.PostgresHub().prune_samples(ME) == 0


def test_one_prune_is_bounded_and_the_next_finishes(pg, monkeypatch):
    monkeypatch.setattr(rr, "HUB_PRUNE_MOST", 2)
    seed(pg, [row(f"old-{i}", ME, "usage_sample", 5) for i in range(5)])
    assert rr.PostgresHub().prune_samples(ME) == 2
    assert rr.PostgresHub().prune_samples(ME) == 2
    assert rr.PostgresHub().prune_samples(ME) == 1
    assert ids(pg) == set()


def test_a_pull_cursor_survives_the_rows_behind_and_ahead_of_it_going(pg):
    seed(pg, [row(f"old-{i}", ME, "usage_sample", 6 - i * 0.1) for i in range(4)] + [
        row("old-rule", ME, "rule_add", 5.8), row("new-rule", OTHER, "rule_add", 1)])
    hub_ = rr.PostgresHub()
    first = hub_.pull([rr.EPOCH, ""], None, 2)
    assert not first["complete"] and len(first["rows"]) == 2
    cursor = [first["rows"][-1]["received_at"], first["rows"][-1]["event_id"]]
    assert hub_.prune_samples(ME) == 4  # the row the cursor names is one of them
    seen, after = [], cursor
    for _ in range(5):
        page = hub_.pull(after, first["until"], 2)
        seen += [r["event_id"] for r in page["rows"]]
        if page["complete"]:
            break
        after = [page["rows"][-1]["received_at"], page["rows"][-1]["event_id"]]
    assert page["complete"] and {"old-rule", "new-rule"} <= set(seen) | {r["event_id"] for r in first["rows"]}


def test_a_senders_status_no_longer_recounts_its_history(pg):
    hub_ = rr.PostgresHub()
    hub_.upload(ME, {"id": "e1", "type": "rule_add", "mac": ME})
    with pg.connect() as conn:
        conn.execute("UPDATE routing_sources SET event_count = 1000 WHERE mac = %s", (ME,))
        conn.commit()
    receipt = hub_.upload(ME, {"id": "e2", "type": "rule_add", "mac": ME})
    assert hub_.upload(ME, {"id": "e2", "type": "rule_add", "mac": ME}) == receipt  # a retry counts once
    with pg.connect() as conn:
        count, order, last = conn.execute("SELECT event_count, last_hub_order, last_event_id FROM routing_sources "
                                          "WHERE mac = %s", (ME,)).fetchone()
    assert count == 1001 and order == receipt["hub_order"] and last == "e2"
    assert [s for s in hub_.pull([rr.EPOCH, ""], None, 10)["sources"] if s["mac"] == ME][0]["event_count"] == 1001


class Counting:
    """The in-memory hub, with what a sync asks it to prune."""

    def __init__(self, hub_):
        self.inner, self.pruned = hub_, []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def prune_samples(self, mac):
        self.pruned.append(mac)
        return 0


def _reading(at):
    return {"type": "usage_sample", "account": "codex|", "window": "weekly", "witness_at": routing._iso(at),
            "used_pct": 10.0, "t_err": 60, "source": "probe", "resets_at": routing._iso(T0 + timedelta(days=3)),
            "written_by": {"kind": "probe"}, "via": "probe"}


def test_a_sync_prunes_its_own_at_most_hourly_and_only_after_sending_readings(pair):
    a, _b, memory = pair
    hub_ = Counting(memory)
    a.activate()
    routing.add_rule("no readings in this one", via="app")
    assert _s11b.sync(a, hub_)["ok"] and hub_.pruned == []
    a.write(_reading(a.now - timedelta(hours=1)))
    assert _s11b.sync(a, hub_)["ok"] and hub_.pruned == [routing._local_mac()]
    a.write(_reading(a.now - timedelta(minutes=40)))
    a.now += timedelta(minutes=30)
    assert _s11b.sync(a, hub_)["ok"] and len(hub_.pruned) == 1
    a.write(_reading(a.now - timedelta(minutes=30)))
    a.now += timedelta(hours=1)
    assert _s11b.sync(a, hub_)["ok"] and len(hub_.pruned) == 2


def test_a_prune_that_fails_never_fails_the_sync(pair):
    a, _b, memory = pair

    class Broken(Counting):
        def prune_samples(self, mac):
            raise OSError("SENTINEL-PRIVATE-CONNECTION")

    a.activate()
    a.write(_reading(a.now - timedelta(hours=1)))
    assert _s11b.sync(a, Broken(memory))["ok"]


def test_a_reading_as_old_as_the_hub_keeps_can_never_be_sent_again():
    from khipu import routing_pace

    assert rr.HUB_SAMPLE_KEPT_S > routing_pace.WIRE_OLDEST_S
