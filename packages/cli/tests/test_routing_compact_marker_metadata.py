"""Generation metadata is neither a clock witness nor legacy identity history."""
from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests import test_routing_audit_fold as audit
from tests.test_routing_compact import END, _log

world = audit.world


def _clock_warnings(now=END):
    return [w for w in routing.doctor_block(now)['warnings'] if 'date and time' in w]


def test_compaction_does_not_create_a_doctor_clock_warning(world):
    world[0] = END
    synth.write(routing.events_file(), _log(days=60))
    before = _clock_warnings()
    assert before == []
    assert rc.compact(END)['result'] == 'compacted'
    assert _clock_warnings() == before


def test_modern_log_identity_is_unchanged_by_compaction(world):
    world[0] = END
    mac = 'a' * 32
    rows = _log(days=60)
    for row in rows:
        row['mac'] = mac
        row['hlc'] = {'wall': int(routing._parse(row['written_at']).timestamp() * 1000), 'counter': 0, 'mac': mac}
    synth.write(routing.events_file(), rows)
    (routing.routing_dir() / 'mac-id').write_text(mac + '\n')
    routing.read_view(END)
    def identity():
        return {name: (routing.routing_dir() / name).read_bytes()
                for name in ('mac-id', 'id-salt', 'operation-salt-only')
                if (routing.routing_dir() / name).exists()}
    before = identity()
    assert 'id-salt' not in before
    assert rc.compact(END)['result'] == 'compacted'
    assert identity() == before
    with routing._lock():
        stamp = routing._stamp_locked(END)[0]
        event = dict(stamp, type='note_set', text='after compaction', written_by={'kind': 'owner'}, via='app', auth='app')
        routing._identify_locked([event])
    assert 'legacy_mac' not in stamp
    assert identity() == before
