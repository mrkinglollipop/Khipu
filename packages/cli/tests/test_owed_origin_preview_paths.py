"""Preview connection setup must not create directories or settle local writes."""
import argparse
import json
from contextlib import nullcontext

import pytest

from khipu import accounts, config, db, keychain, owed_origin, paths

DSN = 'postgresql://synthetic@127.0.0.1/synthetic'


@pytest.fixture
def absent_state(tmp_path, monkeypatch):
    state = tmp_path / 'missing-parent' / 'missing-state'
    monkeypatch.setenv('KHIPU_DATA_DIR', str(state))
    monkeypatch.setattr(db, 'LEGACY_DSN_FILE', tmp_path / 'missing-legacy-dsn')
    monkeypatch.setattr(keychain, 'get_password', lambda *a, **kw: None)
    for name in ('KHIPU_DATABASE_URL', 'ALZY_DATABASE_URL', 'KHIPU_DSN_FILE', 'ALZY_DSN_FILE'):
        monkeypatch.delenv(name, raising=False)
    return state


def test_missing_dsn_resolution_does_not_create_state_directory(absent_state):
    with pytest.raises(RuntimeError, match='No Khipu DSN'):
        db.resolve_dsn(migrate_legacy=False)
    assert not absent_state.parent.exists()


@pytest.mark.parametrize('failure', [False, True])
@pytest.mark.parametrize('source', ['environment', 'file_override'])
def test_read_only_connection_does_not_create_state_directory(absent_state, tmp_path, monkeypatch, failure, source):
    if source == 'environment':
        monkeypatch.setenv('KHIPU_DATABASE_URL', DSN)
    else:
        override = tmp_path / 'explicit-dsn'
        override.write_text(DSN)
        monkeypatch.setenv('KHIPU_DSN_FILE', str(override))
    connected = object()
    def connect(*a, **kw):
        if failure:
            raise db.psycopg.OperationalError('Synthetic unavailable connection')
        return connected
    monkeypatch.setattr(db.psycopg, 'connect', connect)
    if failure:
        with pytest.raises(db.psycopg.OperationalError):
            db.connect(record_reachability=False)
    else:
        assert db.connect(record_reachability=False) is connected
    assert not absent_state.parent.exists()


def test_root_cert_lookup_without_directory_creation(absent_state):
    # The public helper preserves creation by default; read-only connection
    # setup selects its explicit non-mutating path.
    db.conninfo_with_local_root_cert(DSN, create_data_dir=False)
    assert not absent_state.parent.exists()


def test_read_only_connection_keeps_isolated_and_credential_paths_distinct(absent_state, tmp_path, monkeypatch):
    credentials = tmp_path / 'credentials'
    credentials.mkdir()
    (credentials / 'dsn').write_text(DSN)
    cert = credentials / 'root.crt'
    cert.write_text('synthetic certificate')
    monkeypatch.setenv('KHIPU_DATA_DIR', str(credentials))
    monkeypatch.setenv('KHIPU_ISOLATED_DIR', str(absent_state))
    seen = []
    monkeypatch.setattr(db.psycopg, 'connect', lambda conninfo, **kw: seen.append(conninfo) or object())
    db.connect(record_reachability=False)
    assert db.conninfo_to_dict(seen[0])['sslrootcert'] == str(cert.resolve())
    assert not absent_state.parent.exists()


@pytest.fixture
def pending_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path))
    (tmp_path / 'config.json').write_text(json.dumps({config.ACCOUNT_CHANGE_OPEN: {'synthetic': True}}))
    attempted = []
    monkeypatch.setattr(accounts, 'recover', lambda: attempted.append(True))
    return attempted


def test_read_only_connect_does_not_settle_pending_account_change(pending_recovery, monkeypatch):
    monkeypatch.setenv('KHIPU_DATABASE_URL', DSN)
    monkeypatch.setattr(db.psycopg, 'connect', lambda *a, **kw: object())
    db.connect(record_reachability=False)
    assert pending_recovery == []


@pytest.mark.parametrize('requests', [False, True])
def test_preview_and_request_reads_do_not_settle_pending_account_change(pending_recovery, monkeypatch, requests):
    class Cursor:
        def execute(self, *a, **kw):
            pass
        def fetchall(self):
            return []
    class Connection:
        def cursor(self):
            return nullcontext(Cursor())
    monkeypatch.setattr(db, 'connect', lambda **kw: nullcontext(Connection()))
    monkeypatch.setattr(owed_origin, 'preview', lambda *a, **kw: {'counts': {'total': 0}})
    args = argparse.Namespace(confirm=None, requests=requests, request=False, project=None)
    assert owed_origin.command(args) == 0
    assert pending_recovery == []
