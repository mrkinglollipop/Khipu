"""Routing slice 19a fix round 2: the pushed memory slice never carries
a routing direction's words. Only the direction's own project's slice
names it, and then only as a count and its id; a job's slice and another
project's slice name nothing at all. Hermetic: a temp capture home and
data dir, fake data only, no hub, no model."""
from __future__ import annotations

import json
import time

import pytest

from khipu import owner_turns, recall_rule, routing_directions

SESSION = "s-19a-fix2"
CLAUDE_ENV = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "cli"}
GLM = "use GLM only one job at a time"
PROJECTS = {"/work": "proj", "/elsewhere": "other"}


@pytest.fixture
def world(tmp_path, monkeypatch, khipu_app):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": [
        {"label": "Claude", "harness": "claude"}, {"label": "GLM", "harness": "claude"}]}))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    for name in ("KHIPU_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    khipu_app.as_shell("/opt/harness/claude")
    owner_turns.write_settings({"enabled": True})
    from khipu import activity, identity

    monkeypatch.setattr(identity, "resolve_repo_root",
                        lambda cwd: {"repo_root": cwd, "project": PROJECTS.get(cwd)})
    monkeypatch.setattr(activity, "project_slice",
                        lambda **kw: {"commitments": [],
                                      "episodes": [{"id": 5, "summary": "fake episode"}],
                                      "topics": [], "decisions": []})
    return tmp_path


def say_and_scan():
    owner_turns.record({"session_id": SESSION, "prompt": GLM, "cwd": "/work"},
                       CLAUDE_ENV, now=time.time())
    (row,) = routing_directions.capture_scan(SESSION, [GLM])
    return row


def test_an_unrelated_projects_slice_names_nothing(world):
    row = say_and_scan()
    text = recall_rule._pushed_memory_slice("/elsewhere")
    assert "fake episode" in text  # the other project's own slice still renders
    assert GLM not in text and "use GLM" not in text
    assert row["id"] not in text and "routing direction" not in text.lower()


def test_a_job_slice_names_nothing(world):
    row = say_and_scan()
    text = recall_rule._pushed_memory_slice("/work", job=True)
    assert "fake episode" in text
    assert GLM not in text and "use GLM" not in text
    assert row["id"] not in text and "routing direction" not in text.lower()


def test_the_owners_own_session_slice_names_only_a_count_and_the_id(world):
    row = say_and_scan()
    text = recall_rule._pushed_memory_slice("/work")
    assert GLM not in text and "Routing said here" not in text
    assert f"1 routing direction not yet in Khipu: {row['id']}; khipu owed shows them" in text
    # The owner-facing owed listing is unchanged: it still names the words.
    (item,) = routing_directions.owed_items()
    assert item["words"] == GLM
