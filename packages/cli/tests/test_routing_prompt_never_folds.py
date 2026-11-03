"""A prompt never does work that grows with the stored record (owner's rule,
2026-11-03: a fold of 270-480 ms against a 50 ms budget skipped 13.4% of
prompts as ``routing=skipped:budget``). With a saved view.json the prompt stats
the inputs and answers the view, current or not; a view an input or the clock
has overtaken is stamped stale and refolded off the prompt (``kick`` and
``routing.refold_in_background``, run by the per-prompt child after its
answer). Only a record with no usable view.json is folded inline. Hermetic."""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from khipu import agent_view, routing
from tests import routing_compact_synth as synth

T0 = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
ACCOUNTS = [{"label": "Claude", "harness": "claude"}, {"label": "Codex", "harness": "codex"}]
ME = {"kind": "conversation", "harness": "claude_code", "project": "project-a", "session": "s-one"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({"usage_accounts": ACCOUNTS, "routing_prompt_budget_ms": 1000,
                                                  "routing_reset_check": False}))
    capture = Path(tempfile.mkdtemp(prefix="krp-", dir="/tmp"))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(capture))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    now = [T0]
    monkeypatch.setattr(routing, "_now", lambda: now[0])
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    yield now
    shutil.rmtree(capture, ignore_errors=True)


@pytest.fixture
def folds(monkeypatch):
    """Every call of ``routing.fold`` (the fold itself), in a list."""
    calls: list[int] = []
    real = routing.fold

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(routing, "fold", counted)
    return calls


def _record():
    routing.report(account="Codex", state="limited", until="2h", author=ME)
    routing.report(account="Claude", state="open", author=ME)
    return routing.read_view()


def _marker(view, now):
    return {"version": view["version"], "epoch": view["epoch"], "full_at": routing._iso(now[0])}


def _push(marker=None, *, kick=None, session_id=None):
    agent_view.prepare_routing_table(refresh=True)
    return routing.prompt_push(marker, budget=1.0, session_id=session_id, kick=kick)


def _append_raw(row: dict) -> None:
    with routing.events_file().open("a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def _other_write():
    """A line appended behind the saved view's back (a write whose own refold
    has not landed, or a source's): the view is now out of date."""
    _append_raw({"type": "evidence", "id": "late-1", "account": "claude|", "state": "limited",
                 "source": "report", "until": routing._iso(T0 + timedelta(hours=3)),
                 "written_at": routing._iso(T0), "written_by": dict(ME), "via": "mcp", "seq": 999})


def _owner_note(at):
    _append_raw({"type": "note_set", "id": "own-1", "text": "Codex first today", "written_at": routing._iso(at),
                 "written_by": {"kind": "owner"}, "via": "app", "auth": "app", "seq": 998})


# ---- 1. writers refresh the view --------------------------------------------------------


def test_a_prompt_after_a_write_finds_the_view_fresh_and_does_not_fold(home, folds):
    _record()
    routing.report(account="Codex", state="open", author=ME)  # the write refolds and saves, under its lock
    before = len(folds)
    kicks: list[str] = []
    out = _push(kick=kicks.append)
    assert out["kind"] == "full" and kicks == []
    assert len(folds) == before  # no fold on the prompt path


def test_settings_and_replication_writes_refresh_the_view(home, folds):
    _record()
    routing.set_setting("routing_prompt_chars", "300")  # config.json is a fingerprint input
    kicks: list[str] = []
    _push(kick=kicks.append)
    assert kicks == []
    (routing.routing_dir() / "replication.json").write_text("{}")  # what a pull leaves behind
    assert routing.refresh_view() is True
    _push(kick=kicks.append)
    assert kicks == []


# ---- 2. stale-while-revalidate -----------------------------------------------------------


def test_a_stale_view_is_answered_without_a_fold_and_one_refold_is_asked_for(home, folds):
    view = _record()
    _other_write()
    before = len(folds)
    kicks: list[str] = []
    out = _push(kick=kicks.append)
    assert len(folds) == before  # nothing folded on the prompt path
    assert kicks == ["inputs"]  # exactly one refold asked for, with its reason
    assert out["version"] == view["version"]
    assert routing.refold_in_background() == "folded"
    assert len(folds) == before + 1
    kicks.clear()
    again = _push(kick=kicks.append)
    assert kicks == [] and again["version"] > view["version"]  # the next prompt finds the fold's view


def test_the_stale_stamp_names_its_reason_and_age(home):
    _record()
    _other_write()
    home[0] = T0 + timedelta(seconds=45)
    fingerprint, _cfg = routing._light_inputs()
    stamped = routing._prompt_view(home[0], kick=lambda why: None)
    assert stamped["stale_view"]["reason"] == "inputs" and stamped["stale_view"]["age"] == pytest.approx(45, abs=1)
    assert stamped["fingerprint"] == fingerprint  # the answer's generation is the inputs as read now


def test_a_view_past_valid_until_is_answered_and_refolded_off_the_prompt(home, folds):
    view = _record()
    due = routing._parse(view["valid_until"])
    assert due is not None
    home[0] = due + timedelta(seconds=1)
    before = len(folds)
    kicks: list[str] = []
    _push(kick=kicks.append)
    assert kicks == ["due"] and len(folds) == before


def test_an_old_view_with_an_owner_change_behind_it_says_routing_is_updating(home):
    _record()
    home[0] = T0 + timedelta(seconds=600)
    _owner_note(home[0])
    out = _push(kick=lambda why: None)
    assert "Routing is updating" in (out.get("text") or "")
    assert "arrives with the next prompt" in out["text"]


def test_an_old_view_with_no_owner_change_behind_it_says_nothing(home):
    _record()
    home[0] = T0 + timedelta(seconds=600)
    _other_write()
    out = _push(kick=lambda why: None)
    assert "Routing is updating" not in (out.get("text") or "")


def test_a_young_view_with_an_owner_change_behind_it_says_nothing(home):
    _record()
    home[0] = T0 + timedelta(seconds=30)
    _owner_note(home[0])
    assert "Routing is updating" not in (_push(kick=lambda why: None).get("text") or "")


# ---- 3. no usable view.json: the inline fold, as before ----------------------------------


@pytest.mark.parametrize("damage", ["missing", "edited"])
def test_no_usable_view_folds_inline(home, folds, damage):
    _record()
    path = routing.view_file()
    if damage == "missing":
        path.unlink()
    else:
        path.write_text(path.read_text().replace('"version"', '"versio_"', 1))
    before = len(folds)
    kicks: list[str] = []
    out = _push(kick=kicks.append)
    assert kicks == [] and len(folds) == before + 1  # folded on the prompt, nothing left to ask for
    assert out["kind"] == "full"


# ---- 4. an owner change that lands mid-prompt arrives with the next prompt ---------------


def test_an_owner_change_written_mid_prompt_is_delivered_on_the_next_prompt(home):
    seen = _record()
    marker = _marker(seen, home)
    saved = routing.view_file().read_bytes()
    routing.set_note("Codex first for builds today", via="app")  # the owner writes while the prompt is reading
    routing.view_file().write_bytes(saved)  # ... and its refold has not landed yet
    kicks: list[str] = []
    first = _push(dict(marker), kick=kicks.append)
    assert kicks == ["inputs"]
    assert first["kind"] == "none" and first["version"] == seen["version"]  # the saved view, nothing newer yet
    routing.refold_in_background()  # the child's refold, after its answer
    second = _push(dict(marker), kick=kicks.append)
    assert kicks == ["inputs"]  # the view is current now
    assert second["kind"] == "changes" and "Codex first for builds today" in second["text"]


# ---- 5. only one refold at a time --------------------------------------------------------


def test_two_concurrent_prompts_kick_one_refold(home, monkeypatch):
    _record()
    _other_write()
    kicks: list[str] = []
    for _ in range(2):  # two prompts, each answered from the saved view and each asking for a refold
        _push(kick=kicks.append)
    assert kicks == ["inputs", "inputs"]
    started, real = [], routing._start_fold

    def slow(*a, **k):
        started.append(1)
        time.sleep(0.4)
        return real(*a, **k)

    monkeypatch.setattr(routing, "_start_fold", slow)
    results: list[str] = []
    gate = threading.Barrier(2)

    def child():
        gate.wait()
        results.append(routing.refold_in_background())

    threads = [threading.Thread(target=child) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert len(started) == 1  # one fold ran; the other child did not wait for it
    assert sorted(results) in (["busy", "folded"], ["folded", "fresh"])
    assert not routing._stale_reason(routing._load_view(), routing._light_inputs()[0], routing._now())


# ---- 6. the bound ------------------------------------------------------------------------


@pytest.mark.parametrize("stale", [False, True])
def test_prompt_path_is_inside_the_budget_on_a_10000_line_log(home, folds, stale):
    synth.write(routing.events_file(), synth.real_shape(["claude|", "codex|"], T0 - timedelta(minutes=5)))
    view = routing.read_view()
    assert sum(1 for _ in routing.events_file().open()) >= 10_000 and view["record"]
    if stale:
        _other_write()
    agent_view.prepare_routing_table(refresh=True)
    marker = _marker(view, home)
    before, times = len(folds), []
    for _ in range(9):
        routing._ENTRY_INPUTS.clear()  # a fresh process has no parsed log
        routing._PARSED_LINE_CACHE.clear()
        began = time.perf_counter()
        routing.prompt_push(dict(marker), budget=1.0, kick=lambda why: None)
        times.append((time.perf_counter() - began) * 1000)
    assert len(folds) == before  # the fold is 300 ms of this log; the prompt never ran it
    assert sorted(times)[len(times) // 2] < 50, times
