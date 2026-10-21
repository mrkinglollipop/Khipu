"""Fixes: remote priority proposal words follow the proposal-text switch
(including its ``owner_request``), and an all-work hold appears in every
account-filtered ``khipu_route`` answer, local and gateway."""
from __future__ import annotations

import pytest

from khipu import routing
from tests.test_routing_acceptance import AUTHOR, Replay

ROLE = "SYNTHETIC remote role"
REQUEST = "SYNTHETIC remote request"


@pytest.fixture
def mac(tmp_path, monkeypatch):
    for name in ("KHIPU_SESSION_ID", "KHIPU_HARNESS", "CLAUDECODE", "CLAUDE_CODE_SESSION_ID",
                 "CODEX_THREAD_ID", "KHIPU_DATABASE_URL", "ALZY_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    replay = Replay(tmp_path / "mac-a", monkeypatch)
    replay.configure(routing_cloud_copy=True, routing_cloud_copy_rules=True,
                     routing_cloud_copy_hold_words=True)
    return replay


def _remote_priority(mac, *, switch):
    mac.configure(routing_proposal_text_between_macs=switch)
    event = {"type": "priority_set", "id": "e-remote-priority", "remote": True, "pending": True,
             "written_at": routing._iso(mac.now), "written_by": dict(AUTHOR), "via": "mcp",
             "entries": [{"account": "codex|", "label": "Codex", "role": ROLE}],
             "owner_request": REQUEST}
    return mac.fold([event])["priority"]


def test_remote_priority_proposal_words_shared_when_switch_on(mac):
    shown = _remote_priority(mac, switch=True)
    assert shown["entries"][0]["role"] == ROLE
    assert shown["owner_request"] == REQUEST
    assert not shown["proposal_withheld"]


def test_remote_priority_proposal_words_withheld_when_switch_off(mac):
    shown = _remote_priority(mac, switch=False)
    assert shown["proposal_withheld"]
    assert "pending priority proposal" in shown["entries"][0]["role"]
    assert shown["owner_request"] is None


def _seed_holds():
    routing.add_hold("SYNTHETIC all work hold", reason="other", scope="all", owner=True, via="app")
    routing.add_hold("SYNTHETIC codex hold", reason="other", scope="account", account="Codex",
                     owner=True, via="app")
    routing.add_hold("SYNTHETIC claude hold", reason="other", scope="account", account="Claude",
                     owner=True, via="app")


def test_local_account_filter_includes_all_work_hold(mac):
    _seed_holds()
    out = routing.conversation_view(account="Codex")
    texts = [h["text"] for h in out["holds"]]
    assert "SYNTHETIC all work hold" in texts
    assert "SYNTHETIC codex hold" in texts
    assert "SYNTHETIC claude hold" not in texts


def test_local_since_account_filter_includes_all_work_hold(mac):
    _seed_holds()
    out = routing.conversation_view(since_version="0", account="Codex")
    assert any("Hold on all work" in line for line in out["lines"])
    assert not any("SYNTHETIC claude hold" in line for line in out["lines"])


def test_gateway_account_filter_includes_all_work_hold(mac):
    _seed_holds()
    body = routing.gateway_projection(account="Codex")
    assert body is not None
    texts = [h.get("text") for h in body["holds"]]
    assert "SYNTHETIC all work hold" in texts
    assert "SYNTHETIC codex hold" in texts
    assert "SYNTHETIC claude hold" not in texts
