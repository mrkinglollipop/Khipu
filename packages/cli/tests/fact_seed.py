"""Test setup for facts seeded with raw SQL on a hub with migration 0047.

The fact gate (docs/plans/2026-10-23-fact-provenance.md, I6) refuses at
COMMIT any wording write that was not projected in the same transaction. A
test that seeds a commitment, decision or deliverable with a plain INSERT (or
rewords one with a plain UPDATE, to stand in for a later capture) calls
:func:`adopt` on it in the same transaction: it replaces the fact's claims
with ones that reproduce the row as it now stands, through
``provenance.record_claim``, and projects it with ``provenance.project``.

The claimant is the row's anchor episode when it has a live one, else the
owner (a row with no episode). Nothing here asserts anything; the tests'
assertions stay as they were.
"""
from __future__ import annotations

import json
import uuid
from typing import Iterable

from khipu import provenance


def _live_episode(cur, episode_id) -> bool:
    if episode_id is None:
        return False
    cur.execute("SELECT 1 FROM episodes WHERE id = %s AND deleted_at IS NULL", (episode_id,))
    return cur.fetchone() is not None


def _who(cur, episode_id) -> dict:
    if _live_episode(cur, episode_id):
        return {"episode_id": int(episode_id)}
    return {"claimant": "owner"}


def adopt(cur, kind: str, ids: int | Iterable[int]) -> None:
    """Make the claims of each fact reproduce its row, then project it. A
    hub without migration 0047 has no gate and no claims: nothing to do."""
    cur.execute("SELECT to_regclass('public.fact_claims') IS NOT NULL")
    if not cur.fetchone()[0]:
        return
    for fact_id in ([ids] if isinstance(ids, int) else list(ids)):
        fact_id = int(fact_id)
        cur.execute("DELETE FROM fact_claims WHERE kind = %s AND fact_id = %s", (kind, fact_id))
        if kind == "commitment":
            cur.execute("SELECT text, latest_text, prs, pr_check, opened_episode FROM commitments WHERE id = %s",
                        (fact_id,))
            text, latest, prs, check, episode = cur.fetchone()
            who = _who(cur, episode)
            provenance.record_claim(cur, kind, fact_id, "text", text, **who)
            provenance.record_claim(cur, kind, fact_id, "restatement", latest, **who)
            if prs is None:
                # Not parsed yet: the projection has no NULL, so the numbers
                # stored by the historical PR backfill.
                from khipu.pr_refs import parse_prs

                prs = parse_prs(text)
            for n in prs:
                provenance.record_claim(cur, kind, fact_id, "pr", n, **who)
            refs = (check or {}).get("refs") if isinstance(check, dict) else None
            for ref in refs or []:
                provenance.record_claim(cur, kind, fact_id, "pr_ref", ref, **who)
        elif kind == "decision":
            cur.execute("SELECT text, rationale, evidence, episode_id FROM decisions WHERE id = %s", (fact_id,))
            text, rationale, evidence, episode = cur.fetchone()
            who = _who(cur, episode)
            provenance.record_claim(cur, kind, fact_id, "text", text, **who)
            provenance.record_claim(cur, kind, fact_id, "rationale", rationale, **who)
            if evidence is not None:
                provenance.record_claim(cur, kind, fact_id, "evidence", json.dumps(evidence), **who)
        elif kind == "deliverable":
            cur.execute("SELECT title, episode_id FROM deliverables WHERE id = %s", (fact_id,))
            title, episode = cur.fetchone()
            who = _who(cur, episode)
            if title is None:
                provenance.record_claim(cur, kind, fact_id, "support", "", **who)
            else:
                provenance.record_claim(cur, kind, fact_id, "title", title, **who)
        else:
            raise ValueError(kind)
        provenance.project(cur, kind, fact_id)


def adopt_all(cur, kind: str = "commitment") -> None:
    """:func:`adopt` every row of one fact table (a bulk seed)."""
    table = {"commitment": "commitments", "decision": "decisions", "deliverable": "deliverables"}[kind]
    cur.execute(f"SELECT id FROM {table} ORDER BY id")
    adopt(cur, kind, [r[0] for r in cur.fetchall()])


def announce(cur, *identities: str) -> None:
    """Announce source identities for an episode a test writes or merges
    with raw SQL, as a writer does (0047 refuses an episode write that
    announces none): a fresh identity of its own unless ``identities`` are
    given. Transaction-local, like the writer's (for the session on an
    autocommit connection, until the next announcement); a hub without 0047
    ignores it."""
    conn = getattr(cur, "connection", cur)
    cur.execute("SELECT set_config('khipu.source_identities', %s, %s)",
                (",".join(identities or (uuid.uuid4().hex,)), not getattr(conn, "autocommit", False)))


def owner_commitment(cur, text, *, project, episode_id, kind="followup", linked_ids=()):
    """Seed historical owner work directly; capture no longer creates it.

    Keep episode claims on migrated hubs, so forget/provenance assertions
    still exercise the original lineage rather than an immortal owner claim.
    """
    from khipu import commitments as co, db
    import json

    h = co.content_hash(project, text)
    cur.execute("SELECT 1 FROM commitments WHERE project IS NOT DISTINCT FROM %s AND content_hash = %s "
                "AND status = 'open'", (project, h))
    if cur.fetchone():
        h = co.content_hash(project, text + "\x00owner:user:")
    values = dict(text=text, project=project, owner="user", kind=kind, opened_episode=episode_id,
                  content_hash=h, origin="conversation", owner_label=None, linked_ids=list(linked_ids),
                  prs=None)
    from khipu.pr_refs import parse_prs, pr_requirement_labels
    values['prs'] = parse_prs(text)
    values['pr_check'] = json.dumps({'refs': pr_requirement_labels(text, project)})
    have = db.table_columns(cur, "commitments")
    values = {k: v for k, v in values.items() if k in have}
    cur.execute("SELECT to_regclass('public.fact_claims') IS NOT NULL")
    if cur.fetchone()[0]:
        provenance.declare_writer(cur)
    cur.execute("INSERT INTO commitments (" + ", ".join(values) + ") VALUES (" +
                ", ".join("%s::jsonb" if k == "pr_check" else "%s" for k in values) + ") RETURNING id",
                tuple(values.values()))
    cid = int(cur.fetchone()[0])
    adopt(cur, "commitment", cid)
    for linked in linked_ids:
        cur.execute("UPDATE commitments SET linked_ids = array_append(linked_ids, %s) WHERE id = %s", (cid, linked))
    return cid


def archived_facts(cur, payload, episode_id, *, restates=None):
    """Seed legacy rows explicitly for tests of their surviving archive readers."""
    from khipu import commitments as co
    from khipu.pr_refs import parse_prs, pr_requirement_labels
    project = payload.get("project")
    gated = provenance.ready(cur)
    if gated:
        provenance.declare_writer(cur)
    for loop in payload.get("open_loops") or []:
        loop = {"text": loop} if isinstance(loop, str) else loop
        text = loop["text"]
        owner = loop.get("owner") or "assistant"
        hashed = co.content_hash(project, text + ("\x00owner:user:" if owner == "user" else ""))
        cur.execute("SELECT id FROM commitments WHERE project IS NOT DISTINCT FROM %s "
                    "AND content_hash = %s AND status = 'open'", (project, hashed))
        row = cur.fetchone()
        if restates is not None:
            row = (restates,)
        if row:
            cid = row[0]
            if gated:
                provenance.record_claim(cur, "commitment", cid, "restatement", text, episode_id=episode_id)
                for number in parse_prs(text):
                    provenance.record_claim(cur, "commitment", cid, "pr", number, episode_id=episode_id)
                for ref in pr_requirement_labels(text, project):
                    provenance.record_claim(cur, "commitment", cid, "pr_ref", ref, episode_id=episode_id)
                provenance.project(cur, "commitment", cid)
            else:
                cur.execute("SELECT prs, pr_check FROM commitments WHERE id = %s", (cid,))
                held_prs, held_check = cur.fetchone()
                refs = list(dict.fromkeys([*((held_check or {}).get("refs") or []), *pr_requirement_labels(text, project)]))
                prs = sorted(set(held_prs or []) | set(parse_prs(text)))
                cur.execute("UPDATE commitments SET latest_text = %s, prs = %s, pr_check = %s::jsonb, seen_count = seen_count + 1 WHERE id = %s",
                            (text, prs, json.dumps({"refs": refs}), cid))
            continue
        owner = loop.get("owner") or "assistant"
        cur.execute("INSERT INTO commitments(text,project,owner,kind,opened_episode,content_hash,prs,pr_check) "
                    "VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb) RETURNING id",
                    (text, project, owner, loop.get("kind", "followup"), episode_id, hashed,
                     parse_prs(text), json.dumps({"refs": pr_requirement_labels(text, project)})))
        cid = cur.fetchone()[0]
        if gated:
            adopt(cur, "commitment", cid)
    for text in payload.get("decisions") or []:
        cur.execute("SELECT id FROM decisions WHERE project IS NOT DISTINCT FROM %s AND text = %s",
                    (project, text))
        row = cur.fetchone()
        if row:
            if gated:
                provenance.record_claim(cur, "decision", row[0], "text", text, episode_id=episode_id)
                provenance.project(cur, "decision", row[0])
            continue
        cur.execute("INSERT INTO decisions(project,text,episode_id,decided_at) "
                    "SELECT %s,%s,id,ts FROM episodes WHERE id = %s RETURNING id",
                    (project, text, episode_id))
        did = cur.fetchone()[0]
        if gated:
            adopt(cur, "decision", did)
