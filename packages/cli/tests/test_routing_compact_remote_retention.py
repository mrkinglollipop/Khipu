"""Receipt retention uses the same fold rules, preserves owners and advances pulls."""

import json
from datetime import timedelta

from khipu import routing, routing_compact as rc, routing_replication as rr
from tests import routing_compact_synth as synth
from tests import test_routing_s11b_replication as replicas
from tests.test_routing_compact import END, KEYS

pair = replicas.pair


def _fold():
    return rc._normal(routing.fold(routing.read_events()[0], now=END,
                                  cfg=routing.settings(), known=routing.accounts()))


def test_remote_observations_are_bounded_and_refetch_cannot_restore_them(pair):
    a, b, hub = pair
    a.activate()
    a.now = b.now = hub.now = END
    rows = synth.stream_log(KEYS, END, observations=3000, others=0, owner=20, seed=17)
    with routing._lock():
        mac = routing._mac_locked()
    for n, row in enumerate(rows, 1):
        row.update(id=f'{n:032x}', seq=n, mac=mac)
    synth.write(routing.events_file(), rows)
    assert replicas.sync(a, hub)['ok']
    assert replicas.sync(b, hub)['ok']
    before = _fold()
    remote_before = rr._path('remote.jsonl').stat().st_size
    owners = {r['id'] for r in rr.remote_events() if rc._owner(r)}
    assert owners
    result = rc.compact(END)
    assert result['result'] == 'compacted', result
    kept = {r['id'] for r in rr.remote_events()}
    assert rr._path('remote.jsonl').stat().st_size < remote_before * .7
    assert owners <= kept
    assert _fold() == before
    assert rc.health()['bytes'] == sum(p.stat().st_size for p in (routing.events_file(), rr._path('remote.jsonl')) if p.exists())
    # Replay the whole paginated hub scan, including observations that just expired.
    state = rr._state()
    state.pop('pull_cursor', None)
    rr._save(state)
    outcome = replicas.sync(b, hub, limit=31, max_pages=500)
    assert outcome['ok'] and outcome['caught_up'], outcome
    assert {r['id'] for r in rr.remote_events()} == kept
    assert _fold() == before
    a.activate()
    routing.set_note('new owner words after retention', via='app')
    assert replicas.sync(a, hub)['ok']
    assert replicas.sync(b, hub)['ok']
    assert b.view()['note']['text'] == 'new owner words after retention'


def test_remote_copies_do_not_reinflate_a_compacted_local_fold(pair):
    a, _b, hub = pair
    a.activate()
    a.now = hub.now = END
    rows = synth.stream_log(KEYS, END, observations=3000, others=0, owner=20, seed=21)
    with routing._lock():
        mac = routing._mac_locked()
    for n, row in enumerate(rows, 1):
        row.update(id=f'{n:032x}', seq=n, mac=mac)
    synth.write(routing.events_file(), rows)
    assert replicas.sync(a, hub)['ok']
    before = _fold()
    assert rc.compact(END)['result'] == 'compacted'
    # Replicas lack local source details, so their retained sets can differ.
    # The complete merged fold must still fit the existing compaction target.
    assert len(routing.read_events()[0]) <= rc.LINES_TARGET
    assert _fold() == before


def test_expired_unsupported_samples_archive_without_losing_supported_unsent_rows(pair):
    from tests.test_routing_compact import _log

    a, _b, hub = pair
    a.activate()
    a.now = hub.now = END
    rows = _log(days=60)
    synth.write(routing.events_file(), rows)
    assert replicas.sync(a, hub)['ok']
    sample_ids = {row['id'] for row in rows if row['type'] == 'usage_sample'}
    owner_ids = {row['id'] for row in rows if rc._owner(row)}
    assert sample_ids <= set(rr._state()['deferred'])
    unsent = [dict(type='source_seen', id=f'unsent-{n}', seq=max(row['seq'] for row in rows) + n + 1, runner='unsent', status='{}',
                   written_at=routing._iso(END - timedelta(days=20)),
                   written_by={'kind': 'runner'}, via='runner') for n in range(6)]
    synth.append(routing.events_file(), unsent)
    outcome = rc.compact(END)
    assert outcome['result'] == 'compacted', outcome
    live = [event for _line, event in rc._rows(routing.events_file().read_bytes()) if event is not None]
    live_ids = {row.get('id') for row in live}
    archived = sample_ids - live_ids
    assert len(archived) > len(sample_ids) / 2
    assert not archived & set(rr._state()['deferred'])
    assert owner_ids | {row['id'] for row in unsent} <= live_ids
    assert rc.full_log() == [json.dumps(row, sort_keys=True).encode() for row in rows + unsent]
