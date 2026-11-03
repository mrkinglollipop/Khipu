"""Compaction metadata must not acknowledge an unsent legacy owner event."""
from datetime import timedelta

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests import test_routing_s11b_replication as replicas
from tests.test_routing_compact import END

pair = replicas.pair


def test_generation_marker_cannot_advance_the_legacy_upload_cursor(pair):
    a, b, hub = pair
    a.activate()
    a.configure(routing_replication_live=False)
    a.now = hub.now = END
    rows = [dict(type='note_set', id='legacy-first-owner', seq=1, text='protected legacy note',
                 written_at=routing._iso(END - timedelta(days=60)),
                 written_by={'kind': 'owner'}, via='app', auth='app')]
    for n in range(2, 11):
        at = END if n == 10 else END - timedelta(days=60, minutes=-n)
        rows.append(dict(type='source_seen', id=f'legacy-source-{n}', seq=n, runner='r', status='{}',
                         witness_at=routing._iso(at), written_at=routing._iso(at),
                         written_by={'kind': 'runner'}, via='runner'))
    synth.write(routing.events_file(), rows)
    outcome = rc.compact(END)
    assert outcome['result'] == 'compacted', outcome
    a.configure(routing_replication_live=True)
    assert replicas.sync(a, hub)['ok']
    assert 'legacy-first-owner' in hub.rows
    assert replicas.sync(b, hub)['ok']
    b.now = END
    assert b.view()['note']['text'] == 'protected legacy note'
