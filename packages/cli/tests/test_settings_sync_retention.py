"""Day-120 retention: a compacted log keeps the settings events, and a pull that
replays the hub's whole history never applies an old one again. Hermetic: two
disposable Macs and the in-memory hub of the replication tests."""
from datetime import timedelta

from khipu import owner_direction, routing, routing_compact as rc, routing_replication as rr
from tests import routing_compact_synth as synth
from tests import test_routing_s11b_replication as replicas
from tests.test_routing_compact import END, KEYS

pair = replicas.pair
KEY = "routing_summary_chars"


def _setting_ids(rows):
    return {r["id"] for r in rows if r.get("type") == "setting_set"}


def _fill_with_old_samples(mac, now):
    """A Mac with four months of probe samples behind it, all already sent."""
    rows = synth.synth(KEYS, now, days=125, step_min=240, seed=5, owner=False)
    with routing._lock():
        local = routing._mac_locked()
    start = max((r.get("seq", 0) for r in routing._read_event_lines()[0]), default=0)
    for n, row in enumerate(rows, start + 1):
        row.update(id=f"{n:032x}", seq=n, mac=local)
    synth.append(routing.events_file(), rows)
    state = rr._state()
    state.setdefault("uploaded", {})[local] = start + len(rows)
    state["mac"] = local
    rr._save(state)


def test_day_120_compaction_keeps_settings_events_and_never_reapplies_old_ones(pair):
    a, b, hub = pair
    old = END - timedelta(days=130)
    for mac in (a, b):
        mac.now = old
    hub.now = old
    a.activate()
    routing.set_setting(KEY, "900")
    assert replicas.sync(a, hub)["ok"] and replicas.sync(b, hub)["ok"]
    b.activate()
    from khipu.config import load_config
    assert load_config()[KEY] == 900
    (applied,) = [x for x in owner_direction.read_log() if x.get("kind") == "action" and x.get("target") == KEY]

    # The owner takes it back on B, then four months pass.
    assert owner_direction.undo(applied["id"], "app").get("ok", True)
    assert KEY not in load_config()
    for mac in (a, b):
        mac.now = END
    hub.now = END
    _fill_with_old_samples(b, END)
    assert replicas.sync(b, hub)["ok"]
    kept_before = _setting_ids(rr.remote_events()) | _setting_ids(routing._read_event_lines()[0])
    assert kept_before

    out = rc.compact(END)
    assert out["result"] == "compacted", out
    kept_after = _setting_ids(rr.remote_events()) | _setting_ids(routing._read_event_lines()[0])
    assert kept_before <= kept_after

    # A pull that replays the hub's whole history again does not bring the old value back.
    state = rr._state()
    state.pop("pull_cursor", None)
    state.pop("scan_after", None)
    rr._save(state)
    outcome = replicas.sync(b, hub, limit=31, max_pages=500)
    assert outcome["ok"] and outcome["caught_up"], outcome
    assert KEY not in load_config()
    assert len([x for x in owner_direction.read_log() if x.get("kind") == "action" and x.get("target") == KEY]) == 1
