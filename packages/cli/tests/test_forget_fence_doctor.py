"""The forget fence's last outcome is visible: a ``forget_list`` doctor row
(red with a reason word when the list could not be read, grey when nothing was
ever forgotten) and one ``[khipu-forget-fence]`` log line per process per
change of outcome, with counts and a reason word only."""
from __future__ import annotations

import json
import os
import stat

import pytest

from khipu import cli, tombstones

FORGOT = 902


@pytest.fixture(autouse=True)
def fresh_fence(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setattr(tombstones, "_FENCE_LAST", None)
    monkeypatch.setattr(tombstones, "_HIDDEN_CACHE", None)
    # The data dir can outlive a test: start with no list and no recorded outcome.
    for leftover in (tombstones.registry_path(), tombstones.fence_state_path()):
        leftover.unlink(missing_ok=True)
    return tmp_path / "capture" / "logs" / "prompt-recall.log"


def _fence_lines(log):
    if not log.exists():
        return []
    return [line for line in log.read_text().splitlines() if "[khipu-forget-fence]" in line]


def test_not_applicable_before_anything_was_forgotten():
    assert tombstones.forget_list_health() == {"ok": True, "applicable": False}


def test_green_after_a_readable_fence_use():
    tombstones.publish_episode_ids({FORGOT})
    assert tombstones.withhold([{"kind": "episode", "id": str(FORGOT)}, {"kind": "topic", "id": "t"}]) == [
        {"kind": "topic", "id": "t"}]
    assert tombstones.forget_list_health() == {"ok": True, "applicable": True}


def test_malformed_list_goes_red_with_a_reason_word_and_one_log_line(fresh_fence):
    tombstones.publish_episode_ids({FORGOT})
    tombstones.registry_path().write_text("{not json", encoding="utf-8")
    rows = [{"kind": "episode", "id": "904"}, {"kind": "topic", "id": "plain"}]
    assert tombstones.withhold(rows) == []
    assert tombstones.withhold(rows) == []
    health = tombstones.forget_list_health()
    assert health["ok"] is False and health["reason"] == "malformed"
    assert str(FORGOT) not in json.dumps(health) and str(tombstones.registry_path()) not in json.dumps(health)
    lines = _fence_lines(fresh_fence)
    assert len(lines) == 1
    assert lines[0].endswith("[khipu-forget-fence] unreadable: malformed withheld=2")
    mode = stat.S_IMODE(os.stat(tombstones.fence_state_path()).st_mode)
    assert mode == 0o600


def test_unreadable_list_is_named_and_an_explicit_read_records_it_too(fresh_fence):
    tombstones.publish_episode_ids({FORGOT})
    tombstones.registry_path().chmod(0)
    try:
        kept = tombstones.withhold([{"kind": "episode", "id": "904"}, {"kind": "topic", "id": "plain"}],
                                   fail_closed=False)
    finally:
        tombstones.registry_path().chmod(0o600)
    if os.geteuid() == 0:
        pytest.skip("root reads a mode-0 file")
    assert kept == [{"kind": "topic", "id": "plain"}]
    assert tombstones.forget_list_health()["reason"] == "unreadable"
    assert _fence_lines(fresh_fence)[0].endswith("unreadable: unreadable withheld=1")


def test_recovery_turns_green_without_a_log_line(fresh_fence):
    tombstones.publish_episode_ids({FORGOT})
    path = tombstones.registry_path()
    good = path.read_text()
    path.write_text("{not json", encoding="utf-8")
    tombstones.withhold([{"kind": "episode", "id": "904"}])
    assert tombstones.forget_list_health()["ok"] is False
    path.write_text(good, encoding="utf-8")
    tombstones._HIDDEN_CACHE = None
    tombstones.withhold([{"kind": "episode", "id": "904"}])
    assert tombstones.forget_list_health() == {"ok": True, "applicable": True}
    assert len(_fence_lines(fresh_fence)) == 1


def test_doctor_gates_on_the_forget_list_row():
    assert "forget_list_ok" in cli.LOCAL_GATES
    tombstones.publish_episode_ids({FORGOT})
    tombstones.registry_path().write_text("{not json", encoding="utf-8")
    tombstones.withhold([{"kind": "episode", "id": "904"}])
    local = cli._doctor_local_checks(None)
    assert local["forget_list_ok"] is False
    assert local["forget_list"]["reason"] == "malformed"
