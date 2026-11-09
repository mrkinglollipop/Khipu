"""The owner-actions log is a plain append-only JSON-lines file: every owner
action is a line with its receipt, Undo reads it, and nothing checks it for
tampering. It rolls into numbered segments beside it when it grows past a size
bound (a plain rename); segments an earlier version sealed, and the ``v``,
``prev`` and ``hash`` fields it wrote, are read the same way and otherwise ignored.
Hermetic: temp data and capture folders."""
from __future__ import annotations

import json
import threading

import pytest

from khipu import owner_direction as od
from khipu import owner_files


@pytest.fixture
def folders(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    return tmp_path


def _add(n, **extra):
    return od._append({"kind": "action", "id": f"a{n}", "auth": "app", "action": "route.lift",
                       "target": "codex|", **extra})


def _rows(path):
    return [json.loads(t) for t in path.read_text().splitlines() if t.strip()]


def test_a_new_line_is_written_as_given_with_no_chain_fields(folders):
    line = _add(0, words="lift Codex")
    (row,) = _rows(od.log_path())
    assert row == line == {"kind": "action", "id": "a0", "auth": "app", "action": "route.lift",
                           "target": "codex|", "words": "lift Codex"}
    assert not {"v", "prev", "hash", "words_sha"} & set(row)


def test_a_small_log_stays_one_file(folders):
    for n in range(2100):
        _add(n)
    assert od._sealed_paths() == [] and len(_rows(od.log_path())) == 2100 and len(od.read_log()) == 2100
    assert sorted(p.name for p in od.log_path().parent.iterdir() if p.name.startswith("owner-actions")) == [
        od.LOG_NAME]


def test_a_log_past_the_size_bound_rolls_into_the_next_numbered_segment(folders, monkeypatch):
    monkeypatch.setattr(od, "LOG_ROLL_BYTES", 2000)
    for n in range(60):
        _add(n, words="pause the model for a while please " * 3)
    names = sorted(p.name for p in od.log_path().parent.iterdir() if p.name.startswith("owner-actions"))
    segments = [n for n in names if n != od.LOG_NAME]
    assert len(segments) >= 3 and segments == [f"owner-actions.{i:06d}.jsonl" for i in range(1, len(segments) + 1)]
    # The live file is bounded (a line may carry it a little past the mark), and no line is lost or reordered.
    assert od.log_path().stat().st_size < 2000 + 400
    assert [r["id"] for r in od.read_log()] == [f"a{n}" for n in range(60)]
    assert [a["id"] for a in od.actions()] == [f"a{n}" for n in range(60)]
    # No chain fields, hashes or seals: the rolled lines are the lines as given.
    assert not any({"v", "prev", "hash"} & set(r) for r in od.read_log())


def test_a_roll_continues_the_numbering_of_segments_already_there(folders, monkeypatch):
    old = od.log_path().with_name("owner-actions.000004.jsonl")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({"kind": "action", "id": "old", "action": "route.pause", "target": "codex|",
                               "state": "applied", "at": "2026-11-01T00:00:00Z"}) + "\n", encoding="utf-8")
    monkeypatch.setattr(od, "LOG_ROLL_BYTES", 1)
    _add(1)
    _add(2)
    assert [p.name for p in od._sealed_paths()] == ["owner-actions.000004.jsonl", "owner-actions.000005.jsonl"]
    assert [r["id"] for r in od.read_log()] == ["old", "a1", "a2"]


def test_a_state_line_after_a_roll_completes_an_action_in_an_older_segment(folders, monkeypatch):
    od._append({"kind": "action", "id": "x1", "auth": "app", "action": "route.pause", "target": "codex|",
                "state": "pending"})
    monkeypatch.setattr(od, "LOG_ROLL_BYTES", 1)
    od._append({"kind": "state", "id": "s1", "at": "2026-11-09T00:00:00Z", "target": "x1", "state": "applied"})
    assert [p.name for p in od._sealed_paths()] == ["owner-actions.000001.jsonl"]
    assert od.line_state("x1") == "applied" and [a["id"] for a in od.actions()] == ["x1"]


def test_a_reader_that_races_a_roll_still_sees_every_line(folders, monkeypatch):
    for n in range(3):
        _add(n)
    real = od._log_files
    calls = []

    def racing():
        files = real()
        if not calls:  # the first listing is taken, then a writer rolls the log under the reader
            calls.append(1)
            od.log_path().replace(od._next_segment_path())
        return files

    monkeypatch.setattr(od, "_log_files", racing)
    assert [r["id"] for r in od.read_log()] == ["a0", "a1", "a2"]


def test_forget_scrubs_words_in_a_segment_the_log_rolled_into(folders, monkeypatch):
    monkeypatch.setattr(od, "LOG_ROLL_BYTES", 1)
    for n in range(3):
        _add(n, words="pause the model please", session="s-gone")
    assert od._sealed_paths()
    assert od.scrub_words(["s-gone"]) == 3
    assert all(r.get("words") is None and r.get("scrubbed") for r in od.read_log())


def test_lines_an_earlier_version_chained_and_sealed_are_still_read_in_order(folders):
    old = od.log_path().with_name("owner-actions.000001.jsonl")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({"v": 2, "prev": "", "hash": "h1", "words_sha": "w", "kind": "action", "id": "s1",
                               "auth": "app", "action": "route.pause", "target": "codex|", "state": "applied",
                               "at": "2026-11-01T00:00:00Z"}) + "\n", encoding="utf-8")
    od.log_path().write_text(json.dumps({"v": 2, "prev": "h1", "hash": "h2", "kind": "action", "id": "c1",
                                         "auth": "app", "action": "route.lift", "target": "codex|",
                                         "state": "applied", "at": "2026-11-02T00:00:00Z"}) + "\n",
                             encoding="utf-8")
    _add(2)
    assert [r["id"] for r in od.read_log()] == ["s1", "c1", "a2"]
    assert [a["id"] for a in od.actions()] == ["s1", "c1", "a2"]
    assert od.line_state("s1") == "applied"


def test_a_state_line_from_the_chained_log_still_completes_its_action(folders):
    od._append({"kind": "action", "id": "x1", "auth": "app", "action": "route.pause", "target": "codex|",
                "state": "pending"})
    with od.log_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"v": 2, "prev": "p", "hash": "h", "kind": "state", "target": "x1",
                             "state": "applied"}) + "\n")
    assert od.line_state("x1") == "applied"


def test_a_last_line_cut_short_does_not_swallow_the_next(folders):
    _add(0)
    with od.log_path().open("a", encoding="utf-8") as fh:
        fh.write('{"kind": "act')  # a crash mid-write
    _add(1)
    assert [r["id"] for r in od.read_log()] == ["a0", "a1"]


def test_forget_scrubs_words_in_every_file_of_the_log(folders):
    old = od.log_path().with_name("owner-actions.000001.jsonl")
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text(json.dumps({"kind": "action", "id": "s1", "action": "route.pause", "target": "codex|",
                               "session": "s-gone", "words": "pause it", "words_sha": "kept"}) + "\n",
                   encoding="utf-8")
    for n in range(3):
        _add(n, words="pause the model please", session="s-gone")
    assert od.scrub_words(["s-gone"]) == 4
    assert all(r.get("words") is None and r.get("scrubbed") for r in od.read_log())


def test_segments_are_owner_files_to_forget_and_the_expiry_sweep(folders):
    path = od.log_path().with_name("owner-actions.000007.jsonl")
    assert owner_files.kind_of(path) == "log"


def test_appends_from_two_writers_all_land(folders):
    def appender(tag):
        for n in range(25):
            od._append({"kind": "action", "id": f"{tag}{n}", "auth": "app", "action": "route.lift",
                        "target": "codex|"})

    writers = [threading.Thread(target=appender, args=(tag,)) for tag in "ab"]
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    ids = [r["id"] for r in od.read_log()]
    assert len(ids) == 50 and len(set(ids)) == 50
