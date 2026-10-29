"""T3's managed Antigravity runtime keeps its trajectory in SQLite, not JSONL
(real-run 2026-10-29, runtime 1.3.0). The fixture db is built here with the
real table shape and protobuf payloads encoded to the field paths found in a
real session: step 14 text at 19.2, step 15 text at 20.1."""
from __future__ import annotations

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
