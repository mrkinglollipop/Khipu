"""Cold alias resolution in a real preview must remain a pure config read."""
import json
from contextvars import Context

import pytest

from khipu import accounts, cli, commitments as co, config
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_owed_origin_correction import origin_db, seed, command  # noqa: F401


@pytest.fixture
def cold_alias_preview(origin_db, tmp_path, monkeypatch):
    cid = seed(origin_db)
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({config.ACCOUNT_CHANGE_OPEN: {'synthetic': True},
                                'user_aliases': ['Configured Zeta'],
                                'memory_root': str(tmp_path / 'configured-memory')}))
    monkeypatch.delenv('KHIPU_USER_ALIASES', raising=False)
    co.reset_user_patterns()
    attempted = []
    monkeypatch.setattr(accounts, 'recover', lambda: attempted.append(True))
    return cid, path, attempted


def test_actual_preview_with_cold_configured_aliases_does_not_recover(cold_alias_preview):
    cid, path, attempted = cold_alias_preview
    before = path.read_bytes()
    rc, preview = command()
    assert rc == 0 and [item['id'] for item in preview['items']] == [cid]
    assert co.normalize_owner('Configured Zeta') == 'user'
    assert cli.build_parser().parse_args(['status']).memory_root == str(path.parent / 'configured-memory')
    assert attempted == []
    assert path.read_bytes() == before
    # Outside the preview, the normal recovery contract remains active.
    config.load_config()
    assert attempted == [True]


def test_preview_exception_restores_normal_config_recovery(cold_alias_preview, monkeypatch):
    _, _, attempted = cold_alias_preview
    original = co.resolve_owner_and_label
    def fail_after_alias_resolution(*a, **kw):
        original(*a, **kw)
        raise RuntimeError('Synthetic resolution failure')
    monkeypatch.setattr(co, 'resolve_owner_and_label', fail_after_alias_resolution)
    with pytest.raises(RuntimeError, match='Synthetic resolution failure'):
        command()
    assert attempted == []
    config.load_config()
    assert attempted == [True]


def test_read_only_config_context_keeps_aliases_and_is_context_local(tmp_path, monkeypatch):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path))
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({config.ACCOUNT_CHANGE_OPEN: {'synthetic': True},
                                'user_aliases': ['Configured Zeta']}))
    monkeypatch.delenv('KHIPU_USER_ALIASES', raising=False)
    attempted = []
    monkeypatch.setattr(accounts, 'recover', lambda: attempted.append(True))
    with config.read_only_config():
        assert config.list_setting('user_aliases') == ('Configured Zeta',)
        assert attempted == []
        # The scoped snapshot does not switch unrelated execution contexts.
        Context().run(config.load_config)
        assert attempted == [True]
        # Readers get independent values while nested contexts retain the snapshot.
        config.load_config()['user_aliases'].append('unwanted mutation')
        path.write_text(json.dumps({'user_aliases': ['Later Value']}))
        with config.read_only_config():
            assert config.list_setting('user_aliases') == ('Configured Zeta',)
    assert config.list_setting('user_aliases') == ('Later Value',)
