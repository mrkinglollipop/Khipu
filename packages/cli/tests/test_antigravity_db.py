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
