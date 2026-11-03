"""The alias inventory's accounts come from the saved view on the prompt path
(``routing.inventory_accounts``): a cold prompt child no longer parses the
whole routing log before it can say it is ready (235-252 ms at 10.8k lines on
the owner's Mac, 2026-11-03). The view is used only when it holds every account
``routing.accounts()`` would give now; a stale view with an account line since
its fold, a replaced or shorter log, a changed config, or a missing or
hand-edited view falls back to the parse, never to an empty inventory, so a
login folder is redacted either way. Hermetic."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from khipu import agent_view, routing
from khipu.config import config_file
from tests import routing_compact_synth as synth


@pytest.fixture
def record(tmp_path, monkeypatch):
    """A multi-account record: two Claude logins, Codex at its default, and an
    owner-defined Grok account bound to a login folder outside the home
    folder, whose only route into the inventory is its definition in the log."""
    home = tmp_path / "home"
    work = home / ".claude-work"
    work.mkdir(parents=True)
    bound = tmp_path / "elsewhere" / "grok-side-login"
    bound.mkdir(parents=True)
    later = tmp_path / "elsewhere" / "codex-late-login"
    later.mkdir(parents=True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.json").write_text(json.dumps({
        "usage_accounts": [{"label": "Claude", "harness": "claude"},
                           {"label": "Work", "harness": "claude", "home": str(work)},
                           {"label": "Codex", "harness": "codex"}],
        "routing_account_bindings": {"grok:side": {"harness": "grok", "home": str(bound)},
                                     "codex:late": {"harness": "codex", "home": str(later)}},
        "routing_reset_check": False}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("KHIPU_DATA_DIR", str(data))
    monkeypatch.setenv("KHIPU_CAPTURE_HOME", str(tmp_path / "capture"))
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    monkeypatch.setattr(routing, "kick_plan_refresh", lambda: False)
    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})
    keys = ["claude|", f"claude|{work}", "codex|"]
    synth.write(routing.events_file(), synth.real_shape(keys, datetime.now(timezone.utc) - timedelta(minutes=5),
                                                        total=3000))
    routing.set_definition("account", {"id": "grok:side", "provider": "grok", "label": "Grok side"}, via="app")
    routing.read_view()
    assert routing._load_view() is not None
    return {"work": str(work), "bound": str(bound), "later": str(later)}


@pytest.fixture
def parses(monkeypatch):
    """Every whole-log parse (``routing._read_all_events``), counted, with the
    parse caches emptied as in a fresh prompt child."""
    calls: list[int] = []
    real = routing._read_all_events

    def counted(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(routing, "_read_all_events", counted)
    _cold()
    return calls


def _cold():
    routing._ENTRY_INPUTS.clear()
    routing._PARSED_LINE_CACHE.clear()
    agent_view._TABLE.clear()
    agent_view._TABLE.update(sig=None, at=0.0, table=None)


def _homes(accounts):
    return sorted((a["harness"], a["home"]) for a in accounts if a.get("home"))


def _keep_view():
    """view.json as it is now, to put back after a write: the saved view
    then predates that write, as when a prompt answers before the refold."""
    saved = routing.view_file().read_bytes()
    return lambda: routing.view_file().write_bytes(saved)


def _redacted(record, *names, bare=()):
    """Each login folder's path is redacted; so is the bare folder name of
    each in ``bare`` (the name alias comes from the view's own accounts and
    the config, as before: the inventory adds the path)."""
    text = " ".join(f"see {record[n]}/settings.json and {os.path.basename(record[n])}" for n in names)
    out = agent_view.scrub(text)
    for n in names:
        assert record[n] not in out
    for n in bare:
        assert os.path.basename(record[n]) not in out


def test_the_prompt_child_reads_the_inventory_without_parsing_the_log(record, parses):
    agent_view.prepare_routing_table(cold=True)
    assert agent_view._TABLE.get("helper_table") is not None
    with agent_view.routing_projection(()):
        pass
    assert record["bound"] in routing._configured_paths(from_view=True)
    assert _homes(routing.inventory_accounts()) == [("claude", record["work"]), ("grok", record["bound"])]
    assert parses == []
    # Replacing a login path found in text works out its account's id (a
    # read of the identity lines), only when a text holds one.
    _redacted(record, "bound", "work", bare=("bound", "work"))


def test_the_view_inventory_is_the_parse_inventory(record, parses):
    from_view = routing._view_inventory()
    assert from_view is not None and parses == []
    parsed = routing.accounts()
    assert parses
    assert _homes(from_view) == _homes(parsed)
    assert {(a["key"], a["harness"]) for a in parsed} <= {(a["key"], a["harness"]) for a in from_view}
    assert routing._configured_path_names(from_view=True) == routing._configured_path_names()


def test_an_ordinary_line_since_the_fold_keeps_the_view(record, parses):
    restore = _keep_view()
    routing.conversation_write({"action": "report", "state": "open", "account": "claude"},
                               author={"kind": "conversation", "harness": "codex", "session": "w"})
    restore()
    _cold()
    parses.clear()
    assert routing._view_inventory() is not None
    assert parses == []


def test_an_account_defined_after_the_fold_is_still_redacted(record, parses):
    restore = _keep_view()
    routing.set_definition("account", {"id": "codex:late", "provider": "codex", "label": "Codex late"}, via="app")
    restore()  # the saved view predates the definition, as a stale view does
    _cold()
    parses.clear()
    assert routing._view_inventory() is None
    assert ("codex", record["later"]) in _homes(routing.inventory_accounts())
    assert parses
    _cold()
    agent_view.prepare_routing_table(cold=True)  # the build asks for the parse, which then runs alone
    table = agent_view._TABLE.get("helper_table")
    assert table is not None and any(record["later"].lower() in r for r, _to in table.root_to)
    _redacted(record, "later", "bound", "work", bare=("bound", "work"))


@pytest.mark.parametrize("kind", ["account_rename", "account_binding", "definition_remove", "provider_set",
                                  "account_txn"])
def test_any_account_line_since_the_fold_falls_back(record, kind):
    synth.append(routing.events_file(), [{"type": kind, "id": "x1", "written_by": {"kind": "owner"}}])
    assert routing._view_inventory() is None


@pytest.mark.parametrize("line", [b'{"type": "evidence", "txn": "t1"}\n', b'{"type": "evidence", "batch": "b"}\n',
                                  b'not json\n', b'{"type": "evidence"', b'["evidence"]\n'])
def test_an_unreadable_or_grouped_line_since_the_fold_falls_back(record, line):
    with routing.events_file().open("ab") as fh:
        fh.write(line)
    assert routing._view_inventory() is None


def test_a_compacted_or_replaced_log_falls_back(record, parses):
    path = routing.events_file()
    tmp = path.with_suffix(".new")
    tmp.write_bytes(path.read_bytes())
    os.replace(tmp, path)  # the same bytes in another file: what compaction leaves
    assert routing._view_inventory() is None
    assert _homes(routing.inventory_accounts()) == [("claude", record["work"]), ("grok", record["bound"])]
    assert parses


def test_a_shorter_or_rewritten_log_falls_back(record):
    path = routing.events_file()
    data = path.read_bytes()
    with path.open("r+b") as fh:
        fh.truncate(len(data.rstrip(b"\n").rsplit(b"\n", 1)[0]) + 1)
    assert routing._view_inventory() is None
    with path.open("r+b") as fh:
        fh.truncate(0)
        fh.write(data)
    later = time.time_ns() + 10**9
    os.utime(path, ns=(later, later))
    assert routing._view_inventory() is None


def test_a_changed_config_falls_back(record):
    cfg = json.loads(config_file().read_text())
    cfg["routing_account_bindings"]["grok:side"]["home"] = record["later"]
    config_file().write_text(json.dumps(cfg))
    assert routing._view_inventory() is None
    assert ("grok", record["later"]) in _homes(routing.inventory_accounts())


@pytest.mark.parametrize("how", ["missing", "tampered"])
def test_a_missing_or_hand_edited_view_falls_back_to_the_parse(record, parses, how):
    path = routing.view_file()
    if how == "missing":
        path.unlink()
    else:
        view = json.loads(path.read_text())
        view["accounts"] = [a for a in view["accounts"] if a.get("home") != record["bound"]]
        path.write_text(json.dumps(view))  # its digest no longer matches
    assert routing._view_inventory() is None
    assert ("grok", record["bound"]) in _homes(routing.inventory_accounts())
    assert parses
    _cold()
    _redacted(record, "bound", "work", bare=("work",))


def test_an_inventory_that_cannot_be_built_still_withholds(record, monkeypatch):
    """The fallback parse runs inside the same bounded inventory build, so a
    parse that cannot finish leaves no table and the prompt withholds."""
    routing.view_file().unlink()
    _cold()
    monkeypatch.setattr(agent_view, "_BUILD_BUDGET_S", 0.000001)
    monkeypatch.setattr(agent_view, "COLD_FOLD_BUDGET_S", 0.000001)
    agent_view.prepare_routing_table(cold=True)
    assert agent_view._TABLE.get("helper_table") is None
    with pytest.raises(agent_view.InventoryUnavailable):
        with agent_view.routing_projection(()):
            pass


def test_the_pre_ready_cost_does_not_grow_with_the_log(tmp_path):
    from tests import routing_prompt_bench as bench

    small = bench.pre_ready(1_000, root=tmp_path / "small")
    large = bench.pre_ready(10_000, root=tmp_path / "large")
    assert small["lines"] < 1_100 and large["lines"] >= 10_000
    assert small["parses"] == large["parses"] == 0
    assert large["pre_ready_ms"] < max(3 * small["pre_ready_ms"], small["pre_ready_ms"] + 30)
