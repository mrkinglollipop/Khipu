"""The five follow-up retention findings, on disposable routing data."""

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests import test_routing_audit_fold as audit
from tests.test_routing_compact import END

world = audit.world


def test_owner_only_growth_is_yellow_with_nothing_to_do(world, monkeypatch):
    monkeypatch.setattr(rc, 'LINES_YELLOW', 10)
    monkeypatch.setattr(rc, 'LINES_RED', 20)
    rows = [dict(type='note_set', text=str(n), id=f'owner-{n}', seq=n + 1,
                 written_at=routing._iso(END), written_by={'kind': 'owner'}, via='app', auth='app') for n in range(30)]
    synth.write(routing.events_file(), rows)
    for attempted in (False, True):
        if attempted:
            assert rc.compact(END)['result'] == 'skipped'
        health = rc.health()
        assert health['level'] == 'yellow'
        assert "own history" in health['warning'] and "nothing is broken" in health['warning'] and "nothing to do" in health['warning']


def test_red_byte_bound_is_reachable_below_red_line_bound(world, monkeypatch):
    rows = [dict(type='source_seen', id=f'large-{n}', seq=n + 1, runner='r', status='x' * 4096,
                 written_at=routing._iso(END), written_by={'kind': 'runner'}, via='runner') for n in range(800)]
    synth.write(routing.events_file(), rows)
    monkeypatch.setattr(rc, '_droppable', lambda: (400, 800))
    health = rc.health()
    assert health['lines'] < rc.LINES_RED
    assert health['bytes'] > rc.BYTES_RED
    assert health['level'] == 'red'
    assert rc.LINES_RED == 4000 and rc.BYTES_RED == 3 << 20
