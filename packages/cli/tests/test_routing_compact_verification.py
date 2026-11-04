"""Compatibility, concurrent publication and measured synthetic retention evidence."""
import importlib.util
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests.test_routing_compact import END, KEYS, _log
from tests import test_routing_audit_fold as audit
from tests import test_routing_s11b_replication as replication_fixture

world = audit.world
pair = replication_fixture.pair


def test_two_compactors_and_a_locked_append_preserve_every_line(world, monkeypatch):
    synth.write(routing.events_file(), _log(days=60))
    original = routing.events_file().read_bytes().splitlines()
    barrier = threading.Barrier(3)
    choose = rc._choose
    def paused(data, now):
        result = choose(data, now)
        barrier.wait(timeout=60)
        return result
    monkeypatch.setattr(rc, '_choose', paused)
    late = dict(type='note_set', text='concurrent owner', id='concurrent-owner', seq=999999,
                written_at=routing._iso(END), written_by={'kind': 'owner'}, via='app', auth='app')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(rc.compact, END) for _ in range(2)]
        with routing._lock():
            synth.append(routing.events_file(), [late])
        barrier.wait(timeout=60)
        results = [future.result(timeout=90) for future in futures]
    assert sum(r['result'] == 'compacted' for r in results) == 1, results
    assert any('changed' in r.get('why', '') for r in results), results
    assert rc.full_log() == original + [json.dumps(late, sort_keys=True).encode()]


@pytest.fixture
def old_reader(tmp_path):
    source = subprocess.check_output(['git', 'show', '1613a16d:packages/cli/khipu/routing.py'],
                                     cwd=Path(__file__).resolve().parents[3])
    old_path = tmp_path / 'old_routing.py'
    old_path.write_bytes(source)
    spec = importlib.util.spec_from_file_location('khipu._routing_1613a16d', old_path)
    old = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = old
    try:
        spec.loader.exec_module(old)
        yield old
    finally:
        sys.modules.pop(spec.name, None)


def test_1613a16d_reader_folds_compacted_log_identically(world, old_reader):
    old = old_reader
    synth.write(routing.events_file(), _log(days=60))
    def folded():
        events, bad = old.read_events()
        assert bad == 0
        return [rc._normal(old.fold(events, now=END + timedelta(days=d), cfg=old.settings(), known=old.accounts()))
                for d in (0, 1, 9, 60)]
    before = folded()
    assert rc.compact(END)['result'] == 'compacted'
    assert folded() == before


@pytest.mark.parametrize("replication", [False, True])
def test_day_120_stream_figures_and_owner_rate(world, record_property, replication):
    start = END - timedelta(days=120)
    stream = synth.Stream(KEYS, start, seed=7, runners=1, lanes=4)
    rows = stream.until(END)
    owners = sum(r.get('written_by', {}).get('kind') == 'owner' for r in rows)
    synth.write(routing.events_file(), rows)
    if replication:
        from khipu import routing_replication as rr
        from khipu.config import config_file

        config = json.loads(config_file().read_text())
        config["routing_replication_live"] = True
        config_file().write_text(json.dumps(config))
        (routing.routing_dir() / "mac-id").write_text(stream.mac + "\n")
        bindings, _forwards, _aliases = routing.account_identity.state(routing.read_events()[0], routing.accounts())
        projected = [row for row in routing.replication_payload(rows, bindings=bindings) if row.get("id")]
        deferred = {row["id"]: row["type"] for row in rows if row["type"] not in routing._REPLICATION_FIELDS}
        # A bulk post-sync fixture avoids benchmarking per-receipt disk writes.
        # Pair tests exercise actual paginated sync and the completed watermark.
        rr._store([dict(payload=row, event_id=row["id"], hub_order=n,
                        received_at=routing._iso(END), rule_number=None)
                   for n, row in enumerate(projected, 1)])
        rr._save(dict(mac=stream.mac, uploaded={stream.mac: max(row["seq"] for row in rows)},
                      completed_order=len(projected), caught_up_at=routing._iso(END), deferred=deferred))
    before = rc.log_size()
    world[0] = END
    out = rc.compact(END)
    assert out['result'] == 'compacted', out
    after = rc.log_size()
    times = []
    for _ in range(3):
        began = time.perf_counter()
        rc._Log(routing.events_file().read_bytes().splitlines(), routing._inputs()).at(END)
        times.append((time.perf_counter() - began) * 1000)
    archive_bytes = sum(p.stat().st_size for _, p in rc._archives())
    remote_path = routing.routing_dir() / "remote.jsonl"
    figures = dict(seed=7, days=120, replication=replication, before=before, after=after, fold_ms=times,
                   local_lines=rc._lines(routing.events_file().read_bytes()),
                   remote_lines=rc._lines(remote_path.read_bytes()) if remote_path.exists() else 0,
                   merged_lines=len(routing.read_events()[0]),
                   deferred_lines=len(rr._state().get("deferred", {})) if replication else 0,
                   archive_bytes=archive_bytes, owner_lines=owners, owner_lines_per_day=owners / 120,
                   configured_owner_lines_per_day=stream.owner_per_day)
    print('DAY120 ' + json.dumps(figures, sort_keys=True))
    record_property('day120', json.dumps(figures))
    assert rc.full_log() == [json.dumps(row, sort_keys=True).encode() for row in rows]
    assert owners > 0 and after['lines'] < before['lines']


def test_replica_sync_survives_source_compaction_and_later_owner_write(pair, old_reader):
    from tests.test_routing_s11b_replication import sync
    a, b, hub = pair
    a.activate()
    routing.add_rule("protected replica rule", via="app")
    # Upload before compacting: unsent local observations are protected too.
    assert sync(a, hub)["ok"]
    a.now = END
    mac = routing._local_mac()
    rows = synth.stream_log(KEYS, END, observations=4800, others=0, owner=20, seed=17)
    for n, row in enumerate(rows, 10):
        row.update(id=f"{n:032x}", seq=n, mac=mac)
    synth.append(routing.events_file(), rows)
    assert sync(a, hub)["ok"]
    assert sync(b, hub)["ok"]
    b.now = END
    def replica_fold(reader=routing):
        # Persisted view revisions advance with sync receipts. The pure fold
        # compares all routing semantics at one fixed clock, including remote events.
        events, bad = reader.read_events()
        assert bad == 0
        return rc._normal(reader.fold(events, now=END, cfg=reader.settings(), known=reader.accounts()))
    before = (replica_fold(), replica_fold(old_reader))
    a.activate()
    outcome = rc.compact(END)
    assert outcome["result"] == "compacted", outcome
    uploaded = len(hub.rows)
    assert sync(a, hub)["ok"]
    assert len(hub.rows) == uploaded
    assert sync(b, hub)["ok"]
    assert (replica_fold(), replica_fold(old_reader)) == before
    a.activate()
    routing.set_note("after compaction", via="app")
    assert sync(a, hub)["ok"]
    assert sync(b, hub)["ok"]
    assert b.view()["note"]["text"] == "after compaction"
