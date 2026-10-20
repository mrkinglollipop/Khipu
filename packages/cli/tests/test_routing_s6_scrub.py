"""Resolved aliases must survive the helper-to-prompt fork boundary."""
from __future__ import annotations

import json
import os
import select
import signal
import socket
import time
from pathlib import Path

import pytest

from khipu import agent_view, recall_prompt, routing
from tests.test_routing_acceptance import AUTHOR, replay  # noqa: F401


def _read_pipe(fd, *, timeout=5):
    result = b""
    deadline = time.monotonic() + timeout
    while True:
        assert select.select([fd], [], [], max(0, deadline - time.monotonic()))[0], "helper stalled"
        part = os.read(fd, 65536)
        if not part:
            return result
        result += part


@pytest.mark.parametrize("refresh,added", [
    ("slow-signature", None), ("stalled-volume", None),
    ("cold-stalled-volume", None), ("one-shot", None),
    ("slow-signature", "configured"), ("slow-signature", "discovered"),
    ("stalled-volume", "configured"), ("stalled-volume", "discovered"),
    ("fast-signature", "configured"), ("fast-signature", "discovered"),
    ("cold-recovery", None),
])
def test_forked_prompt_never_shows_resolved_login_alias(replay, monkeypatch, refresh, added):
    target = replay.root / "SecretAliasZZZ"
    target.mkdir()
    login = replay.root / "configured-login"
    login.symlink_to(target, target_is_directory=True)
    replay.configure(usage_accounts=[{"label": "Work", "harness": "codex", "home": str(login)}])
    routing.report(account="Work", state="open", author=AUTHOR)
    routing.request_rule("Use SecretAliasZZZ", author=AUTHOR)
    replay.view()
    # Establish the full scrub's result, then start the helper with no inherited table.
    assert "SecretAliasZZZ" not in agent_view.scrub("Use SecretAliasZZZ")
    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})
    allow_refresh = replay.data / "allow-refresh"
    release_stall = replay.data / "release-stall"
    refreshed = replay.data / "inventory-refreshed"
    if not added and refresh != "cold-recovery":
        allow_refresh.touch()
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
    stdin_read, stdin_write = os.pipe()
    ready_read, ready_write = os.pipe()
    pid = os.fork()
    if pid == 0:
        parent.close()
        os.close(stdin_write)
        os.close(ready_read)
        os.dup2(stdin_read, 0)
        os.dup2(ready_write, 1)
        original_signature = agent_view._signature
        original_listdir = agent_view.os.listdir
        original_write = recall_prompt._write_all
        original_prepare = agent_view.prepare_routing_table

        def prepare(*, refresh=False):
            if refresh and not allow_refresh.exists():
                return
            if refresh:
                original_prepare(refresh=True)
            else:
                original_prepare()
            if refresh:
                table = agent_view._TABLE.get("helper_table")
                word = "SecretNewYYY" if added == "configured" else "codex-SideKKK"
                if table is not None and (not added or agent_view._scrub_text(word, table, []) != word):
                    refreshed.touch()

        agent_view.prepare_routing_table = prepare
        recall_prompt._HELPER_TICK_S = 0.05

        def slow_signature(keys):
            if refresh != "stalled-volume" or release_stall.exists():
                time.sleep(0.015)  # Every warm walk exceeds the prompt's 5ms window.
            return original_signature(keys)

        def stalled_volume(path):
            if str(path) == "/Volumes" and not release_stall.exists():
                (replay.data / "volume-stalled").touch()
                time.sleep(2)
            return original_listdir(path)

        def write(fd, data):
            if fd == 1 and data == b"ready\n" and refresh == "stalled-volume":
                agent_view.os.listdir = stalled_volume
            original_write(fd, data)

        if refresh in ("slow-signature", "one-shot", "cold-recovery") or (added and refresh != "fast-signature"):
            agent_view._signature = slow_signature
        recall_prompt._write_all = write
        if refresh in ("cold-stalled-volume", "cold-recovery"):
            agent_view.os.listdir = stalled_volume
        try:
            if refresh == "one-shot":
                # The daemon is down: the launcher forks this child directly.
                recall_prompt._prompt_routing_child(ready_write, "one-shot", None, time.monotonic() + 2, decided=True)
            else:
                os._exit(recall_prompt.routing_forkserver_main(child.detach(), schedule=recall_prompt.RefoldSchedule(None)))
        except BaseException:
            os._exit(1)
    child.close()
    os.close(stdin_read)
    os.close(ready_write)
    try:
        if refresh == "one-shot":
            wire = _read_pipe(ready_read)
            answer = json.loads(wire.split(b"\n")[1])
            assert answer.get("kind") == "full", answer
            assert b"SecretAliasZZZ" not in wire
            assert "Use " in answer["text"]
            return
        assert select.select([ready_read], [], [], 5)[0], "helper never became ready"
        assert os.read(ready_read, 100) == b"ready\n"
        word = None
        if added == "configured":
            word = "SecretNewYYY"
            (replay.root / word).mkdir()
            second_login = replay.root / "second-login"
            second_login.symlink_to(replay.root / word, target_is_directory=True)
            replay.configure(usage_accounts=[
                {"label": "Work", "harness": "codex", "home": str(login)},
                {"label": "Home", "harness": "codex", "home": str(second_login)},
            ])
        elif added == "discovered":
            word = "codex-SideKKK"
            discovered = Path(os.environ["HOME"]) / ("." + word)
            discovered.mkdir()
            (discovered / "auth.json").write_text("{}")
        if added:
            # Full discovery knows the new alias; the helper still has its startup inventory.
            assert word not in agent_view.scrub("Use " + word)
            routing.request_rule("Use " + word, author=AUTHOR)
            replay.view()
        if added and refresh != "fast-signature":
            # An incomplete probe may use the prior table only within two seconds.
            time.sleep(agent_view._PROMPT_TABLE_MAX_AGE_S + 0.05)
        for index in range(3 if added or refresh == "cold-recovery" else 2):
            if index == 2:
                # Let the real helper's idle tick rebuild its inherited table.
                release_stall.touch()
                allow_refresh.touch()
                deadline = time.monotonic() + 5
                while not refreshed.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                assert refreshed.exists(), "helper did not refresh its inventory after recovery"
            read_fd, write_fd = os.pipe()
            try:
                start = time.monotonic()
                request = json.dumps({"session": f"alias-{index}", "cap": start + 2}).encode()
                socket.send_fds(parent, [request], [write_fd])
                os.close(write_fd)
                write_fd = None
                wire = _read_pipe(read_fd)
                assert time.monotonic() - start < 1.5, "prompt waited on the stalled volume"
                answer = json.loads(wire.split(b"\n")[1])
                assert b"SecretAliasZZZ" not in wire
                if word:
                    assert word.encode() not in wire
                if refresh == "cold-stalled-volume" or ((added or refresh == "cold-recovery") and index < 2):
                    assert answer == {"skip": "error-InventoryUnavailable", "done": answer["done"]}
                    assert not recall_prompt.routing_marker(f"alias-{index}")
                else:
                    assert answer.get("kind") == "full", answer
                    assert "Use " in answer["text"]
                    assert "Work: open" in answer["text"]
                    assert answer["routing_changes"]["batch"]
                if refresh in ("stalled-volume", "cold-stalled-volume", "cold-recovery") and not added:
                    assert (replay.data / "volume-stalled").exists()
            finally:
                os.close(read_fd)
                if write_fd is not None:
                    os.close(write_fd)
    finally:
        parent.close()
        os.close(stdin_write)
        os.close(ready_read)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if os.waitpid(pid, os.WNOHANG)[0]:
                break
            time.sleep(0.01)
        else:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


def test_helper_table_expires_without_a_completed_prompt_signature(replay, monkeypatch):
    routing.report(account="Codex", state="open", author=AUTHOR)
    monkeypatch.setattr(agent_view, "_TABLE", {})
    agent_view.prepare_routing_table()
    assert agent_view._PROMPT_TABLE_MAX_AGE_S == 2 * recall_prompt._HELPER_TICK_S == 2.0
    built = agent_view._TABLE["helper_at"]
    built_sig = agent_view._TABLE["helper_sig"]
    # A login appears immediately after the helper's last build.
    folder = Path(os.environ["HOME"]) / ".codex-NewLoginXYZ"
    folder.mkdir()
    (folder / "auth.json").write_text("{}")
    changed = agent_view._signature(None)
    assert agent_view._login_signature(changed) != agent_view._login_signature(built_sig)
    # A completed prompt probe catches it even inside the permitted window.
    monkeypatch.setattr(agent_view, "_signature", lambda _: changed)
    with pytest.raises(agent_view.InventoryUnavailable):
        agent_view._prompt_table(())

    def stalled(_):
        time.sleep(0.1)
        pytest.fail("prompt signature exceeded its deadline")

    monkeypatch.setattr(agent_view, "_signature", stalled)
    monkeypatch.setattr(agent_view.time, "monotonic", lambda: built + 1.99)
    assert agent_view._prompt_table(()) is not None
    assert agent_view._TABLE["helper_at"] == built
    monkeypatch.setattr(agent_view.time, "monotonic", lambda: built + 2.001)
    with pytest.raises(agent_view.InventoryUnavailable):
        agent_view._prompt_table(())


def test_helper_rebuilds_unchanged_inputs_and_failed_refresh_does_not_renew(replay, monkeypatch):
    monkeypatch.setattr(agent_view, "_TABLE", {})
    agent_view.prepare_routing_table()
    previous = agent_view._TABLE["helper_table"]
    built = agent_view._TABLE["helper_at"]
    agent_view.prepare_routing_table(refresh=True)
    assert agent_view._TABLE["helper_table"] is not previous
    assert agent_view._TABLE["helper_at"] >= built
    built = agent_view._TABLE["helper_at"]

    def stalled(_):
        time.sleep(0.5)
        pytest.fail("helper refresh exceeded its deadline")

    monkeypatch.setattr(agent_view, "_signature", stalled)
    agent_view.prepare_routing_table(refresh=True)
    assert agent_view._TABLE["helper_at"] == built


@pytest.mark.parametrize("wall_change", [3.0, -3.0])
def test_helper_table_expires_across_sleep_or_a_backwards_clock(replay, monkeypatch, wall_change):
    agent_view.prepare_routing_table()
    built = agent_view._TABLE["helper_at"]
    wall = agent_view._TABLE["helper_wall_at"]
    monkeypatch.setattr(agent_view.time, "monotonic", lambda: built)
    monkeypatch.setattr(agent_view.time, "time", lambda: wall + wall_change)
    monkeypatch.setattr(agent_view, "_signature", lambda _: pytest.fail("expired table reached discovery"))
    with pytest.raises(agent_view.InventoryUnavailable):
        agent_view._prompt_table(())
