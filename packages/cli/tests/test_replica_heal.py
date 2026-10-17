"""The replica refresh job (docs/plans/2026-10-16-offline-and-self-healing.md, design A):
the decision, the one check, its settings, the plist it renders and the CLI that sets it."""
from __future__ import annotations

import json
import plistlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from khipu import config, hub_snapshot, launchd_gen, replica_heal
from khipu.cli import main


class FakeHub:
    """A hub that is down or up, and a replica that a dump brings level."""

    def __init__(self, monkeypatch):
        self.up = False
        self.dumps = 0
        self.connects = 0
        self.dump_result = {"ok": True}
        self.health = {"exists": False, "age_seconds": None, "refreshed_at": None}
        monkeypatch.setattr(hub_snapshot, "try_hub_connect", self._connect)
        monkeypatch.setattr(hub_snapshot, "snapshot_health", lambda: dict(self.health))
        monkeypatch.setattr(replica_heal, "_full_dump", self._dump)

    def age(self, seconds: float) -> None:
        at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        self.health = {"exists": True, "age_seconds": seconds, "refreshed_at": at.isoformat()}

    @contextmanager
    def _connect(self, **_kw):
        self.connects += 1
        if not self.up:
            raise OSError("connection refused")
        yield _Conn()

    def _dump(self):
        self.dumps += 1
        if self.dump_result.get("ok"):
            self.age(1)
        return dict(self.dump_result)


class _Conn:
    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, *_a):
        return None


@pytest.fixture()
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    return FakeHub(monkeypatch)


def test_hub_down_then_up_dumps_once_then_is_fresh(hub):
    out = replica_heal.run()
    assert out["action"] == "waiting" and out["reason"] == "hub_unreachable" and out["ok"] is True
    assert hub.dumps == 0
    assert replica_heal.read_state()["hub_unreachable_since"]
    hub.up = True
    out = replica_heal.run()
    assert out["ok"] is True and hub.dumps == 1
    state = replica_heal.read_state()
    assert "hub_unreachable_since" not in state and state["last_synced_at"]
    out = replica_heal.run()
    assert (out["action"], out["reason"]) == ("none", "fresh")
    assert hub.dumps == 1


def test_a_fresh_replica_opens_no_connection(hub):
    hub.age(60)
    out = replica_heal.run()
    assert (out["action"], out["reason"]) == ("none", "fresh")
    assert hub.connects == 0 and hub.dumps == 0


def test_a_replica_at_an_older_schema_is_rebuilt_at_once(hub):
    """Promoted code at a newer schema: the copy is rebuilt on the next check,
    not when a lag or the nightly happens to force a dump hours later."""
    hub.up = True
    hub.age(60)
    hub.health["schema_version"] = hub_snapshot.SNAPSHOT_SCHEMA_VERSION - 1
    out = replica_heal.run()
    assert (out["action"], out["reason"]) == ("dump", "schema")
    assert hub.dumps == 1
    out = replica_heal.run()
    assert (out["action"], out["reason"]) == ("none", "fresh")
    assert hub.dumps == 1


def test_a_schema_mismatch_is_a_dump_even_when_the_hub_just_came_back():
    now = datetime.now(timezone.utc)
    health = {"exists": True, "age_seconds": 60, "refreshed_at": (now - timedelta(seconds=60)).isoformat(),
              "schema_version": hub_snapshot.SNAPSHOT_SCHEMA_VERSION - 1}
    action = replica_heal.plan(now=now, state={"hub_unreachable_since": now.isoformat()}, health=health,
                               sidecar={}, last_known=None, refresh_after=900)
    assert action == ("dump", "schema")
    health["schema_version"] = hub_snapshot.SNAPSHOT_SCHEMA_VERSION
    action = replica_heal.plan(now=now, state={}, health=health, sidecar={}, last_known=None, refresh_after=900)
    assert action == ("none", "fresh")


def test_snapshot_health_reports_the_schema_on_disk(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    hub_snapshot.snapshot_path().write_bytes(b"")
    hub_snapshot.meta_path().write_text(json.dumps({"schema_version": 5, "refreshed_at": "2026-10-16T18:00:00Z"}))
    assert hub_snapshot.snapshot_health()["schema_version"] == 5


def test_update_is_due_after_the_refresh_window(hub):
    hub.up = True
    replica_heal.set_setting("replica_refresh_after", 600)
    hub.age(700)
    out = replica_heal.run()
    assert out["reason"] == "due" and hub.dumps == 1


def test_zero_updates_at_every_check(hub):
    hub.up = True
    replica_heal.set_setting("replica_refresh_after", 0)
    hub.age(5)
    replica_heal.run()
    replica_heal.run()
    assert hub.dumps == 2


def test_a_failed_dump_is_not_repeated_inside_the_window(hub):
    hub.up = True
    hub.dump_result = {"ok": False, "error": "disk full"}
    out = replica_heal.run()
    assert out["ok"] is False and hub.dumps == 1
    out = replica_heal.run()
    assert (out["action"], out["reason"]) == ("none", "recently_failed")
    assert hub.dumps == 1


def test_a_dump_already_in_progress_is_skipped_not_failed(hub):
    hub.up = True
    hub.dump_result = {"ok": False, "error": "hub snapshot refresh already in progress"}
    out = replica_heal.run()
    assert out["action"] == "skipped" and out["ok"] is True
    assert "last_failed_at" not in replica_heal.read_state()


def test_settings_validate_store_and_reset(hub):
    with pytest.raises(ValueError):
        replica_heal.set_setting("replica_check_every", 30)
    replica_heal.set_setting("replica_check_every", "10m")
    assert config.load_config()["replica_check_every"] == 600
    assert replica_heal.settings_status()["replica_check_every"]["source"] == "file"
    replica_heal.set_setting("replica_check_every", None)
    status = replica_heal.settings_status()["replica_check_every"]
    assert status["value"] == 300 and status["source"] == "default"


def test_rendered_plist_follows_the_setting(hub):
    assert "replica_refresh" in launchd_gen._DEFAULT_JOBS
    data = plistlib.loads(launchd_gen.render_plist("replica_refresh"))
    assert data["Label"] == "com.khipu.replica-refresh"
    assert data["ProgramArguments"][-2:] == ["snapshot", "heal"]
    assert data["StartInterval"] == 300
    assert data["WatchPaths"] == [str(replica_heal.wake_path())]
    assert "ProcessType" not in data
    replica_heal.set_setting("replica_check_every", 600)
    assert plistlib.loads(launchd_gen.render_plist("replica_refresh"))["StartInterval"] == 600


def test_rerender_skips_a_job_that_is_not_installed(hub, tmp_path, monkeypatch):
    monkeypatch.setattr(launchd_gen, "_launchagents_dir", lambda: tmp_path / "agents")
    monkeypatch.setattr(launchd_gen, "_plist_path", lambda label: tmp_path / "agents" / f"{label}.plist")
    assert launchd_gen.rerender_replica_job() == {"ok": True, "skipped": "not installed"}


def test_cli_stores_minutes_as_seconds(hub, capsys):
    assert main(["config", "--set", "replica_refresh_after", "20m"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["replica_refresh_after"]["value"] == 1200
    assert config.load_config()["replica_refresh_after"] == 1200


def test_cli_check_interval_rerenders_the_job(hub, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(launchd_gen, "rerender_replica_job", lambda: calls.append(1) or {"ok": True, "skipped": "x"})
    assert main(["config", "--set", "replica_check_every", "600"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert calls == [1] and out["job"] == {"ok": True, "skipped": "x"}
    assert main(["config", "--set", "replica_check_every", "30"]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def _pulls(monkeypatch, result):
    calls = []
    monkeypatch.setattr(hub_snapshot, "pull_changes", lambda: calls.append(1) or dict(result))
    return calls


def test_an_update_is_a_pull_and_no_dump_when_it_succeeds(hub, monkeypatch):
    calls = _pulls(monkeypatch, {"ok": True, "episodes": 3})
    out = replica_heal._sync()
    assert out == {"ok": True, "episodes": 3} and calls == [1] and hub.dumps == 0


def test_a_pull_that_needs_a_dump_falls_back_to_one(hub, monkeypatch):
    _pulls(monkeypatch, {"ok": False, "needs_dump": True, "error": "more than 2000 changes since the last update"})
    out = replica_heal._sync()
    assert hub.dumps == 1 and out["ok"] is True
    assert out["fallback_from_pull"] == "more than 2000 changes since the last update"


def test_a_pull_error_while_the_hub_answers_falls_back_to_a_dump(hub, monkeypatch):
    _pulls(monkeypatch, {"ok": False, "error": "OperationalError: relation missing", "hub_unreachable": False})
    out = replica_heal._sync()
    assert hub.dumps == 1 and out["fallback_from_pull"] == "OperationalError: relation missing"


def test_a_hub_lost_during_the_pull_is_waited_out_not_failed(hub, monkeypatch):
    hub.up = True
    hub.age(3600)
    _pulls(monkeypatch, {"ok": False, "error": "ConnectionTimeout: x", "hub_unreachable": True})
    out = replica_heal.run()
    assert out["ok"] is True and (out["action"], out["reason"]) == ("waiting", "hub_unreachable")
    state = replica_heal.read_state()
    assert state["hub_unreachable_since"] and "last_failed_at" not in state
    assert hub.dumps == 0


def test_an_unreachable_hub_or_a_dump_in_progress_is_returned_as_it_is(hub, monkeypatch):
    for result in ({"ok": False, "error": "ConnectionTimeout: x", "hub_unreachable": True},
                   {"ok": False, "error": "hub snapshot refresh in progress"}):
        _pulls(monkeypatch, result)
        assert replica_heal._sync() == result
    assert hub.dumps == 0


def test_a_measurement_older_than_the_last_pull_no_longer_describes_the_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    refreshed = "2026-10-16T06:00:00+00:00"
    health = {"exists": True, "age_seconds": 60, "refreshed_at": refreshed}
    monkeypatch.setattr(hub_snapshot, "snapshot_health", lambda: dict(health))
    checked = datetime.now(timezone.utc) - timedelta(minutes=5)
    hub_snapshot.freshness_path().write_text(json.dumps({
        "refreshed_at": refreshed, "checked_at": checked.isoformat(), "state": "behind",
        "lag_seconds": hub_snapshot.SNAPSHOT_LAG_BEHIND_S + 600}), encoding="utf-8")
    fresh, why = hub_snapshot.snapshot_is_fresh()
    assert fresh is False and why["distrusted_by"] == "measurement"
    hub_snapshot._write_meta({"last_pull_at": (checked + timedelta(minutes=1)).isoformat()})
    fresh, why = hub_snapshot.snapshot_is_fresh()
    assert fresh is True and "distrusted_by" not in why


@pytest.mark.parametrize('needs_refresh', [True, False])
def test_persistent_ledger_failure_refreshes_only_ledger_with_backoff(hub, monkeypatch, needs_refresh):
    hub.up = True
    hub.age(3600)
    replica_heal.set_setting('replica_refresh_after', 0)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(replica_heal, '_now', lambda: now)
    pulls, refreshes = [], []
    failure = {'ok': False, 'error': 'synthetic locked ledger', 'needs_refresh': needs_refresh}

    def pull(**kwargs):
        pulls.append(kwargs)
        return {'ok': True, 'ledger': failure if kwargs.get('include_ledger', True) else
                {'ok': True, 'skipped': 'backoff'}}

    monkeypatch.setattr(hub_snapshot, 'pull_changes', pull)
    monkeypatch.setattr(hub_snapshot, 'refresh_ledger',
                        lambda: refreshes.append(now) or dict(failure), raising=False)
    first = replica_heal.run()
    assert not first['ok'] and 'synthetic locked ledger' in first['result']['ledger']['error']
    for seconds in (300, 600, 899):
        now = refreshes[0] + timedelta(seconds=seconds)
        out = replica_heal.run()
        assert not out['ok'] and out['result']['ledger']['retry_after']
    assert len(refreshes) == 1 and hub.dumps == 0
    assert all(p.get('include_ledger') is False for p in pulls[1:])
    now = refreshes[0] + timedelta(seconds=900)
    assert not replica_heal.run()['ok']
    assert len(refreshes) == 2 and hub.dumps == 0


def test_ledger_refresh_retries_when_memory_is_still_fresh(hub, monkeypatch):
    hub.up = True
    hub.age(3600)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(replica_heal, '_now', lambda: now)
    _pulls(monkeypatch, {'ok': True, 'ledger': {'ok': False, 'needs_refresh': True, 'error': 'broken'}})
    refreshes = []
    monkeypatch.setattr(hub_snapshot, 'refresh_ledger',
                        lambda: refreshes.append(now) or {'ok': False, 'error': 'broken'}, raising=False)
    assert not replica_heal.run()['ok']
    now += timedelta(seconds=300)
    waiting = replica_heal.run()
    assert not waiting['ok'] and waiting['error'] == 'ledger: broken'
    assert len(refreshes) == 1
    replica_heal.set_setting('replica_refresh_after', 3600)
    now += timedelta(seconds=600)
    monkeypatch.setattr(hub_snapshot, 'refresh_ledger',
                        lambda: refreshes.append(now) or {'ok': True})
    out = replica_heal.run()
    assert out['ok'] and out['action'] == 'ledger' and len(refreshes) == 2
    assert 'last_ledger_error' not in replica_heal.read_state()
    assert hub.dumps == 0


def test_successful_ledger_refresh_clears_the_pull_error(hub, monkeypatch):
    _pulls(monkeypatch, {'ok': True, 'failed': ['ledger'], 'error': 'ledger: missing markers',
                        'ledger': {'ok': False, 'needs_refresh': True, 'error': 'missing markers'}})
    monkeypatch.setattr(hub_snapshot, 'refresh_ledger', lambda: {'ok': True})
    out = replica_heal._sync()
    assert out == {'ok': True, 'ledger': {'ok': True}}
    assert hub.dumps == 0


def test_memory_failure_during_ledger_backoff_still_dumps(hub, monkeypatch):
    now = datetime.now(timezone.utc)
    state = {'last_ledger_error': {'ok': False, 'error': 'ledger broken'},
             'last_ledger_refresh_at': now.isoformat()}
    monkeypatch.setattr(hub_snapshot, 'pull_changes',
                        lambda **_: {'ok': False, 'needs_dump': True, 'error': 'memory overflow'})
    out = replica_heal._sync(state, now)
    assert out['ok'] and out['fallback_from_pull'] == 'memory overflow'
    assert hub.dumps == 1 and 'last_ledger_error' not in state


def test_ledger_only_success_preserves_memory_failure_backoff(hub, monkeypatch):
    hub.up = True
    hub.age(3600)
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(replica_heal, '_now', lambda: now)
    state = {'last_failed_at': now.isoformat(), 'last_error': 'memory failure',
             'last_ledger_refresh_at': (now - timedelta(seconds=900)).isoformat(),
             'last_ledger_error': {'ok': False, 'error': 'ledger failure'}}
    replica_heal._write_state(state)
    monkeypatch.setattr(hub_snapshot, 'refresh_ledger', lambda: {'ok': True})
    out = replica_heal.run()
    assert out['ok'] and out['action'] == 'ledger'
    after = replica_heal.read_state()
    assert after['last_failed_at'] == state['last_failed_at']
    assert after['last_error'] == 'memory failure' and 'last_synced_at' not in after
    assert replica_heal.run()['reason'] == 'recently_failed'
    assert hub.dumps == 0
