"""An answer stands while what it shows stands: a local pace reading or other
bookkeeping must not withdraw it (``routing=skipped:stale``, 203 of 767 prompts on
the main Mac on 2026-11-05), and a real change still must. Disposable data only."""
from __future__ import annotations

import json
import threading
import time
from datetime import timedelta

import pytest

from khipu import recall_prompt, routing, routing_generation
from tests.test_routing_acceptance import AUTHOR, ACCOUNTS, Replay, T0  # noqa: F401
from tests.test_routing_push_prompt import _fake_reader

pytest_plugins = ("tests.test_routing_acceptance",)
RESET = T0 + timedelta(days=3)


def sample(at, used, *, account="codex|", window="weekly", reset=RESET):
    return {"type": "usage_sample", "account": account, "window": window, "witness_at": routing._iso(at),
            "used_pct": used, "t_err": 60, "source": "probe", "resets_at": routing._iso(reset),
            "written_by": {"kind": "probe"}, "via": "probe"}


@pytest.fixture
def seeded(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    replay.write(*[sample(T0 - timedelta(hours=30) + timedelta(minutes=30 * i), 20 + i * 0.25) for i in range(59)])
    return replay


@pytest.fixture
def idle(replay):
    routing.report(account="Codex", state="open", author=AUTHOR)
    replay.write(*[sample(T0 - timedelta(hours=30) + timedelta(minutes=50 * i), 40.0) for i in range(36)])
    assert next(a for a in replay.view()["accounts"] if a["key"] == "codex|")["pace"][0]["idle"] is True
    return replay


def answer(replay):
    view = routing._prompt_view(replay.now, kick=lambda why: None)
    return view, routing._generation(view, routing.settings())


def raw_append(row):
    """A line down in the log with no fold yet: what a writer leaves between its
    append and saving the view."""
    full = routing._stamp_locked(routing._now(), 1, events=routing._read_all_events()[0])[0]
    with routing.events_file().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({**row, **full, "id": f"raw-{full['seq']}"}) + "\n")


def current_used(view, key="codex|"):
    entry = next(a for a in view["accounts"] if a["key"] == key)
    return next(p for p in entry["pace"] if p["window"] == "weekly")["used_pct"]


def test_astra_probe_a_same_meaning_local_append_keeps_the_answer(idle):
    """Appending only a usage sample and saving a view that means the same."""
    view, generation = answer(idle)
    raw_append(sample(idle.now, current_used(view)))
    assert recall_prompt._routing_generation_current(generation), "before any refold"
    routing._poll_refold(idle.now)
    assert routing._load_view()["fingerprint"]["events"] != view["fingerprint"]["events"]
    assert recall_prompt._routing_generation_current(generation), "after a refold that means the same"


def test_an_equal_reading_no_fold_has_read_is_not_excused_unless_it_cannot_matter(replay):
    """Codex: 12% at 230, 170 and 110 minutes ago, past the hour and a half a
    quiet reading may be. The buffered view says "last reading 1.8 h old"; a
    current 12% makes it idle with a known allowance."""
    routing.report(account="Codex", state="open", author=AUTHOR)
    reset = replay.now + timedelta(hours=3)
    replay.write(*[sample(replay.now - timedelta(minutes=m), 12.0, window="five_hour", reset=reset) for m in (230, 170, 110)])
    view, generation = answer(replay)
    pace = next(p for a in view["accounts"] if a["key"] == "codex|" for p in a["pace"] if p["window"] == "five_hour")
    assert pace["why"] == "last reading 1.8 h old"
    raw_append(sample(replay.now, 12.0, window="five_hour", reset=reset))
    assert routing_generation.stale_input(generation) == "events:usage_sample"
    routing._poll_refold(replay.now)
    after = routing._load_view()
    pace = next(p for a in after["accounts"] if a["key"] == "codex|" for p in a["pace"] if p["window"] == "five_hour")
    assert pace["idle"] is True and pace["plan"]["needed_pct_per_h"] is not None
    assert routing_generation.revision(after) != routing_generation.revision(view)
    assert routing_generation.stale_input(generation) == "view:revision"


def test_an_equal_reading_on_a_window_with_a_forecast_waits_for_a_fold(seeded):
    """Rate and coverage move with it, so only a fold can say what it changes."""
    view, generation = answer(seeded)
    raw_append(sample(seeded.now, current_used(view)))
    assert routing_generation.stale_input(generation) == "events:usage_sample"
    routing._poll_refold(seeded.now)
    assert routing_generation.revision(routing._load_view()) == routing_generation.revision(view)
    assert routing_generation.stale_input(generation) is None


@pytest.mark.parametrize("change", [
    {"used_pct": 41.0}, {"resets_at": routing._iso(RESET + timedelta(seconds=1))}, {"window": "five_hour"},
    {"account": "grok|"}, {"resets_at": None}])
def test_only_the_same_value_in_the_same_cycle_of_an_idle_window_cannot_change_it(change):
    view = {"accounts": [{"key": "codex|", "pace": [{"window": "weekly", "idle": True, "used_pct": 40.0,
                                                    "cycle_resets_at": routing._iso(RESET)}]}]}
    row = sample(T0, 40.0)
    assert routing_generation._cannot_change(row, view)
    assert not routing_generation._cannot_change(dict(row, **change), view)
    view["accounts"][0]["pace"][0].update(idle=False)
    assert not routing_generation._cannot_change(row, view)
    view["accounts"][0]["pace"][0].update(idle=True, day_cap={"verdict": "unknown"})
    assert not routing_generation._cannot_change(row, view)


def test_a_moved_reading_a_fold_found_harmless_keeps_the_answer(seeded):
    view, generation = answer(seeded)
    used = current_used(view)
    seeded.write(sample(seeded.now, used + 0.5))  # the writer's own fold, under its lock
    after = routing._load_view()
    assert current_used(after) == used + 0.5
    assert routing_generation.revision(after) == routing_generation.revision(view)
    assert recall_prompt._routing_generation_current(generation)


def test_concurrent_samples_and_repeated_prompts_never_reject_unchanged_input(idle):
    seeded = idle
    view, _generation = answer(seeded)
    used = current_used(view)
    stop = threading.Event()
    wrote = [0]
    errors = []

    def writer():
        try:
            while not stop.is_set():
                seeded.write(sample(seeded.now, used))
                wrote[0] += 1
                if wrote[0] % 2:
                    routing._poll_refold(seeded.now)
                time.sleep(0.002)
        except Exception as exc:  # noqa: BLE001 - reported on the main thread
            errors.append(exc)

    thread = threading.Thread(target=writer)
    thread.start()
    rejected, asked = [], 0
    deadline = time.monotonic() + 3.0
    try:
        while time.monotonic() < deadline or asked < 25:
            _view, generation = answer(seeded)
            time.sleep(0.01)  # the answer sits in the pipe while search runs
            asked += 1
            cause = routing_generation.stale_input(generation)
            if cause:
                rejected.append(cause)
    finally:
        stop.set()
        thread.join()
    assert not errors
    assert wrote[0] >= 5, "the writer never ran alongside the prompts"
    assert rejected == [], f"{len(rejected)} of {asked} prompts rejected with {wrote[0]} samples written"


def _fold_lands_during_the_check(monkeypatch, then=None):
    """Between the check's stat of the log and its read of the view, a writer
    appends and a fold saves: the view has read further than the check saw."""
    read = routing_generation._read_view

    def later(path):
        monkeypatch.setattr(routing_generation, "_read_view", read)
        routing._write_many([sample(routing._now(), 40.0)])
        if then:
            then()
        return read(path)
    monkeypatch.setattr(routing_generation, "_read_view", later)


def test_a_fold_ahead_of_what_the_check_saw_is_no_rewrite(idle, monkeypatch):
    view, generation = answer(idle)
    raw_append(sample(idle.now, current_used(view)))
    _fold_lands_during_the_check(monkeypatch)
    assert routing_generation.stale_input(generation) is None


def test_a_log_shorter_than_a_fold_ahead_of_the_check_read_was_rewritten(idle, monkeypatch):
    view, generation = answer(idle)
    raw_append(sample(idle.now, current_used(view)))

    def cut():
        # Back to what the check saw, on the same file: only a rewrite does that.
        path = routing.events_file()
        lines = path.read_bytes().splitlines(keepends=True)
        with path.open("r+b") as fh:
            fh.truncate(sum(map(len, lines[:-1])))
    _fold_lands_during_the_check(monkeypatch, then=cut)
    assert routing_generation.stale_input(generation) == "events:rewritten"


def test_pace_figures_moving_is_no_change_in_what_is_decided():
    base = {"schema": 1, "epoch": "e", "version": 1, "effective": {}, "record": True, "accounts": [
        {"key": "k", "pace": [{"window": "weekly", "why": "readings cover only 7.5 of the last 24 h",
                               "plan": {"verdict": "unknown"},
                               "pace_target": {"verdict": "unknown", "why": "last reading 0.5 h old"}}]}]}
    moved = json.loads(json.dumps(base))
    moved["accounts"][0]["pace"][0].update(why="readings cover only 7.6 of the last 24 h")
    moved["accounts"][0]["pace"][0]["pace_target"]["why"] = "last reading 0.6 h old"
    assert routing_generation.revision(moved) == routing_generation.revision(base)
    other = json.loads(json.dumps(base))
    other["accounts"][0]["pace"][0].update(why=None)
    assert routing_generation.revision(other) != routing_generation.revision(base)
    kind = json.loads(json.dumps(base))
    kind["accounts"][0]["pace"][0].update(why="last reading 7.5 h old")
    assert routing_generation.revision(kind) != routing_generation.revision(base)


def _rule(_replay):
    routing.add_rule("a real rule", via="app")


def _cap(_replay):
    routing.set_cap("Codex", window="weekly", kind="share", pct=50, via="app")


def _limit(replay):
    replay.write(sample(replay.now, 100.0))


def _raw_rule(_replay):
    raw_append({"type": "rule_add", "text": "x", "written_by": {"kind": "owner"}, "via": "app"})


def _raw_moved(replay):
    raw_append(sample(replay.now, 41.0))


def _raw_new_cycle(replay):
    raw_append({**sample(replay.now, current_used(routing._load_view())), "resets_at": routing._iso(RESET + timedelta(days=7))})


def _raw_owner_report(replay):
    raw_append({"type": "evidence", "account": "codex|", "state": "limited", "written_by": {"kind": "owner"}, "via": "app"})


def _torn(_replay):
    with routing.events_file().open("a", encoding="utf-8") as fh:
        fh.write('{"type": "usage_sa')


def _compacted(_replay):
    data = routing.events_file().read_bytes()
    path = routing.events_file()
    path.unlink()
    path.write_bytes(data + b"\n")


@pytest.mark.parametrize("change,word", [
    (_rule, "events:rule_add"), (_cap, "events:cap_set"), (_limit, "view:revision"),
    (_raw_rule, "events:rule_add"), (_raw_moved, "events:usage_sample"), (_raw_new_cycle, "events:usage_sample"),
    (_raw_owner_report, "events:evidence"), (_torn, "events:rewritten"), (_compacted, "events:rewritten")])
def test_a_real_change_is_still_rejected_and_named(seeded, change, word):
    _view, generation = answer(seeded)
    change(seeded)
    assert routing_generation.stale_input(generation) == word
    assert not recall_prompt._routing_generation_current(generation)


def test_a_later_refold_does_not_excuse_a_rule_that_kept_the_revision(seeded):
    """A kind the revision does not hold (a proposal, the tier table) still has
    the log's own line to say it was a change."""
    _view, generation = answer(seeded)
    raw_append({"type": "tier_set", "tiers": {}, "written_by": {"kind": "owner"}, "via": "app"})
    routing._poll_refold(seeded.now)
    assert routing_generation.stale_input(generation) == "events:tier_set"


def test_config_and_remote_changes_name_themselves(seeded):
    _view, generation = answer(seeded)
    config = seeded.data / "config.json"
    config.write_text(config.read_text() + " ")
    assert routing_generation.stale_input(generation) == "config"
    _view, generation = answer(seeded)
    routing._poll_refold(seeded.now)
    _view, generation = answer(seeded)
    (routing.routing_dir() / "remote.jsonl").write_text("")
    assert routing_generation.stale_input(generation) == "remote"


def _buffered(view, generation, kind="changes", **more):
    return {"kind": kind, "text": "words", "version": view["version"], "epoch": view["epoch"],
            "generation": generation, "done": time.monotonic(), **more}


def test_the_log_line_names_the_input_that_changed(seeded):
    view, generation = answer(seeded)
    _rule(seeded)
    at, writer = _fake_reader([(0.0, json.dumps(_buffered(view, generation)).encode() + b"\n")])
    writer.join(5)
    assert at.collect() is None and at.status == "skipped:stale"
    _ctx, info = at.add_to("", None)
    assert info == {"status": "skipped:stale:events:rule_add"}
    field = recall_prompt._routing_delivered(info, True)
    assert field == " routing=skipped:stale:events:rule_add"
    assert recall_prompt._ROUTING_FIELD_RE.search("prompt ok" + field).group("status") == info["status"]


def test_an_answer_with_nothing_to_show_cannot_go_stale(seeded):
    view, generation = answer(seeded)
    _rule(seeded)
    at, writer = _fake_reader([(0.0, json.dumps(_buffered(view, generation, kind="none", text=None)).encode() + b"\n")])
    writer.join(5)
    out = at.collect()
    assert out["kind"] == "none"
    assert at.add_to("", out)[1] == {"status": "none"}


def test_a_rule_that_lands_behind_an_empty_answer_is_delivered_next_prompt(replay):
    """An empty answer skips the check, so this prompt says nothing of the rule
    and logs none. It must move no marker, or the rule would be lost for good."""
    session = "s-none"
    routing.report(account="Codex", state="open", author=AUTHOR)
    _out, _context, info = replay.offer(session)
    replay.ack(info)
    empty, _context, _info = replay.offer(session)
    assert empty["kind"] == "none"
    view = routing._load_view()
    buffered = _buffered(view, routing._generation(view, routing.settings()), kind="none", text=None)
    routing.add_rule("the rule that landed behind it", via="app")
    assert routing_generation.stale_input(buffered["generation"]) == "events:rule_add"
    at, writer = _fake_reader([(0.0, json.dumps(buffered).encode() + b"\n")])
    writer.join(5)
    at.session = session
    before = recall_prompt.routing_marker(session)
    out = at.collect()
    context, logged = at.add_to("recall words", out)
    assert context == "recall words" and logged == {"status": "none"}
    assert recall_prompt._routing_delivered(logged, True) == " routing=none"
    assert recall_prompt.routing_marker(session) == before, "the empty answer moved the marker"
    # The next prompt owes it, and hands it over once.
    following, context, info = replay.offer(session)
    assert following["version"] > before["version"] and following["kind"] != "none"
    assert "the rule that landed behind it" in context
    replay.ack(info)
    assert recall_prompt.routing_marker(session)["version"] == following["version"]
    assert replay.offer(session)[0]["kind"] == "none"


def test_an_answer_with_words_is_delivered_when_only_a_reading_moved_on(idle):
    seeded = idle
    view, generation = answer(seeded)
    raw_append(sample(seeded.now, current_used(view)))
    at, writer = _fake_reader([(0.0, json.dumps(_buffered(view, generation)).encode() + b"\n")])
    writer.join(5)
    out = at.collect()
    assert out is not None and out["text"] == "words"
