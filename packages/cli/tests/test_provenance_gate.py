"""Fact provenance, phase 1: migration 0047, its gates and the projector.

Scope: docs/plans/2026-10-23-fact-provenance.md, section 8 phase 1. Covers
I6 (a direct SQL wording write, a simulated old dedup and an old forget are
each refused at COMMIT), the 08K01 mapping section 5.2 relies on, the legacy
classification table of section 4, the projection rules of section 3.2 at
the SQL level, the 0045/0046 refusal, and a pin of the single-writer
inventory section 4's proof rests on.

Every database here is a hermetic one (tests/hermetic_pg.py).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from khipu import hub_errors, provenance
from tests.hermetic_pg import chain_dir, hub, pg_cluster  # noqa: F401
from tests.fact_seed import announce

PACKAGE = Path(__file__).resolve().parents[1] / "khipu"
REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "ops" / "migrations"
BEFORE = "0044"  # the last migration main ships before 0047
GATE_TRIGGERS = {
    "khipu_commitments_fact_insert", "khipu_commitments_fact_update",
    "khipu_decisions_fact_insert", "khipu_decisions_fact_update",
    "khipu_deliverables_fact_insert", "khipu_deliverables_fact_update",
    "khipu_episodes_forget_gate", "khipu_fact_claims_gate",
}


@pytest.fixture(autouse=True)
def _fresh_ready_cache():
    provenance._reset_ready_cache()
    yield
    provenance._reset_ready_cache()


@pytest.fixture
def pre(hub, tmp_path):  # noqa: F811
    """A hub at main's schema (through 0044), before 0047."""
    result = hub.migrate(directory=chain_dir(tmp_path, BEFORE))
    assert result["ran"][-1].startswith(BEFORE), result
    return hub


def _migrate_0047(hub):  # noqa: F811
    from khipu import db

    db._TABLE_COLUMNS_CACHE.clear()
    result = hub.migrate()
    assert result["ran"] == ["0047_fact_provenance", "0049_commitment_origin_requests"], result
    return result


def _episode(cur, summary="ep", *, forgotten=False) -> int:
    announce(cur)
    cur.execute("INSERT INTO episodes (ts, summary) VALUES (now(), %s) RETURNING id", (summary,))
    eid = cur.fetchone()[0]
    if forgotten:
        cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (eid,))
    return eid


def _commitment(cur, text, episode, *, project="khipu", prs=None, latest=None, refs=None,
                status="open", close_reason=None, h=None) -> int:
    check = json.dumps({"refs": refs}) if refs is not None else None
    cur.execute(
        "INSERT INTO commitments (text, project, opened_episode, content_hash, prs, latest_text, "
        "pr_check, status, close_reason) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s) RETURNING id",
        (text, project, episode, h or f"h-{text}", prs, latest, check, status, close_reason))
    return cur.fetchone()[0]


@pytest.fixture
def migrated(pre):
    """A hub just migrated to 0047, with one live episode-anchored fact of
    each kind (ids in ``.ids``)."""
    conn = pre.connect()
    with conn.cursor() as cur:
        a = _episode(cur, "anchor")
        b = _episode(cur, "spare")
        c = _commitment(cur, "ship the gate", a)
        cur.execute("INSERT INTO decisions (project, text, episode_id) VALUES ('khipu', 'use claims', %s) "
                    "RETURNING id", (a,))
        d = cur.fetchone()[0]
        cur.execute("INSERT INTO deliverables (project, kind, path, title, episode_id) "
                    "VALUES ('khipu', 'file', 'a.py', 'the file', %s) RETURNING id", (a,))
        v = cur.fetchone()[0]
    conn.commit()
    _migrate_0047(pre)
    pre.ids = {"a": a, "b": b, "c": c, "d": d, "v": v}
    pre.conn = conn
    return pre


def _refused(conn, statements, params=()):
    """Run ``statements`` in one transaction and return the COMMIT error."""
    import psycopg

    with conn.cursor() as cur:
        for sql, p in statements:
            cur.execute(sql, p)
    with pytest.raises(psycopg.Error) as caught:
        conn.commit()
    conn.rollback()
    return caught.value


# ---- I6: wording changes only through the projector --------------------------


def test_08K01_is_plain_operational_error_and_old_code_reads_it_as_hub_away(migrated):
    """Section 5.2's stop condition: an old outbox keeps a refused capture
    queued only if psycopg raises 08K01 as plain OperationalError and
    hub_errors.unreachable_error (unchanged from main) says True."""
    import psycopg

    conn = migrated.conn
    exc = _refused(conn, [("UPDATE commitments SET latest_text = 'leaked' WHERE id = %s",
                           (migrated.ids["c"],))])
    assert type(exc) is psycopg.OperationalError, type(exc)
    assert exc.sqlstate == provenance.OLD_WRITER_SQLSTATE
    assert provenance.OLD_WRITER_MESSAGE in str(exc)
    assert hub_errors.unreachable_error(exc) is True
    assert provenance.is_old_writer_refusal(exc)
    with conn.cursor() as cur:
        cur.execute("SELECT latest_text FROM commitments WHERE id = %s", (migrated.ids["c"],))
        assert cur.fetchone()[0] is None


@pytest.mark.parametrize("sql", [
    "UPDATE commitments SET text = 'other words' WHERE id = %(c)s",
    "UPDATE commitments SET latest_text = 'other words' WHERE id = %(c)s",
    "UPDATE commitments SET prs = '{7}' WHERE id = %(c)s",
    "UPDATE commitments SET pr_check = '{\"refs\": [\"a/b#7\"]}' WHERE id = %(c)s",
    "UPDATE commitments SET opened_episode = %(b)s WHERE id = %(c)s",
    "UPDATE decisions SET text = 'other words' WHERE id = %(d)s",
    "UPDATE decisions SET rationale = 'because' WHERE id = %(d)s",
    "UPDATE decisions SET evidence = '{\"x\": 1}' WHERE id = %(d)s",
    "UPDATE decisions SET episode_id = %(b)s WHERE id = %(d)s",
    "UPDATE deliverables SET title = 'other title' WHERE id = %(v)s",
    "UPDATE deliverables SET episode_id = %(b)s WHERE id = %(v)s",
    "INSERT INTO commitments (text, project, opened_episode, content_hash) VALUES ('new', 'khipu', %(b)s, 'hx')",
    "INSERT INTO decisions (project, text, episode_id) VALUES ('khipu', 'new', %(b)s)",
    "INSERT INTO decisions (project, text) VALUES ('khipu', 'an old create_decision')",
    "INSERT INTO deliverables (project, kind, path, episode_id) VALUES ('khipu', 'file', 'b.py', %(b)s)",
])
def test_a_direct_wording_write_is_refused_at_commit(migrated, sql):
    exc = _refused(migrated.conn, [(sql, migrated.ids)])
    assert getattr(exc, "sqlstate", None) == "08K01", exc


def test_an_old_dedup_is_refused_whole_savepoints_included(migrated):
    """main's dedup (commitments.py open_from_episode): touch seen, overwrite
    latest_text, union prs, keep refs, inside the capture's fail-open
    savepoint, after the episode insert. COMMIT refuses all of it, so no
    episode lands without its claims."""
    conn = migrated.conn
    c = migrated.ids["c"]
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episodes")
        before = cur.fetchone()[0]
        _episode(cur, "the restater")
        cur.execute("SAVEPOINT commitments_step")
        cur.execute("UPDATE commitments SET last_seen_at = now(), seen_count = seen_count + 1 WHERE id = %s", (c,))
        cur.execute("UPDATE commitments SET latest_text = %s WHERE id = %s", ("ship the gate today", c))
        cur.execute("UPDATE commitments SET prs = ARRAY(SELECT DISTINCT n FROM "
                    "unnest(COALESCE(prs, '{}'::int[]) || %s::int[]) AS n ORDER BY n) WHERE id = %s", ([41], c))
        cur.execute("UPDATE commitments SET pr_check = %s::jsonb WHERE id = %s",
                    (json.dumps({"refs": ["acme/app#41"]}), c))
        cur.execute("RELEASE SAVEPOINT commitments_step")
    import psycopg

    with pytest.raises(psycopg.OperationalError) as caught:
        conn.commit()
    assert caught.value.sqlstate == "08K01"
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM episodes")
        assert cur.fetchone()[0] == before
        cur.execute("SELECT latest_text, prs, pr_check, seen_count FROM commitments WHERE id = %s", (c,))
        assert cur.fetchone() == (None, [], None, 1)


def test_an_old_forget_of_a_claimed_episode_is_refused(migrated):
    """main's forget_episode: tombstone, then close/retract/delete by anchor.
    The forget gate refuses it at COMMIT while claims still name the episode."""
    a = migrated.ids["a"]
    exc = _refused(migrated.conn, [
        ("UPDATE episodes SET deleted_at = now() WHERE id = %s AND deleted_at IS NULL", (a,)),
        ("UPDATE commitments SET status = 'closed', closed_at = now(), close_reason = 'forgotten' "
         "WHERE opened_episode = %s AND status IN ('open', 'stale')", (a,)),
        ("DELETE FROM deliverables WHERE episode_id = %s", (a,)),
        ("UPDATE decisions SET retracted_at = now(), retract_reason = 'forgotten' "
         "WHERE episode_id = %s AND retracted_at IS NULL", (a,)),
    ])
    assert exc.sqlstate == "08K01"
    with migrated.conn.cursor() as cur:
        cur.execute("SELECT deleted_at FROM episodes WHERE id = %s", (a,))
        assert cur.fetchone()[0] is None
        cur.execute("SELECT status FROM commitments WHERE id = %s", (migrated.ids["c"],))
        assert cur.fetchone()[0] == "open"


def test_forgetting_an_episode_with_no_claims_still_works(migrated):
    conn = migrated.conn
    with conn.cursor() as cur:
        cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (migrated.ids["b"],))
    conn.commit()


def test_lifecycle_and_owner_writes_touch_no_wording_and_pass(migrated):
    """Close, reopen, snooze, reassign, seen counts, a close-rule pr_check
    note that keeps refs, supersede and retract: none is gated."""
    conn = migrated.conn
    ids = migrated.ids
    with conn.cursor() as cur:
        cur.execute("UPDATE commitments SET last_seen_at = now(), seen_count = seen_count + 1 WHERE id = %s",
                    (ids["c"],))
        cur.execute("UPDATE commitments SET owner = 'agent', owner_reassigned_at = now() WHERE id = %s",
                    (ids["c"],))
        cur.execute("UPDATE commitments SET due_after = now() + interval '1 day' WHERE id = %s", (ids["c"],))
        cur.execute("UPDATE commitments SET pr_check = %s::jsonb WHERE id = %s",
                    (json.dumps({"checked_at": "2026-10-23", "waiting": {}}), ids["c"]))
        cur.execute("UPDATE commitments SET status = 'closed', closed_at = now(), close_reason = 'done' "
                    "WHERE id = %s", (ids["c"],))
        cur.execute("UPDATE commitments SET status = 'open', closed_at = NULL WHERE id = %s", (ids["c"],))
        cur.execute("UPDATE decisions SET retracted_at = now(), retract_reason = 'wrong' WHERE id = %s",
                    (ids["d"],))
    conn.commit()


def test_the_projector_path_commits(migrated):
    """New code: insert, claim, project, in one transaction."""
    conn = migrated.conn
    b = migrated.ids["b"]
    with conn.cursor() as cur:
        cid = _commitment(cur, "review PR #77", b, prs=[77])
        assert provenance.record_claim(cur, "commitment", cid, "text", "review PR #77", episode_id=b)
        assert provenance.record_claim(cur, "commitment", cid, "pr", 77, episode_id=b)
        assert not provenance.record_claim(cur, "commitment", cid, "pr", 77, episode_id=b)  # replay
        assert provenance.project(cur, "commitment", cid) == "projected"
        cur.execute("INSERT INTO decisions (project, text, episode_id) VALUES ('khipu', 'go', %s) RETURNING id",
                    (b,))
        did = cur.fetchone()[0]
        provenance.record_claim(cur, "decision", did, "text", "go", episode_id=b)
        provenance.record_claim(cur, "decision", did, "evidence", {"episode_id": b}, episode_id=b)
        assert provenance.project(cur, "decision", did) == "projected"
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT text, prs, opened_episode FROM commitments WHERE id = %s", (cid,))
        assert cur.fetchone() == ("review PR #77", [77], b)
        cur.execute("SELECT evidence FROM decisions WHERE id = %s", (did,))
        assert cur.fetchone()[0] == {"episode_id": b}


def test_a_claim_for_a_forgotten_episode_is_refused(migrated):
    import psycopg

    conn = migrated.conn
    with conn.cursor() as cur:
        gone = _episode(cur, "forgotten", forgotten=True)
    conn.commit()
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            provenance.record_claim(cur, "commitment", migrated.ids["c"], "restatement", "x", episode_id=gone)
    conn.rollback()


def test_record_claim_refuses_a_bad_field_or_claimant():
    with pytest.raises(ValueError):
        provenance.record_claim(None, "commitment", 1, "title", "x", episode_id=1)
    with pytest.raises(ValueError):
        provenance.record_claim(None, "decision", 1, "text", "x", episode_id=None, claimant="episode")
    with pytest.raises(ValueError):
        provenance.record_claim(None, "fact", 1, "text", "x")


def test_ready_caches_only_a_yes(pre):
    conn = pre.connect()
    with conn.cursor() as cur:
        assert provenance.ready(cur) is False
        assert provenance.ready(cur) is False
    conn.rollback()
    _migrate_0047(pre)
    with conn.cursor() as cur:
        assert provenance.ready(cur) is True
        cur.execute("SELECT 1")  # the yes is cached: no further catalog read is needed
        assert provenance.ready(cur) is True


# ---- Projection rules (section 3.2), at the SQL level ------------------------


def test_restatements_anchor_moves_and_retirement(migrated):
    """A restater supplies latest_text and a PR; when its claims go, its
    wording goes; when the anchor's claims go and the restater's stay, the
    anchor moves to the restater and the text is the restater's own words;
    with no claim left the fact retires as forget does."""
    conn = migrated.conn
    a, c = migrated.ids["a"], migrated.ids["c"]
    with conn.cursor() as cur:
        r = _episode(cur, "restater")
        provenance.record_claim(cur, "commitment", c, "restatement", "ship the gate via PR #9", episode_id=r)
        provenance.record_claim(cur, "commitment", c, "pr", 9, episode_id=r)
        assert provenance.project(cur, "commitment", c) == "projected"
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT text, latest_text, prs, opened_episode FROM commitments WHERE id = %s", (c,))
        assert cur.fetchone() == ("ship the gate", "ship the gate via PR #9", [9], a)
        # The anchor's claims go (what the new forget will do before its tombstone).
        cur.execute("DELETE FROM fact_claims WHERE episode_id = %s AND kind = 'commitment'", (a,))
        assert provenance.project(cur, "commitment", c) == "projected"
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT text, latest_text, prs, opened_episode, status FROM commitments WHERE id = %s", (c,))
        assert cur.fetchone() == ("ship the gate via PR #9", None, [9], r, "open")
        cur.execute("DELETE FROM fact_claims WHERE episode_id = %s", (r,))
        assert provenance.project(cur, "commitment", c) == "retired"
        cur.execute("UPDATE episodes SET deleted_at = now() WHERE id = %s", (r,))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT status, close_reason, latest_text, prs, opened_episode FROM commitments "
                    "WHERE id = %s", (c,))
        assert cur.fetchone() == ("closed", "forgotten", None, [], r)


def test_owner_claim_keeps_a_decision_with_no_live_episode(migrated):
    conn = migrated.conn
    a, d, v = migrated.ids["a"], migrated.ids["d"], migrated.ids["v"]
    with conn.cursor() as cur:
        provenance.record_claim(cur, "decision", d, "text", "use claims", claimant="owner")
        cur.execute("DELETE FROM fact_claims WHERE episode_id = %s", (a,))
        assert provenance.project(cur, "decision", d) == "projected"
        assert provenance.project(cur, "deliverable", v) == "deleted"
        assert provenance.project(cur, "deliverable", v) == "missing"
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT text, episode_id, retracted_at FROM decisions WHERE id = %s", (d,))
        assert cur.fetchone() == ("use claims", None, None)
        cur.execute("SELECT count(*) FROM fact_changes WHERE kind = 'deliverable' AND fact_id = %s", (v,))
        assert cur.fetchone()[0] >= 2  # the initial projection, then this transaction's


# ---- Legacy classification (section 4) -----------------------------------------


def _vector(cur, ref):
    cur.execute(
        "INSERT INTO memory_embeddings (profile, kind, ref, chunk_text, content_hash, embedding) "
        "SELECT id, 'commitment', %s, 'x', 'x', array_fill(0.1::real, ARRAY[dim])::vector "
        "FROM embedding_profiles WHERE is_active", (str(ref),))


def test_legacy_classification_table(pre):
    conn = pre.connect()
    with conn.cursor() as cur:
        live = _episode(cur, "live")
        other = _episode(cur, "other")
        gone = _episode(cur, "gone", forgotten=True)
        text = "land PR #12 and github.com/Other/Lib/pull/5 (due: soon)"
        c1 = _commitment(cur, text, live, prs=[5, 12, 99], latest="land PR #12, #99 too",
                         refs=["other/lib#5", "acme/app#12"])
        _vector(cur, c1)
        c2 = _commitment(cur, "forgotten item", gone, status="closed", close_reason="forgotten", latest="x")
        c3 = _commitment(cur, "forgotten but open", gone)
        c4 = _commitment(cur, "plain item", live, prs=None)
        c5 = _commitment(cur, "owner closed", gone, status="closed", close_reason="done")
        _vector(cur, c4)
        cur.execute("INSERT INTO decisions (project, text, rationale, evidence, episode_id) "
                    "VALUES ('khipu', 'd1', 'why', '{\"derivation\": \"extract-details-v1\"}', %s) "
                    "RETURNING id", (live,))
        d1 = cur.fetchone()[0]
        cur.execute("INSERT INTO decisions (project, text, source_kind) VALUES ('khipu', 'd2 by owner', "
                    "'assistant') RETURNING id")
        d2 = cur.fetchone()[0]
        cur.execute("INSERT INTO decisions (project, text, episode_id) VALUES ('khipu', 'd3', %s) RETURNING id",
                    (gone,))
        d3 = cur.fetchone()[0]
        cur.execute("INSERT INTO decisions (project, text, episode_id) VALUES ('khipu', 'd4', %s) RETURNING id",
                    (other,))
        d4 = cur.fetchone()[0]
        cur.execute("INSERT INTO decisions (project, text, episode_id) VALUES ('khipu', 'd5', %s) RETURNING id",
                    (gone,))
        d5 = cur.fetchone()[0]
        cur.execute("INSERT INTO decision_links (old_id, new_id, kind, source, state) VALUES "
                    "(%s, %s, 'supersedes', 'agent', 'applied'), (%s, %s, 'supersedes', 'manual', 'restored'), "
                    "(%s, %s, 'supersedes', 'auto', 'candidate')", (d1, d4, d3, d5, d1, d3))
        cur.execute("INSERT INTO deliverables (project, kind, path, title, episode_id) VALUES "
                    "('khipu', 'file', 'v1.py', 'v1', %s) RETURNING id", (live,))
        v1 = cur.fetchone()[0]
        cur.execute("INSERT INTO deliverables (project, kind, url, episode_id) VALUES "
                    "('khipu', 'pr', 'https://x/pull/1', %s) RETURNING id", (live,))
        v2 = cur.fetchone()[0]
        cur.execute("INSERT INTO deliverables (project, kind, path, title, episode_id) VALUES "
                    "('khipu', 'file', 'v3.py', 'v3', %s) RETURNING id", (gone,))
        v3 = cur.fetchone()[0]
    conn.commit()
    _migrate_0047(pre)

    def claims(kind, fid):
        with conn.cursor() as cur:
            got = provenance.claims(cur, kind, fid)
        conn.rollback()
        return sorted((c["field"], c["value"], c["claimant"], c["episode_id"]) for c in got)

    E, U, OW = "episode", "legacy_unproven", "owner"
    assert claims("commitment", c1) == sorted([
        ("text", text, E, live), ("pr", "5", E, live), ("pr", "12", E, live), ("pr", "99", U, None),
        ("pr_ref", "other/lib#5", E, live), ("pr_ref", "acme/app#12", U, None),
        ("restatement", "land PR #12, #99 too", U, None)])
    assert claims("commitment", c2) == [("restatement", "x", U, None)]
    assert claims("commitment", c3) == []
    assert claims("commitment", c4) == [("text", "plain item", E, live)]
    assert claims("decision", d1) == sorted([
        ("text", "d1", E, live), ("rationale", "why", E, live),
        ("evidence", '{"derivation": "extract-details-v1"}', E, live)])
    assert claims("decision", d2) == [("text", "d2 by owner", OW, None)]
    assert claims("decision", d3) == []
    assert claims("decision", d4) == sorted([("text", "d4", E, other), ("text", "d4", OW, None)])
    assert claims("decision", d5) == [("text", "d5", OW, None)]
    assert claims("deliverable", v1) == [("title", "v1", E, live)]
    assert claims("deliverable", v2) == [("support", "", E, live)]

    with conn.cursor() as cur:
        cur.execute("SELECT text, latest_text, prs, pr_check, opened_episode, status FROM commitments "
                    "WHERE id = %s", (c1,))
        assert cur.fetchone() == (text, None, [5, 12], {"refs": ["other/lib#5"], "needs_owner": True},
                                  live, "open")
        cur.execute("SELECT id, status, close_reason, latest_text FROM commitments WHERE id = ANY(%s) ORDER BY id",
                    ([c2, c3, c4, c5],))
        assert cur.fetchall() == [(c2, "closed", "forgotten", None), (c3, "closed", "forgotten", None),
                                  (c4, "open", None, None), (c5, "closed", "done", None)]
        cur.execute("SELECT prs, pr_check FROM commitments WHERE id = %s", (c4,))
        assert cur.fetchone() == ([], None)
        # c1's wording changed (latest_text dropped), c4's did not.
        cur.execute("SELECT ref FROM memory_embeddings WHERE kind = 'commitment' ORDER BY ref")
        assert cur.fetchall() == [(str(c4),)]
        cur.execute("SELECT id, text, episode_id, retract_reason FROM decisions ORDER BY id")
        assert cur.fetchall() == [(d1, "d1", live, None), (d2, "d2 by owner", None, None),
                                  (d3, "d3", gone, "forgotten"), (d4, "d4", other, None),
                                  (d5, "d5", None, None)]
        cur.execute("SELECT id, title, episode_id FROM deliverables ORDER BY id")
        assert cur.fetchall() == [(v1, "v1", live), (v2, None, live)]
        assert v3 not in {v1, v2}
        # Every fact was projected once, in the migration's own transaction.
        cur.execute("SELECT kind, count(DISTINCT fact_id), count(DISTINCT xid) FROM fact_changes "
                    "GROUP BY kind ORDER BY kind")
        assert cur.fetchall() == [("commitment", 5, 1), ("decision", 5, 1), ("deliverable", 3, 1)]
        cur.execute("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'khipu\\_%%' AND NOT tgisinternal")
        assert GATE_TRIGGERS <= {r[0] for r in cur.fetchall()}
    conn.rollback()


def test_rerunning_migrate_changes_nothing(migrated):
    result = migrated.migrate()
    assert result["ran"] == []


@pytest.mark.parametrize("attempt", ["0045_fact_support", "0046_fact_support_backfill"])
def test_a_hub_from_the_abandoned_attempt_is_refused(pre, attempt):
    import psycopg

    conn = pre.connect()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO schema_migrations (version, note) VALUES (%s, 'attempt')", (attempt,))
    conn.commit()
    with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="0045 or 0046"):
        pre.migrate()
    check = pre.connect()
    with check.cursor() as cur:
        cur.execute("SELECT to_regclass('public.fact_claims'), "
                    "(SELECT count(*) FROM schema_migrations WHERE version LIKE '0047%%')")
        assert cur.fetchone() == (None, 0)


# ---- khipu_text_prs never proves more than parse_prs ---------------------------

PR_TEXTS = [
    "land PR #12", "PRs #3, #4 and #5; then #6", "pull request 88", "Pull Requests #7 #8",
    "see github.com/Acme/App/pull/41 and PR41", "PR #0 and PR #00012", "issue #9 only",
    "PR#5", "PRs#5", "the PR list #1, &#2, x#3, (#4)", "PR #12345678", "fix pr 3: done #4",
    "prepr #5", "PR #5x", "PR #5_", "spr #6", "éPR #7", "PR #8é", "PR #9", "PR #١٢",
    "pull  request\t#10", "https://github.com/a/b/pull/11/files", "github.com/a/b/pull/12x",
    "PR #13.\nPR list: #14", "nothing here", "", "PR #15, #16; #17", "a PR: #18",
    "Merge pull request #19 from x/y", "PR #20 & #21", "PRs —#22", "PR\n#23",
]


def test_sql_pr_parse_is_never_more_than_parse_prs(migrated):
    from khipu.close_rules import parse_prs

    conn = migrated.conn
    disagree = []
    with conn.cursor() as cur:
        for t in PR_TEXTS:
            cur.execute("SELECT khipu_text_prs(%s)", (t,))
            sql = set(cur.fetchone()[0])
            py = set(parse_prs(t))
            assert sql <= py, (t, sql, py)
            if sql != py:
                disagree.append(t)
    conn.rollback()
    # Only texts with a non-ASCII neighbour or space may read stricter.
    assert all(re.search(r"[^\x00-\x7f]", t) for t in disagree), disagree


def test_sql_pull_refs_are_the_qualified_labels_of_pull_links(migrated):
    from khipu.close_rules import pr_requirement_labels

    conn = migrated.conn
    with conn.cursor() as cur:
        for t in PR_TEXTS:
            cur.execute("SELECT khipu_text_pull_refs(%s)", (t,))
            sql = set(cur.fetchone()[0])
            py = set(pr_requirement_labels(t, None))
            assert sql <= py, (t, sql, py)
            assert sql == py or re.search(r"[^\x00-\x7f]", t), (t, sql, py)
    conn.rollback()


# ---- The single-writer inventory section 4's proof rests on ---------------------

_TABLES = ("commitments", "decisions", "deliverables")
_WORDING = {
    "commitments": ("text", "latest_text", "prs", "pr_check", "opened_episode"),
    "decisions": ("text", "rationale", "evidence", "episode_id"),
    "deliverables": ("title", "episode_id"),
}
# (module, function, statement): every hub write of a fact table's wording
# or anchor columns on main. A new one breaks this test: it must go through
# provenance.record_claim/project (phase 2) or the 0047 gate refuses it, and
# section 4's legacy proof must be re-checked before it is added here.
# "dynamic" is an UPDATE whose SET list is built at run time.
WORDING_WRITERS = {
    # prs from the anchor's own text (rows from before 0034).
    ("close_rules", "backfill_prs", "UPDATE commitments"),
    # pr_check notes; keeps refs as they were (and drops needs_owner: phase 3).
    ("close_rules", "_pr_rule", "UPDATE commitments"),
    ("commitments", "_keep_pr_refs", "UPDATE commitments"),
    ("commitments", "open_from_episode", "INSERT INTO commitments"),
    ("commitments", "open_from_episode", "UPDATE commitments"),
    # Owner columns only (owner, owner_label, lineage, reassigned_at).
    ("commitments", "auto_reassign", "UPDATE commitments dynamic"),
    ("commitments", "reassign", "UPDATE commitments dynamic"),
    ("decisions", "create_decision", "INSERT INTO decisions"),
    ("decisions", "insert_decisions_from_episode", "INSERT INTO decisions"),
    ("deliverables", "insert_deliverables_from_episode", "INSERT INTO deliverables"),
}


def _strings(node) -> str:
    parts = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            parts.append(n.value)
        elif isinstance(n, ast.FormattedValue):
            parts.append("{}")
    return " ".join(parts)


def _wording_writes():
    found = set()
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call in ast.walk(fn):
                if not (isinstance(call, ast.Call) and call.args):
                    continue
                sql = _strings(call.args[0])
                if "?" in sql:  # SQLite replica statements
                    continue
                for table in _TABLES:
                    if re.search(rf"\bINSERT\s+INTO\s+{table}\b", sql, re.I):
                        found.add((path.stem, fn.name, f"INSERT INTO {table}"))
                    m = re.search(rf"\bUPDATE\s+{table}\s+SET\b(.*?)(\bWHERE\b|$)", sql, re.I | re.S)
                    if not m:
                        continue
                    sets = m.group(1)
                    if re.match(r"\s*\{\}", sets):
                        found.add((path.stem, fn.name, f"UPDATE {table} dynamic"))
                    elif any(re.search(rf"\b{col}\s*=", sets) for col in _WORDING[table]):
                        found.add((path.stem, fn.name, f"UPDATE {table}"))
    return found


def test_the_wording_writer_inventory_is_pinned():
    found = {(m, f, s) for m, f, s in _wording_writes() if m != "provenance"}
    assert found == WORDING_WRITERS, (
        f"new: {sorted(found - WORDING_WRITERS)}; gone: {sorted(WORDING_WRITERS - found)}")
