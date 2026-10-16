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
