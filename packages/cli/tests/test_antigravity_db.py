"""T3's managed Antigravity runtime keeps its trajectory in SQLite, not JSONL
(real-run 2026-10-29, runtime 1.3.0). The fixture db is built here with the
real table shape and protobuf payloads encoded to the field paths found in a
real session: step 14 text at 19.2, step 15 text at 20.1."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from khipu import antigravity_db as adb, owner_turns, session_capture as sc

HEADER = "<t3_code_instructions>\n## T3 rules\n" + "x" * 300 + "\n</t3_code_instructions>\n"


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _ld(field: int, data: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def _int(field: int, n: int) -> bytes:
    return _varint(field << 3) + _varint(n)


def user_payload(typed: str) -> bytes:
    text = (HEADER + f"<runtime_info>x</runtime_info>\n<user_request>\n{typed}\n</user_request>\n").encode()
    return _int(1, 14) + _int(4, 3) + _ld(5, _int(3, 4)) + _ld(19, _ld(2, text) + _ld(3, _ld(1, text)))


def model_payload(reply: str) -> bytes:
    body = reply.encode()
    return _int(1, 15) + _ld(5, _int(3, 2) + _ld(12, b"conv")) + _ld(20, _ld(1, body) + _ld(6, b"id") + _ld(8, body))


def make_db(path: Path, steps: list[tuple[int, int, bytes]], *, wal: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE `steps` (`idx` integer,`step_type` integer NOT NULL DEFAULT 0,"
                 "`status` integer NOT NULL DEFAULT 0,`step_payload` blob,PRIMARY KEY (`idx`))")
    conn.executemany("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (?,?,3,?)", steps)
    conn.commit()
    conn.close()
    return path


def db_path(tmp_path: Path, conv: str = "ag-conv") -> Path:
    return tmp_path / ".t3" / "tools" / "antigravity-acp" / "trajectories" / f"{conv}.db"


def listing(directory: Path) -> list[str]:
    return sorted(os.listdir(directory))


def test_reader_gets_user_and_assistant_text_with_the_header_stripped(tmp_path):
    path = make_db(db_path(tmp_path), [(0, 14, user_payload("fix the build")), (1, 15, model_payload("on it, done")),
                                       (2, 7, b"\x08\x07"), (3, 14, user_payload("now the tests"))])
    turns = adb.read_turns(path)
    assert turns == [(0, "user", "fix the build"), (1, "assistant", "on it, done"), (3, "user", "now the tests")]
    assert adb.last_user_input(path) == ("step-3", "now the tests")


def test_garbage_and_truncated_payloads_are_skipped_not_raised(tmp_path):
    good = user_payload("real question")
    path = make_db(db_path(tmp_path), [(0, 14, b"\xff\xff\xff\xff\xff"), (1, 15, good[:-5]), (2, 15, b""),
                                       (3, 14, None), (4, 14, good), (5, 15, _ld(20, b"\xc3\x28"))])
    assert adb.read_turns(path) == [(4, "user", "real question")]
    assert adb.read_turns(tmp_path / "missing.db") == []
    bad = tmp_path / "not-sqlite.db"
    bad.write_bytes(b"this is not a database" * 50)
    assert adb.read_turns(bad) == []


def test_wal_and_plain_files_are_read_without_creating_anything_beside_them(tmp_path):
    for wal in (False, True):
        path = make_db(db_path(tmp_path, f"c{wal}"), [(0, 14, user_payload("hello there"))], wal=wal)
        before = listing(path.parent)
        assert adb.read_turns(path) == [(0, "user", "hello there")]
        assert adb.last_user_input(path) == ("step-0", "hello there")
        assert listing(path.parent) == before
        assert not [n for n in listing(path.parent) if n.startswith("khipu-ag-")]


def test_a_live_wal_with_uncheckpointed_rows_is_read_from_a_private_copy(tmp_path):
    path = make_db(db_path(tmp_path), [(0, 14, user_payload("first"))], wal=True)
    writer = sqlite3.connect(path)          # stays open: the WAL holds the next row, uncheckpointed
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (1,15,3,?)", (model_payload("reply"),))
    writer.commit()
    before = listing(path.parent)
    assert [t[1] for t in adb.read_turns(path)] == ["user", "assistant"]
    assert listing(path.parent) == before
    writer.close()


def _pre(path: Path, **extra) -> dict:
    return {"conversationId": "ag-conv", "workspacePaths": ["/work/khipu"], "transcriptPath": str(path),
            "invocationNum": 1, "initialNumSteps": 0, **extra}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_PARENT_SESSION", "")
    monkeypatch.setenv("KHIPU_T3_DB", str(tmp_path / "no-t3.sqlite"))
    for name in list(os.environ):
        if name.startswith(("CLAUDE", "CODEX", "CURSOR_", "GROK_")) or name in ("KHIPU_HARNESS", "CLAUDECODE"):
            monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_prompt_hook_records_a_db_turn_once(home):
    path = make_db(db_path(home), [(0, 14, user_payload("fix the build"))], wal=True)
    first = owner_turns.antigravity_turn(_pre(path), wait=0)
    assert first["prompt"] == "fix the build" and first["cwd"] == "/work/khipu"
    assert owner_turns.antigravity_turn(_pre(path, invocationNum=2), wait=0) == {}
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (1,15,3,?)", (model_payload("ok"),))
    conn.execute("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (2,14,3,?)", (user_payload("next"),))
    conn.commit()
    conn.close()
    assert owner_turns.antigravity_turn(_pre(path, invocationNum=3), wait=0)["prompt"] == "next"
    assert owner_turns.antigravity_turn(_pre(path, invocationNum=4), wait=0) == {}


# ---- a turn the PreInvocation could not read yet (T3, 2026-10-29) -------------------------

ISSUER = {"refused": "test"}


def _rows(session: str = "ag-conv") -> list[dict]:
    path = owner_turns.session_file(session)
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _add(path: Path, idx: int, step_type: int, payload: bytes) -> None:
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (?,?,3,?)", (idx, step_type, payload))
    conn.commit()
    conn.close()


def test_an_unflushed_pre_invocation_is_recorded_once_at_post_invocation_with_its_own_time(home):
    path = make_db(db_path(home), [], wal=True)                                   # USER_INPUT not written yet
    assert owner_turns.antigravity_prompt(_pre(path), now=1000.0, wait=0, issuer=ISSUER) is None
    assert _rows() == []
    _add(path, 0, 14, user_payload("fix the build"))
    turn = owner_turns.antigravity_late_turn(_pre(path), "PostInvocation", now=1004.0, issuer=ISSUER)
    assert turn and [r["ts"] for r in _rows()] == [1000.0]                       # the PreInvocation's time
    assert owner_turns.antigravity_late_turn(_pre(path), "PostInvocation", now=1006.0, issuer=ISSUER) is None
    assert owner_turns.antigravity_late_turn(_pre(path), "Stop", now=1009.0, issuer=ISSUER) is None
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=2), now=1010.0, wait=0, issuer=ISSUER) == {}
    assert len(_rows()) == 1 and _rows()[0]["harness"] == "antigravity"


def test_a_turn_recorded_at_pre_invocation_is_not_recorded_again_later(home):
    path = make_db(db_path(home), [(0, 14, user_payload("fix the build"))], wal=True)
    assert owner_turns.antigravity_prompt(_pre(path), now=1000.0, wait=0, issuer=ISSUER)["prompt"] == "fix the build"
    assert len(_rows()) == 1
    assert owner_turns.antigravity_late_turn(_pre(path), "PostInvocation", now=1002.0, issuer=ISSUER) is None
    assert owner_turns.antigravity_late_turn(_pre(path), "Stop", now=1003.0, issuer=ISSUER) is None
    assert [r["ts"] for r in _rows()] == [1000.0]


def test_stop_alone_records_the_turn_once(home):
    path = make_db(db_path(home), [(0, 14, user_payload("fix the build")), (1, 15, model_payload("done"))], wal=True)
    assert owner_turns.antigravity_late_turn(_pre(path), "Stop", now=1000.0, issuer=ISSUER)
    assert owner_turns.antigravity_late_turn(_pre(path), "Stop", now=1001.0, issuer=ISSUER) is None
    assert [r["ts"] for r in _rows()] == [1000.0]


def test_a_later_turn_takes_the_time_of_its_own_first_unflushed_call_not_the_last_turns(home):
    path = make_db(db_path(home), [(0, 14, user_payload("first"))], wal=True)
    owner_turns.antigravity_prompt(_pre(path), now=1000.0, wait=0, issuer=ISSUER)
    # More model calls of the first turn, then its Stop.
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=2), now=1005.0, wait=0, issuer=ISSUER) == {}
    owner_turns.antigravity_late_turn(_pre(path), "Stop", now=1010.0, issuer=ISSUER)
    # The second turn's PreInvocation still sees only the first turn (its
    # initialNumSteps is the second turn's step index, as T3 sends it).
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=3, initialNumSteps=1), now=2000.0, wait=0,
                                          issuer=ISSUER) == {}
    _add(path, 1, 14, user_payload("second"))
    assert owner_turns.antigravity_late_turn(_pre(path), "PostInvocation", now=2003.0, issuer=ISSUER)
    assert [r["ts"] for r in _rows()] == [1000.0, 2000.0]
    # A turn claimed at its first PreInvocation keeps that call's own time.
    owner_turns.antigravity_late_turn(_pre(path), "Stop", now=2010.0, issuer=ISSUER)
    _add(path, 2, 14, user_payload("third"))
    assert owner_turns.antigravity_prompt(_pre(path, initialNumSteps=2), now=3000.0, wait=0,
                                          issuer=ISSUER)["prompt"] == "third"
    assert [r["ts"] for r in _rows()] == [1000.0, 2000.0, 3000.0]


def test_a_pending_note_from_a_model_call_of_one_turn_never_dates_the_next_turn(home):
    """Codex 2026-10-29: turn A at 1000, a later model call of A leaves a note at
    1010, turn B arrives at 1100 before any Stop: B is dated 1100, not 1010."""
    path = make_db(db_path(home), [(0, 14, user_payload("first"))], wal=True)
    assert owner_turns.antigravity_prompt(_pre(path), now=1000.0, wait=0, issuer=ISSUER)["prompt"] == "first"
    _add(path, 1, 15, model_payload("working"))
    # A's later model calls: T3 counts the turn's initial steps (0), a runtime
    # counting per call would send the steps so far (2); neither may date B.
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=2), now=1010.0, wait=0, issuer=ISSUER) == {}
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=3, initialNumSteps=2), now=1020.0, wait=0,
                                          issuer=ISSUER) == {}
    _add(path, 2, 15, model_payload("done"))
    _add(path, 3, 14, user_payload("second"))
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=4, initialNumSteps=3), now=1100.0, wait=0,
                                          issuer=ISSUER)["prompt"] == "second"
    assert [r["ts"] for r in _rows()] == [1000.0, 1100.0]
    # A further turn with no unflushed call of its own takes the late event's time.
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=5, initialNumSteps=3), now=1110.0, wait=0,
                                          issuer=ISSUER) == {}
    _add(path, 4, 14, user_payload("third"))
    assert owner_turns.antigravity_late_turn(_pre(path), "PostInvocation", now=1200.0, issuer=ISSUER)
    assert [r["ts"] for r in _rows()] == [1000.0, 1100.0, 1200.0]


def test_a_turn_recorded_from_last_user_input_may_be_recorded_again_once_flushed(home):
    """Text-alias suppression is gone on purpose: a duplicate row is acceptable,
    a lost turn is not."""
    path = make_db(db_path(home), [], wal=True)
    first = owner_turns.antigravity_prompt(_pre(path, lastUserInput="fix the build"), now=1000.0, wait=0,
                                           issuer=ISSUER)
    assert first["prompt"] == "fix the build" and len(_rows()) == 1
    _add(path, 0, 14, user_payload("fix the build"))
    assert owner_turns.antigravity_late_turn(_pre(path), "PostInvocation", now=1004.0, issuer=ISSUER)
    assert owner_turns.antigravity_late_turn(_pre(path), "Stop", now=1005.0, issuer=ISSUER) is None
    assert [r["ts"] for r in _rows()] == [1000.0, 1004.0]


def test_late_events_ignore_other_harnesses_and_prompted_payloads(home):
    path = make_db(db_path(home), [(0, 14, user_payload("fix the build"))])
    assert owner_turns.antigravity_late_turn({"session_id": "cc", "prompt": "hi", "cwd": "/w",
                                              "transcript_path": "/u/.claude/p/s.jsonl"}, "Stop", issuer=ISSUER) is None
    assert owner_turns.antigravity_late_turn(_pre(path, prompt="typed"), "Stop", issuer=ISSUER) is None
    assert owner_turns.antigravity_late_turn("nope", "Stop", issuer=ISSUER) is None
    assert _rows() == []


def test_capture_reads_the_db_turns_and_resumes_by_offset(home):
    path = make_db(db_path(home), [(0, 14, user_payload("Question one " + "q" * 60)),
                                   (1, 15, model_payload("Answer one " + "a" * 60))])
    msgs, end, users = sc.read_window(path, 0)
    assert users == 1 and [r for r, _ in msgs] == ["user", "assistant"]
    assert msgs[0][1].startswith("Question one ") and "t3_code_instructions" not in msgs[0][1]
    assert sc.read_window(path, end)[0] == []
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (2,14,3,?)", (user_payload("Question two"),))
    conn.commit()
    conn.close()
    msgs2, end2, users2 = sc.read_window(path, end)
    assert users2 == 1 and msgs2 == [("user", "Question two")] and end2 > end


# ---- status honesty for runtime 1.3.0 ----------------------------------------------

def test_status_reports_mcp_and_rule_unsupported_for_the_proven_runtime(home):
    from khipu import t3_antigravity as ag

    project = home / "proj"
    project.mkdir()
    assert ag.install_project(str(project), home=home)["ok"]
    plain = ag.project_status(project, home)
    assert plain["unsupported"] == [] and plain["proof"] is None and plain["mcp"] and plain["rule"]
    assert ag.functions_for("inst", home)["functions"]["mcp"] == "installed"
    (home / ".t3" / "tools" / "antigravity-acp" / "darwin-arm64" / "versions" / "1.3.0").mkdir(parents=True)
    proven = ag.project_status(project, home)
    assert proven["unsupported"] == ["mcp", "rule"] and proven["proof"] == "real-run 2026-10-29"
    found = ag.functions_for("inst", home)
    assert found["functions"]["mcp"] == "unsupported" and found["functions"]["capture"] == "installed"
    assert found["proof"] == {"mcp": "real-run 2026-10-29", "rule": "real-run 2026-10-29"}


# ---- fix round 1 (Codex review 2026-10-29) -----------------------------------------

AG_CONV = "ag-conv"


def _capture_home(home, monkeypatch):
    monkeypatch.setattr("khipu.identity.resolve_repo_root", lambda cwd: {
        "repo_root": "/repo", "project": "acme/project", "is_worktree": False})


def _stop(path: Path, n: int) -> dict:
    import json
    raw = json.dumps({"conversationId": AG_CONV, "workspacePaths": ["/work/khipu"], "transcriptPath": str(path),
                      "executionNum": n, "terminationReason": "session_end"})
    return sc.hook_main(raw, None, "Stop")


def _job_texts() -> list[str]:
    import json
    return [json.dumps(json.loads(p.read_text(encoding="utf-8"))) for p in sc.queued_jobs()]


def test_a_wal_rewrite_of_captured_rows_resets_capture_without_replaying_kept_turns(home, monkeypatch):
    _capture_home(home, monkeypatch)
    path = make_db(db_path(home), [(0, 14, user_payload("Question one " + "q" * 150)),
                                   (1, 15, model_payload("Answer one " + "a" * 150))], wal=True)
    writer = sqlite3.connect(path)          # stays open: every change below sits in the WAL
    writer.execute("PRAGMA wal_autocheckpoint=0")
    assert _stop(path, 1)["due"] is True
    db_stat = path.stat()
    writer.execute("UPDATE steps SET step_payload=? WHERE idx=1",
                   (model_payload("Replaced answer " + "r" * 400),))
    writer.commit()
    assert (path.stat().st_size, path.stat().st_mtime_ns) == (db_stat.st_size, db_stat.st_mtime_ns)
    out = _stop(path, 2)
    texts = _job_texts()
    assert out["due"] is True and len(texts) == 2, out
    new = [t for t in texts if "Replaced answer" in t]
    assert new and "r" * 400 in new[0]
    assert "Question one" not in new[0]
    # The stream shrinks (the reply is gone) and a new question follows.
    writer.execute("DELETE FROM steps WHERE idx=1")
    writer.execute("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (2,14,3,?)",
                   (user_payload("Question two " + "w" * 250),))
    writer.commit()
    assert _stop(path, 3)["due"] is True
    texts = _job_texts()
    assert len(texts) == 3
    latest = [t for t in texts if "Question two" in t]
    assert latest and "Question one" not in latest[0]
    writer.close()


def test_first_capture_starts_from_the_reconstructed_stream_not_the_file_size(home, monkeypatch):
    _capture_home(home, monkeypatch)
    path = make_db(db_path(home), [(0, 14, user_payload("Question one " + "q" * 100)),
                                   (1, 15, model_payload("Answer one " + "a" * 1500)),
                                   (2, 7, os.urandom(60_000))])
    stream = len(adb.as_jsonl(path))
    physical = path.stat().st_size
    assert physical > 3 * stream
    # Old start = physical - tail = stream // 2, inside the reply; the whole
    # stream fits in the tail, so the first capture must start at 0.
    monkeypatch.setattr(sc, "FIRST_SIGHT_TAIL", physical - stream // 2)
    assert _stop(path, 1)["due"] is True
    texts = _job_texts()
    assert len(texts) == 1 and "Question one" in texts[0] and "a" * 1500 in texts[0]


def test_only_the_last_user_request_is_taken_and_instructions_naming_the_tag_do_not_leak():
    header = ("<t3_code_instructions>\nUse <user_request> tags. PRIVATE RULES never to be captured\n"
              "</t3_code_instructions>\n")
    text = (header + "<user_request>\nthe real question\n</user_request>\nTRAILING RULES\n").encode()
    payload = _ld(19, _ld(2, text))
    assert adb.decode_step(adb.USER_STEP, payload) == "the real question"


def test_non_blob_and_oversized_payloads_are_rejected_before_any_allocation(tmp_path):
    import tracemalloc
    tracemalloc.start()
    try:
        assert adb.decode_step(adb.USER_STEP, 50_000_000) == ""
        assert adb.decode_step(adb.USER_STEP, "text, not a blob") == ""
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000
    big = user_payload("huge " + "h" * (adb.MAX_PAYLOAD + 10))
    path = make_db(db_path(tmp_path), [(0, 14, 50_000_000), (1, 14, big), (2, 14, user_payload("small one"))])
    assert adb.read_turns(path) == [(2, "user", "small one")]


# ---- fix round 2 (Codex re-check 2026-10-29) ---------------------------------------

def test_unchanged_sessions_are_not_reconstructed_by_later_sweeps(home, monkeypatch):
    _capture_home(home, monkeypatch)
    path = make_db(db_path(home), [(0, 14, user_payload("Question one " + "q" * 150)),
                                   (1, 15, model_payload("Answer one " + "a" * 150))], wal=True)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (2,7,3,?)", (b"x",))
    writer.commit()
    assert _stop(path, 1)["due"] is True
    calls = []
    real = adb.as_jsonl
    monkeypatch.setattr(adb, "as_jsonl", lambda p: calls.append(p) or real(p))
    sc._DB_STREAMS.clear()          # a sweep is its own process: nothing cached in memory
    for _ in range(2):
        out = sc.sweep_idle()
        assert out["skipped"].get("nothing_unread") == 1 and out["read"] == 0, out
    assert calls == []
    # A real change is still seen.
    writer.execute("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (3,14,3,?)",
                   (user_payload("Question two " + "w" * 250),))
    writer.commit()
    sc._DB_STREAMS.clear()
    assert sc.sweep_idle()["skipped"].get("nothing_unread") is None
    writer.close()


def test_short_retained_steps_are_not_replayed_after_a_rewrite_and_a_short_repeat_still_counts(home, monkeypatch):
    _capture_home(home, monkeypatch)
    path = make_db(db_path(home), [(0, 14, user_payload("Question one " + "q" * 150)),
                                   (1, 15, model_payload("Answer one " + "a" * 150)),
                                   (2, 14, user_payload("keep this short question")),
                                   (3, 15, model_payload("short reply"))], wal=True)
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    assert _stop(path, 1)["due"] is True
    writer.execute("UPDATE steps SET step_payload=? WHERE idx=1",
                   (model_payload("Replaced answer " + "r" * 400),))
    writer.execute("INSERT INTO steps(idx, step_type, status, step_payload) VALUES (4,14,3,?)",
                   (user_payload("keep this short question"),))
    writer.commit()
    assert _stop(path, 2)["due"] is True
    new = [t for t in _job_texts() if "Replaced answer" in t]
    assert len(new) == 1
    assert "Question one" not in new[0] and "short reply" not in new[0]
    # Step 2 is a kept copy; step 4 is the person asking again.
    assert new[0].count("keep this short question") == 1
    writer.close()


def test_a_request_naming_the_tag_round_trips_exactly():
    typed = "Please explain the literal <user_request> tag and its </user_request> closer, please."
    text = (HEADER + "<runtime_info>x</runtime_info>\n<user_request>\n" + typed + "\n</user_request>\n").encode()
    assert adb.decode_step(adb.USER_STEP, _ld(19, _ld(2, text))) == typed


# ---- replays of the real T3 sequences (runtime 1.3.0, proof runs 1-5, 2026-10-29/30) ----------
#
# One PreInvocation per user turn (invocationNum 1, initialNumSteps 0), however
# many model calls the turn makes. Its PostInvocation carries the step count at
# the END of the invocation (4 after three tool calls), so Post's value never
# names the turn. A single-call turn shows Pre 0 / Post 0. A Stop with no
# invocation fields can fire before the first PreInvocation.

def _stop_payload(path: Path, **extra) -> dict:
    return {"conversationId": "ag-conv", "workspacePaths": ["/work/khipu"], "transcriptPath": str(path),
            "executionNum": 1, "terminationReason": "session_end", "error": "", **extra}


def _counted(now: float = 9999.0) -> list[int]:
    """Occurrences of the rows a reader counts: a duplicate copy never counts."""
    return [t["occurrence"] for t in owner_turns.turns("ag-conv", now) if not t.get("duplicate_of")]



def test_real_multi_call_turn_pre_0_post_4_then_a_retype_records_one_row_per_typing(home):
    path = make_db(db_path(home), [], wal=True)
    assert owner_turns.antigravity_late_turn(_stop_payload(path), "Stop", now=998.0, issuer=ISSUER) is None
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=1, initialNumSteps=0), now=1000.0, wait=0,
                                          issuer=ISSUER) is None
    for idx, kind, body in ((0, 14, user_payload("ship it")), (1, 15, model_payload("calling a tool")),
                            (2, 7, b"\x08\x07"), (3, 15, model_payload("done"))):
        _add(path, idx, kind, body)
    assert owner_turns.antigravity_late_turn(_pre(path, invocationNum=1, initialNumSteps=4), "PostInvocation",
                                             now=1018.0, issuer=ISSUER)
    assert owner_turns.antigravity_late_turn(_stop_payload(path), "Stop", now=1020.0, issuer=ISSUER) is None
    assert [r["ts"] for r in _rows()] == [1000.0] and _counted() == [1]
    # Turn 2 retypes the same words; its PreInvocation still sees turn 1's step.
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=2, initialNumSteps=4), now=2000.0, wait=0,
                                          issuer=ISSUER) == {}
    _add(path, 4, 14, user_payload("ship it"))
    _add(path, 5, 15, model_payload("again"))
    assert owner_turns.antigravity_late_turn(_pre(path, invocationNum=2, initialNumSteps=6), "PostInvocation",
                                             now=2010.0, issuer=ISSUER)
    assert owner_turns.antigravity_late_turn(_stop_payload(path), "Stop", now=2012.0, issuer=ISSUER) is None
    assert [r["ts"] for r in _rows()] == [1000.0, 2000.0] and _counted() == [1, 2]


def test_real_single_call_turns_pre_0_post_0_then_a_retype_records_one_row_per_typing(home):
    path = make_db(db_path(home), [], wal=True)
    assert owner_turns.antigravity_late_turn(_stop_payload(path), "Stop", now=999.0, issuer=ISSUER) is None
    assert owner_turns.antigravity_prompt(_pre(path), now=1000.0, wait=0, issuer=ISSUER) is None
    _add(path, 0, 14, user_payload("ship it"))
    _add(path, 1, 15, model_payload("done"))
    assert owner_turns.antigravity_late_turn(_pre(path), "PostInvocation", now=1010.0, issuer=ISSUER)
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=2, initialNumSteps=2), now=2000.0, wait=0,
                                          issuer=ISSUER) == {}
    _add(path, 2, 14, user_payload("ship it"))
    _add(path, 3, 15, model_payload("done"))
    assert owner_turns.antigravity_late_turn(_pre(path, invocationNum=2, initialNumSteps=2), "PostInvocation",
                                             now=2010.0, issuer=ISSUER)
    assert [r["ts"] for r in _rows()] == [1000.0, 2000.0] and _counted() == [1, 2]


def test_real_sequence_with_last_user_input_never_binds_post_or_stop_by_their_own_step_count(home):
    """The documented payload shape (``lastUserInput``) over the real event
    order, transcript still unwritten at Post and Stop: Post's end-of-call 4
    and a Stop with no step count are not new turns; each flushed step pairs
    with its own turn's PreInvocation row."""
    path = make_db(db_path(home), [], wal=True)
    said = {"lastUserInput": "ship it"}
    # A stray Stop before any PreInvocation records nothing, even with words.
    assert owner_turns.antigravity_late_turn(_stop_payload(path, **said), "Stop", now=998.0, issuer=ISSUER) is None
    assert owner_turns.antigravity_prompt(_pre(path, initialNumSteps=0, **said), now=1000.0, wait=0,
                                          issuer=ISSUER)["prompt"] == "ship it"
    assert owner_turns.antigravity_late_turn(_pre(path, initialNumSteps=4, **said), "PostInvocation",
                                             now=1018.0, issuer=ISSUER) is None
    assert owner_turns.antigravity_late_turn(_stop_payload(path, **said), "Stop", now=1020.0, issuer=ISSUER) is None
    assert _counted() == [1]
    _add(path, 0, 14, user_payload("ship it"))
    _add(path, 3, 15, model_payload("done"))
    assert owner_turns.antigravity_late_turn(_stop_payload(path, **said), "Stop", now=1030.0, issuer=ISSUER)
    rows = _rows()
    assert [r["occurrence"] for r in rows] == [1, 1] and rows[1]["duplicate_of"] == rows[0]["turn"]
    # Turn 2: the same words retyped. Its PreInvocation sees turn 1's step,
    # so it waits for its own; Post's 6 names no turn.
    assert owner_turns.antigravity_prompt(_pre(path, invocationNum=2, initialNumSteps=4, **said), now=2000.0,
                                          wait=0, issuer=ISSUER) == {}
    assert owner_turns.antigravity_late_turn(_pre(path, invocationNum=2, initialNumSteps=6, **said),
                                             "PostInvocation", now=2010.0, issuer=ISSUER) is None
    _add(path, 4, 14, user_payload("ship it"))
    assert owner_turns.antigravity_late_turn(_stop_payload(path, **said), "Stop", now=2020.0, issuer=ISSUER)
    rows = _rows()
    assert [r["occurrence"] for r in rows] == [1, 1, 2] and rows[2]["ts"] == 2000.0
    assert _counted() == [1, 2]


def test_a_late_last_user_input_is_bound_to_the_pre_invocation_that_could_not_read_it(home):
    """Pre had no words and no flushed step; Post brings the words with its
    end-of-call 4. The row is bound to Pre's 0, so the flushed step 0 pairs."""
    path = make_db(db_path(home), [], wal=True)
    assert owner_turns.antigravity_prompt(_pre(path), now=1000.0, wait=0, issuer=ISSUER) is None
    assert owner_turns.antigravity_late_turn(_pre(path, initialNumSteps=4, lastUserInput="ship it"),
                                             "PostInvocation", now=1018.0, issuer=ISSUER)
    _add(path, 0, 14, user_payload("ship it"))
    assert owner_turns.antigravity_late_turn(_stop_payload(path), "Stop", now=1020.0, issuer=ISSUER)
    assert [r["ts"] for r in _rows()] == [1000.0, 1000.0] and _counted() == [1]


def test_several_pre_invocations_of_one_readable_turn_still_record_once(home):
    """Not seen on 1.3.0, kept safe: a runtime firing one PreInvocation per
    model call with growing step counts records the turn once and leaves no
    note that dates the next turn."""
    path = make_db(db_path(home), [(0, 14, user_payload("ship it"))], wal=True)
    assert owner_turns.antigravity_prompt(_pre(path), now=1000.0, wait=0, issuer=ISSUER)["prompt"] == "ship it"
    for n, at in ((2, 1005.0), (4, 1010.0)):
        _add(path, n - 1, 15, model_payload("call"))
        assert owner_turns.antigravity_prompt(_pre(path, invocationNum=n, initialNumSteps=n), now=at, wait=0,
                                              issuer=ISSUER) == {}
    assert owner_turns.antigravity_late_turn(_pre(path, initialNumSteps=6), "PostInvocation", now=1012.0,
                                             issuer=ISSUER) is None
    _add(path, 6, 14, user_payload("ship it"))
    assert owner_turns.antigravity_late_turn(_pre(path, initialNumSteps=8), "PostInvocation", now=2000.0,
                                             issuer=ISSUER)
    assert [r["ts"] for r in _rows()] == [1000.0, 2000.0] and _counted() == [1, 2]
