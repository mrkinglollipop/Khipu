"""Batch 1110a fix round: the forget fence before the new row fields (search path), one Mac-name helper, and the
five-minute sweep publishing the status row last."""
from __future__ import annotations

import argparse
import json
import socket

import pytest

from khipu import cli, embed, jobs, macs, memory_rows, owner_direction, replica_heal as rh, routing, storage, tombstones
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.test_audit_recall_search import configure_hub
from tests.test_combined_recovery import fake_world  # noqa: F401 - fixture
from tests.test_search_his_words import _owner_corpus

THIS, OTHER = "a" * 32, "b" * 32


# ---- S1, search path: a forgotten conversation's text is never read for the new fields ---------------------------


@pytest.mark.parametrize("owner_words_only", [True, False])
def test_search_looks_up_the_new_fields_only_for_rows_that_remain(hub, monkeypatch, tmp_path, owner_words_only):  # noqa: F811
    configure_hub(monkeypatch, hub)
    ids = _owner_corpus(hub, tmp_path)
    monkeypatch.setattr(embed, "_cosine_candidates", lambda q, **kw: [])
    asked: list = []
    real = memory_rows.episode_facts

    def recording(cur, wanted):
        asked.extend(str(i) for i in wanted)
        return real(cur, wanted)

    monkeypatch.setattr(memory_rows, "episode_facts", recording)
    tombstones.publish_episode_ids({int(ids[0])})
    out = embed.hybrid_search("violet reset", mode="literal", owner_words_only=owner_words_only, memory_fields=True)
    shown = [str(r["id"]) for r in out["results"]]
    assert ids[0] not in shown and ids[0] not in asked
    if not owner_words_only:
        assert ids[1] in shown and ids[1] in asked


# ---- G5: one Mac-name helper -------------------------------------------------------------------------------------


def _routing(monkeypatch, names):
    monkeypatch.setattr(routing, "settings", lambda config=None: {"routing_mac_names": names})
    monkeypatch.setattr(routing, "_local_mac", lambda: THIS)
    monkeypatch.setattr("khipu.machine_identity.local_origin", lambda: "local:this")


def test_search_activity_health_and_macs_name_a_named_mac_the_same(monkeypatch):
    _routing(monkeypatch, {THIS: "M5", OTHER: "Studio"})
    resolve = memory_rows.mac_resolver()
    assert resolve("local:this") == macs.mac_label() == macs.name_of(THIS) == "M5"
    assert macs.name_of(OTHER, "row-name") == "Studio"  # the owner's name beats the name on the row
    assert resolve("gateway:x") is None and resolve(None) is None


def test_search_activity_health_and_macs_name_an_unnamed_mac_the_same(monkeypatch):
    _routing(monkeypatch, {})
    resolve = memory_rows.mac_resolver()
    here = socket.gethostname().split(".")[0] or "This Mac"
    assert resolve("local:this") == macs.mac_label() == macs.name_of(THIS, "ignored") == here
    # another Mac: no name given -> the name on its row -> one fallback, in every reader
    assert macs.name_of(OTHER, "studio") == "studio"
    assert macs.name_of(OTHER, None) == macs.name_of(None) == macs.OTHER_MAC == resolve("local:someone-else")


def test_a_failing_settings_read_never_fails_a_name(monkeypatch):
    def broken(config=None):
        raise OSError("no config")

    monkeypatch.setattr(routing, "settings", broken)
    assert macs.mac_label() and macs.name_of(OTHER) == macs.OTHER_MAC


# ---- G7: the sweep publishes the status row last, and a bad publish never fails it --------------------------------


def _stages(monkeypatch, calls):
    monkeypatch.setattr(storage, "sweep_if_due", lambda: calls.append("storage") or {"ok": True})
    monkeypatch.setattr(rh, "run", lambda: calls.append("replica") or {"ok": True, "action": "none"})
    monkeypatch.setattr(owner_direction, "timer", lambda: calls.append("owner") or {"ok": True})
    monkeypatch.setattr(jobs, "_write_job_state", lambda *a: calls.append("job_state"))
    monkeypatch.setattr(routing, "poll_refold", lambda: calls.append("routing_copy") or {"ok": True})


def test_the_sweep_publishes_the_status_row_after_the_routing_copy(fake_world, monkeypatch, capsys):  # noqa: F811
    calls: list = []
    _stages(monkeypatch, calls)
    monkeypatch.setattr(macs, "publish", lambda *a, **k: calls.append("mac_status") or {"ok": True})
    assert cli.cmd_snapshot(argparse.Namespace(snapshot_cmd="heal")) == 0
    out = json.loads(capsys.readouterr().out)
    assert calls == ["storage", "replica", "owner", "job_state", "routing_copy", "mac_status"]
    assert out["mac_status"] == {"ok": True}
    keys = list(out)
    assert keys.index("routing_copy") < keys.index("mac_status")


def test_a_failed_or_slow_status_publish_never_fails_or_delays_the_sweep(fake_world, monkeypatch, capsys):  # noqa: F811
    import time

    calls: list = []
    _stages(monkeypatch, calls)
    monkeypatch.setattr(macs, "publish", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("hub down")))
    assert cli.cmd_snapshot(argparse.Namespace(snapshot_cmd="heal")) == 0
    assert json.loads(capsys.readouterr().out)["mac_status"]["ok"] is False
    monkeypatch.setattr(cli, "MAC_STATUS_MAX_S", 0.4)
    monkeypatch.setattr(macs, "publish", lambda *a, **k: time.sleep(60))
    started = time.monotonic()
    assert cli.cmd_snapshot(argparse.Namespace(snapshot_cmd="heal")) == 0
    out = json.loads(capsys.readouterr().out)
    assert time.monotonic() - started < 8
    assert out["mac_status"]["ok"] is False and "stopped" in out["mac_status"]["error"]
    assert calls.count("routing_copy") == 2


def test_the_sweep_builds_no_work_view_for_the_status_row(fake_world, monkeypatch, capsys):  # noqa: F811
    from khipu import work

    calls: list = []
    _stages(monkeypatch, calls)
    monkeypatch.setattr(macs, "local_mac_id", lambda: THIS)
    monkeypatch.setattr("khipu.hub_reach.known_unreachable", lambda: {"since": "x"})
    monkeypatch.setattr(work, "build_view", lambda *a, **k: calls.append("view"))
    assert cli.cmd_snapshot(argparse.Namespace(snapshot_cmd="heal")) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mac_status"] == {"ok": False, "why": "hub_unreachable"} and "view" not in calls
