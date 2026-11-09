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
        assert "own history" in health['note'] and "nothing is broken" in health['note'] and "nothing to do" in health['note']
        assert 'warning' not in health


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


def test_health_names_next_eligible_attempt_and_never_blames_old_code(world, monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr(rc, "log_size", lambda: {"lines": 5000, "bytes": 4 << 20})
    monkeypatch.setattr(rc, "_droppable", lambda *a: (400, 800))
    monkeypatch.setattr(rc, "_stored_rows", lambda: ([], []))
    for generation in (None, rc.ATTEMPT_GENERATION):
        rc._write_state({"attempted_at": routing._iso(END), "result": "failed: verification failed",
                         "attempt_generation": generation})
        warning = rc.health(END + timedelta(hours=1))["warning"]
        assert "2026-11-02T18:00:00Z" in warning
        assert ("and failed" in warning) == (generation == rc.ATTEMPT_GENERATION)
        assert "may be failing" not in warning
    rc._write_state({})
    warning = rc.health(END)["warning"]
    assert "has not run under this code yet" in warning and "time is not known" in warning


def test_a_busy_compactor_is_waiting_and_an_exception_is_a_failed_attempt(world, monkeypatch):
    def busy(now):
        raise routing.LockBusy("busy")
    monkeypatch.setattr(rc, "_compact_remote", busy)
    assert rc.compact(END)["result"] == "skipped"
    assert rc._read_state()["result"] == "skipped: the routing record is busy"
    def broken(now):
        raise ValueError("bad input")
    monkeypatch.setattr(rc, "_compact_remote", broken)
    assert rc.compact(END)["result"] == "skipped"
    assert rc._read_state()["result"] == "failed: ValueError"
