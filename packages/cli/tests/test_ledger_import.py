"""Importing an old tracker file into the ledger, `khipu_ledger` and the
ledger line of `khipu_status` (Work view phase 2), against the scratch
PostgreSQL of test_ledger (never the live hub).

Every tracker file here is synthetic and written in a temporary folder.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests import test_ledger as _tl
from tests.test_ledger import KEY, P, _real_connect, _sql, ok, run

# The scratch PostgreSQL and a clean ledger in it, as test_ledger has them.
pg_dsn, hub = _tl.pg_dsn, _tl.hub


@pytest.fixture(autouse=True)
def _started_by_the_app(khipu_app):
    """An import is the owner's change: here the app itself started it,
    unless a test says otherwise."""
    return khipu_app

STATES = ["VERIFIED", "MERGED-UNVERIFIED", "BUILT-UNMERGED", "IN FLIGHT", "QUEUED", "NOT STARTED", "NEEDS MATT",
          "DROPPED", "DONE", "ON HOLD"]  # the last is not a standard word
KINDS = ["goal", "slice", "review", "loose-end", "needs-matt", "audit-item", "drive", "wave", "train",
         "odd-kind"]  # the last is not a standard word


def _tracker(n_rows: int = 1200) -> dict:
    """``n_rows`` rows holding 2.5 history entries each on average, in both
    shapes, with every state and kind, mixed id forms, multi-line text, a
    missing parent, a 1,400-character title and a key-shaped evidence."""
    rows: dict[str, dict] = {}
    roots: list[str] = []
    for i in range(n_rows):
        row_id = str(1000 + i) if i % 5 == 0 else f"s-{i:04d}-x"
        state, kind = STATES[i % len(STATES)], KINDS[(i // 3) % len(KINDS)]
        parent = roots[(i * 7) % len(roots)] if i % 4 == 1 and roots else ""
        if i % 4 == 0:
            roots.append(row_id)
        history = [{"ts": f"2026-09-{1 + i % 28:02d}T10:00:{i % 60:02d}Z", "from": None, "to": "NOT STARTED",
                    "note": "seeded"},
                   {"at": f"2026-09-{1 + i % 28:02d}T11:00:00Z", "event": f"Owner authorised step {i}\nin chat"}]
        if i % 2 == 0:
            history.append({"ts": f"2026-10-{1 + i % 14:02d}T09:30:00Z", "from": "NOT STARTED", "to": state,
                            "note": "moved\nwith a two-line note"})
        rows[row_id] = {"id": row_id, "title": f"Row {i}" + ("\nsecond line of the title" if i % 9 == 0 else ""),
                        "kind": kind, "parent": parent, "state": state, "evidence": f"PR {i}",
                        "owner": "lane-a" if i % 3 else "none", "updated": f"2026-10-{1 + i % 14:02d}T09:30:00Z",
                        "history": history}
    ids = list(rows)
    rows[ids[3]]["parent"] = "ghost-parent"
    rows[ids[5]]["title"] = "T" * 1400
    rows[ids[7]]["evidence"] = f"token {KEY}"
    return {"rows": rows}


def _write(tmp_path: Path, doc: dict, name: str = "tracker.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return path


def _import(path: Path, *extra: str, project: str = P) -> tuple[int, dict]:
    rc, out, err = run("import", str(path), "--project", project, "--json", *extra)
    if out.strip():
        return rc, json.loads(out)
    return rc, {"error": err}


def _counts(dsn) -> tuple[int, int, int]:
    return (_sql(dsn, "SELECT count(*) FROM ledger_rows")[0][0],
            _sql(dsn, "SELECT count(*) FROM ledger_history")[0][0],
            _sql(dsn, "SELECT count(*) FROM ledger_imports")[0][0])


def _export() -> dict:
    rc, out, err = run("export", "--tracker-json", "--project", P)
    assert rc == 0, err
    return json.loads(out)["rows"]


# ---- (a) a large synthetic tracker round-trips --------------------------------------


def test_a_large_tracker_imports_with_matching_counts_and_round_trips(hub, tmp_path):
    doc = _tracker()
    assert sum(len(r["history"]) for r in doc["rows"].values()) == 3000
    rc, report = _import(_write(tmp_path, doc))
    assert rc == 0, report
    assert report["result"] == "done"
    counts = report["counts_in_file"]
    assert counts["rows"] == 1200 and counts["history"] == 3000
    assert report["check"]["ok"] and not report["check"]["counts_differ"]
    assert report["check"]["counts_in_ledger"]["by_state"] == counts["by_state"]
    assert set(counts["by_state"]) == set(STATES) and set(counts["by_kind"]) == set(KINDS)
    masked_id = list(doc["rows"])[7]
    assert report["masked"] == [f"{masked_id}.evidence"]
    assert list(doc["rows"])[3] in counts["missing_parent"]
    assert report["new_words"]["state"] == [["ON HOLD", "open"]] and report["new_words"]["kind"] == ["odd-kind"]
    # Every entry plus one "Imported from" line per row.
    assert _counts(hub) == (1200, 3000 + 1200, 1)
    assert _sql(hub, "SELECT count(*) FROM ledger_history WHERE raw IS NOT NULL")[0][0] == 3000

    back = _export()
    assert set(back) == set(doc["rows"])
    differs = [(i, k) for i, row in doc["rows"].items() for k in set(row) | set(back[i])
               if row.get(k) != back[i].get(k)]
    assert differs == [(masked_id, "evidence")]
    assert KEY not in json.dumps(back) and back[masked_id]["evidence"].startswith("token ")
    assert back[list(doc["rows"])[5]]["title"] == "T" * 1400

    # The board follows the tracker's order: kind, then parent group, then id.
    vocab_kinds = [w for (w,) in _sql(hub, "SELECT word FROM ledger_vocab WHERE project = %s AND field = 'kind' "
                                           "ORDER BY position", (P,))]

    def key(r):
        known = r["kind"] in vocab_kinds
        return (vocab_kinds.index(r["kind"]) if known else len(vocab_kinds), "" if known else r["kind"],
                r["parent"] or r["id"], r["id"])

    expected = [r["id"] for r in sorted(doc["rows"].values(), key=key)]
    board = ok("render")
    section = board.split("## Rows", 1)[1].split("## Changed since", 1)[0]
    shown = [ln[2:].split(" · ", 1)[0] for ln in section.splitlines() if ln.startswith("- ") and " · " in ln]
    assert shown == expected
    # The words new to the standard list are the project's own now (rule 7), stored as written.
    assert ok("check").startswith("CHECK OK: 1200 rows")
    assert _sql(hub, "SELECT count(*) FROM ledger_rows WHERE kind = 'odd-kind' OR state = 'ON HOLD'")[0][0] > 0


# ---- (b) nothing is skipped silently --------------------------------------------------


@pytest.mark.parametrize("bad", ["id", "history"])
def test_a_bad_row_refuses_the_whole_import_and_writes_nothing(bad, hub, tmp_path):
    doc = _tracker(10)
    if bad == "id":
        doc["rows"]["bad id!"] = {"id": "bad id!", "title": "t", "kind": "slice", "state": "QUEUED"}
    else:
        doc["rows"][next(iter(doc["rows"]))]["history"] = {"not": "a list"}
    rc, report = _import(_write(tmp_path, doc))
    assert rc == 1 and report["reason"] == "bad_rows"
    assert "Nothing was imported" in report["error"]
    assert _counts(hub) == (0, 0, 0)


# ---- (c) existing and purged ids ------------------------------------------------------


def test_an_import_refuses_a_purged_id_and_an_existing_id(hub, tmp_path):
    from khipu import ledger

    ok("add", "p-1", "--title", "Gone", "--kind", "slice")
    ok("remove", "p-1")
    with _real_connect(hub) as conn:
        ledger.purge(conn, P, "p-1")
    doc = {"rows": {"p-1": {"id": "p-1", "title": "Back?", "kind": "slice", "parent": "", "state": "QUEUED",
                            "evidence": "", "owner": "none", "updated": "2026-10-01T00:00:00Z", "history": []},
                    "n-1": {"id": "n-1", "title": "New", "kind": "slice", "parent": "", "state": "QUEUED",
                            "evidence": "", "owner": "none", "updated": "2026-10-01T00:00:00Z", "history": []}}}
    rc, report = _import(_write(tmp_path, doc))
    assert rc == 1 and report["result"] == "refused" and report["reason"] == "purged"
    assert report["purged_ids"] == ["p-1"] and "removed for good" in report["words"]
    assert _sql(hub, "SELECT count(*) FROM ledger_rows")[0][0] == 0

    ok("add", "e-1", "--title", "Here", "--kind", "slice")
    doc = {"rows": {"e-1": dict(doc["rows"]["n-1"], id="e-1"), "n-1": doc["rows"]["n-1"]}}
    rc, report = _import(_write(tmp_path, doc, "other.json"))
    assert rc == 1 and report["reason"] == "exists" and report["existing_ids"] == ["e-1"]
    assert [r for (r,) in _sql(hub, "SELECT row_id FROM ledger_rows")] == ["e-1"]


# ---- (d) a mismatch rolls back ------------------------------------------------------


def test_a_forced_mismatch_rolls_the_whole_import_back(hub, tmp_path, monkeypatch):
    from khipu import ledger_import

    monkeypatch.setattr(ledger_import, "_compare", lambda expected, got: ["s-0001-x.title"])
    rc, report = _import(_write(tmp_path, _tracker(20)))
    assert rc == 1 and report["result"] == "rolled back"
    assert "1 field(s) that differ" in report["words"]
    assert _sql(hub, "SELECT count(*) FROM ledger_rows")[0][0] == 0
    assert _sql(hub, "SELECT count(*) FROM ledger_history")[0][0] == 0
    assert _sql(hub, "SELECT result FROM ledger_imports") == [("rolled back",)]


# ---- (e) re-importing the same file -------------------------------------------------


def test_re_importing_the_same_file_is_a_no_op(hub, tmp_path):
    path = _write(tmp_path, _tracker(30))
    assert _import(path)[0] == 0
    before = _counts(hub)
    rc, report = _import(path)
    assert rc == 0 and report["result"] == "nothing new"
    assert report["words"] == "Nothing new: every row and history entry is already here."
    assert _counts(hub) == before


# ---- (f) catching up ------------------------------------------------------------------


def _row(row_id, state="QUEUED", history=None, updated="2026-10-01T00:00:00Z"):
    return {"id": row_id, "title": f"Row {row_id}", "kind": "slice", "parent": "", "state": state,
            "evidence": "", "owner": "lane-a", "updated": updated,
            "history": history if history is not None else [{"ts": "2026-10-01T00:00:00Z", "from": None,
                                                             "to": state, "note": "seeded"}]}


def test_catching_up_brings_only_what_is_new_and_never_picks_a_winner(hub, tmp_path):
    doc = {"rows": {"a": _row("a"), "b": _row("b"), "c": _row("c")}}
    path = _write(tmp_path, doc)
    assert _import(path)[0] == 0
    ok("set", "c", "--state", "IN FLIGHT")  # changed in Khipu since the import
    # The old script keeps writing the old file.
    entry = {"ts": "2026-10-02T00:00:00Z", "from": "QUEUED", "to": "BUILT-UNMERGED", "note": "built"}
    doc["rows"]["a"]["history"].append({"at": "2026-10-02T00:00:00Z", "event": "Looked at it"})
    doc["rows"]["b"].update(state="BUILT-UNMERGED", updated="2026-10-02T00:00:00Z")
    doc["rows"]["b"]["history"].append(entry)
    doc["rows"]["c"].update(state="BUILT-UNMERGED", updated="2026-10-02T00:00:00Z")
    doc["rows"]["c"]["history"].append(entry)
    doc["rows"]["d"] = _row("d")
    _write(tmp_path, doc)

    rc, dry = _import(path, "--catch-up", "--dry-run")
    assert rc == 0 and dry["result"] == "dry run"
    assert _sql(hub, "SELECT count(*) FROM ledger_rows")[0][0] == 3
    rc, report = _import(path)  # without --catch-up a changed file is refused, never brought in
    assert rc == 1 and "--catch-up" in report["words"]

    rc, report = _import(path, "--catch-up")
    assert rc == 0 and report["result"] == "caught up", report
    plan = report["plan"]
    assert plan["new_rows"] == ["d"] and plan["new_entries"] == 4
    assert plan["updated"] == ["b"] and plan["both"] == ["c"]
    assert "Changed in both places" in report["words"]
    back = _export()
    # Each file entry is in the ledger exactly once.
    for row_id in "abd":
        raw = [h for h in back[row_id]["history"] if "by" not in h]
        assert raw == doc["rows"][row_id]["history"], row_id
    assert back["b"]["state"] == "BUILT-UNMERGED"
    brought = [h for h in back["b"]["history"] if h.get("changes")]
    assert brought[-1]["changes"]["state"] == {"old": "QUEUED", "new": "BUILT-UNMERGED"}
    # The row changed in both places keeps the ledger's value and gets the file's entry as history.
    assert back["c"]["state"] == "IN FLIGHT"
    assert entry in back["c"]["history"]
    assert any("Changed in both places" in (h.get("event") or "") for h in back["c"]["history"])

    rc, again = _import(path, "--catch-up")
    assert rc == 0 and again["result"] == "nothing new"


# ---- (g) a dry run ----------------------------------------------------------------------


def test_a_dry_run_writes_nothing(hub, tmp_path):
    rc, report = _import(_write(tmp_path, _tracker(40)), "--dry-run")
    assert rc == 0 and report["result"] == "dry run" and report["hub_checked"]
    assert "Nothing was written" in report["words"]
    assert _counts(hub) == (0, 0, 0)
    assert _sql(hub, "SELECT count(*) FROM ledger_projects")[0][0] == 0
    rc, out, _err = run("import", str(tmp_path / "tracker.json"), "--project", P, "--dry-run")
    assert rc == 0 and out.startswith(f"tracker.json -> {P}: dry run") and "In the file: 40 rows" in out


# ---- (h) a private folder ----------------------------------------------------------------


def test_a_file_in_a_credential_location_is_refused_before_it_is_opened(hub, tmp_path, monkeypatch):
    import os

    from khipu import routing_sources

    opened = []
    real_open = os.open
    monkeypatch.setattr(routing_sources.os, "open", lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    # Never created: the refusal comes from the path's names alone.
    rc, report = _import(tmp_path / "API Keys" / "tracker.json")
    assert rc == 1 and report["reason"] == "refused" and "private folder" in report["error"]
    assert opened == [] and _counts(hub) == (0, 0, 0)


# ---- (i) khipu_ledger --------------------------------------------------------------------


def _tool(args: dict) -> tuple[bool, dict]:
    from khipu import mcp_server as ms

    out = ms.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": "khipu_ledger", "arguments": args}})
    return out["result"]["isError"], json.loads(out["result"]["content"][0]["text"])


def test_khipu_ledger_adds_sets_reads_lists_and_removes(hub):
    err, got = _tool({"verb": "add", "project": P, "id": "t-1", "title": "Do it", "kind": "slice"})
    assert not err and got["line"] == "added t-1: NOT STARTED" and got["op_id"]
    err, got = _tool({"verb": "set", "project": P, "id": "t-1", "state": "IN FLIGHT", "evidence": "PR 7",
                      "op_id": "retry-1"})
    assert not err and got["changed"] == ["state", "evidence"] and got["op_id"] == "retry-1"
    err, got = _tool({"verb": "get", "project": P, "id": "t-1"})
    assert not err and got["row"]["state"] == "IN FLIGHT"
    assert [(h["from"], h["to"]) for h in got["row"]["history"]] == [(None, "NOT STARTED"),
                                                                    ("NOT STARTED", "IN FLIGHT")]
    err, got = _tool({"verb": "list", "project": P})
    assert not err and [r["id"] for r in got["rows"]] == ["t-1"]
    err, got = _tool({"verb": "render", "project": P})
    assert not err and got["board"].startswith("# Orchestrator Tracker")
    err, got = _tool({"verb": "remove", "project": P, "id": "t-1", "note": "not needed"})
    assert not err and got["line"] == "removed t-1"
    err, got = _tool({"verb": "removed", "project": P})
    assert not err and [r["id"] for r in got["removed"]] == ["t-1"]
    err, got = _tool({"verb": "set", "project": P, "id": "nope", "state": "DONE"})
    assert err and got["reason"] == "not_found" and got["error"] == "no such row: nope"
    err, got = _tool({"verb": "purge", "project": P, "id": "t-1"})
    assert err and "verb must be one of" in got["error"]


def test_khipu_ledger_is_listed_with_rule_4():
    from khipu import mcp_server as ms

    tools = ms.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]["tools"]
    tool = next(t for t in tools if t["name"] == "khipu_ledger")
    assert "never code, diffs, file contents, command output, logs, prompts, briefs, customer or financial " \
           "details" in tool["description"]
    assert tool["inputSchema"]["properties"]["verb"]["enum"][-1] == "removed"


# ---- (j) the ledger line of khipu_status ----------------------------------------------------


def test_the_status_line_counts_rows_loose_ends_and_needs(hub):
    from khipu import ledger

    ok("add", "o-1", "--title", "Open", "--kind", "slice")
    ok("add", "n-1", "--title", "Needs", "--kind", "slice", "--state", "NEEDS OWNER")
    ok("add", "d-1", "--title", "Done", "--kind", "slice", "--state", "DONE")
    ok("add", "w-1", "--title", "Odd", "--kind", "slice")
    ok("add", "r-1", "--title", "Removed", "--kind", "slice")
    ok("remove", "r-1")
    _sql(hub, "UPDATE ledger_rows SET state = 'WEIRD' WHERE row_id = 'w-1'")
    assert ledger.status_line() == {"available": True, "rows": 4, "loose_ends": 2, "needs_owner": 1, "outbox": 0}
    assert ledger.status_line(hub=False) == {"available": False, "rows": None, "loose_ends": None,
                                             "needs_owner": None, "outbox": 0}


# ---- (k) review regressions: the import's model -----------------------------------------


def _threaded_set(dsn, row_id: str, state: str):
    """A set from another writer on its own connection (another Mac, say),
    started on a thread; returns the thread."""
    import threading

    from khipu import ledger

    def go():
        with _real_connect(dsn) as conn:
            ledger.apply_op(conn, ledger.new_op("set", P, row_id, fields={"state": state}, source="cli"))

    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def test_catching_up_never_overwrites_a_writer_that_lands_during_it(hub, tmp_path, monkeypatch):
    import time

    from khipu import ledger_import

    doc = {"rows": {"a": _row("a")}}
    path = _write(tmp_path, doc)
    assert _import(path)[0] == 0
    doc["rows"]["a"].update(state="BUILT-UNMERGED", updated="2026-10-02T00:00:00Z")
    _write(tmp_path, doc)
    seen: dict = {}
    real = ledger_import._save_vocab

    def other_writer_arrives(*args, **kw):
        seen["thread"] = _threaded_set(hub, "a", "IN FLIGHT")
        time.sleep(0.5)
        seen["blocked"] = seen["thread"].is_alive()
        return real(*args, **kw)

    monkeypatch.setattr(ledger_import, "_save_vocab", other_writer_arrives)
    rc, report = _import(path, "--catch-up")
    seen["thread"].join(10)
    assert rc == 0 and report["result"] == "caught up", report
    # The other writer waited for the catch-up, then landed after it: its value stands.
    assert seen["blocked"], "the other writer was not held off while the catch-up ran"
    assert _sql(hub, "SELECT state FROM ledger_rows WHERE row_id = 'a'") == [("IN FLIGHT",)]


def test_an_extra_row_written_during_an_import_rolls_it_back(hub, tmp_path, monkeypatch):
    from khipu import ledger_import

    real = ledger_import._insert_rows

    def and_one_more(cur, project, ids, rows, now):
        real(cur, project, ids, rows, now)
        real(cur, project, ["zz-extra"], {"zz-extra": _row("zz-extra")}, now)

    monkeypatch.setattr(ledger_import, "_insert_rows", and_one_more)
    rc, report = _import(_write(tmp_path, {"rows": {"a": _row("a")}}))
    assert rc == 1 and report["result"] == "rolled back", report
    assert _sql(hub, "SELECT count(*) FROM ledger_rows")[0][0] == 0


def test_a_duplicated_history_entry_in_a_catch_up_rolls_it_back(hub, tmp_path, monkeypatch):
    from khipu import ledger_import

    doc = {"rows": {"a": _row("a")}}
    path = _write(tmp_path, doc)
    assert _import(path)[0] == 0
    before = _counts(hub)
    doc["rows"]["a"]["history"].append({"at": "2026-10-02T00:00:00Z", "event": "Looked at it"})
    _write(tmp_path, doc)
    real = ledger_import._insert_history

    def twice(cur, project, entries):
        real(cur, project, entries)
        real(cur, project, [e for e in entries if e[2] is None])

    monkeypatch.setattr(ledger_import, "_insert_history", twice)
    rc, report = _import(path, "--catch-up")
    assert rc == 1 and report["result"] == "rolled back", report
    assert _counts(hub)[:2] == before[:2]


def test_a_history_only_change_in_both_places_is_listed(hub, tmp_path):
    doc = {"rows": {"a": _row("a")}}
    path = _write(tmp_path, doc)
    assert _import(path)[0] == 0
    ok("set", "a", "--note", "Local note")
    doc["rows"]["a"]["history"].append({"at": "2026-10-02T00:00:00Z", "event": "Old tracker note"})
    _write(tmp_path, doc)
    rc, report = _import(path, "--catch-up")
    assert rc == 0 and report["plan"]["both"] == ["a"], report
    assert "Changed in both places" in report["words"]


def test_secret_shaped_keys_are_masked_on_import_and_export(hub, tmp_path):
    row = _row("a")
    row["extra"] = {KEY: "synthetic"}
    doc = {"rows": {"a": row}, KEY: 1}
    rc, report = _import(_write(tmp_path, doc))
    assert rc == 0 and report["result"] == "done", report
    assert report["masked"] and KEY not in json.dumps(report)
    assert KEY not in json.dumps(_export())


@pytest.mark.parametrize("how", ["row", "history"])
def test_duplicate_json_members_are_refused(how, tmp_path):
    from khipu import ledger, ledger_import

    a, b = _row("a"), _row("a")
    b["title"] = "Second row"
    if how == "row":
        text = '{"rows": {"a": ' + json.dumps(a) + ', "a": ' + json.dumps(b) + "}}"
    else:
        text = json.dumps({"rows": {"a": _row("a", history=[])}}).replace(
            '"history": []', '"history": [{"ts": "2026-10-01T00:00:00Z", "note": "first", "note": "last"}]')
    path = tmp_path / "tracker.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ledger.LedgerError, match="more than once"):
        ledger_import.read_tracker(path)


# ---- (l) review regressions: reading the file safely ----------------------------------------


def test_a_link_swapped_in_after_the_checks_is_never_followed(tmp_path, monkeypatch):
    import os

    from khipu import ledger, ledger_import

    chosen = _write(tmp_path, {"rows": {"a": _row("a")}}, "chosen.json")
    other = _write(tmp_path, {"rows": {"b": _row("b")}}, "other.json")
    real_open = os.open

    def swap(name, flags, *a, **k):
        if Path(os.fspath(name)).name == "chosen.json":
            chosen.unlink()
            chosen.symlink_to(other)
        return real_open(name, flags, *a, **k)

    monkeypatch.setattr(os, "open", swap)
    try:
        got = ledger_import.read_tracker(chosen)
    except ledger.LedgerError as exc:
        assert exc.reason == "refused"
        return
    assert set(got["rows"]) == {"a"}, f"read another file: {list(got['rows'])}"


@pytest.mark.parametrize("how", ["symlink", "hardlink"])
def test_a_linked_tracker_file_is_refused(how, tmp_path):
    import os

    from khipu import ledger, ledger_import

    target = _write(tmp_path, {"rows": {"a": _row("a")}}, "real.json")
    link = tmp_path / "chosen.json"
    link.symlink_to(target) if how == "symlink" else os.link(target, link)
    with pytest.raises(ledger.LedgerError) as caught:
        ledger_import.read_tracker(link)
    assert caught.value.reason == "refused"


def test_a_configured_login_folder_is_refused(tmp_path, monkeypatch):
    from khipu import config, ledger, ledger_import

    login = tmp_path / "login-home"
    login.mkdir()
    path = _write(login, {"rows": {"a": _row("a")}})
    monkeypatch.setattr(config, "load_config",
                        lambda *a, **k: {"usage_accounts": [{"harness": "codex", "home": str(login)}]})
    with pytest.raises(ledger.LedgerError) as caught:
        ledger_import.read_tracker(path)
    assert caught.value.reason == "refused" and "private" in str(caught.value)


# ---- (m) review regressions: khipu_ledger keeps to titles (rule 4) ----------------------------


@pytest.mark.parametrize("field", ["title", "evidence", "note", "owner"])
def test_khipu_ledger_refuses_multiline_code(field, hub):
    from khipu import ledger

    text = 'def customer_export():\n    return open("records.csv").read()'
    args = {"verb": "add", "project": P, "id": "a", "title": "Work title", "kind": "slice", field: text}
    err, got = _tool(args)
    assert err and got["reason"] == "not_one_line", got
    assert ledger.queued_ops() == [] and _sql(hub, "SELECT count(*) FROM ledger_rows")[0][0] == 0


def test_khipu_ledger_cuts_full_paths_to_their_last_name(hub):
    err, got = _tool({"verb": "add", "project": P, "id": "a", "kind": "slice",
                      "title": "Check /private/customer/records.csv today",
                      "evidence": "see ~/work/notes/plan.md and C:\\data\\x.txt, https://example.com/a/b/c",
                      "note": "moved from /srv/app/build"})
    assert not err, got
    err, got = _tool({"verb": "get", "project": P, "id": "a"})
    row = got["row"]
    assert row["title"] == "Check records.csv today"
    assert row["evidence"] == "see plan.md and x.txt, https://example.com/a/b/c"
    assert row["history"][0]["note"] == "moved from build"


# ---- (n) review round 2b regressions -------------------------------------------------------


def test_an_unresolved_conflict_blocks_updates_on_the_next_catch_up(hub, tmp_path):
    doc = {"rows": {"a": _row("a")}}
    path = _write(tmp_path, doc)
    assert _import(path)[0] == 0
    ok("set", "a", "--state", "IN FLIGHT")
    doc["rows"]["a"].update(state="BUILT-UNMERGED", updated="2026-10-02T00:00:00Z")
    doc["rows"]["a"]["history"].append({"at": "2026-10-02T00:00:00Z", "event": "Built"})
    _write(tmp_path, doc)
    rc, first = _import(path, "--catch-up")
    assert rc == 0 and first["plan"]["both"] == ["a"], first
    # Neither the conflict note nor the file's entries brought in with it settle the row.
    doc["rows"]["a"].update(state="DONE", updated="2026-10-03T00:00:00Z")
    _write(tmp_path, doc)
    rc, second = _import(path, "--catch-up")
    state = _sql(hub, "SELECT state FROM ledger_rows WHERE row_id = 'a'")[0][0]
    assert rc == 0 and second["plan"]["both"] == ["a"] and not second["plan"]["updated"], second
    assert state == "IN FLIGHT"


def test_two_secret_shaped_keys_in_one_object_are_both_kept(hub, tmp_path):
    row = _row("a")
    row["extra"] = {KEY: "first value", KEY[:-1] + "Z": "second value"}
    rc, report = _import(_write(tmp_path, {"rows": {"a": row}}))
    assert rc == 0 and report["result"] == "done", report
    assert len(report["masked"]) == 2 and len(set(report["masked"])) == 2, report["masked"]
    back = _export()["a"]["extra"]
    assert sorted(back.values()) == ["first value", "second value"], back
    assert KEY not in json.dumps(back) and KEY not in json.dumps(report)


def test_a_changed_native_history_entry_rolls_the_import_back(hub, tmp_path, monkeypatch):
    from khipu import ledger_import

    ok("add", "local", "--title", "Local row", "--kind", "slice", "--note", "Original note")
    real = ledger_import._insert_rows

    def corrupt(cur, project, ids, rows, now):
        real(cur, project, ids, rows, now)
        cur.execute("UPDATE ledger_history SET note = 'CORRUPTED' WHERE project = %s AND row_id = 'local'", (P,))

    monkeypatch.setattr(ledger_import, "_insert_rows", corrupt)
    rc, report = _import(_write(tmp_path, {"rows": {"a": _row("a")}}))
    notes = _sql(hub, "SELECT note FROM ledger_history WHERE row_id = 'local'")
    assert rc == 1 and report["result"] == "rolled back", report
    assert notes == [("Original note",)]


def test_a_link_in_a_parent_folder_is_refused(tmp_path):
    from khipu import ledger, ledger_import

    real = tmp_path / "real"
    real.mkdir()
    _write(real, {"rows": {"a": _row("a")}})
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(ledger.LedgerError) as caught:
        ledger_import.read_tracker(alias / "tracker.json")
    assert caught.value.reason == "refused" and "link" in str(caught.value)


def test_khipu_ledger_cuts_a_path_in_the_owner_and_refuses_one_as_a_project(hub):
    err, got = _tool({"verb": "add", "project": P, "id": "a", "title": "Work", "kind": "slice",
                      "owner": "/private/customer/alice"})
    assert not err, got
    assert _sql(hub, "SELECT owner FROM ledger_rows WHERE row_id = 'a'") == [("alice",)]
    err, got = _tool({"verb": "add", "project": "/private/customer/acme", "id": "b", "title": "Work",
                      "kind": "slice"})
    assert err and got["reason"] == "path", got


# ---- review regressions, round 3: one safe read, the whole project verified ---------------


@pytest.mark.parametrize("variable", ["CODEX_HOME", "GROK_HOME"])
def test_a_login_folder_set_through_a_harness_variable_is_refused(variable, tmp_path, monkeypatch):
    from khipu import agent_view, ledger, ledger_import

    login = tmp_path / "alternate-login"
    login.mkdir()
    path = _write(login, {"rows": {"a": _row("a")}})
    monkeypatch.setenv(variable, str(login))
    monkeypatch.setattr(agent_view, "_saved_view", lambda: {})
    with pytest.raises(ledger.LedgerError) as caught:
        ledger_import.read_tracker(path)
    assert caught.value.reason == "refused"


def _unlink_after_open(monkeypatch, chosen: Path):
    import os

    real_open = os.open

    def race(name, flags, *a, **k):
        fd = real_open(name, flags, *a, **k)
        if str(name) == chosen.name:
            chosen.unlink()
        return fd

    monkeypatch.setattr(os, "open", race)


def test_a_hard_link_whose_outside_name_goes_after_the_open_is_refused(tmp_path, monkeypatch):
    import os

    from khipu import config, ledger, ledger_import

    login = tmp_path / "configured-login"
    login.mkdir()
    private = _write(login, {"rows": {"private": _row("private")}})
    chosen = tmp_path / "chosen.json"
    os.link(private, chosen)
    monkeypatch.setattr(config, "load_config",
                        lambda *a, **k: {"usage_accounts": [{"harness": "codex", "home": str(login)}]})
    _unlink_after_open(monkeypatch, chosen)
    with pytest.raises(ledger.LedgerError) as caught:
        ledger_import.read_tracker(chosen)
    assert caught.value.reason == "refused"


def test_the_summary_reader_refuses_the_same_hard_link_race(tmp_path, monkeypatch):
    import os

    from khipu import config, work

    login = tmp_path / "configured-login"
    login.mkdir()
    private = login / "work-summary.json"
    private.write_text("{}", encoding="utf-8")
    chosen = tmp_path / "work-summary.json"
    os.link(private, chosen)
    monkeypatch.setattr(config, "load_config",
                        lambda *a, **k: {"usage_accounts": [{"harness": "codex", "home": str(login)}]})
    _unlink_after_open(monkeypatch, chosen)
    ok_, problems, _parsed = work.check_file(str(chosen))
    assert not ok_ and problems == [work.SOURCE_PROBLEMS["refused"]]


def test_both_readers_open_through_the_one_safe_read(tmp_path, monkeypatch):
    from khipu import ledger_import, safe_read, work

    for name in ("_open_tracker", "_read_all", "_login_folder", "_system_links_resolved", "_Refused"):
        assert not hasattr(ledger_import, name), name
    for name in ("_open_summary", "_read_fd", "_system_links_resolved", "_Refused"):
        assert not hasattr(work, name), name
    seen = []
    real = safe_read.open_file
    monkeypatch.setattr(safe_read, "open_file", lambda p, *a: (seen.append(Path(p).name), real(p, *a))[1])
    ledger_import.read_tracker(_write(tmp_path, {"rows": {"a": _row("a")}}))
    work.check_file(str(_write(tmp_path, {}, "work-summary.json")))
    assert seen == ["tracker.json", "work-summary.json"]


def test_a_key_shaped_file_name_never_reaches_the_import_record(hub, tmp_path):
    from khipu import ledger_import

    # The CLI refuses a key-shaped argument before this; the app calls run_import directly.
    report = ledger_import.run_import(_write(tmp_path, {"rows": {"a": _row("a")}}, KEY + ".json"), P)
    assert report["result"] == "done", report
    stored = _sql(hub, "SELECT file_name, counts::text FROM ledger_imports")
    events = _sql(hub, "SELECT event FROM ledger_history WHERE event IS NOT NULL")
    assert KEY not in json.dumps([stored, events, report], default=str)


@pytest.mark.parametrize("damage", ["title", "imported history", "removed for good"])
def test_damage_to_a_removed_row_rolls_the_import_back(damage, hub, tmp_path, monkeypatch):
    from khipu import ledger_import

    assert _import(_write(tmp_path, {"rows": {"old": _row("old")}}, "first.json"))[0] == 0
    ok("remove", "old")
    before = _sql(hub, "SELECT row_id, title, removed_at FROM ledger_rows ORDER BY row_id")
    real = ledger_import._insert_rows
    query = {"title": "UPDATE ledger_rows SET title = 'CORRUPTED' WHERE project = %s AND row_id = 'old'",
             "imported history": "UPDATE ledger_history SET raw = '{\"event\": \"CORRUPTED\"}'::jsonb "
                                 "WHERE project = %s AND row_id = 'old' AND raw IS NOT NULL",
             "removed for good": "DELETE FROM ledger_rows WHERE project = %s AND row_id = 'old'"}[damage]

    def corrupt(cur, project, ids, rows, now):
        real(cur, project, ids, rows, now)
        cur.execute(query, (P,))

    monkeypatch.setattr(ledger_import, "_insert_rows", corrupt)
    rc, report = _import(_write(tmp_path, {"rows": {"new": _row("new")}}, "second.json"))
    assert rc == 1 and report["result"] == "rolled back", report
    assert _sql(hub, "SELECT row_id, title, removed_at FROM ledger_rows ORDER BY row_id") == before


def test_a_catch_up_that_deletes_a_removed_row_rolls_back(hub, tmp_path, monkeypatch):
    from khipu import ledger_import

    doc = {"rows": {"a": _row("a"), "old": _row("old")}}
    path = _write(tmp_path, doc)
    assert _import(path)[0] == 0
    ok("remove", "old")
    doc["rows"]["b"] = _row("b")
    _write(tmp_path, doc)
    real = ledger_import._insert_rows

    def and_delete(cur, project, ids, rows, now):
        real(cur, project, ids, rows, now)
        cur.execute("DELETE FROM ledger_history WHERE project = %s AND row_id = 'old'", (P,))
        cur.execute("DELETE FROM ledger_rows WHERE project = %s AND row_id = 'old'", (P,))

    monkeypatch.setattr(ledger_import, "_insert_rows", and_delete)
    rc, report = _import(path, "--catch-up")
    assert rc == 1 and report["result"] == "rolled back", report
    assert _sql(hub, "SELECT row_id FROM ledger_rows ORDER BY row_id") == [("a",), ("old",)]


def test_a_catch_up_that_reorders_imported_history_rolls_back(hub, tmp_path, monkeypatch):
    from khipu import ledger_import

    doc = {"rows": {"a": _row("a", history=[{"at": "2026-10-01T00:00:00Z", "event": "first"},
                                             {"at": "2026-10-02T00:00:00Z", "event": "second"}])}}
    path = _write(tmp_path, doc)
    assert _import(path)[0] == 0
    doc["rows"]["a"]["history"].append({"at": "2026-10-03T00:00:00Z", "event": "third"})
    _write(tmp_path, doc)
    real = ledger_import._insert_history

    def swap(cur, project, entries):
        real(cur, project, entries)
        cur.execute("UPDATE ledger_history SET raw = CASE raw->>'event' "
                    "WHEN 'first' THEN '{\"at\": \"2026-10-02T00:00:00Z\", \"event\": \"second\"}'::jsonb "
                    "WHEN 'second' THEN '{\"at\": \"2026-10-01T00:00:00Z\", \"event\": \"first\"}'::jsonb "
                    "ELSE raw END WHERE project = %s AND row_id = 'a' AND raw IS NOT NULL", (P,))

    monkeypatch.setattr(ledger_import, "_insert_history", swap)
    rc, report = _import(path, "--catch-up")
    assert rc == 1 and report["result"] == "rolled back", report
    assert [e.get("event") for e in _export()["a"]["history"]] == ["first", "second"]


def test_the_check_after_writing_refuses_rows_stored_in_reverse_order(monkeypatch):
    from khipu import ledger_import

    expected = {"a": _row("a"), "b": _row("b")}
    before = {"ids": set(), "all": {}, "hist": {}, "rows": {}}
    # The writer gave the rows their places backwards: b first, then a.
    after = {"ids": set(expected), "all": {"a": {"seq": 2}, "b": {"seq": 1}},
             "hist": {i: [{"has_raw": False, "op_id": "import:1"}] for i in expected},
             "rows": {i: expected[i] for i in ("b", "a")}}
    monkeypatch.setattr(ledger_import, "_snapshot", lambda *a: after)
    report = ledger_import._verify(None, P, expected, ledger_import.tracker_counts(expected), before)
    assert not report["ok"], report
    assert any("order" in d for d in report["fields_differ"]), report


def test_an_xai_shaped_key_is_masked_on_reading_and_reported(tmp_path):
    from khipu import ledger_import

    token = "xai-" + "Ab12" * 12
    source = _row("a")
    source["evidence"] = token
    src = ledger_import.read_tracker(_write(tmp_path, {"rows": {"a": source}}))
    assert token not in json.dumps(src)
    assert src["masked"] == ["a.evidence"], src["masked"]


def test_a_ledger_row_changed_in_both_places_is_noted_once_per_change_in_the_file(hub, tmp_path):
    """A row the ledger added itself (no import record) that the old file
    also holds, with other values: the first catch-up notes it as changed in
    both places; the same file again has nothing new and adds no history; a
    new change in the file is noted again. The note keeps nothing the export
    gives back."""
    rc, report = _import(_write(tmp_path, {"rows": {"a-1": _row("a-1")}}))
    assert rc == 0 and report["result"] == "done", report
    ok("add", "b-1", "--title", "The ledger's own", "--kind", "slice")
    doc = {"rows": {"a-1": _row("a-1"), "b-1": _row("b-1", state="IN FLIGHT")}}
    rc, first = _import(_write(tmp_path, doc, "t2.json"), "--catch-up")
    assert rc == 0 and first["result"] == "caught up" and first["plan"]["both"] == ["b-1"], first

    def notes():
        return _sql(hub, "SELECT count(*) FROM ledger_history WHERE row_id = 'b-1'")[0][0]

    n1 = notes()
    # The same rows, written another way (a new digest for the file).
    path3 = tmp_path / "t3.json"
    path3.write_text(json.dumps(doc, indent=4), encoding="utf-8")
    rc, second = _import(path3, "--catch-up")
    assert rc == 0 and second["result"] == "nothing new" and notes() == n1, (second, n1, notes())
    doc["rows"]["b-1"]["state"] = "QUEUED"
    rc, third = _import(_write(tmp_path, doc, "t4.json"), "--catch-up")
    assert rc == 0 and third["plan"]["both"] == ["b-1"] and notes() == n1 + 1, third
    b1 = _export()["b-1"]
    assert "refs" not in b1 and b1["title"] == "The ledger's own", b1


# ---- owner-only import and in-order catch-up (cross-family review) ----------------

def test_import_is_refused_when_the_app_did_not_start_it_even_without_from_app(monkeypatch, tmp_path, capsys):
    from khipu import cli, ledger_import, routing

    tracker = tmp_path / "tracker.json"
    tracker.write_text(json.dumps({"rows": {}}), encoding="utf-8")
    monkeypatch.setattr(routing, "_caller", lambda: {"app": False, "chain": ["pytest"]})
    called = []
    monkeypatch.setattr(
        ledger_import,
        "run_import",
        lambda *a, **k: called.append((a, k)) or {
            "ok": True,
            "result": "done",
            "file": "tracker.json",
            "project": P,
            "words": "imported",
            "counts_in_file": {
                "rows": 0,
                "history": 0,
                "with_parent": 0,
                "by_state": {},
                "by_kind": {},
                "missing_parent": [],
            },
            "masked": [],
            "other_keys": [],
        },
    )
    args = cli.build_parser().parse_args(
        ["ledger", "import", str(tracker), "--project", P, "--json"]
    )
    rc = args.func(args)
    output = capsys.readouterr()
    assert rc == 1 and "only the Khipu app" in (output.out + output.err)
    assert called == []


def test_catch_up_rejects_history_inserted_before_an_existing_entry(hub, tmp_path):
    first = {"at": "2026-10-01T00:00:00Z", "event": "first"}
    second = {"at": "2026-10-02T00:00:00Z", "event": "second"}
    third = {"at": "2026-10-03T00:00:00Z", "event": "third"}
    doc = {"rows": {"a": _row("a", history=[first, third])}}
    path = _write(tmp_path, doc)
    assert _import(path)[0] == 0

    doc["rows"]["a"]["history"] = [first, second, third]
    _write(tmp_path, doc)
    rc, report = _import(path, "--catch-up")

    assert rc == 1 and report["result"] == "rolled back", report
    raw = [h for h in _export()["a"]["history"] if "by" not in h]
    assert raw == [first, third]


def test_catch_up_verifier_rejects_a_middle_entry_appended_out_of_order(monkeypatch):
    from khipu import ledger_import

    first = {"at": "2026-10-01T00:00:00Z", "event": "first"}
    second = {"at": "2026-10-02T00:00:00Z", "event": "second"}
    third = {"at": "2026-10-03T00:00:00Z", "event": "third"}
    source = {"a": _row("a", history=[first, second, third])}
    row = {"row_id": "a", "refs": {"imported": {"file": "digest"}}, "state": "QUEUED"}
    before_hist = [
        {"has_raw": True, "raw": first},
        {"has_raw": True, "raw": third},
    ]
    before = {
        "ids": {"a"},
        "all": {"a": row},
        "hist": {"a": before_hist},
        "rows": {"a": _row("a", history=[first, third])},
    }
    after = {
        "ids": {"a"},
        "all": {"a": row},
        "hist": {"a": before_hist + [{"has_raw": True, "raw": second}]},
        "rows": {"a": _row("a", history=[first, third, second])},
    }
    monkeypatch.setattr(ledger_import, "_snapshot", lambda *a: after)

    check = ledger_import._verify_catch_up(
        None,
        P,
        source,
        {"new_rows": [], "updated": [], "both": []},
        {"a": [second]},
        before,
    )
    assert not check["ok"], check
