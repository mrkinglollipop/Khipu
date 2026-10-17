"""Owner direction: the owner-actions log can't be downgraded or cut short
(docs/plans/2026-10-16-owner-direction.md, 2.8; the second review's probes).

Every line Khipu writes is versioned and hash-chained, so a chained line
without its hash is a break, never an old line; and the chain's head and
length are kept a second time, beside the log, so lines deleted from its end
(or the whole log) are found too. Hermetic: temp data and capture folders.
"""
from __future__ import annotations

import json

import pytest

from khipu import owner_direction
from tests import test_owner_direction as _base

world = _base.world  # the fixture, shared


@pytest.fixture
def folders(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    return tmp_path





def test_log_rewrite_downgrade_is_detected(world):
    words = "pause Codex for now"
    rc, out = world.run(["route", "pause", "Codex"], words, world.say(words))
    assert rc == 0, out
    path = owner_direction.log_path()
    rows = [json.loads(s) for s in path.read_text().splitlines()]
    for row in rows:
        row.pop("hash", None)
        row.pop("prev", None)
    rows[0]["target"] = "grok|"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert owner_direction.verify_log()["ok"] is False, owner_direction.verify_log()


@pytest.mark.parametrize("strip", [("hash", "prev"), ("hash", "prev", "v")])
def test_a_chained_line_without_its_hash_is_a_break_not_an_old_line(folders, strip):
    for n in range(2):
        owner_direction._append({"kind": "action", "id": f"h{n}", "auth": "app", "action": "route.lift",
                                 "target": "codex|"})
    path = owner_direction.log_path()
    rows = [json.loads(s) for s in path.read_text().splitlines()]
    for key in strip:
        rows[1].pop(key, None)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    out = owner_direction.verify_log()
    assert out["ok"] is False and out["first_bad"]["line"] == 2, out


def test_tail_deletion_is_detected(world):
    words = "pause Codex for now"
    rc, out = world.run(["route", "pause", "Codex"], words, world.say(words))
    assert rc == 0, out
    words = "lift Codex pause please"
    rc, out = world.run(["route", "lift", "Codex"], words, world.say(words))
    assert rc == 0, out
    path = owner_direction.log_path()
    lines = path.read_text().splitlines()
    assert len(lines) == 4
    path.write_text("\n".join(lines[:-2]) + "\n")
    assert owner_direction.verify_log()["ok"] is False, owner_direction.verify_log()


@pytest.mark.parametrize("keep", [0, 1, 2])
def test_any_cut_from_the_end_is_detected_and_doctor_is_red(folders, keep):
    for n in range(3):
        owner_direction._append({"kind": "action", "id": f"t{n}", "auth": "app", "action": "route.lift",
                                 "target": "codex|"})
    path = owner_direction.log_path()
    lines = path.read_text().splitlines()
    path.write_text("".join(text + "\n" for text in lines[:keep]))
    out = owner_direction.verify_log()
    assert out["ok"] is False and out["first_bad"]["line"] == keep + 1, out
    assert owner_direction.doctor_block()["ok"] is False


def test_a_deleted_log_with_its_head_left_is_detected(folders):
    owner_direction._append({"kind": "action", "id": "g1", "auth": "app", "action": "route.lift",
                             "target": "codex|"})
    owner_direction.log_path().unlink()
    assert owner_direction.verify_log()["ok"] is False


def test_a_missing_head_record_is_detected(folders):
    owner_direction._append({"kind": "action", "id": "m1", "auth": "app", "action": "route.lift",
                             "target": "codex|"})
    owner_direction.head_path().unlink()
    assert owner_direction.verify_log()["ok"] is False


def test_an_append_that_crashed_before_its_head_was_written_still_verifies(folders, monkeypatch):
    owner_direction._append({"kind": "action", "id": "c1", "auth": "app", "action": "route.lift",
                             "target": "codex|"})
    head = owner_direction.head_path().read_text()
    owner_direction._append({"kind": "action", "id": "c2", "auth": "app", "action": "route.lift",
                             "target": "codex|"})
    owner_direction.head_path().write_text(head)
    assert owner_direction.verify_log()["ok"] is True, owner_direction.verify_log()


@pytest.mark.parametrize("tamper", ["rewrite", "append"])
def test_ordinary_log_tamper_is_detected(world, tamper):
    words = "pause Codex for now"
    rc, out = world.run(["route", "pause", "Codex"], words, world.say(words))
    assert rc == 0, out
    path = owner_direction.log_path()
    rows = [json.loads(s) for s in path.read_text().splitlines()]
    if tamper == "rewrite":
        rows[0]["target"] = "grok|"
    else:
        rows.append({"kind": "action", "id": "injected", "action": "route.lift", "target": "codex|"})
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    check = owner_direction.verify_log()
    doctor = owner_direction.doctor_block()
    assert check["ok"] is False and doctor["ok"] is False, (check, doctor)
    assert check["first_bad"]["line"] == (1 if tamper == "rewrite" else 3), check
