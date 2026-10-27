import hashlib
import json
import pytest
from copy import deepcopy
from tests.test_outbox_legacy_projection import world, queue, projection, ORIGINAL
from khipu import capture, outbox, mirror, tombstones, paths


def test_held_conflict_stays_held_on_recovery(world, monkeypatch):
    monkeypatch.setattr(outbox, 'MAX_ATTEMPTS', 1)
    payload = {**projection(), 'preferences': ['real additional evidence']}
    mirror._queue_conflict(payload)
    assert outbox.drain().get('buried') == 1
    assert outbox.status()['held_conflicts'] == 1
    mirror._queue_conflict(payload)
    assert not outbox.jobs(), 'Recovery recreated held conflict with attempts=0'


def test_resolved_projection_is_terminal_on_recovery(world):
    mirror._queue_conflict(projection())
    assert outbox.drain().get('resolved') == 1
    first_records = list(outbox.resolved_dir().glob('*.json'))
    mirror._queue_conflict(projection())
    outbox.drain()
    assert len(list(outbox.resolved_dir().glob('*.json'))) == len(first_records), 'Same file projection creates another resolution record on each recovery'


def test_conflicting_resolution_record_has_bounded_retries(world, monkeypatch):
    monkeypatch.setattr(outbox, 'MAX_ATTEMPTS', 1)
    path = queue()
    before = path.read_bytes()
    outbox.resolved_dir().mkdir(parents=True)
    dst = outbox.resolved_dir() / (hashlib.sha256(before).hexdigest() + '.json')
    dst.write_text('{}')
    assert outbox.drain().get('resolution_failed') == 1
    assert not path.exists(), 'Permanent journal conflict repeats with attempts=0 and no owner hold'
    held = json.loads(outbox.dead_jobs()[0].read_bytes())
    assert held['attempts'] == 1
    assert held['payload'] == projection()
    assert held['resolution_evidence']['payload'] == ORIGINAL
    assert 'owner' in held['hold_reason'].lower()
    assert dst.read_bytes() == b'{}'
    assert outbox.status()['held_conflicts'] == 1


def test_restore_checks_nested_queued_identity(world):
    queue()
    assert outbox.drain().get('resolved') == 1
    data = next(outbox.resolved_dir().glob('*.json')).read_bytes()
    tombstones.publish_payloads([projection()])
    gate = tombstones.read_gate()
    assert not gate.allows(projection())
    assert gate.allows(ORIGINAL)
    assert paths._job_refusal(data, gate) == 'forgotten', 'Restore accepts forgotten queued_evidence because it checks only original payload'


def test_existing_record_must_match_canonical_payload(world):
    cur, _ = world
    cur.episodes[1]['raw']['future_evidence'] = 1
    queued = {**projection(), 'future_evidence': 1}
    path = queue(queued)
    job = json.loads(path.read_bytes())
    original = deepcopy(cur.episodes[1]['raw'])
    from khipu.capture_identity import payload_sha256
    record = {'payload': {**original, 'future_evidence': True}, 'queued_evidence': job,
              'resolution': {'kind': 'legacy_comma_projection', 'episode_id': 1,
                             'original_payload_sha256': payload_sha256(original),
                             'queued_payload_sha256': payload_sha256(queued), 'resolved_at': 'earlier'}}
    outbox.resolved_dir().mkdir(parents=True)
    dst = outbox.resolved_dir() / (hashlib.sha256(path.read_bytes()).hexdigest() + '.json')
    dst.write_text(json.dumps(record))
    report = outbox.drain()
    assert report.get('resolution_failed') == 1
    assert path.exists() or outbox.dead_jobs(), f'Different original evidence accepted as equal: {report}'


def _recovery(world, monkeypatch, tmp_path, payload):
    from khipu import provenance, drift
    cur, original = world
    monkeypatch.setattr(provenance, 'ready', lambda cur: True)
    monkeypatch.setattr(capture, '_refusals_settled', lambda cur: None)
    monkeypatch.setattr(drift, 'episodes_missing_in_pg', lambda *a, **kw: [])
    execute = cur.execute
    def sql(statement, params=()):
        if statement.startswith('SELECT v.ts, v.md,'):
            cur._result = [(original['ts'], hashlib.md5(original['summary'].encode()).hexdigest(),
                            None, cur.episodes[1]['raw'],
                            *[original.get(k) for k in mirror._FACT_KEYS], *([[]] * 5))]
            return
        return execute(statement, params)
    monkeypatch.setattr(cur, 'execute', sql)
    root = tmp_path / 'memory'
    root.mkdir()
    (root / 'episodes.jsonl').write_text(json.dumps(payload) + '\n')
    return lambda: mirror.sync_recent_episodes(root)


def test_full_recovery_requeues_held_conflict(world, monkeypatch, tmp_path):
    monkeypatch.setattr(outbox, 'MAX_ATTEMPTS', 1)
    recover = _recovery(world, monkeypatch, tmp_path, {**projection(), 'preferences': ['extra']})
    assert recover()['conflicts'] == 1
    assert outbox.drain().get('buried') == 1
    recover()
    assert not outbox.jobs(), 'Full sync_recent_episodes recreated held job'


def test_full_recovery_does_not_queue_a_landed_line(world, monkeypatch, tmp_path):
    recover = _recovery(world, monkeypatch, tmp_path, projection())
    assert recover().get('conflicts', 0) == 0
    assert not outbox.jobs() and not list(outbox.resolved_dir().glob('*.json'))


def test_full_import_restores_forgotten_nested_payload(world, tmp_path):
    queue()
    assert outbox.drain().get('resolved') == 1
    record = next(outbox.resolved_dir().glob('*.json'))
    backup = tmp_path / 'backup'
    target = backup / 'outbox' / 'resolved' / record.name
    target.parent.mkdir(parents=True)
    target.write_bytes(record.read_bytes())
    tombstones.publish_payloads([projection()])
    report = paths.import_local(source=backup)
    destination = paths.data_dir() / 'outbox' / 'resolved' / record.name
    assert not destination.exists(), f'Forgotten queued payload restored: {report}'


def test_forget_scans_nested_queued_identity(world, tmp_path, monkeypatch):
    from tests.test_forget_write_gate import _complete_forget
    queue()
    assert outbox.drain().get("resolved") == 1
    record = next(outbox.resolved_dir().glob("*.json"))
    temp = record.parent / ".nested.synthetic.tmp"
    temp.write_bytes(record.read_bytes())
    _complete_forget(tmp_path, monkeypatch, projection())
    assert not record.exists() and not temp.exists()


def test_queued_forget_after_proof_prevents_publication(world, monkeypatch):
    path = queue()
    write = capture.write_pg
    def proof_then_forget(*args, **kwargs):
        result = write(*args, **kwargs)
        assert result.get("legacy_projection_duplicate")
        tombstones.publish_payloads([projection()])
        return result
    monkeypatch.setattr(capture, "write_pg", proof_then_forget)
    assert outbox.drain().get("forgotten") == 1
    assert not path.exists() and not list(outbox.resolved_dir().glob("*.json"))


@pytest.mark.parametrize("malformed", [None, [], {}, {"payload": []}])
def test_restore_refuses_malformed_nested_archives(world, malformed):
    envelope = {"payload": ORIGINAL, "queued_evidence": malformed}
    assert paths._job_refusal(json.dumps(envelope).encode(), tombstones.read_gate()) == "unreadable"


@pytest.mark.parametrize("field", ["original_payload_sha256", "queued_payload_sha256"])
def test_forged_journal_hashes_are_rejected(world, field):
    from khipu.capture_identity import payload_sha256
    path = queue()
    job = json.loads(path.read_bytes())
    record = {"payload": ORIGINAL, "queued_evidence": job,
              "resolution": {"kind": "legacy_comma_projection", "episode_id": 1,
                             "original_payload_sha256": payload_sha256(ORIGINAL),
                             "queued_payload_sha256": payload_sha256(projection()),
                             "resolved_at": "earlier"}}
    record["resolution"][field] = "0" * 64
    outbox.resolved_dir().mkdir(parents=True)
    dst = outbox.resolved_dir() / (hashlib.sha256(path.read_bytes()).hexdigest() + ".json")
    dst.write_text(json.dumps(record))
    before = dst.read_bytes()
    assert outbox.drain().get("resolution_failed") == 1
    assert dst.read_bytes() == before


@pytest.mark.parametrize("section", ["queued", "resolution"])
def test_python_equal_journal_values_remain_distinct(world, section):
    from khipu.capture_identity import payload_sha256
    path = queue()
    job = json.loads(path.read_bytes())
    record = {"payload": ORIGINAL, "queued_evidence": job,
              "resolution": {"kind": "legacy_comma_projection", "episode_id": 1,
                             "original_payload_sha256": payload_sha256(ORIGINAL),
                             "queued_payload_sha256": payload_sha256(projection()),
                             "resolved_at": "earlier"}}
    if section == "queued":
        # 0 == False in Python, but the original envelope has integer attempts.
        record["queued_evidence"]["attempts"] = False
    else:
        record["resolution"]["episode_id"] = True
    outbox.resolved_dir().mkdir(parents=True)
    dst = outbox.resolved_dir() / (hashlib.sha256(path.read_bytes()).hexdigest() + ".json")
    dst.write_text(json.dumps(record))
    assert outbox.drain().get("resolution_failed") == 1


def test_resolution_failures_are_durable_and_bounded_with_both_payloads(world, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 2)
    path = queue()
    before = json.loads(path.read_bytes())
    atomic = outbox._atomic_write
    def fail_record(dst, value):
        if dst.parent == outbox.resolved_dir():
            raise OSError("synthetic journal failure")
        return atomic(dst, value)
    monkeypatch.setattr(outbox, "_atomic_write", fail_record)
    assert outbox.drain().get("resolution_failed") == 1
    updated = json.loads(path.read_bytes())
    assert updated["attempts"] == 1
    assert updated["resolution_evidence"]["payload"] == ORIGINAL
    assert updated["resolution_evidence"]["queued_evidence"] == before
    assert outbox.drain().get("buried") == 1
    held = json.loads(outbox.dead_jobs()[0].read_bytes())
    assert held["attempts"] == 2 and held["resolution_evidence"] == updated["resolution_evidence"]
    assert outbox.drain()["jobs"] == 0


@pytest.mark.parametrize("forgotten", ["queued", "original"])
def test_retry_dead_checks_both_resolution_identities(world, monkeypatch, forgotten):
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 1)
    queue()
    atomic = outbox._atomic_write
    def fail_record(dst, value):
        if dst.parent == outbox.resolved_dir():
            raise OSError("synthetic journal failure")
        return atomic(dst, value)
    with monkeypatch.context() as patch:
        patch.setattr(outbox, "_atomic_write", fail_record)
        assert outbox.drain().get("buried") == 1
    tombstones.publish_payloads([projection() if forgotten == "queued" else ORIGINAL])
    assert outbox.retry_dead()["moved"] == 0
    assert not outbox.jobs() and not outbox.dead_jobs()


@pytest.mark.parametrize("forgotten", ["queued", "original"])
def test_resolution_failure_rechecks_forget_before_retry_publication(world, monkeypatch, forgotten):
    from contextlib import contextmanager
    path = queue()
    atomic = outbox._atomic_write
    def fail_record(dst, value):
        if dst.parent == outbox.resolved_dir():
            raise OSError("synthetic journal failure")
        return atomic(dst, value)
    guard = tombstones.write_guard
    calls = []
    @contextmanager
    def forget_after_failed_publication(*payloads):
        calls.append(payloads)
        number = len(calls)
        try:
            with guard(*payloads) as allowed:
                yield allowed
        finally:
            if number == 2:
                tombstones.publish_payloads([projection() if forgotten == "queued" else ORIGINAL])
    monkeypatch.setattr(tombstones, "write_guard", forget_after_failed_publication)
    monkeypatch.setattr(outbox, "_atomic_write", fail_record)
    assert outbox.drain().get("forgotten") == 1
    assert len(calls) == 3
    assert not path.exists() and not outbox.dead_jobs()


def test_recovery_matches_strict_evidence_and_preserves_owner_retry(world, monkeypatch):
    monkeypatch.setattr(outbox, "MAX_ATTEMPTS", 1)
    first = {**projection(), "future_evidence": 1}
    mirror._queue_conflict(first)
    assert outbox.drain().get("buried") == 1
    held = outbox.dead_jobs()[0].read_bytes()
    mirror._queue_conflict({**first, "future_evidence": True})
    assert len(outbox.jobs()) == 1
    assert outbox.dead_jobs()[0].read_bytes() == held
    assert outbox.retry_dead()["moved"] == 1
    assert len(outbox.jobs()) == 2


def test_failed_publication_then_success_reuses_original_journal_identity(world, monkeypatch):
    path = queue()
    original_bytes = path.read_bytes()
    original_job = json.loads(original_bytes)
    atomic = outbox._atomic_write
    def fail_record(dst, value):
        if dst.parent == outbox.resolved_dir():
            raise OSError("synthetic journal failure")
        return atomic(dst, value)
    with monkeypatch.context() as patch:
        patch.setattr(outbox, "_atomic_write", fail_record)
        assert outbox.drain().get("resolution_failed") == 1
    assert json.loads(path.read_bytes())["attempts"] == 1
    assert outbox.drain().get("resolved") == 1
    records = list(outbox.resolved_dir().glob("*.json"))
    assert len(records) == 1
    assert records[0].name == hashlib.sha256(original_bytes).hexdigest() + ".json"
    assert json.loads(records[0].read_bytes())["queued_evidence"] == original_job
    assert not path.exists()
    mirror._queue_conflict(projection())
    assert not outbox.jobs()


@pytest.mark.parametrize("damage", ["bad_hash", "malformed", "different_original"])
def test_recovery_does_not_trust_corrupt_resolution_evidence(world, damage):
    queue()
    assert outbox.drain().get("resolved") == 1
    record_path = next(outbox.resolved_dir().glob("*.json"))
    record = json.loads(record_path.read_bytes())
    if damage == "bad_hash":
        record["resolution"]["queued_payload_sha256"] = "0" * 64
    elif damage == "malformed":
        record["queued_evidence"] = []
    else:
        from khipu.capture_identity import payload_sha256
        record["payload"]["preferences"] = ["forged original evidence"]
        record["resolution"]["original_payload_sha256"] = payload_sha256(record["payload"])
    record_path.write_text(json.dumps(record))
    before = record_path.read_bytes()
    mirror._queue_conflict(projection())
    assert len(outbox.jobs()) == 1
    assert record_path.read_bytes() == before


@pytest.mark.parametrize("where", ["active", "dead"])
@pytest.mark.parametrize("nested_key", ["queued_evidence", "resolution_evidence"])
def test_malformed_outer_payload_cannot_hide_forgotten_nested_identity(world, where, nested_key):
    folder = outbox.outbox_dir() if where == "active" else outbox.dead_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "bad-parent.json"
    path.write_text(json.dumps({"payload": [], nested_key: {"payload": projection()}}))
    tombstones.publish_payloads([projection()])
    if where == "active":
        assert outbox.drain().get("forgotten") == 1
    else:
        assert outbox.retry_dead()["moved"] == 0
    assert not path.exists()
    assert not list(outbox.quarantine_dir().glob("*.json"))


@pytest.mark.parametrize("where", ["active", "dead"])
def test_uncheckable_nested_evidence_is_never_republished_to_quarantine(world, where):
    folder = outbox.outbox_dir() if where == "active" else outbox.dead_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "bad-nested.json"
    path.write_text(json.dumps({"payload": ORIGINAL, "queued_evidence": [{"payload": projection()}]}))
    before = path.read_bytes()
    if where == "active":
        assert outbox.drain().get("unreadable") == 1
    else:
        assert outbox.retry_dead()["moved"] == 0
    assert path.read_bytes() == before
    assert not list(outbox.quarantine_dir().glob("*.json"))


def test_canonical_reordered_journal_is_reused_without_rewrite(world):
    from khipu.capture_identity import payload_sha256
    path = queue()
    job = json.loads(path.read_bytes())
    record = {"resolution": {"resolved_at": "earlier", "episode_id": 1,
                             "kind": "legacy_comma_projection",
                             "queued_payload_sha256": payload_sha256(projection()),
                             "original_payload_sha256": payload_sha256(ORIGINAL)},
              "queued_evidence": job, "payload": dict(reversed(list(ORIGINAL.items())))}
    outbox.resolved_dir().mkdir(parents=True)
    dst = outbox.resolved_dir() / (hashlib.sha256(path.read_bytes()).hexdigest() + ".json")
    dst.write_text(json.dumps(record, indent=2))
    before = dst.read_bytes()
    assert outbox.drain().get("resolved") == 1
    assert dst.read_bytes() == before
    assert not path.exists()


def test_forget_tombstones_nested_projection_when_deleting_exact_outbox(world, tmp_path, monkeypatch):
    """Exact-match outbox delete must fence every nested payload (re-check P2)."""
    from khipu import db, forget
    from tests import test_forget_ownership as fo

    queue()
    assert outbox.drain().get("resolved") == 1
    root = tmp_path / "memory"
    root.mkdir()
    fo.write_lines(root, [dict(ORIGINAL), projection()])
    world_state = {
        "episodes": {1: dict(ORIGINAL, id=1, ts=fo.BASE, raw=dict(ORIGINAL), deleted=False)},
        "sources": [fo.ForgetOwnershipTest._source(None, 1, dict(ORIGINAL))],
        "sources_enabled": True,
    }
    monkeypatch.setattr(db, "connect", lambda *a, **kw: fo._Conn(fo._WorldCur(world_state)))
    result = forget.forget_everywhere(1, memory_root=root)
    assert result.get("complete") is True, result
    assert result.get("remaining") == [], result
    assert not list(outbox.resolved_dir().glob("*.json"))
    assert not tombstones.read_gate().allows(projection()), (
        "projection nested in deleted resolution archive stayed allowed"
    )


def test_status_survives_deeply_nested_dead_job(world):
    """Held-conflict scan must not crash doctor on RecursionError (re-check P3)."""
    outbox.dead_dir().mkdir(parents=True)
    (outbox.dead_dir() / "deep.json").write_text(
        '{"payload":{},"queued_evidence":' + "[" * 100000 + "]" * 100000 + "}"
    )
    outbox.status()
