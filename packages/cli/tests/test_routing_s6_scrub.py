"""Resolved aliases must survive the helper-to-prompt fork boundary."""
from __future__ import annotations

import json
import os
import select
import signal
import socket
import time

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


@pytest.mark.parametrize("refresh", ["slow-signature", "stalled-volume", "cold-stalled-volume", "one-shot"])
def test_forked_prompt_never_shows_resolved_login_alias(replay, monkeypatch, refresh):
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

        def slow_signature(keys):
            time.sleep(0.015)  # Every warm walk exceeds the prompt's 5ms window.
            return original_signature(keys)

        def stalled_volume(path):
            if str(path) == "/Volumes":
                (replay.root / "volume-stalled").touch()
                time.sleep(2)
            return original_listdir(path)

        def write(fd, data):
            if fd == 1 and data == b"ready\n" and refresh == "stalled-volume":
                agent_view.os.listdir = stalled_volume
            original_write(fd, data)

        if refresh in ("slow-signature", "one-shot"):
            agent_view._signature = slow_signature
        recall_prompt._write_all = write
        if refresh == "cold-stalled-volume":
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
        for index in range(2):
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
                if refresh == "cold-stalled-volume":
                    assert answer == {"skip": "error-InventoryUnavailable", "done": answer["done"]}
                    assert not recall_prompt.routing_marker(f"alias-{index}")
                else:
                    assert answer["kind"] == "full", answer
                    assert "Use " in answer["text"]
                    assert "Work: open" in answer["text"]
                    assert answer["routing_changes"]["batch"]
                if refresh != "slow-signature":
                    assert (replay.root / "volume-stalled").exists()
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
