"""Replication as an owner switch: registered like the cloud-copy switches,
default off, strict boolean in the file, and only through the owner gate."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from khipu import routing

T0 = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]


@pytest.fixture
def home(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setattr(routing, "_now", lambda: T0)
    return {"data": data, "callers": khipu_app}


def _cli(argv, capsys):
    from khipu import cli

    rc = cli.main(argv)
    return rc, capsys.readouterr().out


def _set_file(home, **values):
    path = home["data"] / "config.json"
    config = json.loads(path.read_text())
    config.update(values)
    path.write_text(json.dumps(config))


def test_replication_live_is_registered_default_off(home):
    assert routing.SETTINGS["routing_replication_live"][0] == "switch"
    assert routing.SETTINGS["routing_replication_live"][1] is False
    assert routing.settings()["routing_replication_live"] is False
    status = routing.settings_status()["routing_replication_live"]
    assert status["value"] is False and status["default"] is False and status["kind"] == "switch"
    assert routing._replication_live() is False


@pytest.mark.parametrize("raw", ["true", "on", "yes", 1, 1.0, "True"])
def test_replication_live_still_requires_a_strict_boolean(home, raw):
    _set_file(home, routing_replication_live=raw)
    assert routing._replication_live() is False, raw


def test_replication_live_strict_true_enables(home):
    _set_file(home, routing_replication_live=True)
    assert routing._replication_live() is True
    _set_file(home, routing_replication_live=False)
    assert routing._replication_live() is False


def test_config_set_replication_live_from_app_records_owner_action(home, capsys):
    from khipu import owner_direction

    rc, out = _cli(["config", "--from-app", "--set", "routing_replication_live", "on"], capsys)
    assert rc == 0, out
    body = json.loads(out)
    assert body["routing_replication_live"]["value"] is True
    stored = json.loads((home["data"] / "config.json").read_text())
    assert stored["routing_replication_live"] is True
    assert routing._replication_live() is True
    line = owner_direction.actions()[-1]
    assert line["action"] == "config.routing_setting"
    assert line["target"] == "routing_replication_live"
    assert line["auth"] == "app"

    rc, out = _cli(["config", "--from-app", "--set", "routing_replication_live", "off"], capsys)
    assert rc == 0, out
    assert json.loads(out)["routing_replication_live"]["value"] is False
    assert routing._replication_live() is False

    rc, out = _cli(["config", "--from-app", "--unset", "routing_replication_live"], capsys)
    assert rc == 0, out
    assert routing.settings()["routing_replication_live"] is False


def test_config_set_replication_live_without_owner_is_refused(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    home["callers"].as_shell("/usr/local/bin/claude")
    rc, out = _cli(["config", "--set", "routing_replication_live", "on"], capsys)
    assert rc == 2 and json.loads(out)["code"] == "owner_only"
    rc, out = _cli(["config", "--from-app", "--set", "routing_replication_live", "on"], capsys)
    assert rc == 2 and json.loads(out)["code"] == "owner_only"
    assert "routing_replication_live" not in json.loads((home["data"] / "config.json").read_text())
    assert routing._replication_live() is False


def test_config_set_replication_live_refuses_non_switch_value(home, capsys):
    rc, out = _cli(["config", "--from-app", "--set", "routing_replication_live", "maybe"], capsys)
    assert rc == 2 and "on or off" in json.loads(out)["error"]
    assert routing._replication_live() is False


def test_config_set_replication_live_at_owner_request_waits_for_owner(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc, out = _cli(["config", "--at-owner-request", "turn replication on",
                    "--set", "routing_replication_live", "on"], capsys)
    assert rc == 0, out
    assert routing.settings_status()["routing_replication_live"]["request"]["request"] == "turn replication on"
    # The value is in place but unconfirmed; the owner confirms it from the app.
    rc, out = _cli(["config", "--from-app", "--answer-request", "routing_replication_live", "confirm"], capsys)
    assert rc == 0, out
    assert routing.settings_status()["routing_replication_live"]["request"] is None
    assert routing._replication_live() is True


class _RecordingHub:
    def __init__(self):
        self.calls = []

    def reserve_numbers(self, mac, floor):
        self.calls.append(("reserve_numbers", mac, floor))

    def upload(self, mac, event):
        self.calls.append(("upload", mac, event))

    def pull(self, after, until, limit):
        self.calls.append(("pull", after, until, limit))


def _request_replication_on(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc, out = _cli(["config", "--at-owner-request", "turn replication on",
                    "--set", "routing_replication_live", "on"], capsys)
    assert rc == 0, out


def test_pending_replication_request_is_not_live_and_makes_no_hub_call(home, capsys, monkeypatch):
    from khipu import routing_replication

    _request_replication_on(home, capsys, monkeypatch)
    assert routing.settings_status()["routing_replication_live"]["request"] is not None
    assert routing._replication_live() is False
    hub = _RecordingHub()
    assert routing_replication.sync(hub=hub)["disabled"] is True
    assert hub.calls == []


def test_confirm_pending_replication_request_makes_it_live(home, capsys, monkeypatch):
    _request_replication_on(home, capsys, monkeypatch)
    assert routing._replication_live() is False
    rc, out = _cli(["config", "--from-app", "--answer-request", "routing_replication_live", "confirm"], capsys)
    assert rc == 0, out
    assert routing._replication_live() is True


def test_undo_pending_replication_request_restores_off(home, capsys, monkeypatch):
    _request_replication_on(home, capsys, monkeypatch)
    rc, out = _cli(["config", "--from-app", "--answer-request", "routing_replication_live", "undo"], capsys)
    assert rc == 0, out
    assert routing.settings_status()["routing_replication_live"]["request"] is None
    assert routing._replication_live() is False


@pytest.mark.parametrize("raw", ["true", "yes", 1, "1", "on", 1.0, "True"])
def test_settings_status_replication_live_uses_strict_boolean(home, raw):
    _set_file(home, routing_replication_live=raw)
    status = routing.settings_status()["routing_replication_live"]
    assert status["value"] is False, raw
    assert status["source"] == "invalid", raw
    assert routing._replication_live() is False, raw


def test_settings_status_replication_live_reports_strict_true(home):
    _set_file(home, routing_replication_live=True)
    status = routing.settings_status()["routing_replication_live"]
    assert status["value"] is True and status["source"] == "file"
    _set_file(home, routing_replication_live=False)
    status = routing.settings_status()["routing_replication_live"]
    assert status["value"] is False and status["source"] == "file"


def test_settings_status_other_switches_still_coerce_words(home):
    _set_file(home, routing_cloud_copy="true")
    assert routing.settings_status()["routing_cloud_copy"]["value"] is True


def test_settings_status_pending_request_shows_live_state_and_flag(home, capsys, monkeypatch):
    _request_replication_on(home, capsys, monkeypatch)
    status = routing.settings_status()["routing_replication_live"]
    assert status["pending"] is True
    assert status["value"] is False  # the live state, not the unconfirmed word
    rc, out = _cli(["config", "--from-app", "--answer-request", "routing_replication_live", "confirm"], capsys)
    assert rc == 0, out
    status = routing.settings_status()["routing_replication_live"]
    assert status["pending"] is False and status["value"] is True


def test_settings_status_not_pending_without_request(home):
    status = routing.settings_status()["routing_replication_live"]
    assert status["pending"] is False


def test_request_matching_confirmed_value_changes_nothing(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    _set_file(home, routing_replication_live=True)
    before = (home["data"] / "config.json").read_text()
    rc, out = _cli(["config", "--at-owner-request", "keep replication on",
                    "--set", "routing_replication_live", "on"], capsys)
    assert rc == 0, out
    body = json.loads(out)
    assert body["already_in_effect"] is True
    assert (home["data"] / "config.json").read_text() == before
    assert routing._replication_live() is True
    assert routing.settings_status()["routing_replication_live"]["pending"] is False


def test_request_off_when_already_off_changes_nothing(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    rc, out = _cli(["config", "--at-owner-request", "keep it off",
                    "--set", "routing_replication_live", "off"], capsys)
    assert rc == 0, out
    assert json.loads(out)["already_in_effect"] is True
    assert routing.settings_status()["routing_replication_live"]["pending"] is False


def test_request_changing_the_value_still_waits(home, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    _set_file(home, routing_replication_live=True)
    rc, out = _cli(["config", "--at-owner-request", "turn replication off",
                    "--set", "routing_replication_live", "off"], capsys)
    assert rc == 0, out
    assert "already_in_effect" not in json.loads(out)
    status = routing.settings_status()["routing_replication_live"]
    assert status["pending"] is True and status["value"] is False
    assert routing._replication_live() is False


def test_request_on_from_off_never_goes_live_without_owner(home, capsys, monkeypatch):
    _request_replication_on(home, capsys, monkeypatch)
    assert routing.settings_status()["routing_replication_live"]["pending"] is True
    assert routing._replication_live() is False
