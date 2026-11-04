"""Receipt retention uses the same fold rules, preserves owners and advances pulls."""

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
