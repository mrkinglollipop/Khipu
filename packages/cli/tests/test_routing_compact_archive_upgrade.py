"""Legacy full-copy archive pruning cannot poison a complete upgraded chain."""
import json
import warnings
from datetime import timedelta

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests import test_routing_audit_fold as audit
from tests.test_routing_compact import END, _log

world = audit.world


def _clock_warnings(now):
    return [w for w in routing.doctor_block(now)['warnings'] if 'date and time' in w]


def test_old_full_copy_pruning_does_not_poison_a_complete_upgrade_chain(world):
    world[0] = END
    rows = _log(days=60)
    synth.write(routing.events_file(), rows)
    expected = routing.events_file().read_bytes().splitlines()
    rc.archive_dir().mkdir()
    for day in range(1, 7):
        (rc.archive_dir() / f'events-2020010{day}T120000Z-aaaaaa.jsonl').write_bytes(routing.events_file().read_bytes())
    assert rc.prune_archives(END) > 0
    with warnings.catch_warnings(record=True) as found:
        warnings.simplefilter('always')
        assert rc.full_log() == expected
    assert not found, [str(w.message) for w in found]
    assert not rc._read_state().get('archive_retention_expired')
    assert rc.compact(END)['result'] == 'compacted'
    later = END + timedelta(days=60)
    tail = _log(seed=9, days=59, end=later)
    # Distinct ids and sequences across the upgrade's two compactions.
    for n, row in enumerate(tail, len(rows) + 1):
        row.update(id=f'upgrade-{n}', seq=n)
    synth.append(routing.events_file(), tail)
    expected += [json.dumps(row, sort_keys=True).encode() for row in tail]
    world[0] = later
    assert rc.compact(later)['result'] == 'compacted'
    with warnings.catch_warnings(record=True) as found:
        warnings.simplefilter('always')
        assert rc.full_log() == expected
    assert not found, [str(w.message) for w in found]
    assert not _clock_warnings(later)
    assert routing.doctor_block(later)['log']['level'] == 'green'
