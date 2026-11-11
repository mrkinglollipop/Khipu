"""`khipu hygiene blank-field` (khipu/blank_field.py) against the throwaway
PostgreSQL tests/hermetic_pg.py starts. Every row is synthetic; never the
live hub, the real home or a model API."""
from __future__ import annotations

import argparse
import copy
import json
import sqlite3

import psycopg
import pytest

from khipu import blank_field, db, embed, features
from khipu import hub_snapshot as hs
from tests.fact_seed import announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

VEC = "[" + ",".join(["0.25"] * 768) + "]"
PREF = "brief rule chartreuse heron"
MERGED_PREF = "merged rule saffron otter"
LINE = "typed brief about the cobalt quarry gate"
MERGED_LINE = "merged brief about the umber kiln"
QUOTE = "the quote that stays put"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.setenv("KHIPU_TEST", "1")
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("KHIPU_ISOLATED_DIR", raising=False)
    db._TABLE_COLUMNS_CACHE.clear()
    yield
    db._TABLE_COLUMNS_CACHE.clear()


@pytest.fixture
def pg(hub, monkeypatch):  # noqa: F811
    monkeypatch.setenv("KHIPU_DATABASE_URL", hub.dsn)
    monkeypatch.setattr(db, "connect", lambda *a, **kw: hub.connect())
    monkeypatch.setattr(
        hs, "try_hub_connect",
        lambda *, connect_timeout=5, **_kw: psycopg.Connection.connect(hub.dsn, connect_timeout=connect_timeout))
    monkeypatch.setattr(features, "enabled", lambda name: False)
    conn = hub.connect()
    hub.migrate(conn=conn)
    conn.commit()
    db._TABLE_COLUMNS_CACHE.clear()
    return conn


def add(conn, name, *, prefs=(PREF,), lines=(LINE,), merged=True, vector=False) -> int:
    verbatim = {"quotes": [QUOTE], "owner_lines": list(lines), "paths": ["a/b.py"]}
    raw = {"ts": "2026-11-10T09:00:00Z", "summary": f"scratch {name}", "harness": "codex",
           "session_id": f"codex:{name}", "transcript_range": "0:10", "preferences": list(prefs),
           "verbatim": verbatim, "score": 1.50, "big": 2.0e3, "nested": {"n": 10.10}}
    if merged:
        raw["merged_from"] = [
            {"source_id": f"s-{name}", "score": 0.9000,
             "payload": {"summary": "merged", "preferences": [MERGED_PREF], "weight": 3.10,
                         "verbatim": {"quotes": ["merged quote"], "owner_lines": [MERGED_LINE]}}},
            "not an object",
        ]
    # Written as JSON text with the numerals as typed, so jsonb keeps them.
    text = json.dumps(raw).replace('"score": 1.5', '"score": 1.50').replace('"weight": 3.1', '"weight": 3.10')
    cur = conn.cursor()
    announce(cur)
    cur.execute(
        "INSERT INTO episodes (ts, session_id, summary, raw, preferences, verbatim, harness, transcript_range) "
        "VALUES (%s::timestamptz, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s) RETURNING id",
        (raw["ts"], raw["session_id"], raw["summary"], text, json.dumps(list(prefs)),
         json.dumps(verbatim), raw["harness"], raw["transcript_range"]))
    eid = cur.fetchone()[0]
    if vector:
        cur.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, content_hash, embedding) "
            "SELECT id, 'episode', %s, 0, %s, %s, %s::vector FROM embedding_profiles WHERE is_active",
            (str(eid), f"scratch {name}\npreferences: {PREF}", f"h-{name}", VEC))
    conn.commit()
    return eid


def forget_row(conn, eid):
    conn.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (eid,))
    conn.commit()


def row_text(conn, eid):
    """The whole row as text, every column."""
    return conn.execute("SELECT to_jsonb(e)::text FROM episodes e WHERE id = %s", (eid,)).fetchone()[0]


def exact(text):
    """JSON with every numeral kept as its text, so a compare is byte-level."""
    return json.loads(text, parse_float=lambda s: ("F", s), parse_int=lambda s: ("I", s))


def columns(conn, eid):
    r = conn.execute("SELECT preferences::text, raw::text, verbatim::text FROM episodes WHERE id = %s",
                     (eid,)).fetchone()
    return {"preferences": exact(r[0]), "raw": exact(r[1]), "verbatim": exact(r[2])}


def run(tmp_path, capsys, *, field=None, ids=(), backup="b.json", apply=False, restore=None):
    ids_file = tmp_path / "ids.txt"
    ids_file.write_text("".join(f"{i}\n" for i in ids) + "\n")
    args = argparse.Namespace(field=field, ids_file=str(ids_file) if field else None,
                              backup=str(tmp_path / backup) if backup else None,
                              restore=str(tmp_path / restore) if restore else None, apply=apply)
    capsys.readouterr()
    code = blank_field.main(args)
    printed = capsys.readouterr().out
    lines = printed.strip().splitlines()
    assert len(lines) == 1, printed
    for secret in (PREF, MERGED_PREF, LINE, MERGED_LINE, QUOTE):
        assert secret not in printed
    return code, json.loads(lines[0])


# ---- dry run ----------------------------------------------------------------------

def test_a_dry_run_changes_nothing_and_counts(pg, tmp_path, capsys):
    a, b = add(pg, "a"), add(pg, "b", prefs=(), merged=False)
    c = add(pg, "c")
    forget_row(pg, c)
    before = {i: row_text(pg, i) for i in (a, b, c)}
    code, out = run(tmp_path, capsys, field="preferences", ids=(a, b, c, 999999))
    assert code == 0, out
    assert out["dry_run"] is True
    assert (out["ids_given"], out["live"], out["holding"], out["strings"]) == (4, 2, 1, 2), out
    assert (out["skipped_missing"], out["skipped_forgotten"], out["skipped_empty"]) == (1, 1, 1), out
    assert {i: row_text(pg, i) for i in (a, b, c)} == before
    assert not (tmp_path / "b.json").exists()


# ---- apply: preferences ------------------------------------------------------------

def test_apply_empties_preferences_everywhere_and_only_there(pg, tmp_path, capsys):
    a, other = add(pg, "a", vector=True), add(pg, "other")
    before, other_before = columns(pg, a), row_text(pg, other)
    rest_before = pg.execute("SELECT (to_jsonb(e) - 'preferences' - 'raw' - 'changed_at')::text "
                             "FROM episodes e WHERE id = %s", (a,)).fetchone()[0]
    code, out = run(tmp_path, capsys, field="preferences", ids=(a,), apply=True)
    assert code == 0 and out["changed"] == 1 and out["vectors_to_refresh"] == 1, out
    after = columns(pg, a)
    assert after["preferences"] == []
    want = copy.deepcopy(before["raw"])
    want["preferences"] = []
    want["merged_from"][0]["payload"]["preferences"] = []
    assert after["raw"] == want                       # every other raw key byte-equal
    assert after["verbatim"] == before["verbatim"]
    assert pg.execute("SELECT (to_jsonb(e) - 'preferences' - 'raw' - 'changed_at')::text "
                      "FROM episodes e WHERE id = %s", (a,)).fetchone()[0] == rest_before
    assert row_text(pg, other) == other_before        # an unlisted row is untouched


def test_apply_writes_the_backup_before_any_row_changes(pg, tmp_path, capsys, monkeypatch):
    a = add(pg, "a")
    before = row_text(pg, a)

    def no_disk(path, backup):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(blank_field, "write_backup", no_disk)
    code, out = run(tmp_path, capsys, field="preferences", ids=(a,), apply=True)
    assert code == 1 and out["ok"] is False and "rolled back" in out["error"], out
    assert row_text(pg, a) == before


def test_a_backup_that_does_not_read_back_changes_nothing_and_is_removed(pg, tmp_path, capsys, monkeypatch):
    a = add(pg, "a")
    before = row_text(pg, a)
    real = blank_field.json.loads
    monkeypatch.setattr(blank_field.json, "loads",
                        lambda s, *k, **kw: {"tampered": True} if isinstance(s, str) and BLANK_KIND in s
                        else real(s, *k, **kw))
    code, out = run(tmp_path, capsys, field="preferences", ids=(a,), apply=True)
    assert code == 2 and "differs" in out["error"], out
    assert row_text(pg, a) == before
    assert not (tmp_path / "b.json").exists()


BLANK_KIND = blank_field.BACKUP_KIND


def test_an_existing_backup_file_is_refused(pg, tmp_path, capsys):
    a = add(pg, "a")
    before = row_text(pg, a)
    (tmp_path / "b.json").write_text("keep me")
    code, out = run(tmp_path, capsys, field="preferences", ids=(a,), apply=True)
    assert code == 2 and "already exists" in out["error"], out
    assert (tmp_path / "b.json").read_text() == "keep me"
    assert row_text(pg, a) == before


def test_forgotten_missing_and_empty_ids_are_skipped_and_counted(pg, tmp_path, capsys):
    a, empty, gone = add(pg, "a"), add(pg, "e", prefs=(), merged=False), add(pg, "g")
    forget_row(pg, gone)
    untouched = {i: row_text(pg, i) for i in (empty, gone)}
    code, out = run(tmp_path, capsys, field="preferences", ids=(a, empty, gone, 999998), apply=True)
    assert code == 0 and out["changed"] == 1, out
    assert (out["skipped_missing"], out["skipped_forgotten"], out["skipped_empty"]) == (1, 1, 1), out
    assert {i: row_text(pg, i) for i in (empty, gone)} == untouched
    backup = json.loads((tmp_path / "b.json").read_text())
    assert [r["id"] for r in backup["rows"]] == [a]
    assert backup["field"] == "preferences" and backup["hub"] and backup["at"]


def test_a_second_apply_with_the_same_ids_is_a_no_op(pg, tmp_path, capsys):
    a = add(pg, "a")
    assert run(tmp_path, capsys, field="preferences", ids=(a,), apply=True)[1]["changed"] == 1
    after = row_text(pg, a)
    code, out = run(tmp_path, capsys, field="preferences", ids=(a,), backup="b2.json", apply=True)
    assert code == 0 and out["changed"] == 0 and out["skipped_empty"] == 1, out
    assert row_text(pg, a) == after
    assert not (tmp_path / "b2.json").exists()


# ---- apply: owner_lines -------------------------------------------------------------

def test_owner_lines_go_from_verbatim_and_raw_and_quotes_stay(pg, tmp_path, capsys):
    a = add(pg, "a")
    before = columns(pg, a)
    code, out = run(tmp_path, capsys, field="owner_lines", ids=(a,), apply=True)
    assert code == 0 and out["changed"] == 1 and out["strings"] == 2, out
    after = columns(pg, a)
    want_v = dict(before["verbatim"])
    del want_v["owner_lines"]
    assert after["verbatim"] == want_v and after["verbatim"]["quotes"] == [QUOTE]
    want_raw = copy.deepcopy(before["raw"])
    del want_raw["verbatim"]["owner_lines"]
    del want_raw["merged_from"][0]["payload"]["verbatim"]["owner_lines"]
    assert after["raw"] == want_raw
    assert after["preferences"] == before["preferences"]


def test_only_my_words_no_longer_finds_a_blanked_line_and_finds_an_untouched_one(pg, tmp_path, capsys,
                                                                                monkeypatch):
    monkeypatch.setattr(embed, "_cosine_candidates", lambda q, **kw: [])
    a, kept = add(pg, "a"), add(pg, "kept")
    found = {r["id"] for r in embed.hybrid_search("cobalt quarry gate", mode="literal",
                                                  owner_words_only=True)["results"]}
    assert {str(a), str(kept)} <= found, found
    assert run(tmp_path, capsys, field="owner_lines", ids=(a,), apply=True)[1]["changed"] == 1
    db._TABLE_COLUMNS_CACHE.clear()
    found = {r["id"] for r in embed.hybrid_search("cobalt quarry gate", mode="literal",
                                                  owner_words_only=True)["results"]}
    assert str(a) not in found and str(kept) in found, found


# ---- restore -------------------------------------------------------------------------

@pytest.mark.parametrize("field", blank_field.FIELDS)
def test_restore_puts_back_exact_values(pg, tmp_path, capsys, field):
    a, b = add(pg, "a"), add(pg, "b")
    before = {i: columns(pg, i) for i in (a, b)}
    assert run(tmp_path, capsys, field=field, ids=(a, b), apply=True)[1]["changed"] == 2
    code, out = run(tmp_path, capsys, restore="b.json", backup=None)
    assert code == 0 and out["dry_run"] is True and out["restorable"] == 2, out
    assert columns(pg, a) != before[a]
    code, out = run(tmp_path, capsys, restore="b.json", backup=None, apply=True)
    assert code == 0 and out["restored"] == 2, out
    assert {i: columns(pg, i) for i in (a, b)} == before


def test_restore_skips_a_row_changed_since(pg, tmp_path, capsys):
    a, b = add(pg, "a"), add(pg, "b")
    b_before = columns(pg, b)
    assert run(tmp_path, capsys, field="preferences", ids=(a, b), apply=True)[1]["changed"] == 2
    pg.execute("UPDATE episodes SET preferences = '[\"a new preference\"]'::jsonb WHERE id = %s", (a,))
    pg.commit()
    changed_a = row_text(pg, a)
    code, out = run(tmp_path, capsys, restore="b.json", backup=None, apply=True)
    assert code == 0 and out["restored"] == 1 and out["skipped_changed_since"] == 1, out
    assert row_text(pg, a) == changed_a
    assert columns(pg, b) == b_before


def test_restore_skips_a_row_forgotten_since(pg, tmp_path, capsys):
    a = add(pg, "a")
    assert run(tmp_path, capsys, field="preferences", ids=(a,), apply=True)[1]["changed"] == 1
    forget_row(pg, a)
    gone = row_text(pg, a)
    code, out = run(tmp_path, capsys, restore="b.json", backup=None, apply=True)
    assert code == 0 and out["restored"] == 0 and out["skipped_forgotten"] == 1, out
    assert row_text(pg, a) == gone


# ---- the replica ----------------------------------------------------------------------

def replica(sql, *args):
    con = sqlite3.connect(f"file:{hs.snapshot_path()}?mode=ro", uri=True)
    try:
        return con.execute(sql, args).fetchall()
    finally:
        con.close()


def test_the_next_pull_carries_the_change_to_the_replica(pg, tmp_path, capsys, monkeypatch):
    a, kept = add(pg, "a"), add(pg, "kept")
    assert hs.refresh()["ok"]
    hits = lambda word: {r[0] for r in replica(  # noqa: E731
        "SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH ?", f'"{word}"')}
    ids = lambda rows: {replica("SELECT id FROM episodes WHERE rowid = ?", r)[0][0] for r in rows}  # noqa: E731
    assert ids(hits("chartreuse heron")) == {a, kept}
    assert run(tmp_path, capsys, field="preferences", ids=(a,), apply=True)[1]["changed"] == 1
    assert run(tmp_path, capsys, field="owner_lines", ids=(a,), backup="b2.json", apply=True)[1]["changed"] == 1

    def no_dump():
        raise AssertionError("a full dump ran")

    monkeypatch.setattr(hs, "refresh", no_dump)
    out = hs.pull_changes()
    assert out["ok"] is True, out
    prefs, raw = replica("SELECT preferences, raw FROM episodes WHERE id = ?", a)[0]
    assert json.loads(prefs) == []
    for secret in (PREF, MERGED_PREF, LINE, MERGED_LINE):
        assert secret not in raw
    assert QUOTE in raw
    assert ids(hits("chartreuse heron")) == {kept}
    kept_raw = replica("SELECT raw FROM episodes WHERE id = ?", kept)[0][0]
    assert PREF in kept_raw and LINE in kept_raw


def test_restore_skips_a_row_whose_merged_source_was_forgotten_since(pg, tmp_path, capsys, monkeypatch):
    from khipu import tombstones

    a, b = add(pg, "a"), add(pg, "b", merged=False)
    b_before = columns(pg, b)
    assert run(tmp_path, capsys, field="preferences", ids=(a, b), apply=True)[1]["changed"] == 2
    blanked = row_text(pg, a)
    # This Mac's forget list now names the capture merged into a.
    monkeypatch.setattr(tombstones, "is_forgotten", lambda p: p.get("summary") == "merged")
    code, out = run(tmp_path, capsys, restore="b.json", backup=None, apply=True)
    assert code == 0 and out["restored"] == 1 and out["skipped_forgotten"] == 1, out
    assert row_text(pg, a) == blanked
    assert columns(pg, b) == b_before


# ---- round 2: 0047's gates, non-list values, output fields --------------------------

def with_fact_receipt(conn, eid):
    """Make the row's last merge receipt a capture that also held decisions
    (0047's writer gate watches the last receipt's payload)."""
    receipt = {"source_id": f"s-facts-{eid}", "ts": "2026-11-10T10:00:00Z", "score": 0.8000,
               "payload": {"summary": "merged with facts", "preferences": [MERGED_PREF],
                           "decisions": [{"text": "keep the kiln lit", "weight": 2.50}],
                           "verbatim": {"owner_lines": [MERGED_LINE]}}}
    text = json.dumps(receipt).replace('"weight": 2.5', '"weight": 2.50')
    cur = conn.cursor()
    announce(cur, f"s-facts-{eid}")
    from khipu import provenance
    provenance.declare_writer(cur)
    cur.execute("UPDATE episodes SET raw = jsonb_set(raw, '{merged_from}', (raw->'merged_from') "
                "|| jsonb_build_array(%s::jsonb)) WHERE id = %s", (text, eid))
    conn.commit()


@pytest.mark.parametrize("field", blank_field.FIELDS)
def test_a_last_receipt_with_decisions_commits_and_keeps_them(pg, tmp_path, capsys, field):
    a = add(pg, "a")
    with_fact_receipt(pg, a)
    before = columns(pg, a)
    code, out = run(tmp_path, capsys, field=field, ids=(a,), apply=True)
    assert code == 0 and out["changed"] == 1, out
    after = columns(pg, a)
    last = after["raw"]["merged_from"][-1]["payload"]
    assert last["decisions"] == before["raw"]["merged_from"][-1]["payload"]["decisions"]
    if field == "preferences":
        assert after["preferences"] == [] and after["raw"]["preferences"] == [] and last["preferences"] == []
    else:
        assert "owner_lines" not in after["verbatim"] and "owner_lines" not in last["verbatim"]
    code, out = run(tmp_path, capsys, restore="b.json", backup=None, apply=True)
    assert code == 0 and out["restored"] == 1, out
    assert columns(pg, a) == before


@pytest.mark.parametrize("receipt_value", ["list", "string"])
def test_a_row_whose_receipt_carries_a_forgotten_identity_is_skipped_and_the_rest_commit(
        pg, tmp_path, capsys, receipt_value):
    carrier, plain = add(pg, "carrier"), add(pg, "plain")
    if receipt_value == "string":
        # Emptying a bare string is a new receipt to 0047 (not contained in
        # the old one), so its update check would refuse the whole run.
        cur = pg.cursor()
        announce(cur, "s-carrier")
        cur.execute("UPDATE episodes SET raw = jsonb_set(raw, '{merged_from,0,payload,preferences}', "
                    "to_jsonb(%s::text)) WHERE id = %s", (MERGED_PREF, carrier))
        pg.commit()
    # A forget recorded before 0047 indexed this receipt (no carrier row stops it).
    pg.execute("SET LOCAL session_replication_role = replica")
    pg.execute("INSERT INTO forgotten_sources (identity) VALUES (%s)", ("s-carrier",))
    pg.commit()
    carrier_before = row_text(pg, carrier)
    code, out = run(tmp_path, capsys, field="preferences", ids=(carrier, plain))
    assert code == 0 and out["skipped_forgotten_source"] == 1, out
    code, out = run(tmp_path, capsys, field="preferences", ids=(carrier, plain), apply=True)
    assert code == 0 and out["changed"] == 1 and out["skipped_forgotten_source"] == 1, out
    assert row_text(pg, carrier) == carrier_before
    assert columns(pg, plain)["preferences"] == []
    backup = json.loads((tmp_path / "b.json").read_text())
    assert [r["id"] for r in backup["rows"]] == [plain]


def test_a_string_preferences_value_is_blanked_and_counted(pg, tmp_path, capsys):
    cur = pg.cursor()
    announce(cur)
    raw = {"ts": "2026-11-10T09:00:00Z", "summary": "scratch string", "preferences": PREF}
    cur.execute("INSERT INTO episodes (ts, session_id, summary, raw, preferences) "
                "VALUES (%s::timestamptz, 'codex:string', %s, %s::jsonb, %s::jsonb) RETURNING id",
                (raw["ts"], raw["summary"], json.dumps(raw), json.dumps(PREF)))
    eid = cur.fetchone()[0]
    pg.commit()
    code, out = run(tmp_path, capsys, field="preferences", ids=(eid,), apply=True)
    assert code == 0 and out["changed"] == 1 and out["strings"] == 1 and out["skipped_empty"] == 0, out
    after = columns(pg, eid)
    assert after["preferences"] == [] and after["raw"]["preferences"] == []


def test_output_reports_the_hub_check_and_repeated_ids(pg, tmp_path, capsys, monkeypatch):
    a, b = add(pg, "a"), add(pg, "b")
    code, out = run(tmp_path, capsys, field="preferences", ids=(a, a, b), apply=True)
    assert code == 0 and out["changed"] == 2, out
    assert (out["ids_given"], out["ids_distinct"], out["hub_check"]) == (3, 2, "ok"), out
    code, out = run(tmp_path, capsys, restore="b.json", backup=None)
    assert code == 0 and out["hub_check"] == "ok", out
    # A hub role that cannot read its incarnation: the wrong-hub guard is off and says so.
    monkeypatch.setattr(hs, "_INCARNATION_READ", "SELECT 1/0")
    code, out = run(tmp_path, capsys, restore="b.json", backup=None)
    assert code == 0 and out["hub_check"] == "skipped", out
    c = add(pg, "c")
    code, out = run(tmp_path, capsys, field="preferences", ids=(c,), backup="b2.json", apply=True)
    assert code == 0 and out["changed"] == 1 and out["hub_check"] == "skipped", out
