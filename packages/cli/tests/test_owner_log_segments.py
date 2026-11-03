"""The owner-actions log is kept in sealed, hash-chained segments: an append
reads only the current one, the all-segment check still finds any change, and
no owner action is ever dropped. Hermetic: temp data and capture folders."""
from __future__ import annotations

import json

import pytest

from khipu import owner_direction as od
from khipu import owner_files


@pytest.fixture
def folders(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.setattr(od, "SEGMENT_MAX_LINES", 3)
    return tmp_path


def _add(n, **extra):
    return od._append({"kind": "action", "id": f"a{n}", "auth": "app", "action": "route.lift",
                       "target": "codex|", **extra})


def _fill(count):
    return [_add(n) for n in range(count)]


def _rows(path):
    return [json.loads(t) for t in path.read_text().splitlines() if t.strip()]


def test_a_full_log_is_sealed_into_numbered_segments_and_the_chain_runs_through_them(folders):
    lines = _fill(8)
    sealed = od._sealed_paths()
    assert [p.name for p in sealed] == ["owner-actions.000001.jsonl", "owner-actions.000002.jsonl"]
    assert [len(_rows(p)) for p in sealed] == [3, 3] and len(_rows(od.log_path())) == 2
    # The first line of each new segment chains to the last hash of the one before.
    assert _rows(sealed[1])[0]["prev"] == _rows(sealed[0])[-1]["hash"]
    assert _rows(od.log_path())[0]["prev"] == _rows(sealed[1])[-1]["hash"]
    out = od.verify_log()
    assert out["ok"] is True and out["lines"] == 8 and out["segments"] == 2, out
    assert [r["id"] for r in od.read_log()] == [line["id"] for line in lines]
    assert od.doctor_block()["ok"] is True and "3 files" in od.doctor_block()["summary"]


def test_an_append_reads_and_checks_only_the_current_segment(folders, monkeypatch):
    _fill(7)
    walked = []
    real = od._walk
    monkeypatch.setattr(od, "_walk", lambda texts, rows, incoming, base, segment: walked.append(segment)
                        or real(texts, rows, incoming, base, segment))
    reads = []
    real_read = od.Path.read_text
    monkeypatch.setattr(od.Path, "read_text", lambda self, *a, **k: reads.append(self.name) or real_read(self, *a, **k))
    _add(99)
    assert walked == [od.LOG_NAME]
    assert not [n for n in reads if n.startswith("owner-actions.0")]
    walked.clear()
    assert od.verify_log()["ok"] is True
    assert walked == ["owner-actions.000001.jsonl", "owner-actions.000002.jsonl", od.LOG_NAME]


def test_changing_a_sealed_line_is_found_by_the_all_segment_check(folders):
    _fill(8)
    seg = od._sealed_paths()[0]
    seg.write_text(seg.read_text().replace("route.lift", "route.fake", 1))
    out = od.verify_log()
    assert out["ok"] is False and out["first_bad"]["segment"] == seg.name and out["first_bad"]["line"] == 1
    assert od.doctor_block()["ok"] is False


@pytest.mark.parametrize("which", ["first", "middle", "last"])
def test_a_removed_sealed_segment_is_found(folders, which):
    _fill(11)  # three sealed segments and two lines
    sealed = od._sealed_paths()
    assert len(sealed) == 3
    sealed[{"first": 0, "middle": 1, "last": 2}[which]].unlink()
    assert od.verify_log()["ok"] is False


def test_lines_cut_from_a_sealed_segments_end_are_found(folders):
    _fill(8)
    seg = od._sealed_paths()[1]
    kept = seg.read_text().splitlines()[:-1]
    seg.write_text("".join(t + "\n" for t in kept))
    assert od.verify_log()["ok"] is False


def test_a_sealed_segment_added_from_nowhere_is_found(folders):
    _fill(4)
    forged = od.log_path().with_name(od.segment_name(2))
    forged.write_text(od.log_path().read_text())
    assert od.verify_log()["ok"] is False


def test_the_nightly_check_and_the_append_check_agree_a_tampered_current_segment_raises_the_alarm(folders):
    _fill(5)
    path = od.log_path()
    path.write_text(path.read_text().replace("route.lift", "route.fake"))
    _add(50)
    assert od.read_alarm() is not None
    assert od.verify_log()["ok"] is False


def test_a_log_written_before_segments_still_verifies_and_then_rotates(folders):
    for n in range(2):
        _add(n)
    # What an earlier build left: the same lines and a version 1 head with no segment count.
    head = json.loads(od.head_path().read_text())
    od.head_path().write_text(json.dumps({"v": 1, "lines": head["lines"], "head": head["head"]}) + "\n")
    assert od.verify_log()["ok"] is True
    for n in range(2, 7):
        _add(n)
    out = od.verify_log()
    assert out["ok"] is True and out["lines"] == 7 and out["segments"] == 2, out


def test_lines_from_before_the_chain_are_sealed_with_the_first_segment(folders):
    path = od.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = [{"kind": "action", "id": f"old{n}", "at": "2026-01-01T00:00:00Z", "words": "pause it"} for n in range(4)]
    path.write_text("".join(json.dumps(r) + "\n" for r in old))
    _fill(6)
    out = od.verify_log()
    assert out["ok"] is True and out["segments"] >= 1, out
    assert "before the log was chained" in out["note"]
    assert [r["id"] for r in od.read_log()][:4] == ["old0", "old1", "old2", "old3"]
    # A legacy line changed after sealing is still caught.
    first = od._sealed_paths()[0]
    first.write_text(first.read_text().replace("pause it", "lift it", 1))
    assert od.verify_log()["ok"] is False


def test_forget_scrubs_words_in_every_segment_and_the_chain_still_holds(folders):
    for n in range(8):
        _add(n, words="pause the model please", session="s-gone")
    assert od.scrub_words(["s-gone"]) == 8
    assert all(r.get("words") is None and r.get("scrubbed") for r in od.read_log())
    assert od.verify_log()["ok"] is True


def test_a_seal_cut_short_before_its_head_was_written_still_verifies(folders):
    _fill(2)
    before = od.head_path().read_text()
    _add(2)  # the third line fills the segment; the next append seals it
    _add(3)
    od.head_path().write_text(json.dumps({"v": 2, "seq": 0, "lines": 3, "head": json.loads(
        od._sealed_paths()[0].read_text().splitlines()[-1])["hash"]}) + "\n")
    od.log_path().unlink()  # crashed after the rename, before the next line landed
    assert od.verify_log()["ok"] is True, od.verify_log()
    assert before  # the earlier head is only here to show the log started from it


def test_no_segment_is_ever_dropped_however_long_the_log_grows(folders):
    _fill(40)
    assert len(od._sealed_paths()) == 13 and len(od.read_log()) == 40
    assert od.verify_log()["ok"] is True


def test_segments_are_owner_files_to_forget_and_the_expiry_sweep(folders):
    path = od.log_path().with_name(od.segment_name(7))
    assert owner_files.kind_of(path) == "log"


def test_a_check_running_beside_appends_that_seal_never_reads_a_false_break(folders):
    import threading

    errors, stop = [], threading.Event()

    def appender(tag):
        for n in range(25):
            od._append({"kind": "action", "id": f"{tag}{n}", "auth": "app", "action": "route.lift",
                        "target": "codex|"})

    def checker():
        while not stop.is_set():
            out = od.verify_log()
            if not out["ok"]:
                errors.append(out["first_bad"])
                return

    check = threading.Thread(target=checker)
    writers = [threading.Thread(target=appender, args=(tag,)) for tag in "ab"]
    check.start()
    for w in writers:
        w.start()
    for w in writers:
        w.join()
    stop.set()
    check.join()
    assert not errors, errors[:1]
    assert od.read_alarm() is None
    final = od.verify_log()
    assert final["ok"] is True and final["lines"] == 50, final


def test_verify_log_takes_the_log_lock(folders, monkeypatch):
    _fill(2)
    taken = []
    real = od._log_locked

    import contextlib

    @contextlib.contextmanager
    def spy():
        taken.append(1)
        with real() as path:
            yield path

    monkeypatch.setattr(od, "_log_locked", spy)
    od.verify_log()
    assert taken == [1]
