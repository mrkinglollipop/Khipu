"""The copy between Macs rewrites replication.json on every sync (its pull
position and clock readings, about every 5 s with the copy on). That is not a
change of the record: it must not make a saved view stale, withdraw a buffered
routing answer (``routing=skipped:stale``) or drop the cached read of the log.
What another Mac sent (remote.jsonl) still does, at once."""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest

from khipu import recall_prompt, routing, routing_replication as replication
from tests.test_routing_push_prompt import _fake_reader
from tests import test_routing_s11b_replication as _s11b

pair = _s11b.pair  # the two disposable Macs and the in-memory hub, as the copy's own tests use them
sync = _s11b.sync


def _read_state():
    return json.loads((routing.routing_dir() / "replication.json").read_text())


def _bookkeeping_write(hub, seconds=7):
    """What a sync that brought nothing and sent nothing leaves behind: the
    cursors and clock readings moved, nothing else."""
    state = _read_state()
    later = routing._iso(hub.now + timedelta(seconds=seconds))
    state.update(pull_cursor=later.replace("Z", ".500000Z"), caught_up_at=later, checked_local_at=later,
                 hub_offset_seconds=state.get("hub_offset_seconds", 0) + 0.4)
    replication._save(state)


def _settled(pair):
    """Both Macs synced twice (nothing left to say), then Mac a's view saved."""
    a, b, hub = pair
    a.activate()
    routing.add_rule("keep the copy honest", via="app")
    for mac in (a, b, a, b):
        assert sync(mac, hub)["ok"]
    a.activate()
    routing.refresh_view()
    return a, b, hub


def _answer(view, generation):
    return {"kind": "changes", "text": "words", "version": view["version"], "epoch": view["epoch"],
            "generation": generation, "done": time.monotonic()}


def test_a_no_op_sync_does_not_move_the_fingerprint_the_view_or_the_entry_cache(pair):
    a, b, hub = _settled(pair)
    view_before = routing._load_view()
    stat_before = routing._stat(routing.view_file())
    key_before = replication.cache_key()
    cache_before = routing._entry_cache()
    replication_before = routing._stat(routing.routing_dir() / "replication.json")
    hub.now += timedelta(seconds=11)
    assert sync(a, hub)["ok"]
    assert routing._stat(routing.routing_dir() / "replication.json") != replication_before  # it was rewritten
    assert replication.cache_key() == key_before
    assert routing._entry_cache() is cache_before
    assert routing._stat(routing.view_file()) == stat_before  # and the view was not folded again for it
    fingerprint, _cfg, _known = routing._inputs()
    assert routing._fresh(routing._load_view(), fingerprint, routing._now())
    assert routing._load_view()["fingerprint"] == view_before["fingerprint"]


def test_a_buffered_answer_survives_the_bookkeeping_written_while_the_prompt_searched(pair):
    """The gate's prompt-2 failure on the live path: an answer is read, the
    prompt searches, a sync rewrites replication.json (no refold has run yet),
    and the answer used to be withdrawn as ``skipped:stale``."""
    a, b, hub = _settled(pair)
    view = routing._load_view()
    generation = routing._generation(view, routing.settings())
    assert recall_prompt._routing_generation_current(generation)
    _bookkeeping_write(hub)
    assert recall_prompt._routing_generation_current(generation)
    at, writer = _fake_reader([(0.0, json.dumps(_answer(view, generation)).encode() + b"\n")])
    writer.join(5)
    assert at.collect() is not None
    assert at.status != "skipped:stale"


def test_the_generation_does_not_name_replication_json_but_still_names_remote_jsonl(pair):
    a, b, hub = _settled(pair)
    names = {item[0].rsplit("/", 1)[-1] for item in routing._generation(routing._load_view(), routing.settings())}
    assert "remote.jsonl" in names and "replication.json" not in names


def test_a_real_incoming_rule_still_withdraws_the_answer_and_shows_on_the_next_read(pair):
    a, b, hub = _settled(pair)
    view = routing._load_view()
    generation = routing._generation(view, routing.settings())
    key_before = replication.cache_key()
    b.activate()
    routing.add_rule("a rule from the other Mac", via="app")
    assert sync(b, hub)["ok"]
    hub.now += timedelta(seconds=11)
    assert sync(a, hub)["ok"]  # pulls it, and refolds the view
    assert replication.cache_key() != key_before
    assert not recall_prompt._routing_generation_current(generation)
    at, writer = _fake_reader([(0.0, json.dumps(_answer(view, generation)).encode() + b"\n")])
    writer.join(5)
    assert at.collect() is None and at.status == "skipped:stale"
    a.activate()
    assert any("a rule from the other Mac" in (r.get("text") or "") for r in a.view()["rules"])


def test_remote_rows_that_arrive_between_the_read_and_the_check_withdraw_the_answer_before_any_refold(pair):
    a, b, hub = _settled(pair)
    view = routing._load_view()
    generation = routing._generation(view, routing.settings())
    remote = routing.routing_dir() / "remote.jsonl"
    remote.write_text(remote.read_text() + "\n")  # as a store of new rows leaves it: another stat
    assert not recall_prompt._routing_generation_current(generation)


def test_the_fingerprint_follows_what_the_state_says_not_how_current_it_is(pair):
    a, b, hub = _settled(pair)
    before = replication.fingerprint()
    _bookkeeping_write(hub, seconds=30)
    assert replication.fingerprint() == before
    state = _read_state()
    state["uploaded"] = {**state.get("uploaded", {}), "some-mac": 99}
    replication._save(state)
    assert replication.fingerprint() != before
    settled = replication.fingerprint()
    state["hub_offset_seconds"] = 125.0  # a real skew, not jitter
    replication._save(state)
    assert replication.fingerprint() != settled


def test_a_damaged_or_missing_state_file_counts_as_a_change(pair):
    a, b, hub = _settled(pair)
    before = replication.fingerprint()
    path = routing.routing_dir() / "replication.json"
    path.write_text("{not json")
    damaged = replication.fingerprint()
    assert damaged != before and damaged[1] is not None
    path.unlink()
    assert replication.fingerprint()[1] is None


@pytest.mark.parametrize("field", ["pull_cursor", "caught_up_at", "checked_local_at", "scan_after", "scan_until", "scan_order",
                                   "completed_order"])
def test_each_bookkeeping_field_alone_leaves_the_fingerprint_alone(pair, field):
    a, b, hub = _settled(pair)
    state = _read_state()
    before = replication.fingerprint()
    state[field] = "2026-11-05T05:55:47Z" if field != "scan_after" else ["2026-11-05T05:55:47Z", "x"]
    replication._save(state)
    assert replication.fingerprint() == before


def test_a_sync_with_only_deferred_rows_saves_the_state_once_not_once_per_row(pair, monkeypatch):
    """A Mac with a thousand usage samples used to rewrite and sync
    replication.json once per sample, every round. These never cross: nothing
    can place them, as they carry no witness or reset time."""
    a, _, hub = pair
    a.activate()
    for i in range(1, 41):
        a.write(a.event("usage_sample", i, account="codex|", window="weekly", used_pct=1))
    routing.add_rule("still ships", via="app")
    assert sync(a, hub)["deferred_kinds"] == []
    writes = []
    real = replication._atomic
    monkeypatch.setattr(replication, "_atomic", lambda path, data: (writes.append(path.name), real(path, data)))
    hub.now += timedelta(seconds=10)
    assert sync(a, hub)["deferred_kinds"] == []
    assert writes.count("replication.json") <= 2
