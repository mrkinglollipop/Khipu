"""The ledger store and `khipu ledger` (Work view phase 1), against a real
PostgreSQL started for this module on loopback in a temporary folder and
removed afterwards. Never the live hub: the DSN is the scratch cluster's.

Every fixture row is synthetic.
"""
from __future__ import annotations

import ast
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "ops" / "migrations"
INITDB, PG_CTL = shutil.which("initdb"), shutil.which("pg_ctl")
P = "acme/widget"
KEY = "sk-ant-" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _real_connect(dsn):
    import psycopg

    return psycopg.Connection.connect(dsn)


@pytest.fixture(scope="module")
def pg_dsn():
    if not INITDB or not PG_CTL:
        pytest.skip("needs PostgreSQL's initdb and pg_ctl on PATH")
    root = Path(tempfile.mkdtemp(prefix="khipu-ledger-pg-"))
    data, log = root / "data", root / "log"
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    started = False
    try:
        subprocess.run([INITDB, "-D", str(data), "-U", "khipu", "--auth=trust", "-E", "UTF8", "--locale=C"],
                       check=True, capture_output=True, env=env, timeout=120)
        port = _free_port()
        subprocess.run([PG_CTL, "-D", str(data), "-l", str(log), "-w", "-t", "60", "-o",
                        f"-c unix_socket_directories='' -c listen_addresses=127.0.0.1 -p {port} -c fsync=off",
                        "start"], check=True, capture_output=True, env=env, timeout=120)
        started = True
        dsn = f"postgresql://khipu@127.0.0.1:{port}/postgres"
        with _real_connect(dsn) as conn:
            for name in ("0000_bootstrap.sql", "0028_ledger.sql", "0028_ledger.sql"):  # re-run is safe
                conn.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
                conn.commit()
        yield dsn
    finally:
        if started:
            subprocess.run([PG_CTL, "-D", str(data), "-m", "immediate", "stop"], capture_output=True, env=env,
                           timeout=60)
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def hub(pg_dsn, monkeypatch, tmp_path):
    import psycopg

    monkeypatch.setattr(psycopg, "connect", psycopg.Connection.connect)
    for name in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID",
                 "KHIPU_SESSION_ID", "KHIPU_HARNESS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("KHIPU_DATABASE_URL", pg_dsn)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    with _real_connect(pg_dsn) as conn:
        conn.execute("TRUNCATE ledger_rows, ledger_history, ledger_ops, ledger_removed, ledger_vocab, "
                     "ledger_imports, ledger_projects")
        conn.commit()
    return pg_dsn


def _away(monkeypatch):
    monkeypatch.setenv("KHIPU_DATABASE_URL", f"postgresql://khipu@127.0.0.1:{_free_port()}/postgres")


def run(*argv):
    from khipu import cli

    args = cli.build_parser().parse_args(["ledger", *argv])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = args.func(args)
    return rc, out.getvalue(), err.getvalue()


def ok(*argv) -> str:
    rc, out, err = run(*argv, "--project", P)
    assert rc == 0, (argv, out, err)
    return out


def _sql(dsn, query, params=()):
    with _real_connect(dsn) as conn:
        rows = conn.execute(query, params).fetchall() if query.lstrip().upper().startswith("SELECT") else None
        if rows is None:
            conn.execute(query, params)
        conn.commit()
    return rows


def _history(dsn, row_id):
    return _sql(dsn, "SELECT from_state, to_state, changes, note FROM ledger_history WHERE project = %s "
                     "AND row_id = %s ORDER BY id", (P, row_id))


def _snapshot(tmp_path) -> Path:
    from khipu import hub_snapshot as hs

    path = hs.snapshot_path()
    con = sqlite3.connect(str(path))
    hs._create_schema(con)
    con.execute("INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('refreshed_at', '2026-10-15T12:00:00Z')")
    con.commit()
    con.close()
    return path


# ---- the migration ------------------------------------------------------------


def test_the_migration_is_additive_and_safe_to_re_run(hub):
    got = _sql(hub, "SELECT version FROM schema_migrations WHERE version = '0028_ledger'")
    assert got == [("0028_ledger",)]
    text = (MIGRATIONS / "0028_ledger.sql").read_text(encoding="utf-8")
    for statement in ("DROP ", "ALTER ", "DELETE ", "TRUNCATE "):
        assert statement not in text.upper()


# ---- the printed shapes -------------------------------------------------------------


def test_the_printed_lines_match_the_trackers_for_the_same_input(hub, monkeypatch):
    from khipu import ledger

    ledger.set_setting("ledger_owner_word", "NEEDS MATT")
    assert ok("add", "g-1", "--title", "Ship the thing", "--kind", "goal").strip() == "added g-1: NOT STARTED"
    assert ok("add", "s-1", "--title", "First slice", "--kind", "slice", "--parent", "g-1", "--owner",
              "lane-a").strip() == "added s-1: NOT STARTED"
    assert ok("add", "n-1", "--title", "Pick a name", "--kind", "needs-matt", "--state", "NEEDS MATT").strip() \
        == "added n-1: NEEDS MATT"
    assert ok("set", "s-1", "--state", "IN FLIGHT", "--evidence", "PR 41").strip() == "set s-1: state, evidence"
    assert ok("set", "s-1", "--note", "still going").strip() == "set s-1: note appended"
    lines = ok("list").splitlines()
    assert lines[0].startswith("g-1 · NOT STARTED · Ship the thing · owner=none · updated=20")
    assert lines[-2:] == ["", "3 row(s)"]
    loose = ok("loose-ends").splitlines()
    assert loose[0] == "== IN FLIGHT (1)"
    assert loose[1] == "s-1 · First slice · owner=lane-a · PR 41"
    assert loose[-1].startswith("2 loose end(s); 1 without an owner; terminal states = ['DONE', 'DROPPED', "
                                "'NEEDS MATT', 'VERIFIED']")
    needs = ok("needs-matt").splitlines()
    assert needs == ["n-1 · Pick a name · evidence=", "", "1 row(s) need Matt"]
    assert ok("needs-owner").splitlines() == needs
    assert ok("check").startswith("CHECK OK: 3 rows")
    board = ok("render")
    heads = [ln for ln in board.splitlines() if ln.startswith("#")]
    assert [h.split(" (")[0] for h in heads[:-1]] == [
        "# Orchestrator Tracker", "## Counts by state", "## Counts by kind", "## Rows", "### goal", "### slice",
        "### needs-matt"]
    assert heads[-1].startswith("## Changed since 20")
    assert "- NEEDS MATT: 1" in board and "- needs-matt: 1" in board
    assert "**parent: g-1: Ship the thing**" in board
    out = Path(os.environ["KHIPU_DATA_DIR"]) / "board.md"
    assert ok("render", "--out", str(out)).strip() == f"rendered 3 rows -> {out}"
    assert out.read_text(encoding="utf-8").startswith("# Orchestrator Tracker")
    got = json.loads(ok("get", "s-1"))
    assert {"id", "title", "kind", "parent", "state", "evidence", "owner", "updated", "history"} <= set(got)
    assert [(h["from"], h["to"]) for h in got["history"]] == [(None, "NOT STARTED"), ("NOT STARTED", "IN FLIGHT"),
                                                            ("IN FLIGHT", "IN FLIGHT")]
    assert got["history"][0]["ts"].endswith("Z")


def test_errors_use_the_trackers_exit_code_and_name_the_allowed_words(hub):
    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    rc, _out, err = run("add", "s-1", "--title", "Again", "--kind", "slice", "--project", P)
    assert rc == 1 and err.strip() == "error: row s-1 already exists; use set to update it"
    rc, _out, err = run("set", "s-1", "--state", "SHIPPED", "--project", P)
    assert rc == 1 and "state must be one of: VERIFIED, MERGED-UNVERIFIED, BUILT-UNMERGED, IN FLIGHT" in err
    rc, _out, err = run("set", "nope", "--state", "DONE", "--project", P)
    assert rc == 1 and err.strip() == "error: no such row: nope"
    rc, out, _err = run("set", "nope", "--state", "DONE", "--project", P, "--json")
    assert rc == 1 and json.loads(out)["reason"] == "not_found"


# ---- many writers ------------------------------------------------------------------


def test_two_writers_at_once_both_land_in_history(hub):
    from khipu import ledger

    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    barrier, errors = threading.Barrier(2), []

    def writer(evidence):
        try:
            barrier.wait()
            ledger.write(ledger.new_op("set", P, "s-1", fields={"evidence": evidence}))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(e,)) for e in ("from A", "from B")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    entries = [h for h in _history(hub, "s-1") if "evidence" in h[2]]
    assert sorted(h[2]["evidence"]["new"] for h in entries) == ["from A", "from B"]
    # History order is the order writes landed: the later one is the value.
    final = _sql(hub, "SELECT evidence, version FROM ledger_rows WHERE row_id = 's-1'")[0]
    assert final[0] == entries[-1][2]["evidence"]["new"] and final[1] == 3
    assert entries[-1][2]["evidence"]["old"] == entries[0][2]["evidence"]["new"]


def test_two_new_rows_at_once_keep_distinct_order(hub):
    from khipu import ledger

    barrier = threading.Barrier(4)

    def adder(i):
        barrier.wait()
        ledger.write(ledger.new_op("add", P, f"s-{i}", fields={"title": f"Slice {i}", "kind": "slice"}))

    threads = [threading.Thread(target=adder, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [r[0] for r in _sql(hub, "SELECT seq FROM ledger_rows ORDER BY seq")]
    assert seqs == [1, 2, 3, 4]


def test_if_state_refuses_a_moved_row(hub):
    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    ok("set", "s-1", "--state", "IN FLIGHT")
    rc, _out, err = run("set", "s-1", "--state", "DONE", "--if-state", "NOT STARTED", "--project", P)
    assert rc == 1
    assert "row s-1 is IN FLIGHT now, set by a shell command" in err and "nothing was changed" in err
    assert _sql(hub, "SELECT state FROM ledger_rows")[0][0] == "IN FLIGHT"
    assert ok("set", "s-1", "--state", "DONE", "--if-state", "IN FLIGHT").strip() == "set s-1: state"


def test_a_retried_op_id_writes_once(hub):
    first = ok("add", "s-1", "--title", "A slice", "--kind", "slice", "--op-id", "op-add-1")
    again = ok("add", "s-1", "--title", "A slice", "--kind", "slice", "--op-id", "op-add-1")
    assert first == again == "added s-1: NOT STARTED\n"
    ok("set", "s-1", "--state", "QUEUED", "--op-id", "op-set-1")
    ok("set", "s-1", "--state", "QUEUED", "--op-id", "op-set-1")
    assert len(_history(hub, "s-1")) == 2


def test_history_keeps_old_and_new_values_and_a_note_alone(hub):
    ok("add", "s-1", "--title", "A slice", "--kind", "slice", "--evidence", "first")
    ok("set", "s-1", "--evidence", "second", "--owner", "lane-b", "--title", "A better slice",
       "--link", "https://example.com/pull/7?x=1#frag")
    ok("set", "s-1", "--note", "only a note")
    hist = _history(hub, "s-1")
    assert hist[1][2] == {"evidence": {"old": "first", "new": "second"}, "owner": {"old": "none", "new": "lane-b"},
                          "title": {"old": "A slice", "new": "A better slice"},
                          "link": {"old": "", "new": "https://example.com/pull/7"}}
    assert hist[2][2] == {} and hist[2][3] == "only a note"


# ---- per-project words ------------------------------------------------------------


def test_each_project_keeps_its_own_words(hub):
    from khipu import ledger

    vocab = {"state": [["review", "open"], ["complete", "done"], ["ON HOLD", "open"], ["NEEDS MATT", "needs"]],
             "kind": ["implementation", "verification"]}
    with _real_connect(hub) as conn:
        with conn.cursor() as cur:
            ledger.ensure_project(cur, "other/proj", vocab)
        conn.commit()
    rc, _o, err = run("add", "i-1", "--title", "Build it", "--kind", "slice", "--state", "review",
                      "--project", "other/proj")
    assert rc == 1 and "kind must be one of: implementation, verification" in err
    rc, out, _e = run("add", "i-1", "--title", "Build it", "--kind", "implementation", "--state", "review",
                      "--project", "other/proj")
    assert rc == 0 and out.strip() == "added i-1: review"
    rc, _o, err = run("set", "i-1", "--state", "VERIFIED", "--project", "other/proj")
    assert rc == 1 and "state must be one of: review, complete, ON HOLD, NEEDS MATT" in err
    assert run("set", "i-1", "--state", "complete", "--project", "other/proj")[0] == 0
    # NEEDS OWNER names this project's own needs-you word; the stored word is the project's.
    assert run("set", "i-1", "--state", "NEEDS OWNER", "--project", "other/proj")[0] == 0
    assert _sql(hub, "SELECT state FROM ledger_rows WHERE project = 'other/proj'")[0][0] == "NEEDS MATT"
    # The default project in the same database still uses the standard words.
    assert ok("add", "s-1", "--title", "A slice", "--kind", "slice", "--state", "NEEDS MATT").strip() \
        == "added s-1: NEEDS OWNER"


def test_history_is_kept_verbatim_in_both_shapes(hub):
    ok("add", "s-1", "--title", "Line one\nline two of a multi-line title", "--kind", "slice")
    raw_event = {"at": "2026-09-01T10:00:00Z", "event": "Owner authorised the release in chat"}
    raw_transition = {"ts": "2026-09-02T11:00:00Z", "from": None, "to": "QUEUED", "note": "a\nmulti-line\nnote"}
    with _real_connect(hub) as conn:
        for shape, raw in (("event", raw_event), ("transition", raw_transition)):
            conn.execute("INSERT INTO ledger_history (project, row_id, at, at_text, author, shape, event, raw, "
                         "source) VALUES (%s, 's-1', %s, %s, '{\"kind\": \"import\"}', %s, %s, %s, 'import')",
                         (P, raw.get("at") or raw["ts"], raw.get("at") or raw["ts"], shape, raw.get("event"),
                          json.dumps(raw)))
        conn.commit()
    got = json.loads(ok("get", "s-1"))
    assert got["title"] == "Line one\nline two of a multi-line title"
    assert got["history"][1:] == [raw_event, raw_transition]


# ---- limits are settings ----------------------------------------------------------


def test_every_limit_is_a_setting_and_0_lifts_it(hub):
    from khipu import ledger

    long_title = "t" * 2001
    rc, _o, err = run("add", "s-1", "--title", long_title, "--kind", "slice", "--project", P)
    assert rc == 1 and "title is 2001 characters; the limit is 2000 (ledger_title_chars)" in err
    ledger.set_setting("ledger_title_chars", "0")
    assert ok("add", "s-1", "--title", long_title, "--kind", "slice").strip() == "added s-1: NOT STARTED"
    assert set(ledger.SETTINGS) == {"ledger_id_chars", "ledger_title_chars", "ledger_evidence_chars",
                                    "ledger_stale_after", "ledger_purge_after", "ledger_owner_word"}


def test_a_key_shaped_string_is_refused(hub):
    for argv in (("add", "s-1", "--title", f"use {KEY}", "--kind", "slice"),
                 ("add", "s-1", "--title", "fine", "--kind", "slice", "--evidence", KEY),
                 ("add", "s-1", "--title", "fine", "--kind", "slice", "--note", f"token {KEY}")):
        rc, _o, err = run(*argv, "--project", P)
        assert rc == 1 and "This looks like a key or password; nothing was saved" in err
    assert _sql(hub, "SELECT count(*) FROM ledger_rows")[0][0] == 0
    assert _sql(hub, "SELECT count(*) FROM ledger_history")[0][0] == 0


# ---- order -----------------------------------------------------------------------


def test_listings_never_order_by_state(hub):
    for i, state in enumerate(("VERIFIED", "NOT STARTED", "IN FLIGHT", "DONE")):
        ok("add", f"z-{i}", "--title", f"Slice {i}", "--kind", "slice", "--state", state)
    ok("set", "z-0", "--state", "QUEUED")
    ok("set", "z-3", "--state", "BUILT-UNMERGED")
    listed = [ln.split(" · ")[0] for ln in ok("list").splitlines() if " · " in ln]
    assert listed == ["z-0", "z-1", "z-2", "z-3"]
    rendered = [ln[2:].split(" · ")[0] for ln in ok("render").split("## Rows")[1].split("## Changed")[0].splitlines()
                if ln.startswith("- ")]
    assert rendered == listed


# ---- remove, restore, purge -----------------------------------------------------------


def test_a_removed_row_is_gone_everywhere_at_once(hub, tmp_path):
    from khipu import hub_snapshot as hs

    _snapshot(tmp_path)
    ok("add", "n-1", "--title", "Needs a call", "--kind", "needs-owner", "--state", "NEEDS OWNER")
    ok("add", "s-1", "--title", "Stays", "--kind", "slice")
    assert {r["row_id"] for r in hs.ledger_read_snapshot(P)["rows"]} == {"n-1", "s-1"}
    assert ok("remove", "n-1", "--note", "duplicate").strip() == "removed n-1"
    assert "n-1" not in ok("list") and "n-1" not in ok("render") and "n-1" not in ok("loose-ends")
    assert ok("needs-owner").splitlines()[-1] == "0 row(s) need the owner"
    assert "Total rows: 1" in ok("render") and "- NEEDS OWNER: 0" in ok("render")
    assert ok("check").startswith("CHECK OK: 1 rows")
    assert {r["row_id"] for r in hs.ledger_read_snapshot(P)["rows"]} == {"s-1"}
    rc, _o, err = run("get", "n-1", "--project", P)
    assert rc == 1 and "row n-1 was removed on 20" in err and "by a shell command" in err
    removed = json.loads(ok("removed", "--json"))["removed"]
    assert [(r["id"], r["days_left"]) for r in removed] == [("n-1", 30)]


def test_restore_brings_a_row_back_with_its_history(hub):
    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    ok("set", "s-1", "--state", "IN FLIGHT")
    ok("remove", "s-1")
    rc, _o, err = run("restore", "s-1", "--project", P)
    assert rc == 1 and "restored in the Khipu app" in err
    assert ok("restore", "s-1", "--from-app").strip() == "restored s-1"
    got = json.loads(ok("get", "s-1"))
    assert got["state"] == "IN FLIGHT"
    assert [h["note"] for h in got["history"]] == ["seeded", "", "Removed", "Restored by you, in the app"]
    assert got["history"][-1]["by"] == "you, in the app"


def test_a_row_removed_30_days_ago_is_purged_to_a_bare_marker(hub):
    from khipu import ledger

    ok("add", "s-1", "--title", "A slice", "--kind", "slice", "--op-id", "op-first-add")
    ok("add", "s-2", "--title", "Removed yesterday", "--kind", "slice")
    ok("remove", "s-1")
    ok("remove", "s-2")
    _sql(hub, "UPDATE ledger_rows SET removed_at = now() - interval '31 days' WHERE row_id = 's-1'")
    with _real_connect(hub) as conn:
        assert ledger.purge_due(conn) == {"ok": True, "purged": 1}
    assert _sql(hub, "SELECT row_id FROM ledger_rows") == [("s-2",)]
    assert _sql(hub, "SELECT count(*) FROM ledger_history WHERE row_id = 's-1'") == [(0,)]
    marker = _sql(hub, "SELECT project, row_id, removed_by->>'kind' FROM ledger_removed")
    assert marker == [(P, "s-1", "conversation")]
    rc, _o, err = run("add", "s-1", "--title", "Back again", "--kind", "slice", "--project", P)
    assert rc == 1 and "row s-1 was removed for good on 20" in err
    rc, _o, err = run("add", "s-1", "--title", "A slice", "--kind", "slice", "--op-id", "op-first-add",
                      "--project", P)
    assert rc == 1 and "removed for good" in err
    rc, _o, err = run("get", "s-1", "--project", P)
    assert rc == 1 and "removed for good" in err


def test_remove_for_good_at_once_is_the_apps_and_needs_a_removed_row(hub):
    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    assert run("purge", "s-1", "--project", P)[0] == 1
    rc, _o, err = run("purge", "s-1", "--project", P, "--from-app")
    assert rc == 1 and "remove it first" in err
    ok("remove", "s-1")
    assert ok("purge", "s-1", "--from-app").strip() == "removed s-1 for good"
    assert _sql(hub, "SELECT count(*) FROM ledger_rows") == [(0,)]


def test_remove_with_children_needs_a_choice_and_each_does_what_it_says(hub):
    ok("add", "g-1", "--title", "Goal", "--kind", "goal")
    ok("add", "s-1", "--title", "Slice", "--kind", "slice", "--parent", "g-1")
    ok("add", "r-1", "--title", "Review", "--kind", "review", "--parent", "s-1")
    ok("add", "g-2", "--title", "Goal two", "--kind", "goal")
    ok("add", "s-2", "--title", "Slice two", "--kind", "slice", "--parent", "g-2")
    rc, _o, err = run("remove", "g-1", "--project", P)
    assert rc == 1 and "row g-1 has 1 child row(s); say --children remove" in err
    assert ok("remove", "g-1", "--children", "remove").strip() == "removed g-1 and 2 child row(s)"
    assert _sql(hub, "SELECT row_id FROM ledger_rows WHERE removed_at IS NULL ORDER BY seq") == [("g-2",), ("s-2",)]
    assert ok("remove", "g-2", "--children", "move-up").strip() == "removed g-2; 1 child row(s) moved up to the top"
    assert _sql(hub, "SELECT parent FROM ledger_rows WHERE row_id = 's-2'") == [("",)]
    assert _history(hub, "s-2")[-1][2] == {"parent": {"old": "g-2", "new": ""}}


def test_khipu_forget_on_a_ledger_id_removes_it(hub):
    from khipu import mcp_server

    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    out = mcp_server._tool_forget({"id": f"ledger:{P}#s-1", "note": "written by mistake"})
    assert out["ok"] and out["line"] == "removed s-1"
    assert _sql(hub, "SELECT removed_note, removed_at IS NOT NULL FROM ledger_rows") == [("written by mistake", True)]
    with pytest.raises(ValueError, match="no such row"):
        mcp_server._tool_forget({"id": f"ledger:{P}#nope"})


def test_the_only_delete_in_the_module_is_the_purge():
    from khipu import ledger

    tree = ast.parse(Path(ledger.__file__).read_text(encoding="utf-8"))
    purge = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_purge_row")
    inside = {id(n) for n in ast.walk(purge)}
    deletes = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
               and "DELETE " in n.value.upper() and n.value.lstrip().upper().startswith("DELETE")]
    assert deletes and all(id(n) in inside for n in deletes)


# ---- the hub away ----------------------------------------------------------------


def test_the_hub_away_queues_doctor_goes_red_and_a_drain_lands_it(hub, monkeypatch):
    from khipu import cli, ledger

    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    _away(monkeypatch)
    out = ok("set", "s-1", "--state", "IN FLIGHT", "--if-state", "NOT STARTED")
    assert out.splitlines() == ["set s-1: queued",
                                "Saved on this Mac; it reaches the ledger when the database answers"]
    assert ledger.outbox_status()["pending"] == 1
    assert cli._doctor_local_checks(None)["ledger_outbox_ok"] is False
    monkeypatch.setenv("KHIPU_DATABASE_URL", hub)
    # The next ledger call that reaches the hub lands it first.
    assert "s-1 · IN FLIGHT" in ok("list")
    assert ledger.outbox_status() == {"pending": 0, "refused": 0, "oldest_age_s": None}
    assert cli._doctor_local_checks(None)["ledger_outbox_ok"] is True


def test_a_late_if_state_becomes_a_history_entry(hub, monkeypatch):
    from khipu import ledger

    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    _away(monkeypatch)
    ok("set", "s-1", "--state", "DONE", "--if-state", "NOT STARTED", "--note", "finished offline")
    monkeypatch.setenv("KHIPU_DATABASE_URL", hub)
    with _real_connect(hub) as conn:
        ledger.apply_op(conn, ledger.new_op("set", P, "s-1", fields={"state": "IN FLIGHT"}))
        assert ledger.drain(conn)["landed"] == 1
    assert _sql(hub, "SELECT state FROM ledger_rows") == [("IN FLIGHT",)]
    last = _history(hub, "s-1")[-1]
    assert last[0] == last[1] == "IN FLIGHT"
    assert last[3] == "arrived late; the row had moved to IN FLIGHT. finished offline"
    assert last[2] == {"requested": {"state": {"old": "IN FLIGHT", "new": "DONE"}}}


def test_reads_while_away_come_from_the_offline_copy_plus_the_queue(hub, monkeypatch, tmp_path):
    _snapshot(tmp_path)
    ok("add", "s-1", "--title", "In the copy", "--kind", "slice")
    ok("add", "s-2", "--title", "Removed while away", "--kind", "slice")
    _away(monkeypatch)
    assert ok("add", "s-3", "--title", "Queued", "--kind", "slice").splitlines()[0] == "added s-3: NOT STARTED"
    ok("set", "s-1", "--state", "IN FLIGHT")
    ok("remove", "s-2")
    out = ok("list")
    assert [ln.split(" · ")[:2] for ln in out.splitlines() if " · " in ln] == [["s-1", "IN FLIGHT"],
                                                                             ["s-3", "NOT STARTED"]]
    assert "read from the offline copy as of 2026-10-15T12:00:00Z, with 3 waiting change(s) applied" in out
    rc, _o, err = run("add", "s-1", "--title", "Dup", "--kind", "slice", "--project", P)
    assert rc == 1 and "already exists" in err


def test_the_refresh_copies_live_rows_only(hub, tmp_path):
    from khipu import hub_snapshot as hs

    ok("add", "s-1", "--title", "Kept", "--kind", "slice")
    ok("add", "s-2", "--title", "Removed", "--kind", "slice")
    ok("remove", "s-2")
    con = sqlite3.connect(":memory:")
    hs._create_schema(con)
    with _real_connect(hub) as conn:
        with conn.cursor() as cur:
            assert hs._insert_ledger(cur, con) == 1
    assert con.execute("SELECT row_id FROM ledger_rows").fetchall() == [("s-1",)]
    assert {r[0] for r in con.execute("SELECT row_id FROM ledger_history")} == {"s-1"}
    assert con.execute("SELECT count(*) FROM ledger_vocab").fetchone()[0] == 18


def test_doctor_block_reports_counts_and_the_nightly_purges(hub):
    from khipu import ledger

    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    ok("add", "s-2", "--title", "Removed", "--kind", "slice")
    ok("remove", "s-2")
    block = ledger.doctor_block()
    assert block["projects"] == {P: {"rows": 1, "removed": 1, "history": 3}}
    assert block["removed_waiting_to_purge"] == 1 and block["purged_markers"] == 0
    assert block["in_dump"] == "whole database"
    _sql(hub, "UPDATE ledger_rows SET removed_at = now() - interval '40 days' WHERE row_id = 's-2'")
    assert ledger.nightly() == {"ok": True, "landed": 0, "refused": 0, "purged": 1}
    assert ledger.doctor_block()["purged_markers"] == 1


# ---- review round: identifiers, the drain lock, the offline copy, order, the board -----------


SECRET_ID = "sk-" + "A" * 48


def _offline(monkeypatch, tmp_path):
    from khipu import ledger

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir(exist_ok=True)

    def away():
        raise ledger.HubAway("synthetic: no hub")

    monkeypatch.setattr(ledger, "_connect", away)
    return ledger


@pytest.mark.parametrize("field", ["project", "row_id", "op_id", "parent", "if_state"])
def test_a_key_shaped_identifier_is_refused_before_it_is_stored(field, monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    params = {"project": "acme/widget", "row_id": "r1", "op_id": "op-1"}
    verb, fields, if_state = "add", {"title": "fine", "kind": "slice"}, None
    if field in params:
        params[field] = SECRET_ID
    elif field == "parent":
        fields["parent"] = SECRET_ID
    else:
        verb, fields, if_state = "set", {"state": "DONE"}, SECRET_ID
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op(verb, **params, fields=fields, if_state=if_state))
    assert err.value.reason == "secret" and SECRET_ID not in str(err.value)
    assert ledger._outbox_jobs() == []


@pytest.mark.parametrize("argv", [
    ("add", SECRET_ID + "?", "--title", "fine", "--kind", "slice"),
    ("add", "r1", "--title", "fine", "--kind", "slice", "--parent", SECRET_ID + "?"),
    ("get", SECRET_ID),
    ("set", SECRET_ID, "--state", "DONE"),
    ("remove", SECRET_ID, "--json"),
])
def test_a_key_shaped_argument_is_never_echoed(argv, monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    rc, out, err = run(*argv, "--project", P)
    assert rc == 1 and SECRET_ID not in out + err
    assert "This looks like a key or password; nothing was saved" in out + err
    assert ledger._outbox_jobs() == []


def test_a_write_waits_behind_a_busy_drain_and_never_jumps_the_queue(monkeypatch, tmp_path):
    import fcntl

    ledger = _offline(monkeypatch, tmp_path)
    first = ledger.new_op("set", P, "s-1", fields={"evidence": "first"})
    ledger.enqueue(first)
    applied = []
    monkeypatch.setattr(ledger, "apply_op", lambda _conn, op, **_kw: applied.append(op["op_id"]) or {"ok": True})
    monkeypatch.setattr(ledger, "DRAIN_WAIT_S", 0.2)
    fd = os.open(str(ledger.outbox_dir() / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process is draining
        second = ledger.new_op("set", P, "s-1", fields={"evidence": "second"})
        got = ledger.write(second, conn=object())
    finally:
        os.close(fd)
    assert applied == [] and got["queued"] is True
    assert [op["op_id"] for op in ledger.queued_ops()] == [first["op_id"], second["op_id"]]


def test_a_write_behind_a_busy_drain_lands_after_the_older_one(hub, monkeypatch):
    import fcntl

    from khipu import ledger

    ok("add", "s-1", "--title", "A slice", "--kind", "slice")
    _away(monkeypatch)
    ok("set", "s-1", "--evidence", "first")
    monkeypatch.setenv("KHIPU_DATABASE_URL", hub)
    monkeypatch.setattr(ledger, "DRAIN_WAIT_S", 0.2)
    fd = os.open(str(ledger.outbox_dir() / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert ok("set", "s-1", "--evidence", "second").splitlines()[0] == "set s-1: queued"
    finally:
        os.close(fd)
    assert "s-1 · NOT STARTED" in ok("list")
    assert _sql(hub, "SELECT evidence FROM ledger_rows WHERE row_id = 's-1'") == [("second",)]
    assert [h[2]["evidence"]["new"] for h in _history(hub, "s-1") if "evidence" in h[2]] == ["first", "second"]


def _replica_row(row_id, version, title="t"):
    return {"row_id": row_id, "title": title, "kind": "slice", "parent": "", "state": "NOT STARTED",
            "owner": "none", "evidence": "", "link": "", "refs": {}, "seq": 1, "created_at": "2026-10-15T00:00:00Z",
            "updated_at": "2026-10-15T00:00:00Z", "version": version}


def _replica_titles(project="p"):
    from khipu import hub_snapshot as hs

    return {r["row_id"]: r["title"] for r in hs.ledger_read_snapshot(project)["rows"]}


def test_a_late_sync_never_brings_back_a_removed_row_or_an_older_value(monkeypatch, tmp_path):
    from khipu import hub_snapshot as hs

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    _snapshot(tmp_path)
    proj, vocab = ("p", False, "2026-10-15T00:00:00Z"), {"state": [], "kind": []}
    assert hs.ledger_write_snapshot("p", proj, vocab, [_replica_row("r", 3, "newer")], [], ["r"])["ok"]
    assert hs.ledger_write_snapshot("p", proj, vocab, [_replica_row("r", 2, "older")], [], ["r"])["ok"]
    assert _replica_titles() == {"r": "newer"}
    # Removed at version 4; a writer that read version 3 before the removal lands after it.
    assert hs.ledger_write_snapshot("p", proj, vocab, [], [], ["r"], gone={"r": 4})["ok"]
    assert hs.ledger_write_snapshot("p", proj, vocab, [_replica_row("r", 3, "stale")], [], ["r"])["ok"]
    assert _replica_titles() == {}
    # Restored (version 5) comes back; purged (gone with no version) never does.
    assert hs.ledger_write_snapshot("p", proj, vocab, [_replica_row("r", 5, "restored")], [], ["r"])["ok"]
    assert _replica_titles() == {"r": "restored"}
    assert hs.ledger_write_snapshot("p", proj, vocab, [], [], ["r"])["ok"]
    assert hs.ledger_write_snapshot("p", proj, vocab, [_replica_row("r", 9, "after purge")], [], ["r"])["ok"]
    assert _replica_titles() == {}


def test_a_sync_skipped_by_a_busy_refresh_is_retried_on_the_next_call(hub, tmp_path):
    from khipu import hub_snapshot as hs

    _snapshot(tmp_path)
    ok("add", "s-1", "--title", "Kept", "--kind", "slice")
    ok("add", "s-2", "--title", "Removed while the copy is busy", "--kind", "slice")
    lock = hs._acquire_refresh_lock()
    assert lock is not None
    try:
        ok("remove", "s-2")
        ok("set", "s-1", "--title", "Renamed while busy")
    finally:
        hs._release_refresh_lock(lock)
    assert _replica_titles(P) == {"s-1": "Kept", "s-2": "Removed while the copy is busy"}
    ok("list")
    assert _replica_titles(P) == {"s-1": "Renamed while busy"}


def test_the_refresh_keeps_removed_and_purged_ids_out_of_a_late_sync(hub, tmp_path):
    from khipu import hub_snapshot as hs

    ok("add", "s-1", "--title", "Removed", "--kind", "slice")
    ok("add", "s-2", "--title", "Purged", "--kind", "slice")
    ok("remove", "s-1")
    ok("remove", "s-2")
    ok("purge", "s-2", "--from-app")
    path = _snapshot(tmp_path)
    con = sqlite3.connect(str(path))
    with _real_connect(hub) as conn:
        with conn.cursor() as cur:
            hs._insert_ledger(cur, con)
    con.commit()
    con.close()
    vocab = {"state": [], "kind": []}
    for rid in ("s-1", "s-2"):
        assert hs.ledger_write_snapshot(P, None, vocab, [_replica_row(rid, 1, "stale")], [], [rid])["ok"]
    assert _replica_titles(P) == {}


def test_a_row_queued_offline_can_be_changed_and_removed_offline(monkeypatch, tmp_path):
    from khipu import hub_snapshot as hs

    ledger = _offline(monkeypatch, tmp_path)
    _snapshot(tmp_path)
    assert hs.ledger_write_snapshot(P, (P, False, "2026-10-15T00:00:00Z"), ledger.default_vocab("NEEDS OWNER"),
                                    [_replica_row("r1", 1, "copied")], [], ["r1"])["ok"]
    assert ledger.write(ledger.new_op("add", P, "r2", fields={"title": "queued", "kind": "slice"}))["queued"]
    assert ledger.write(ledger.new_op("set", P, "r2", fields={"state": "DONE"}))["queued"]
    assert [(r["row_id"], r["state"]) for r in ledger.load(P)["rows"]] == [("r1", "NOT STARTED"), ("r2", "DONE")]
    assert ledger.write(ledger.new_op("remove", P, "r2"))["queued"]
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("set", P, "r2", fields={"state": "DONE"}))
    assert err.value.reason == "not_found"
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("add", P, "r1", fields={"title": "again", "kind": "slice"}))
    assert err.value.reason == "exists"


def test_rows_keep_the_order_they_were_added_in_and_children_stay_with_their_parent():
    from khipu import ledger

    vocab = ledger.default_vocab("NEEDS OWNER")
    rows = [{"row_id": "z-first", "kind": "slice", "parent": "", "seq": 1},
            {"row_id": "a-second", "kind": "slice", "parent": "", "seq": 2},
            {"row_id": "m-child", "kind": "slice", "parent": "z-first", "seq": 3},
            {"row_id": "b-child", "kind": "slice", "parent": "a-second", "seq": 4}]
    assert [r["row_id"] for r in ledger._order(rows, vocab)] == ["z-first", "m-child", "a-second", "b-child"]


TRACKER_FIXTURE = Path(__file__).parent / "fixtures" / "ledger_tracker"
# The tracker script's states and kinds for the project the fixture stands for.
TRACKER_STATES = [("VERIFIED", "done"), ("MERGED-UNVERIFIED", "open"), ("BUILT-UNMERGED", "open"),
                  ("IN FLIGHT", "open"), ("QUEUED", "open"), ("NOT STARTED", "open"), ("NEEDS MATT", "needs"),
                  ("DROPPED", "dropped"), ("ON HOLD", "done"), ("DONE", "done")]
TRACKER_KINDS = ["audit-item", "drive", "wave", "train", "loose-end", "needs-matt"]


def test_render_matches_the_trackers_board_for_the_same_rows(monkeypatch):
    """board.md is what the tracker script itself printed for tracker.json
    (synthetic rows). The ledger's board differs only in its own punctuation
    (no dashes), the missing-parent words and the generated times."""
    import re
    from datetime import datetime, timezone

    from khipu import ledger

    monkeypatch.setattr(ledger, "_now", lambda: datetime(2026, 10, 15, 15, 55, 26, tzinfo=timezone.utc))
    source = json.loads((TRACKER_FIXTURE / "tracker.json").read_text(encoding="utf-8"))["rows"]
    kind_at = {k: i for i, k in enumerate(TRACKER_KINDS)}
    # Phase 2's import gives rows seq in the tracker's own render order: kind, then parent, then id.
    ordered = sorted(source.values(), key=lambda r: (kind_at[r["kind"]], r["parent"] or r["id"], r["id"]))
    rows = [{"row_id": r["id"], "title": r["title"], "kind": r["kind"], "parent": r["parent"], "state": r["state"],
             "evidence": r["evidence"], "owner": r["owner"], "updated_at": ledger._parse_time(r["updated"]),
             "seq": seq} for seq, r in enumerate(ordered, 1)]
    rows.reverse()  # the order rows arrive in does not matter
    view = {"vocab": {"state": [list(s) for s in TRACKER_STATES], "kind": TRACKER_KINDS}, "rows": rows}
    expected = (TRACKER_FIXTURE / "board.md").read_text(encoding="utf-8")
    expected = expected.replace(" — (unknown parent)**", ": (parent not found)**").replace(" — ", ": ")
    expected = expected.replace("## Rows (grouped by kind, then parent; id order within group: never reordered "
                                "by state)", "## Rows (grouped by kind, then parent; in the order added within a "
                                "group; never reordered by state)")
    stamp = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\)?$", re.M)
    assert stamp.sub("TIME", ledger.render_board(view)) == stamp.sub("TIME", expected)


def test_a_write_waiting_for_the_lock_keeps_its_place_in_line(monkeypatch, tmp_path):
    import fcntl
    import time

    ledger = _offline(monkeypatch, tmp_path)

    class Conn:
        def close(self):
            pass

    landed, results = [], {}

    def apply(_conn, op, **_kw):  # idempotent by op id, as the hub is
        if op["op_id"] in results:
            return {**results[op["op_id"]], "retried": True}
        landed.append(op["op_id"])
        results[op["op_id"]] = {"ok": True, "line": op["op_id"]}
        return results[op["op_id"]]

    monkeypatch.setattr(ledger, "_connect", Conn)
    monkeypatch.setattr(ledger, "apply_op", apply)
    ledger.enqueue(ledger.new_op("set", P, "r1", fields={"evidence": "0"}, op_id="op0"))
    fd = os.open(str(ledger.outbox_dir() / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another process is draining op0
    older = threading.Thread(target=lambda: ledger.write(ledger.new_op("set", P, "r1", fields={"evidence": "1"},
                                                                       op_id="op1")))
    older.start()
    deadline = time.monotonic() + 5
    while not any("op1" in p.name for p in ledger._outbox_jobs()):
        if time.monotonic() > deadline:
            os.close(fd)
            older.join(10)
            pytest.fail("a write waiting for the lock did not join the line")
        time.sleep(0.01)
    for path in ledger._outbox_jobs():
        if "op0" in path.name:
            path.unlink()  # that drain landed op0 and lets go
    os.close(fd)
    newer = ledger.write(ledger.new_op("set", P, "r1", fields={"evidence": "2"}, op_id="op2"))
    older.join(10)
    assert landed == ["op1", "op2"] and newer == {"ok": True, "line": "op2"}
    assert ledger._outbox_jobs() == []


@pytest.mark.parametrize("argv", [
    ("remove", "x", "--children", SECRET_ID),
    (SECRET_ID,),
    ("add", "x", "--title", "t", "--kind", "slice", "--bogus", SECRET_ID),
])
def test_a_usage_error_masks_a_key_shaped_argument(argv):
    from khipu import cli

    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(SystemExit):
        cli.build_parser().parse_args(["ledger", *argv])
    assert SECRET_ID not in err.getvalue() and "error:" in err.getvalue()


def test_offline_checks_know_removed_purged_and_queued_removals_and_children(hub, tmp_path, monkeypatch):
    from khipu import hub_snapshot as hs
    from khipu import ledger

    path = _snapshot(tmp_path)
    ok("add", "keep", "--title", "Kept", "--kind", "slice")
    ok("add", "kid", "--title", "Child", "--kind", "slice", "--parent", "keep")
    for rid in ("gone-1", "gone-2", "x"):
        ok("add", rid, "--title", "Short-lived", "--kind", "slice")
    ok("remove", "gone-1")
    ok("remove", "gone-2")
    with _real_connect(hub) as conn:
        ledger.purge(conn, P, "gone-2")
    con = sqlite3.connect(str(path))  # a fresh refresh: markers come from the dump too
    con.execute("DELETE FROM ledger_gone")
    with _real_connect(hub) as conn:
        with conn.cursor() as cur:
            con.execute("DELETE FROM ledger_rows")
            con.execute("DELETE FROM ledger_history")
            con.execute("DELETE FROM ledger_vocab")
            con.execute("DELETE FROM ledger_projects")
            hs._insert_ledger(cur, con)
    con.commit()
    con.close()
    _away(monkeypatch)
    assert run("add", "gone-1", "--title", "Again", "--kind", "slice", "--project", P, "--json")[1].count(
        '"reason": "removed"') == 1
    assert '"reason": "purged"' in run("add", "gone-2", "--title", "Again", "--kind", "slice", "--project", P,
                                       "--json")[1]
    assert ok("remove", "x").splitlines()[0] == "removed x: queued"
    assert '"reason": "removed"' in run("add", "x", "--title", "Again", "--kind", "slice", "--project", P,
                                        "--json")[1]
    assert '"reason": "has_children"' in run("remove", "keep", "--project", P, "--json")[1]
    assert ok("remove", "keep", "--children", "move-up").splitlines()[0] == "removed keep: queued"


def test_offline_checks_do_not_refuse_a_row_the_copy_has_not_caught_up_with(hub, tmp_path, monkeypatch):
    from khipu import hub_snapshot as hs

    _snapshot(tmp_path)
    ok("add", "a", "--title", "In the copy", "--kind", "slice")
    lock = hs._acquire_refresh_lock()
    try:
        ok("add", "b", "--title", "Landed while the copy was busy", "--kind", "slice")
    finally:
        hs._release_refresh_lock(lock)
    _away(monkeypatch)
    assert ok("set", "b", "--state", "IN FLIGHT").splitlines()[0] == "set b: queued"


class _ReplicaHub:
    """A hub that holds one row at one version, for the offline-copy catch-up."""

    def __init__(self, version, removed=False):
        self.version, self.removed = version, removed

    def cursor(self):
        hub = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def execute(self, query, _params=()):
                self.query = query

            def fetchone(self):
                return ("p", False, "2026-10-15T00:00:00Z")

            def fetchall(self):
                if "FROM ledger_rows" not in self.query:
                    return []
                from khipu import ledger

                row = {**_replica_row("r", hub.version, f"v{hub.version}"), "removed_at": None, "removed_by": None,
                       "removed_note": ""}
                if hub.removed:
                    row["removed_at"] = "2026-10-15T00:00:00Z"
                return [tuple(row[c] for c in ledger._ROW_COLS)]

        return Cur()

    def rollback(self):
        pass


def test_an_older_catch_up_never_clears_a_newer_changes_retry(monkeypatch, tmp_path):
    from khipu import hub_snapshot as hs
    from khipu import ledger

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    _snapshot(tmp_path)
    monkeypatch.setattr(ledger, "_vocab", lambda *_a: {"state": [], "kind": []})
    with ledger._replica_pending() as pending:  # a change not copied yet
        pending["p#r"] = 1  # the old file kept counts
    entered, release = threading.Event(), threading.Event()
    real_write = hs.ledger_write_snapshot

    def held_at_version_1(*a, **kw):
        if a[3] and a[3][0]["version"] == 1:
            entered.set()
            assert release.wait(5)
        return real_write(*a, **kw)

    monkeypatch.setattr(hs, "ledger_write_snapshot", held_at_version_1)
    older = threading.Thread(target=lambda: ledger.replica_catch_up(_ReplicaHub(1)))
    older.start()
    try:
        assert entered.wait(5)
        assert ledger.replica_catch_up(_ReplicaHub(2))["left"] == 0
        lock = hs._acquire_refresh_lock()  # the removal's copy is skipped and must be retried
        assert lock is not None
        try:
            ledger._replica_sync(_ReplicaHub(3, removed=True), "p", ["r"])
        finally:
            hs._release_refresh_lock(lock)
        assert ledger.replica_pending() == 1
    finally:
        release.set()
        older.join(5)
    assert ledger.replica_pending() == 1
    ledger.replica_catch_up(_ReplicaHub(3, removed=True))
    assert _replica_titles() == {} and ledger.replica_pending() == 0


def test_a_pending_copy_marker_does_not_hide_a_duplicate_queued_add(monkeypatch, tmp_path):
    from khipu import hub_snapshot as hs

    ledger = _offline(monkeypatch, tmp_path)
    _snapshot(tmp_path)
    assert hs.ledger_write_snapshot(P, (P, False, "2026-10-15T00:00:00Z"), ledger.default_vocab("NEEDS OWNER"),
                                    [], [], [])["ok"]
    with ledger._replica_pending() as pending:  # changes not copied yet
        pending.update({f"{P}#r": "a", f"{P}#q": "b"})
    assert ledger.write(ledger.new_op("add", P, "r", fields={"title": "first", "kind": "slice"}))["queued"]
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("add", P, "r", fields={"title": "duplicate", "kind": "slice"}))
    assert err.value.reason == "exists"
    # Removed by a waiting write: definite, whatever the copy holds.
    assert ledger.write(ledger.new_op("remove", P, "r"))["queued"]
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("set", P, "r", fields={"state": "DONE"}))
    assert err.value.reason == "not_found"
    # Uncertain for the copy alone: the hub decides.
    assert ledger.write(ledger.new_op("set", P, "q", fields={"state": "DONE"}))["queued"]


def _late_setup(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger, "DRAIN_WAIT_S", 0.05)
    history = []
    row = {"row_id": "r", "state": "IN FLIGHT", "title": "t", "owner": "none", "evidence": "", "link": ""}
    monkeypatch.setattr(ledger, "_live_row", lambda *_a: row)
    monkeypatch.setattr(ledger, "_vocab", lambda *_a: ledger.default_vocab("NEEDS OWNER"))
    monkeypatch.setattr(ledger, "_last_setter", lambda *_a: "a synthetic writer")
    monkeypatch.setattr(ledger, "_history", lambda *_a, **kw: history.append(kw))

    def apply(_conn, op, *, late=False):
        if op["op_id"] == "older":
            return {"ok": True}
        return ledger._set(object(), op, late=late)

    monkeypatch.setattr(ledger, "apply_op", apply)
    ledger.enqueue(ledger.new_op("set", P, "r", fields={"evidence": "older"}, op_id="older"))
    return ledger, history


def test_a_write_that_gave_up_waiting_is_kept_as_a_queued_write(monkeypatch, tmp_path):
    import fcntl

    ledger, history = _late_setup(monkeypatch, tmp_path)
    fd = os.open(str(ledger.outbox_dir() / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        op = ledger.new_op("set", P, "r", fields={"state": "DONE"}, if_state="NOT STARTED", op_id="waiting")
        assert ledger.write(op, conn=object())["queued"]
    finally:
        os.close(fd)
    got = ledger.drain(conn=object())
    assert (got["landed"], got["refused"]) == (2, 0)
    assert [h["note"] for h in history] == ["arrived late; the row had moved to IN FLIGHT"]


def test_a_write_that_drains_its_own_line_is_refused_as_a_fresh_write(monkeypatch, tmp_path):
    ledger, history = _late_setup(monkeypatch, tmp_path)
    op = ledger.new_op("set", P, "r", fields={"state": "DONE"}, if_state="NOT STARTED", op_id="waiting")
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(op, conn=object())
    assert err.value.reason == "moved" and history == []
    assert ledger._outbox_jobs() == [] and not list(ledger._refused_dir().glob("*.json"))


def test_a_write_landed_by_another_drain_answers_landed_when_the_hub_drops(monkeypatch, tmp_path):
    import fcntl
    import time

    ledger = _offline(monkeypatch, tmp_path)
    calls = []

    def apply(_conn, op, **_kw):
        calls.append(op["op_id"])
        raise ledger.HubAway("synthetic: dropped after the other drain landed it")

    monkeypatch.setattr(ledger, "apply_op", apply)
    ledger.enqueue(ledger.new_op("set", P, "r1", fields={"evidence": "0"}, op_id="op0"))
    fd = os.open(str(ledger.outbox_dir() / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    got = {}
    writer = threading.Thread(target=lambda: got.update(ledger.write(
        ledger.new_op("add", P, "r1", fields={"title": "t", "kind": "slice"}, op_id="op1"), conn=object())))
    writer.start()
    deadline = time.monotonic() + 5
    while not any("op1" in p.name for p in ledger._outbox_jobs()):
        assert time.monotonic() < deadline, "the write did not join the line"
        time.sleep(0.01)
    for path in ledger._outbox_jobs():  # the other drain landed both, and wrote down each outcome first
        job = json.loads(path.read_text(encoding="utf-8"))["op"]
        ledger._record_outcome(job, "landed", result={"ok": True, "line": f"landed {job['op_id']}"})
        path.unlink()
    os.close(fd)
    writer.join(10)
    assert got == {"ok": True, "line": "landed op1"} and ledger._outbox_jobs() == [] and calls == []


def test_a_write_gone_from_the_line_without_an_outcome_is_never_called_landed(monkeypatch, tmp_path):
    import fcntl
    import time

    ledger = _offline(monkeypatch, tmp_path)

    def apply(_conn, op, **_kw):
        raise AssertionError("a write that left the line is never applied again to find out")

    monkeypatch.setattr(ledger, "apply_op", apply)
    monkeypatch.setattr(ledger, "_landed_result", lambda *_a: (_ for _ in ()).throw(ledger.HubAway("dropped")))
    ledger.enqueue(ledger.new_op("set", P, "r1", fields={"evidence": "0"}, op_id="op0"))
    fd = os.open(str(ledger.outbox_dir() / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    errors = []

    def write():
        try:
            ledger.write(ledger.new_op("set", P, "r1", fields={"evidence": "1"}, op_id="op1"), conn=object())
        except ledger.LedgerError as exc:
            errors.append(exc.reason)

    writer = threading.Thread(target=write)
    writer.start()
    deadline = time.monotonic() + 5
    while not any("op1" in p.name for p in ledger._outbox_jobs()):
        assert time.monotonic() < deadline, "the write did not join the line"
        time.sleep(0.01)
    for path in ledger._outbox_jobs():
        path.unlink()  # gone, but nothing says what became of it
    os.close(fd)
    writer.join(10)
    assert errors == ["unconfirmed"]


def test_waiting_writes_keep_their_order_when_the_clock_steps_back(monkeypatch, tmp_path):
    import time

    ledger = _offline(monkeypatch, tmp_path)
    clock = iter([2_000_000_000_000_000_000, 1_000_000_000_000_000_000, 1_500_000_000_000_000_000])
    monkeypatch.setattr(time, "time_ns", lambda: next(clock))
    ids = [ledger.enqueue(ledger.new_op("set", P, "r1", fields={"evidence": str(i)}, op_id=f"op{i}")) and f"op{i}"
           for i in range(3)]
    assert [op["op_id"] for op in ledger.queued_ops()] == ids


def test_an_op_id_holding_a_dash_never_hides_another_queued_write(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    assert ledger.write(ledger.new_op("set", P, "r1", fields={"evidence": "1"}, op_id="slice-done"))["queued"]
    assert ledger.write(ledger.new_op("set", P, "r2", fields={"evidence": "2"}, op_id="done"))["queued"]
    assert [op["op_id"] for op in ledger.queued_ops()] == ["slice-done", "done"]


def test_an_op_id_reused_for_another_row_is_refused(hub):
    ok("add", "a", "--title", "A", "--kind", "slice")
    ok("add", "b", "--title", "B", "--kind", "slice")
    ok("set", "a", "--state", "DONE", "--op-id", "step1")
    assert ok("set", "a", "--state", "DONE", "--op-id", "step1").strip() == "set a: state"  # a true retry
    rc, out, err = run("set", "b", "--state", "DONE", "--op-id", "step1", "--project", P)
    assert rc == 1 and "already used for a different write" in out + err
    assert _sql(hub, "SELECT state FROM ledger_rows WHERE row_id = 'b'") == [("NOT STARTED",)]
    # The same row and verb with another payload is another write, too.
    ok("set", "a", "--evidence", "first", "--op-id", "step2")
    rc, out, err = run("set", "a", "--evidence", "second", "--op-id", "step2", "--project", P)
    assert rc == 1 and "already used for a different write" in out + err
    assert _sql(hub, "SELECT evidence FROM ledger_rows WHERE row_id = 'a'") == [("first",)]


@pytest.mark.parametrize("fault", ["tables_missing", "broken_job"])
def test_a_write_in_line_is_told_what_became_of_it_when_an_older_job_fails(fault, monkeypatch, tmp_path):
    import psycopg

    ledger = _offline(monkeypatch, tmp_path)
    ledger.enqueue(ledger.new_op("set", P, "r1", fields={"evidence": "0"}, op_id="older"))

    def apply(_conn, op, **_kw):
        if op["op_id"] == "older":
            if fault == "tables_missing":
                raise psycopg.errors.UndefinedTable('relation "ledger_rows" does not exist')
            raise ValueError("a job that fails every time")
        return {"ok": True, "line": "landed mine"}

    monkeypatch.setattr(ledger, "apply_op", apply)
    got = ledger.write(ledger.new_op("set", P, "r1", fields={"evidence": "1"}, op_id="mine"), conn=object())
    queued = [op["op_id"] for op in ledger.queued_ops()]
    if fault == "tables_missing":  # the line waits for khipu migrate, and says so
        assert got["queued"] and queued == ["older", "mine"]
    else:  # the broken job is set aside, kept, and the line moves on
        assert got == {"ok": True, "line": "landed mine"} and queued == []
        assert [p.name.split("-", 1)[1] for p in ledger._refused_dir().glob("*.json")] == ["older.json"]


def _copy_row(row_id, parent=""):
    return {**_replica_row(row_id, 1), "parent": parent, "created_at": None, "updated_at": None,
            "removed_at": None, "removed_by": None, "removed_note": ""}


def test_a_set_after_a_queued_restore_is_not_refused_offline(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    view = {"vocab": ledger.default_vocab(), "rows": [_copy_row("x"), _copy_row("y")], "history": [], "gone": {},
            "as_of": None, "offline": True}
    monkeypatch.setattr(ledger, "_offline_view", lambda _p: view)
    ledger.enqueue(ledger.new_op("remove", P, "x", op_id="rm"))
    ledger.enqueue(ledger.new_op("restore", P, "x", op_id="rs", from_app=True))
    assert ledger.write(ledger.new_op("set", P, "x", fields={"evidence": "after restore"}))["queued"]
    assert ledger.write(ledger.new_op("remove", P, "y"))["queued"]
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("set", P, "y", fields={"evidence": "after remove"}))
    assert err.value.reason == "not_found"


def test_a_pending_parent_still_sees_the_children_the_copy_knows(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    view = {"vocab": ledger.default_vocab(), "rows": [_copy_row("p"), _copy_row("c", parent="p")], "history": [],
            "gone": {}, "as_of": None, "offline": True}
    monkeypatch.setattr(ledger, "_offline_view", lambda _p: view)
    with ledger._replica_pending() as pending:
        pending[f"{P}#p"] = "a"
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("remove", P, "p"))
    assert err.value.reason == "has_children"


def test_a_change_during_the_first_refresh_is_retried_once_the_copy_exists(monkeypatch, tmp_path):
    from khipu import hub_snapshot as hs
    from khipu import ledger

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(ledger, "_vocab", lambda *_a: {"state": [], "kind": []})
    lock = hs._acquire_refresh_lock()  # the first refresh is dumping; no copy on disk yet
    assert lock is not None
    try:
        ledger._replica_sync(_ReplicaHub(2), "p", ["r"])
        assert ledger.replica_pending() == 1
    finally:
        hs._release_refresh_lock(lock)
    _snapshot(tmp_path)  # that refresh read the hub before the change
    assert ledger.replica_catch_up(_ReplicaHub(2))["left"] == 0
    assert _replica_titles() == {"r": "v2"}


# ---- round 4: one op id is one write; outcomes are shared; the copy is whole ----


def test_an_op_id_reused_for_another_row_while_away_is_refused(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    assert ledger.write(ledger.new_op("set", P, "first", fields={"evidence": "one"}, op_id="same"))["queued"]
    assert ledger.write(ledger.new_op("set", P, "first", fields={"evidence": "one"}, op_id="same"))["queued"]
    for other in (ledger.new_op("set", P, "second", fields={"evidence": "two"}, op_id="same"),
                  ledger.new_op("set", P, "first", fields={"evidence": "changed"}, op_id="same")):
        with pytest.raises(ledger.LedgerError) as err:
            ledger.write(other)
        assert err.value.reason == "op_id_reused"
    assert [(q["row_id"], q["fields"]) for q in ledger.queued_ops()] == [("first", {"evidence": "one"})]
    # Landed and gone from the line: the op id still stands for that one write.
    ledger._record_outcome(ledger.queued_ops()[0], "landed", result={"ok": True})
    ledger._outbox_jobs()[0].unlink()
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("set", P, "second", fields={"evidence": "two"}, op_id="same"))
    assert err.value.reason == "op_id_reused" and ledger.queued_ops() == []


def test_two_writers_of_one_op_id_are_both_told_it_was_refused(monkeypatch, tmp_path):
    import fcntl

    ledger = _offline(monkeypatch, tmp_path)
    ledger.enqueue(ledger.new_op("set", P, "r", fields={"evidence": "older"}, op_id="older"))
    repeated = ledger.new_op("set", P, "r", fields={"state": "DONE"}, if_state="NOT STARTED", op_id="same")
    applied = []

    def apply(_conn, op, **_kw):
        applied.append(op["op_id"])
        if op["op_id"] == "older":
            return {"ok": True, "line": "older landed"}
        raise ledger.LedgerError("condition moved", "moved")

    monkeypatch.setattr(ledger, "apply_op", apply)
    real_enqueue, first_done, calls = ledger.enqueue, threading.Event(), []

    def enqueue(op, reason=""):
        path = real_enqueue(op, reason)
        if op["op_id"] == "same":
            calls.append(path)
            if len(calls) == 2:  # the second writer shares the job, and looks once the first was told
                assert first_done.wait(5)
        return path

    monkeypatch.setattr(ledger, "enqueue", enqueue)
    fd = os.open(str(ledger.outbox_dir() / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    told = []

    def writer():
        try:
            told.append(ledger._write_in_line(object(), dict(repeated)))
        except ledger.LedgerError as exc:
            told.append(exc.reason)

    first, second = threading.Thread(target=writer), threading.Thread(target=writer)
    first.start()
    while len(calls) < 1:
        threading.Event().wait(0.01)
    second.start()
    while len(calls) < 2:
        threading.Event().wait(0.01)
    os.close(fd)
    first.join(10)
    first_done.set()
    second.join(10)
    assert told == ["moved", "moved"] and applied == ["older", "same"]


class _HistoryHub(_ReplicaHub):
    """One row at one version, with the history entries named by id."""

    def __init__(self, entries):
        super().__init__(1)
        self.entries = entries

    def cursor(self):
        from khipu import ledger

        cur, entries = super().cursor(), self.entries
        rows_only = cur.fetchall

        def fetchall():
            if "FROM ledger_history" in cur.query:
                return [(i, "r", "2026-10-15T00:00:00Z", "2026-10-15T00:00:00Z", {"kind": "app"}, "transition",
                         "NOT STARTED", "NOT STARTED", {}, f"entry {i}", None, None, "cli")[:len(ledger._HIST_COLS)]
                        for i in entries]
            return rows_only()

        cur.fetchall = fetchall
        return cur


def test_an_older_catch_up_never_erases_history_added_without_a_new_version(monkeypatch, tmp_path):
    from khipu import hub_snapshot as hs
    from khipu import ledger

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    _snapshot(tmp_path)
    monkeypatch.setattr(ledger, "_vocab", lambda *_a: ledger.default_vocab("NEEDS OWNER"))
    with ledger._replica_pending() as pending:
        pending["p#r"] = "a change not copied yet"
    entered, release = threading.Event(), threading.Event()
    real_write = hs.ledger_write_snapshot

    def held_with_one_entry(*a, **kw):
        if len(a[4]) == 1:
            entered.set()
            assert release.wait(5)
        return real_write(*a, **kw)

    monkeypatch.setattr(hs, "ledger_write_snapshot", held_with_one_entry)
    older = threading.Thread(target=lambda: ledger.replica_catch_up(_HistoryHub([1])))
    older.start()
    try:
        assert entered.wait(5)
        # A late conditional write: history grows, the row's version does not.
        ledger._replica_sync(_HistoryHub([1, 2]), "p", ["r"])
    finally:
        release.set()
        older.join(5)
    assert [h["id"] for h in hs.ledger_read_snapshot("p")["history"]] == [1, 2]
    assert ledger.replica_pending() == 0


def test_a_queued_cascade_removal_is_known_for_a_child_while_its_parent_is_pending(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    view = {"vocab": ledger.default_vocab(), "rows": [_copy_row("parent"), _copy_row("child", parent="parent"),
                                                      _copy_row("grandchild", parent="child")],
            "history": [], "gone": {}, "as_of": None, "offline": True}
    monkeypatch.setattr(ledger, "_offline_view", lambda _p: view)
    with ledger._replica_pending() as pending:
        pending[f"{P}#parent"] = "a change not copied yet"
    assert ledger.write(ledger.new_op("remove", P, "parent", children="remove"))["queued"]
    assert ledger.load(P)["rows"] == []
    for row_id in ("child", "grandchild"):
        with pytest.raises(ledger.LedgerError) as err:
            ledger.write(ledger.new_op("set", P, row_id, fields={"evidence": "must be refused"}))
        assert err.value.reason == "not_found"


def test_a_retry_of_a_write_already_in_line_is_answered_as_queued_while_away(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    for _ in range(2):
        assert ledger.write(ledger.new_op("add", P, "r1", fields={"title": "one"}, op_id="add1"))["queued"]
    for _ in range(2):
        assert ledger.write(ledger.new_op("remove", P, "r1", op_id="rm1"))["queued"]
    assert [q["op_id"] for q in ledger.queued_ops()] == ["add1", "rm1"]


def test_a_write_landed_directly_still_owns_its_op_id_while_away(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger, "apply_op", lambda _conn, _op, **_kw: {"ok": True, "line": "set r: evidence"})

    class Conn:
        def close(self):
            pass

    assert ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="same"), conn=Conn())["ok"]
    assert ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="same")) == {
        "ok": True, "line": "set r: evidence"}
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "other"}, op_id="same"))
    assert err.value.reason == "op_id_reused" and ledger.queued_ops() == []


def test_an_old_outcome_keeps_its_op_ids_digest(monkeypatch, tmp_path):
    import time

    ledger = _offline(monkeypatch, tmp_path)
    ledger._record_outcome(ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="same"), "landed",
                           result={"ok": True})
    old = time.time() - ledger.OUTCOME_KEEP_S - 60
    os.utime(ledger._outcome_path("same"), (old, old))
    ledger._prune_outcomes()
    assert "result" not in ledger._outcome("same")
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "other"}, op_id="same"))
    assert err.value.reason == "op_id_reused"


def test_a_queued_add_of_a_pending_row_does_not_make_its_copy_trusted(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    view = {"vocab": ledger.default_vocab(), "rows": [_copy_row("p"), _copy_row("c", parent="p")], "history": [],
            "gone": {}, "as_of": None, "offline": True}
    monkeypatch.setattr(ledger, "_offline_view", lambda _p: view)
    with ledger._replica_pending() as pending:
        pending[f"{P}#c"] = "a change not copied yet"  # on the hub c may be removed already
    assert ledger.write(ledger.new_op("add", P, "c", fields={"title": "again", "kind": "slice"}))["queued"]
    with pytest.raises(ledger.LedgerError) as err:  # the waiting add is known
        ledger.write(ledger.new_op("add", P, "c", fields={"title": "twice", "kind": "slice"}))
    assert err.value.reason == "exists"
    assert ledger.write(ledger.new_op("remove", P, "p"))["queued"]  # only the stale copy says p has a child


def test_a_queued_cascade_from_a_parent_missing_from_the_copy_reaches_its_children(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    view = {"vocab": ledger.default_vocab(), "rows": [_copy_row("c", parent="x"), _copy_row("g", parent="c")],
            "history": [], "gone": {}, "as_of": None, "offline": True}
    monkeypatch.setattr(ledger, "_offline_view", lambda _p: view)
    with ledger._replica_pending() as pending:
        pending[f"{P}#x"] = "added on the hub, not copied yet"
    assert ledger.write(ledger.new_op("remove", P, "x", children="remove"))["queued"]
    assert ledger.load(P)["rows"] == []
    for row_id in ("c", "g"):
        with pytest.raises(ledger.LedgerError) as err:
            ledger.write(ledger.new_op("set", P, row_id, fields={"evidence": "must be refused"}))
        assert err.value.reason == "not_found"


# ---- round 5: an op id, once taken, is taken for good; one lock decides it -------


class _Conn:
    def close(self):
        pass


def test_a_purged_rows_op_ids_are_never_taken_again(hub):
    ok("add", "a", "--title", "A", "--kind", "slice")
    ok("add", "b", "--title", "B", "--kind", "slice")
    ok("set", "a", "--evidence", "first", "--op-id", "kept")
    ok("remove", "a")
    assert ok("purge", "a", "--from-app").strip() == "removed a for good"
    rc, out, err = run("set", "b", "--evidence", "first", "--op-id", "kept", "--project", P)
    assert rc == 1 and "already used for a different write" in out + err
    assert _sql(hub, "SELECT evidence FROM ledger_rows WHERE row_id = 'b'") != [("first",)]
    # Only the binding is kept: its id, its write's digest and how it ended.
    assert _sql(hub, "SELECT row_id, verb, result FROM ledger_ops WHERE op_id = 'kept'") == [
        ("a", "set", {"ok": True, "purged": True})]


def test_a_refused_op_id_stays_refused_and_never_runs_another_write(hub):
    ok("add", "a", "--title", "A", "--kind", "slice")
    ok("add", "b", "--title", "B", "--kind", "slice")
    ok("set", "a", "--state", "IN FLIGHT")
    first = run("set", "a", "--state", "DONE", "--if-state", "NOT STARTED", "--op-id", "once", "--project", P)
    assert first[0] == 1 and "IN FLIGHT now" in first[2]
    rc, out, err = run("set", "b", "--state", "DONE", "--op-id", "once", "--project", P)
    assert rc == 1 and "already used for a different write" in out + err
    ok("set", "a", "--state", "NOT STARTED")
    # The same write again gets the answer it first had, though it would pass now.
    again = run("set", "a", "--state", "DONE", "--if-state", "NOT STARTED", "--op-id", "once", "--project", P)
    assert again[0] == 1 and "IN FLIGHT now" in again[2]
    assert _sql(hub, "SELECT row_id, state FROM ledger_rows ORDER BY row_id") == [("a", "NOT STARTED"),
                                                                                  ("b", "NOT STARTED")]


def test_a_direct_write_honours_an_op_id_this_mac_saw_refused(monkeypatch, tmp_path):
    from khipu import ledger

    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    first = ledger.new_op("set", P, "r1", fields={"evidence": "first"}, op_id="shared")
    ledger._record_outcome(first, "refused", refused="condition moved", reason_word="moved")
    applied = []
    monkeypatch.setattr(ledger, "apply_op", lambda _c, op, **_kw: applied.append(op["row_id"]) or {"ok": True})
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("set", P, "r2", fields={"evidence": "second"}, op_id="shared"), conn=_Conn())
    assert err.value.reason == "op_id_reused"
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(dict(first, digest=None), conn=_Conn())
    assert err.value.reason == "moved"
    assert applied == [] and ledger._outcome("shared")["digest"] == ledger.op_digest(dict(first, digest=None))


def test_a_retry_of_a_landed_write_repairs_the_offline_copy(hub, tmp_path, monkeypatch):
    from khipu import ledger

    _snapshot(tmp_path)
    ok("add", "a", "--title", "Before", "--kind", "slice")
    real_sync = ledger._replica_sync
    # The write lands, and the process ends before the offline copy hears of it.
    monkeypatch.setattr(ledger, "_replica_sync", lambda *_a: None)
    ok("set", "a", "--title", "After", "--op-id", "once")
    monkeypatch.setattr(ledger, "_replica_sync", real_sync)
    assert _replica_titles(P) == {"a": "Before"}
    assert ok("set", "a", "--title", "After", "--op-id", "once").strip() == "set a: title"
    assert _replica_titles(P) == {"a": "After"}


def test_a_later_writer_cannot_take_a_place_ahead_of_the_lock_holders_decision(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    real_jobs, real_enqueue = ledger._outbox_jobs, ledger.enqueue
    at_check, later_queued, first_look, landed = threading.Event(), threading.Event(), [True], []

    def jobs():
        if threading.current_thread().name == "older" and first_look[0]:
            first_look[0] = False
            at_check.set()
            later_queued.wait(1)  # a later write that can queue now would go first
        return real_jobs()

    def enqueue(op, reason=""):
        path = real_enqueue(op, reason)
        if op["op_id"] == "later":
            later_queued.set()
        return path

    monkeypatch.setattr(ledger, "_outbox_jobs", jobs)
    monkeypatch.setattr(ledger, "enqueue", enqueue)
    monkeypatch.setattr(ledger, "apply_op", lambda _c, op, **_kw: landed.append(op["op_id"]) or {"ok": True})

    def writer(name):
        ledger.write(ledger.new_op("set", P, "r", fields={"evidence": name}, op_id=name), conn=_Conn())

    older = threading.Thread(target=writer, args=("older",), name="older")
    later = threading.Thread(target=writer, args=("later",), name="later")
    older.start()
    assert at_check.wait(5)
    later.start()
    older.join(10)
    later.join(10)
    assert landed == ["older", "later"]


def test_a_retry_whose_job_lands_while_it_looks_is_answered_as_landed(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    op = ledger.new_op("set", P, "r", fields={"evidence": "same"}, op_id="retry")
    ledger.enqueue(op)
    ledger._record_outcome(op, "landed", result={"ok": True, "line": "already landed"})
    real_job_path = ledger._job_path

    def gone_after_lookup(op_id):
        found = real_job_path(op_id)
        if found is not None:
            found.unlink()
        return found

    monkeypatch.setattr(ledger, "_job_path", gone_after_lookup)
    assert ledger._known_op(dict(op)) == {"ok": True, "line": "already landed"}


def test_a_direct_write_that_finds_the_hub_away_keeps_its_place_ahead_of_a_later_one(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    in_flight, landed = threading.Event(), []
    real_enqueue, later_queued = ledger.enqueue, threading.Event()

    def apply(_c, op, **_kw):
        if op["op_id"] == "older" and not landed and not in_flight.is_set():
            in_flight.set()
            later_queued.wait(1)  # a later write that could queue and drain now would go first
            raise ledger.HubAway("synthetic: the hub went away mid-write")
        landed.append(op["op_id"])
        return {"ok": True}

    def enqueue(op, reason=""):
        path = real_enqueue(op, reason)
        if op["op_id"] == "later":
            later_queued.set()
        return path

    monkeypatch.setattr(ledger, "apply_op", apply)
    monkeypatch.setattr(ledger, "enqueue", enqueue)
    monkeypatch.setattr(ledger, "DRAIN_WAIT_S", 2)
    answers = {}

    def writer(name):
        answers[name] = ledger.write(ledger.new_op("set", P, "r", fields={"evidence": name}, op_id=name),
                                     conn=_Conn())

    older = threading.Thread(target=writer, args=("older",))
    older.start()
    assert in_flight.wait(5)
    later = threading.Thread(target=writer, args=("later",))
    later.start()
    older.join(10)
    later.join(10)
    # The older write joined the line before letting go: the later one drains it first.
    assert landed == ["older", "later"] and ledger.queued_ops() == []


def test_a_retry_of_a_long_landed_write_while_away_is_answered_as_landed(monkeypatch, tmp_path):
    import time

    ledger = _offline(monkeypatch, tmp_path)
    op = ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="old")
    ledger._record_outcome(op, "landed", result={"ok": True, "line": "set r: evidence"})
    old = time.time() - ledger.OUTCOME_KEEP_S - 60
    os.utime(ledger._outcome_path("old"), (old, old))
    ledger._prune_outcomes()
    got = ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="old"))
    assert got["ok"] and not got.get("queued") and ledger.queued_ops() == []


def test_the_hubs_record_of_a_purged_rows_op_answers_purged(hub):
    from khipu import ledger

    op = ledger.new_op("add", P, "a", fields={"title": "A", "kind": "slice"}, op_id="first")
    assert ledger.write(dict(op))["ok"]
    ok("remove", "a")
    ok("purge", "a", "--from-app")
    with _real_connect(hub) as conn, pytest.raises(ledger.LedgerError) as err:
        ledger._landed_result(conn, dict(op))
    assert err.value.reason == "purged"


def test_case_distinct_op_ids_are_distinct_writes(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    first = ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="CaseID")
    ledger._record_outcome(first, "landed", result={"ok": True, "line": "set r: evidence"})
    got = ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="caseid"))
    assert got.get("queued") and [q["op_id"] for q in ledger.queued_ops()] == ["caseid"]
    assert ledger._outcome("caseid") is None and ledger._outcome("CaseID")["op_id"] == "CaseID"


def test_an_outcome_named_by_its_op_id_is_moved_and_answers_only_that_exact_id(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    op = ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="CaseID")
    legacy = ledger.outbox_dir() / "outcomes"
    legacy.mkdir(parents=True)
    # As an older Khipu named it, and as a case-insensitive disk hands it back for "caseid".
    (legacy / "caseid.json").write_text(json.dumps({"op_id": "CaseID", "digest": ledger.op_digest(op),
                                               "status": "landed", "result": {"ok": True, "line": "landed"}}))
    got = ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="caseid"))
    assert got.get("queued") and [q["op_id"] for q in ledger.queued_ops()] == ["caseid"]
    assert ledger._outcome("CaseID")["status"] == "landed"
    assert list(legacy.iterdir()) == []
    assert [p.name for p in ledger._outcomes_dir().glob("*.json")] == [ledger._outcome_path("CaseID").name]
    assert "caseid" not in ledger._outcome_path("CaseID").name.lower()


def test_an_offline_refusal_binds_its_op_id(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    view = {"vocab": ledger.default_vocab(), "rows": [_copy_row("a")], "history": [], "gone": {},
            "as_of": None, "offline": True}
    monkeypatch.setattr(ledger, "_offline_view", lambda _p: view)
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("add", P, "a", fields={"title": "again", "kind": "slice"}, op_id="once"))
    assert err.value.reason == "exists"
    assert ledger._outcome("once")["status"] == "refused"
    with pytest.raises(ledger.LedgerError) as err:  # another write under the refused id
        ledger.write(ledger.new_op("add", P, "b", fields={"title": "B", "kind": "slice"}, op_id="once"))
    assert err.value.reason == "op_id_reused" and ledger.queued_ops() == []
    view["rows"] = []  # a retry is answered as first refused, even once the copy would allow it
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("add", P, "a", fields={"title": "again", "kind": "slice"}, op_id="once"))
    assert err.value.reason == "exists" and ledger.queued_ops() == []


def test_old_outcomes_that_disagree_are_each_kept_aside(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    ledger._record_outcome(ledger.new_op("set", P, "r", fields={"evidence": "one"}, op_id="C1"), "landed")
    legacy = ledger.outbox_dir() / "outcomes"
    legacy.mkdir(parents=True)
    for digest in ("b1", "b2"):  # an older Khipu still running writes the old name twice
        (legacy / "C1.json").write_text(json.dumps({"op_id": "C1", "digest": digest, "status": "landed"}))
        ledger._migrate_outcomes()
    kept = sorted(json.loads(p.read_text())["digest"] for p in legacy.glob("*.conflict"))
    assert kept == ["b1", "b2"] and ledger._outcome("C1")["status"] == "landed"


def test_a_hub_reused_refusal_in_the_line_does_not_bind_the_id_here(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    assert ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "theirs"}, op_id="dup"))["queued"]

    class Conn:
        def close(self):
            pass

    def hub_holds_another(_conn, op, **_kw):
        raise ledger._reused_error(op)

    monkeypatch.setattr(ledger, "apply_op", hub_holds_another)
    assert ledger.drain(conn=Conn())["refused"] == 1
    assert ledger._outcome("dup") is None and len(list(ledger._refused_dir().glob("*.json"))) == 1
    # The id's owner retrying here is answered by the hub, not by the losing write.
    monkeypatch.setattr(ledger, "apply_op", lambda _conn, _op, **_kw: {"ok": True, "line": "set r: mine"})
    assert ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "mine"}, op_id="dup"), conn=Conn())["ok"]


# ---- round 7: one answer for what this Mac already knows of an op id --------------


def test_a_landed_retry_never_joins_the_line_behind_a_later_write(monkeypatch, tmp_path):
    import fcntl

    ledger = _offline(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger, "DRAIN_WAIT_S", 0)
    old = ledger.new_op("set", P, "r", fields={"evidence": "old"}, op_id="old-op")
    ledger._record_outcome(old, "landed", result={"ok": True, "line": "set r: evidence", "affected": ["r"]})
    ledger.enqueue(ledger.new_op("set", P, "r", fields={"evidence": "later"}, op_id="later-op"))
    fd = os.open(str(ledger.outbox_dir() / ".drain.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)  # another process is draining the line
    try:
        for conn in (_Conn(), None):  # the hub reachable, then away
            got = ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "old"}, op_id="old-op"), conn=conn)
            assert got["ok"] and not got.get("queued") and got["line"] == "set r: evidence"
    finally:
        os.close(fd)
    assert [q["op_id"] for q in ledger.queued_ops()] == ["later-op"]


def test_a_legacy_outcome_answers_its_retry_when_it_cannot_be_moved(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)
    op = ledger.new_op("set", P, "r", fields={"evidence": "x"}, op_id="landed-op")
    legacy = ledger.outbox_dir() / "outcomes"
    legacy.mkdir(parents=True)
    (legacy / "landed-op.json").write_text(json.dumps({"op_id": "landed-op", "digest": ledger.op_digest(op),
                                                       "status": "landed", "result": {"ok": True, "line": "set r"}}))
    ledger._outcomes_dir().write_text("blocks the new folder")  # the move cannot happen
    got = ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "x"}, op_id="landed-op"))
    assert got == {"ok": True, "line": "set r"} and ledger.queued_ops() == []
    # A record that is there but cannot be read refuses the write; it never frees the id.
    ledger._outcomes_dir().unlink()
    ledger._outcomes_dir().mkdir()
    ledger._outcome_path("torn").write_text("{not json")
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "x"}, op_id="torn"))
    assert err.value.reason == "op_unreadable" and ledger.queued_ops() == []


def test_a_write_the_hub_refused_as_reused_is_refused_again_while_away(monkeypatch, tmp_path):
    ledger = _offline(monkeypatch, tmp_path)

    def loser(op_id):
        return ledger.new_op("set", P, "r", fields={"evidence": "losing"}, op_id=op_id)

    real_apply = ledger.apply_op
    monkeypatch.setattr(ledger, "apply_op", lambda _c, op, **_kw: (_ for _ in ()).throw(ledger._reused_error(op)))
    assert ledger.write(loser("drained"))["queued"]
    assert ledger.drain(conn=_Conn())["refused"] == 1  # refused in the line
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(loser("direct"), conn=_Conn())  # refused as a direct write
    assert err.value.reason == "op_id_reused"
    monkeypatch.setattr(ledger, "apply_op", real_apply)
    for op_id in ("drained", "direct"):
        with pytest.raises(ledger.LedgerError) as err:
            ledger.write(loser(op_id))
        assert err.value.reason == "op_id_reused" and ledger.queued_ops() == []
    # The id's owner, whose write the hub holds, is not refused here.
    assert ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "mine"}, op_id="drained"))["queued"]


def test_a_retry_of_a_write_on_a_row_purged_since_is_refused_as_purged(monkeypatch, tmp_path):
    from khipu import hub_snapshot as hs

    ledger = _offline(monkeypatch, tmp_path)
    added = ledger.new_op("add", P, "r", fields={"title": "R", "kind": "slice"}, op_id="original-add")
    ledger._record_outcome(added, "landed", result={"ok": True, "verb": "add", "id": "r", "line": "added r"})
    con = sqlite3.connect(str(_snapshot(tmp_path)))
    con.execute("INSERT INTO ledger_gone VALUES (?, ?, ?)", (P, "r", hs.LEDGER_PURGED_VERSION))
    con.commit()
    con.close()
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("add", P, "r", fields={"title": "R", "kind": "slice"}, op_id="original-add"))
    assert err.value.reason == "purged" and ledger.queued_ops() == []
    # An offline copy that is there but cannot be read refuses the retry rather than guess.
    hs.snapshot_path().write_bytes(b"not a database" * 100)
    with pytest.raises(ledger.LedgerError) as err:
        ledger.write(ledger.new_op("add", P, "r", fields={"title": "R", "kind": "slice"}, op_id="original-add"))
    assert err.value.reason == "op_unreadable" and ledger.queued_ops() == []


def test_a_write_that_lands_through_the_line_is_answered_landed_though_the_copy_is_torn(monkeypatch, tmp_path):
    from khipu import hub_snapshot as hs

    ledger = _offline(monkeypatch, tmp_path)
    _snapshot(tmp_path).write_bytes(b"not a database" * 100)
    assert hs.snapshot_path().is_file()
    monkeypatch.setattr(ledger, "apply_op", lambda _c, op, **_kw: {"ok": True, "line": f"set r: {op['op_id']}"})
    ledger.enqueue(ledger.new_op("set", P, "r", fields={"evidence": "earlier"}, op_id="earlier"))
    assert ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "mine"}, op_id="mine"),
                        conn=_Conn()) == {"ok": True, "line": "set r: mine"}
    # A torn old outcome a disk that ignores case hands back says nothing of another id.
    legacy = ledger.outbox_dir() / "outcomes"
    legacy.mkdir()
    (legacy / "CaseID.json").write_text("{torn")
    assert ledger.write(ledger.new_op("set", P, "r", fields={"evidence": "x"}, op_id="caseid"), conn=_Conn())["ok"]


def test_the_drain_never_resends_a_decided_job_or_stalls_on_a_refusal_it_cannot_keep(monkeypatch, tmp_path):
    from khipu.outbox import _atomic_write

    ledger = _offline(monkeypatch, tmp_path)
    sent = []

    def apply(_c, op, **_kw):
        sent.append(op["op_id"])
        if op["op_id"] == "dup":
            raise ledger._reused_error(op)
        return {"ok": True}

    monkeypatch.setattr(ledger, "apply_op", apply)
    landed = ledger.new_op("set", P, "r", fields={"evidence": "x"}, op_id="landed")
    ledger._record_outcome(landed, "landed", result={"ok": True})
    # A process ended after keeping the outcome, before the job left the line.
    _atomic_write(ledger.outbox_dir() / f"{1:020d}-landed.json", {"op": landed, "reason": ""})
    ledger.enqueue(ledger.new_op("set", P, "r", fields={"evidence": "y"}, op_id="dup"))
    ledger.enqueue(ledger.new_op("set", P, "r", fields={"evidence": "z"}, op_id="next"))
    (ledger.outbox_dir() / "op-rejected").write_text("the refusal cannot be kept")
    assert ledger.drain(conn=_Conn())["refused"] == 1
    assert sent == ["dup", "next"] and ledger.queued_ops() == []
