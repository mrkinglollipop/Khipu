"""Antigravity capture (docs/plans/2026-10-27-cursor-antigravity-hooks.md
section 5.3 capture, slice D): the transcript reader, the PostInvocation/Stop
cadence, the idle-sweep backstop and the liveness heartbeat. Hermetic: fake
HOME and capture home, a transcript fixture built from the documented step
format, no real ~/.gemini."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from khipu import hook_shape, session_capture as sc

CONV = "ag-conv-0001"
BIN = Path(__file__).resolve().parents[1] / "bin"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_PARENT_SESSION", "")
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "no-t3.sqlite"))
    for name in list(os.environ):
        if name.startswith(("CLAUDE", "CODEX", "CURSOR_", "GROK_")) or name in ("KHIPU_HARNESS", "CLAUDECODE"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("khipu.identity.resolve_repo_root", lambda cwd: {
        "repo_root": "/repo", "project": "acme/project", "is_worktree": False})
    return tmp_path


def transcript_file(home: Path, surface: str = "antigravity", conv: str = CONV) -> Path:
    return home / ".gemini" / surface / "brain" / conv / ".system_generated" / "logs" / "transcript.jsonl"


def step(kind: str, content, idx: int, **extra) -> dict:
    return {"type": kind, "content": content, "step_index": idx, "created_at": "2026-10-27T12:00:00Z",
            "source": "MODEL" if kind != "USER_INPUT" else "USER_EXPLICIT", **extra}


def turn(n: int) -> list[dict]:
    """One user turn: a question, a tool call, a tool-ish step the reader drops, an answer."""
    base = n * 4
    return [step("USER_INPUT", f"<USER_REQUEST>Question {n} " + "q" * 80 + "</USER_REQUEST>", base),
            step("AGENCY_TOOL_CALL", {"name": "run_command", "args": {"CommandLine": "ls"}}, base + 1),
            step("CHECKPOINT", "internal bookkeeping that is not a turn", base + 2),
            step("PLANNER_RESPONSE", f"Answer {n} " + "a" * 80, base + 3)]


def write(path: Path, steps: list[dict], *, mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open(mode, encoding="utf-8") as f:
        for s in steps:
            f.write(json.dumps(s) + "\n")


def payload(path: Path, **extra) -> str:
    return json.dumps({"conversationId": CONV, "workspacePaths": ["/work/khipu"], "modelName": "gemini-3.8-flash",
                       "transcriptPath": str(path), "artifactDirectoryPath": str(path.parents[2]), **extra})


def jobs() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sc.queued_jobs()]


# ---- the reader -------------------------------------------------------------------

def test_reader_yields_user_assistant_and_one_line_tools(home):
    path = transcript_file(home)
    write(path, turn(0) + turn(1))
    msgs, end, users = sc.read_window(path, 0)
    assert users == 2
    assert [r for r, _ in msgs] == ["user", "tool", "assistant", "user", "tool", "assistant"]
    assert msgs[0][1].startswith("Question 0 ") and "<USER_REQUEST>" not in msgs[0][1]
    assert msgs[1] == ("tool", "run_command")
    assert msgs[2][1].startswith("Answer 0 ")
    assert end == path.stat().st_size
    assert "internal bookkeeping" not in sc.render(msgs, max_chars=10**9)


def test_reader_tolerates_content_shapes_and_partial_trailing_line(home):
    path = transcript_file(home)
    write(path, [step("USER_INPUT", {"text": "dict user " + "x" * 50}, 0),
                 step("PLANNER_RESPONSE", [{"text": "part one"}, "part two"], 1),
                 step("AGENCY_TOOL_CALL", "view_file /a/b.py\nmore", 2),
                 step("AGENCY_TOOL_CALL", {"toolCall": {"name": "grep_search"}}, 3),
                 step("PLANNER_RESPONSE", None, 4), step("USER_INPUT", "   ", 5)])
    with path.open("a", encoding="utf-8") as f:
        f.write('{"type": "USER_INPUT", "content": "half writ')   # a step still being written
    msgs, end, users = sc.read_window(path, 0)
    assert users == 1
    assert msgs == [("user", "dict user " + "x" * 50), ("assistant", "part one\npart two"),
                    ("tool", "view_file /a/b.py"), ("tool", "grep_search")]
    assert end < path.stat().st_size                       # the half line waits for next time
    with path.open("a", encoding="utf-8") as f:
        f.write('ten"}\n')
    msgs2, _, users2 = sc.read_window(path, end)
    assert users2 == 1 and msgs2 == [("user", "half written")]


def test_other_harness_transcripts_are_unchanged_by_the_new_branch():
    assert sc._parse_line({"type": "user", "message": {"role": "user", "content": "hi"}}) == [("user", "hi")]
    assert sc._parse_line({"type": "event_msg", "payload": {"type": "user_message", "message": "yo"}}) == [("user", "yo")]


# ---- event naming -----------------------------------------------------------------

def test_event_comes_from_the_argument_else_from_the_payload_fields():
    post = {"conversationId": "c", "invocationNum": 3}
    stop = {"conversationId": "c", "executionNum": 1, "terminationReason": "idle", "fullyIdle": True}
    end = {"conversationId": "c", "executionNum": 1, "terminationReason": "session_end"}
    assert sc.hook_event(post, "antigravity", "PostInvocation") == "postinvocation"
    assert sc.hook_event(post, "antigravity") == "postinvocation"
    assert sc.hook_event(stop, "antigravity") == "stop"
    assert sc.hook_event(stop, "antigravity", "Stop") == "stop"
    assert sc.hook_event(end, "antigravity", "Stop") == "sessionend"      # T3's one Stop is the session's end
    assert sc.hook_event({"conversationId": "c"}, "antigravity") == ""
    assert sc.hook_event({"hook_event_name": "stop"}, "cursor") == "stop"
    assert sc.hook_event({"hookEventName": "SessionEnd"}, "claude_code") == "sessionend"


# ---- cadence ----------------------------------------------------------------------

def test_post_invocation_queues_only_at_five_user_turns(home):
    path = transcript_file(home)
    write(path, [])
    sc.hook_main(payload(path, invocationNum=1), None, "PostInvocation")           # first sight: starts the clock
    for n in range(4):
        write(path, turn(n), mode="a")
        out = sc.hook_main(payload(path, invocationNum=n + 2), None, "PostInvocation")
        assert out["harness"] == "antigravity" and out["event"] == "postinvocation"
        assert out["due"] is False and out["new_turns"] == n + 1
    assert jobs() == []
    write(path, turn(4), mode="a")
    out = sc.hook_main(payload(path, invocationNum=6), None, "PostInvocation")
    assert out["due"] is True and out["reason"] == f"turns>={sc.MIN_TURNS}" and out["new_turns"] == 5
    (job,) = jobs()
    assert job["harness"] == "antigravity" and job["session_id"] == CONV and job["event"] == "postinvocation"
    assert job["cwd"] == "/work/khipu" and job["turns"] == 5
    assert "USER: Question 0 " in job["transcript"] and "ASSISTANT: Answer 4 " in job["transcript"]
    assert "run_command" in job["transcript"] and "internal bookkeeping" not in job["transcript"]
    assert (job["offset_before"], job["offset_after"]) == (0, path.stat().st_size)
    # The next window starts where this one ended: the same turns are never queued twice.
    out = sc.hook_main(payload(path, invocationNum=7), None, "PostInvocation")
    assert out["due"] is False and out["new_turns"] == 0 and len(jobs()) == 1


def test_post_invocation_time_cadence_and_stop_use_the_same_rule(home):
    path = transcript_file(home)
    write(path, turn(0))
    sc.hook_main(payload(path, executionNum=1, terminationReason="idle", fullyIdle=True))   # event inferred: stop
    st = sc.load_state("antigravity", CONV)
    st["last_ts"] = st["window_started_ts"] = time.time() - sc.MIN_MINUTES * 60 - 5
    sc.save_state("antigravity", CONV, st)
    out = sc.hook_main(payload(path, executionNum=2, terminationReason="idle", fullyIdle=True))
    assert out["event"] == "stop" and out["due"] is True and out["reason"].startswith("elapsed>=")


def test_session_end_stop_is_always_due_and_a_small_window_is_not(home):
    path = transcript_file(home)
    write(path, [])
    sc.hook_main(payload(path, invocationNum=1), None, "PostInvocation")
    write(path, turn(0), mode="a")
    out = sc.hook_main(payload(path, executionNum=1, terminationReason="session_end"), None, "Stop")
    assert out["event"] == "sessionend" and out["due"] is True and len(jobs()) == 1
    path2 = transcript_file(home, conv="tiny")
    write(path2, [step("USER_INPUT", "hi", 0)])
    out = sc.hook_main(payload(path2).replace(CONV, "tiny"), None, "PostInvocation")
    assert out["due"] is False and out["reason"].startswith("nothing new")


def test_acp_session_uses_the_antigravity_harness_and_t3_surface(home):
    path = transcript_file(home).parents[5] / ".t3" / "userdata" / "providers" / "antigravity" / "ab12" \
        / "antigravity-acp" / "brain" / CONV / ".system_generated" / "logs" / "transcript.jsonl"
    write(path, turn(0))
    raw = payload(path, executionNum=1, terminationReason="session_end")
    assert hook_shape.detect(json.loads(raw), {}) == ("antigravity", "antigravity")
    assert hook_shape.antigravity_entrypoint(json.loads(raw)) == "t3"
    out = sc.hook_main(raw, None, "Stop")
    assert out["harness"] == "antigravity" and out["due"] is True


def test_missing_transcript_path_falls_back_to_the_brain_folder(home):
    path = transcript_file(home, surface="antigravity-cli")
    write(path, turn(0))
    env = {"conversationId": CONV, "workspacePaths": ["/w"]}
    assert sc.transcript_path(env, "antigravity") == path
    assert sc.transcript_path({"conversationId": "other"}, "antigravity") is None


# ---- heartbeat, liveness, idle sweep -----------------------------------------------

def test_every_hook_run_writes_the_antigravity_heartbeat(home):
    path = transcript_file(home)
    write(path, turn(0))
    sc.hook_main(payload(path, invocationNum=1), None, "PostInvocation")
    beat = json.loads((home / "capture" / "dispatch" / "antigravity.json").read_text())
    assert beat["harness"] == "antigravity" and beat["event"] == "postinvocation" and beat["dispatches"] == 1
    assert sc.liveness("antigravity")["ok"] is True and sc.liveness("antigravity")["seen"] is True
    assert "antigravity" in sc.liveness_all()["harnesses"]
    # A hook that could not find its transcript is a visible, counted event, not silence.
    gone = home / "nowhere" / "transcript.jsonl"
    sc.hook_main(payload(gone, invocationNum=2).replace(CONV, "brand-new"), None, "PostInvocation")
    live = sc.liveness("antigravity")
    assert live["ok"] is True, live["reasons"]         # never written yet: not evidence of failure
    # But a session Khipu had read turns from losing its transcript is red.
    sc.hook_main(payload(path.with_name("moved.jsonl"), invocationNum=3), None, "PostInvocation")
    assert any("lost their transcript" in r for r in sc.liveness("antigravity")["reasons"])


def test_liveness_turns_red_when_turns_land_the_hook_never_saw(home):
    path = transcript_file(home)
    write(path, turn(0))
    sc.hook_main(payload(path, invocationNum=1), None, "PostInvocation")
    assert sc.liveness("antigravity")["ok"] is True
    # The hook goes quiet while the user keeps working: a new turn lands well after its last look,
    # then the transcript sits still longer than a Stop could take.
    now = time.time()
    seen, wrote = now - 3 * sc.HOOK_SILENT_S, now - 1.5 * sc.HOOK_SILENT_S
    st = sc.load_state("antigravity", CONV)
    st["seen_ts"] = seen
    sc.save_state("antigravity", CONV, st)
    write(path, turn(1), mode="a")
    os.utime(path, (wrote, wrote))
    live = sc.liveness("antigravity")
    assert live["ok"] is False
    assert any("the hook has stopped firing" in r for r in live["reasons"])
    assert sc.liveness_all()["red"] == ["antigravity"]


def test_the_idle_sweep_queues_an_antigravity_window_no_hook_event_closed(home):
    path = transcript_file(home)
    write(path, [])
    sc.hook_main(payload(path, invocationNum=1), None, "PostInvocation")
    write(path, turn(0) + turn(1), mode="a")                  # the app was closed: no Stop, no end event
    old = time.time() - sc.MIN_MINUTES * 60 - 60
    os.utime(path, (old, old))
    st = sc.load_state("antigravity", CONV)
    st["seen_ts"] = old
    sc.save_state("antigravity", CONV, st)
    out = sc.sweep_idle(now=time.time())
    assert out["swept"] == 1 and out["jobs"] == 1
    (job,) = jobs()
    assert job["harness"] == "antigravity" and job["event"] == "idle_sweep" and job["turns"] == 2
    assert "Answer 1 " in job["transcript"]
    assert sc.sweep_idle(now=time.time())["swept"] == 0       # nothing unread now


# ---- the shim -----------------------------------------------------------------------

def test_stop_hook_script_takes_an_event_argument_and_post_invocation_only_queues(home, tmp_path):
    path = transcript_file(home)
    write(path, [])
    repo = Path(__file__).resolve().parents[3]
    env = {**os.environ, "HOME": str(home), "KHIPU_CAPTURE_HOME": str(home / "capture"),
           "KHIPU_ROOT": str(repo), "KHIPU_PYTHON": sys.executable, "KHIPU_MEMORY_ROOT": "",
           "PYTHONPYCACHEPREFIX": str(tmp_path / "pyc"), "PYTHONPATH": os.pathsep.join(
               [str(repo / "packages" / "cli"), str(repo / ".python_libs")]),
           "KHIPU_PARENT_SESSION": "", "KHIPU_T3_DB": str(home / "no-t3.sqlite")}

    def run(*args: str, **extra) -> str:
        subprocess.run([str(BIN / "khipu-stop-hook"), *args], input=payload(path, **extra), env=env,
                       text=True, check=True, timeout=60, capture_output=True)
        return (home / "Library" / "Logs" / "khipu" / "stop-hook.log").read_text()

    run("--event", "PostInvocation", invocationNum=1)
    write(path, [s for n in range(5) for s in turn(n)], mode="a")
    log = run("--event", "PostInvocation", invocationNum=2)
    assert "capture antigravity postinvocation due=True" in log
    assert "background pid=" not in log                          # PostInvocation never starts a drain
    assert len(jobs()) == 1
    write(path, turn(5), mode="a")
    log = run("--event", "Stop", executionNum=1, terminationReason="session_end")
    assert "capture antigravity sessionend due=True" in log
