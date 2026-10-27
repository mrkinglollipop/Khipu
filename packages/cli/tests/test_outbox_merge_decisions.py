"""``merge-line-preferences --include-decisions``: the decisions a held line
carries that its hub episode lacks are stored through the capture's own
decision writer (``decisions.insert_decisions_from_episode``: a row each,
dedup, fact claims; then reversal detection), attached to the hub episode,
and appended to its decision list in column and raw. Undo retracts exactly
the rows it created and restores the list; a row changed since is refused.
Also the review fixes for ``--include-content`` (an alias folded only into
the row's tags, an empty hub scope, one bad line never stopping the rest)."""
from copy import deepcopy
import json
import re

import pytest

from khipu import db, decisions, outbox, outbox_preferences, provenance, tombstones
from tests.test_outbox_landed_lines import HUB, LINE, _queue, _writes, hub  # noqa: F401 — fixture
from tests.test_outbox_legacy_projection import world  # noqa: F401 — fixture
from tests.test_outbox_merge_content import _classify, _handles_content_sql
from tests.test_outbox_merge_preferences import _episode, _line, _md5, _run, _direction
from khipu import hygiene

DKIND = outbox_preferences.DECISIONS_KIND
_DECISION_COLUMNS = ("id", "project", "text", "episode_id", "session_id", "decided_at", "superseded_by",
                     "source_kind", "rationale", "evidence", "superseded_at", "supersede_source",
                     "supersede_reason", "retracted_at", "retract_reason")


def _handles_decisions_sql(cur):
    """The fake hub's decisions registry and fact claims, enough for the
    real decision writer, retract and the merge's own statements."""
    _handles_content_sql(cur)
    base = cur.execute
    cur.decisions = {}
    cur.fact_claims = []
    cur.next_decision = 500

    def rows(ids):
        return [(i, e.get("deleted_at"), e["raw"], e.get("preferences"), e.get("topics"), e.get("tags"),
                 e.get("decisions"), e.get("project"), e.get("session_id"), e["ts"])
                for i, e in sorted(cur.episodes.items()) if i in ids]

    def execute(sql, params=None):
        s = " ".join(sql.split())
        params = params or ()
        if s.startswith("SELECT column_name FROM information_schema.columns") and params == ("decisions",):
            cur.calls.append((s, params))
            cur._result = [(c,) for c in _DECISION_COLUMNS]
            return
        if s == "SELECT to_regclass('public.fact_claims') IS NOT NULL":
            cur._result = [(True,)]
            return
        if s.startswith("SELECT set_config(%s, 'on', true)"):
            cur._result = [("on",)]
            return
        if s.startswith("SELECT DISTINCT ON (v.i) v.i, e.id, e.deleted_at, e.raw, e.preferences, e.topics, "
                        "e.tags, e.decisions"):
            cur.calls.append((s, params))
            out = []
            for i, (ts, md) in enumerate(zip(*params), start=1):
                hits = sorted(((e.get("deleted_at") is not None, -eid), eid) for eid, e in cur.episodes.items()
                              if e["ts"] == ts and _md5(e["summary"]) == md)
                if hits:
                    out.append((i, *rows({hits[0][1]})[0][:7]))
            cur._result = out
            return
        if s.startswith("SELECT id, deleted_at, raw, preferences, topics, tags, decisions, project, session_id, ts "
                        "FROM episodes WHERE id = ANY"):
            cur.calls.append((s, params))
            assert s.endswith("ORDER BY id FOR UPDATE")
            cur._result = rows(set(params[0]))
            return
        m = re.match(r"UPDATE episodes SET decisions = %s::jsonb, raw = ", s)
        if m:
            cur.calls.append((s, params))
            e = cur.episodes[params[-1]]
            assert s.endswith("WHERE id = %s AND deleted_at IS NULL")
            if e.get("deleted_at") is not None:
                return
            e["decisions"] = None if params[0] is None else json.loads(params[0])
            e["raw"] = dict(e["raw"])
            if "jsonb_set" in s:
                e["raw"]["decisions"] = json.loads(params[1])
            else:
                e["raw"].pop("decisions", None)
            return
        if s.startswith("SELECT id FROM decisions WHERE project"):
            project, norm = params[0], params[1]
            hits = sorted((d for d in cur.decisions.values()
                           if d["project"] == project and decisions.normalize_text(d["text"]) == norm
                           and d["superseded_by"] is None and d["retracted_at"] is None),
                          key=lambda d: -d["id"])
            cur._result = [(hits[0]["id"],)] if hits else []
            return
        if s.startswith("SELECT id, project, superseded_by FROM decisions WHERE id = %s FOR UPDATE"):
            d = cur.decisions.get(params[0])
            cur._result = [(d["id"], d["project"], d["superseded_by"])] if d else []
            return
        if s.startswith("INSERT INTO decisions (project, text, episode_id, session_id, decided_at)"):
            cur.calls.append((s, params))
            project, text, eid, session_id, decided_at = params
            did = cur.next_decision
            cur.next_decision += 1
            cur.decisions[did] = {"id": did, "project": project, "text": text, "episode_id": eid,
                                  "session_id": session_id, "decided_at": decided_at, "superseded_by": None,
                                  "retracted_at": None, "retract_reason": None}
            cur.rowcount = 1
            cur._result = [(did,)]
            return
        if s.startswith("INSERT INTO fact_claims"):
            kind, fact_id, field, value, claimant, eid = params
            row = (kind, fact_id, field, value, claimant, eid)
            cur.rowcount = 0 if row in cur.fact_claims else 1
            if cur.rowcount:
                cur.fact_claims.append(row)
            return
        if s.startswith("SELECT khipu_project_fact(%s, %s)"):
            cur._result = [("projected",)]
            return
        if s == " ".join(outbox_preferences._DECISION_IDS_SQL.split()):
            cur.calls.append((s, params))
            cur._result = [(d["id"], d["text"]) for d in sorted(cur.decisions.values(), key=lambda d: d["id"])
                           if d["episode_id"] == params[0]]
            return
        if s == " ".join(outbox_preferences._DECISION_ROWS_SQL.split()):
            cur.calls.append((s, params))
            cur._result = [(d["id"], d["text"], d["episode_id"], d["superseded_by"], d["retracted_at"])
                           for i, d in sorted(cur.decisions.items()) if i in params[0]]
            return
        if s.startswith("SELECT DISTINCT fact_id FROM fact_claims WHERE kind = 'decision'"):
            ids, eid = params
            cur._result = sorted({(c[1],) for c in cur.fact_claims if c[0] == "decision" and c[1] in ids
                                  and (c[4] != "episode" or c[5] != eid)})
            return
        if s.startswith("UPDATE decisions SET retracted_at = now(), retract_reason = %s WHERE id = %s"):
            cur.calls.append((s, params))
            d = cur.decisions.get(params[1])
            cur.rowcount = 0
            if d and d["retracted_at"] is None:
                d["retracted_at"], d["retract_reason"] = "now", params[0]
                cur.rowcount = 1
            return
        return base(sql, params)

    cur.execute = execute


LINE_DECISIONS = LINE["decisions"]


@pytest.fixture
def held(hub, monkeypatch):  # noqa: F811
    monkeypatch.setattr(db, "_TABLE_COLUMNS_CACHE", {})
    monkeypatch.setattr(provenance, "_READY", False)
    monkeypatch.setattr("khipu.identity.project_group", lambda _cur, p: [p])
    monkeypatch.setattr(hygiene, "classify_topics", _classify)
    _handles_decisions_sql(hub)
    hub.episodes[1]["summary"] = hub.episodes[1]["raw"]["summary"] = "Synthetic episode 1"
    for eid in range(2, 9):
        _episode(hub, eid)
    for e in hub.episodes.values():
        e["project"] = "khipu"
    hub.episodes[7]["deleted_at"] = "2026-10-20T00:00:00Z"
    hub._existing_keys = {(e["ts"], _md5(e["summary"])) for e in hub.episodes.values()}
    hub.next_id = 100
    # A decision another episode already put on file: the writer dedups onto it.
    hub.decisions[400] = {"id": 400, "project": "khipu", "text": "Already decided elsewhere", "episode_id": 1,
                          "session_id": "s", "decided_at": HUB["ts"], "superseded_by": None,
                          "retracted_at": None, "retract_reason": None}
    hub.fact_claims.append(("decision", 400, "text", "Already decided elsewhere", "episode", 1))
    paths = {
        # A new decision first: no exact append lands it, the decision rule does.
        "new": _queue(_line(2, decisions=["Ship the import", *LINE_DECISIONS])),
        "dedup": _queue(_line(3, decisions=["Already decided elsewhere", *LINE_DECISIONS])),
        "with_topic": _queue(_line(4, topics=["outbox", "graph"],
                                   decisions=["Two decisions, one line", "A third", *LINE_DECISIONS])),
        "subset": _queue(_line(5, decisions=["Keep the hub"])),
        "other": _queue(_line(6, people=["Someone the hub lacks"], decisions=["Never imported"])),
        "deleted": _queue(_line(7, decisions=["On a forgotten episode"])),
        "hidden": _queue(_line(8, decisions=["Forgotten under the lock"])),
    }
    return hub, paths


def _records():
    return {json.loads(p.read_text())["resolution"]["episode_id"]: json.loads(p.read_text())
            for p in outbox.resolved_dir().glob("*.json")}


def _hide_under_lock(cur, monkeypatch, eid):
    """Episode ``eid`` is forgotten on this Mac after planning, before its row lock."""
    hidden = set()
    real = tombstones.episode_hidden
    monkeypatch.setattr(tombstones, "episode_hidden", lambda i: int(i) in hidden or real(i))
    execute = cur.execute

    def spy(sql, params=None):
        if " ".join(sql.split()).startswith("SELECT id, deleted_at, raw, preferences, topics, tags, decisions"):
            hidden.add(eid)
        return execute(sql, params)

    cur.execute = spy


def test_dry_run_counts_decisions_and_writes_nothing(held):
    cur, _paths = held
    before, rows_before = deepcopy(cur.episodes), deepcopy(cur.decisions)
    rc, out = _run("--include-content")
    assert rc == 0 and out["needs_review_decisions_episode_ids"] == [2, 3, 4, 5, 6, 8], out
    rc, out = _run("--include-decisions")
    assert rc == 0 and out["dry_run"] is True, out
    assert out[DKIND] == 5 and out["needs_review"] == 1 and out["forgotten"] == 1, out
    assert out["needs_review_episode_ids"] == [6] and out["needs_review_decisions_episode_ids"] == [6], out
    # Ship the import; Already decided elsewhere; two on 4; 8's (forgotten only under the lock).
    assert out["decisions_to_add"] == 5 and out["decisions_episode_ids"] == [2, 3, 4, 8], out
    assert out["topics_to_add"] == 1, out
    assert "Ship the import" not in json.dumps(out)
    assert cur.episodes == before and cur.decisions == rows_before and not _writes(cur)


def test_apply_needs_the_owner_and_changes_nothing_without_it(held, monkeypatch):
    cur, paths = held
    before, rows_before = deepcopy(cur.episodes), deepcopy(cur.decisions)
    rc, out = _run("--include-decisions", "--apply")
    assert rc == 2 and out["code"] == "owner_only", out
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-nobody-typed-here")
    rc, out = _run("--include-decisions", "--apply", "--owner-direction")
    assert rc == 2 and out["ok"] is False, out
    assert cur.episodes == before and cur.decisions == rows_before and not _writes(cur)
    assert all(p.exists() for p in paths.values())


def test_apply_stores_rows_through_the_writer_appends_and_archives(held, tmp_path, monkeypatch):
    cur, paths = held
    before = deepcopy(cur.episodes)
    jobs_before = {k: json.loads(p.read_text()) for k, p in paths.items()}
    calls = {"insert": [], "detect": []}
    real_insert, real_detect = decisions.insert_decisions_from_episode, decisions.detect_reversals_from_episode
    monkeypatch.setattr(decisions, "insert_decisions_from_episode",
                        lambda c, p, e: calls["insert"].append((deepcopy(p), e)) or real_insert(c, p, e))
    monkeypatch.setattr(decisions, "detect_reversals_from_episode",
                        lambda c, p, e: calls["detect"].append(e) or real_detect(c, p, e))
    _hide_under_lock(cur, monkeypatch, 8)
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-decisions", "--apply", "--owner-direction")
    assert rc == 0 and out["episode_ids"] == [2, 3, 4], out
    # The normal writer, once per episode, with the episode's own project, session and ts.
    assert [e for _p, e in calls["insert"]] == [2, 3, 4] and calls["detect"] == [2, 3, 4]
    payload, _eid = calls["insert"][0]
    assert payload == {"decisions": ["Ship the import"], "project": "khipu",
                       "session_id": HUB["session_id"], "ts": HUB["ts"]}
    new = {d["text"]: d for d in cur.decisions.values() if d["id"] != 400}
    assert sorted(new) == ["A third", "Ship the import", "Two decisions, one line"]
    assert new["Ship the import"]["episode_id"] == 2 and new["A third"]["episode_id"] == 4
    assert out["decision_ids_created"] == sorted(d["id"] for d in new.values()), out
    assert out["decisions_added"] == 4 and out["decisions_deduped"] == 1, out
    # Each created row carries this episode's fact claim; the dedup is a claim on 400, no new row.
    assert ("decision", new["Ship the import"]["id"], "text", "Ship the import", "episode", 2) in cur.fact_claims
    assert ("decision", 400, "text", "Already decided elsewhere", "episode", 3) in cur.fact_claims
    assert cur.decisions[400]["retracted_at"] is None
    # Appended to the episode's list in column and raw, after what it held.
    for eid, added in ((2, ["Ship the import"]), (3, ["Already decided elsewhere"]),
                       (4, ["Two decisions, one line", "A third"])):
        assert cur.episodes[eid]["raw"]["decisions"] == HUB["decisions"] + added, eid
        assert cur.episodes[eid]["decisions"] == HUB["decisions"] + added, eid
    assert cur.episodes[4]["raw"]["topics"] == ["outbox", "graph"]
    for eid in (5, 6, 7, 8):
        assert cur.episodes[eid] == before[eid], eid
    assert not any(d["episode_id"] in (7, 8) for d in cur.decisions.values())
    # The log holds the texts and created ids, written before the commit.
    from khipu import owner_direction

    [entry2] = [e for e in owner_direction.preference_merges(out["owner_action"]) if e["episode_id"] == 2]
    spec = entry2["content"]["decisions"]
    assert spec["added"] == ["Ship the import"] and spec["created"] == [
        {"id": new["Ship the import"]["id"], "text": "Ship the import"}] and spec["deduped"] == []
    [entry3] = [e for e in owner_direction.preference_merges(out["owner_action"]) if e["episode_id"] == 3]
    assert entry3["content"]["decisions"]["created"] == []
    assert entry3["content"]["decisions"]["deduped"] == ["Already decided elsewhere"]
    # Every line the row now holds is archived with a validated kind; the rest stay held.
    records = _records()
    assert sorted(records) == [2, 3, 4, 5], records.keys()
    for eid in (2, 3, 4, 5):
        outbox._validate_resolution(records[eid])
        assert records[eid]["resolution"]["kind"] == DKIND, eid
        assert records[eid]["payload"] == cur.episodes[eid]["raw"], eid
    assert out["lines_resolved"] == 4 and "lines_failed" not in out, out
    for key in ("other", "deleted", "hidden"):
        assert json.loads(paths[key].read_text()) == jobs_before[key], key


def test_a_second_apply_adds_nothing(held, tmp_path, monkeypatch):
    cur, _paths = held
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-decisions", "--apply", "--owner-direction")
    assert rc == 0, out
    after, rows_after = deepcopy(cur.episodes), deepcopy(cur.decisions)
    rc, again = _run("--include-decisions", "--apply", "--owner-direction")
    assert rc == 0 and again["episodes_merged"] == 0 and again["decision_ids_created"] == [], again
    assert cur.episodes == after and cur.decisions == rows_after


def test_undo_retracts_only_created_rows_and_restores_the_lists(held, tmp_path, monkeypatch):
    cur, paths = held
    before = deepcopy(cur.episodes)
    jobs_before = {k: p.read_bytes() for k, p in paths.items()}
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-decisions", "--apply", "--owner-direction")
    assert rc == 0, out
    created = out["decision_ids_created"]
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    # 2, 3, 4 and 8 (nothing hides 8 in this run).
    assert out["episode_ids"] == [2, 3, 4, 8], out
    assert rc == 0 and undone["restored"] == 4 and undone["lines_unresolved"] == 5, undone
    assert undone["decisions_retracted"] == created and undone["decisions_changed"] == [], undone
    for did in created:
        assert cur.decisions[did]["retracted_at"] is not None
        assert out["owner_action"] in cur.decisions[did]["retract_reason"]
    # The deduped-onto decision is never retracted.
    assert cur.decisions[400]["retracted_at"] is None
    assert cur.episodes == before
    assert {k: p.read_bytes() for k, p in paths.items()} == jobs_before


@pytest.mark.parametrize("change", ["superseded", "retracted", "claimed"])
def test_undo_refuses_an_episode_whose_created_row_changed(held, tmp_path, monkeypatch, change):
    cur, _paths = held
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-decisions", "--apply", "--owner-direction")
    assert rc == 0, out
    [ship] = [d for d in cur.decisions.values() if d["text"] == "Ship the import"]
    if change == "superseded":
        ship["superseded_by"] = 400
    elif change == "retracted":
        ship["retracted_at"], ship["retract_reason"] = "earlier", "someone else"
    else:
        cur.fact_claims.append(("decision", ship["id"], "text", "Ship the import", "owner", None))
    episode2, ship_before = deepcopy(cur.episodes[2]), deepcopy(ship)
    rc, undone = _run("--undo", out["owner_action"], "--owner-direction")
    assert rc == 0 and undone["left_alone"] == [2] and undone["restored"] == 3, undone
    assert undone["decisions_changed"] == [{"episode_id": 2, "decision_ids": [ship["id"]]}], undone
    assert cur.episodes[2] == episode2 and cur.decisions[ship["id"]] == ship_before
    others = [d for d in cur.decisions.values() if d["episode_id"] == 4]
    assert others and all(d["retracted_at"] is not None for d in others)
    assert undone["decisions_retracted"] == [i for i in out["decision_ids_created"] if i != ship["id"]]


def test_a_failed_writer_skips_the_episode_and_keeps_its_lines(held, tmp_path, monkeypatch):
    cur, paths = held
    real = decisions.insert_decisions_from_episode

    def flaky(c, payload, eid):
        if eid == 2:
            raise RuntimeError("hub said no")
        return real(c, payload, eid)

    monkeypatch.setattr(decisions, "insert_decisions_from_episode", flaky)
    before2 = deepcopy(cur.episodes[2])
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-decisions", "--apply", "--owner-direction")
    assert rc == 0 and out["decisions_failed_episode_ids"] == [2] and 2 not in out["episode_ids"], out
    assert cur.episodes[2] == before2 and paths["new"].exists()
    assert 3 in out["episode_ids"] and not paths["dedup"].exists()


@pytest.mark.parametrize("tamper", [
    lambda r: r["payload"].update(decisions=HUB["decisions"]),
    lambda r: r["resolution"].update(kind=outbox_preferences.CONTENT_KIND),
    lambda r: r["resolution"].update(scope_kept="yes"),
])
def test_archive_validation_re_checks_the_decision_proof(held, tmp_path, monkeypatch, tamper):
    from khipu.capture_identity import payload_sha256

    _cur, _paths = held
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-decisions", "--apply", "--owner-direction")
    assert rc == 0, out
    bad = deepcopy(_records()[2])
    tamper(bad)
    bad["resolution"]["original_payload_sha256"] = payload_sha256(bad["payload"])
    with pytest.raises(ValueError):
        outbox._validate_resolution(bad)


def test_decisions_missing_reads_the_legacy_comma_join():
    raw = {"decisions": ["Keep the hub, archive the line, never merge"]}
    assert outbox_preferences.decisions_missing(raw, {"decisions": ["archive the line", "never merge"]}) == []
    assert outbox_preferences.decisions_missing(raw, {"decisions": ["never merge, Keep the hub"]}) == \
        ["never merge, Keep the hub"]
    assert outbox_preferences.decisions_missing(raw, {"decisions": [3]}) is None
    assert outbox_preferences.decisions_missing({"decisions": "x"}, {"decisions": ["a"]}) is None


# --- review fixes for --include-content (ec7c01da) -------------------------------------------


@pytest.fixture
def tricky(hub, monkeypatch):  # noqa: F811
    monkeypatch.setattr(hygiene, "classify_topics", _classify)
    _handles_content_sql(hub)
    hub.episodes[1]["summary"] = hub.episodes[1]["raw"]["summary"] = "Synthetic episode 1"
    _episode(hub, 2, tags=["graph"])           # the alias's canonical only in tags
    _episode(hub, 3, scope="")                 # an empty hub scope
    _episode(hub, 4)                           # an ordinary topic to append
    _episode(hub, 5)
    hub._existing_keys = {(e["ts"], _md5(e["summary"])) for e in hub.episodes.values()}
    hub.next_id = 100
    paths = {
        "alias_in_tags": _queue(_line(2, topics=["outbox", "graphs"])),
        "empty_scope": _queue(_line(3, scope="aegis", topics=["outbox", "graph"])),
        "four": _queue(_line(4, topics=["outbox", "graph"], preferences=["One new preference"])),
        "five": _queue(_line(5, topics=["outbox", "graph"], preferences=["Another new preference"])),
    }
    return hub, paths


def test_lines_whose_proof_would_fail_stay_held_and_the_apply_finishes(tricky, tmp_path, monkeypatch):
    cur, paths = tricky
    before = deepcopy(cur.episodes)
    rc, dry = _run("--include-content")
    assert rc == 0 and dry["needs_review_episode_ids"] == [2, 3], dry
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-content", "--apply", "--owner-direction")
    assert rc == 0 and out["ok"] is True and out["episode_ids"] == [4, 5], out
    assert out["lines_resolved"] == 2 and "lines_failed" not in out, out
    for eid in (2, 3):
        assert cur.episodes[eid] == before[eid], eid
    assert paths["alias_in_tags"].exists() and paths["empty_scope"].exists()
    for record in _records().values():
        outbox._validate_resolution(record)


def test_one_bad_line_never_stops_later_batches(tricky, tmp_path, monkeypatch):
    cur, paths = tricky
    monkeypatch.setattr(outbox_preferences, "BATCH", 1)
    real = outbox.archive_resolved

    def archive(path, snapshot, record):
        if record["resolution"]["episode_id"] == 4:
            raise ValueError("resolution queued envelope is invalid")
        return real(path, snapshot, record)

    monkeypatch.setattr(outbox, "archive_resolved", archive)
    _direction(tmp_path, monkeypatch)
    rc, out = _run("--include-content", "--apply", "--owner-direction")
    assert rc == 0 and out["ok"] is True and out["episode_ids"] == [4, 5], out
    assert [f["episode_id"] for f in out["lines_failed"]] == [4], out
    assert out["lines_resolved"] == 1 and paths["four"].exists() and not paths["five"].exists()
    assert cur.episodes[5]["raw"]["preferences"][-1] == "Another new preference"
