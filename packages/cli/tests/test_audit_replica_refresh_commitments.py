# ruff: noqa: F811 - pytest fixtures imported from test_provenance_forget and replica_copy
"""Expected invariant: a full refresh must not serve a source the registry refused.

Since phase 7B (design section 6.2) the replica keeps a copy of the hub's
facts: a forgotten episode id this Mac holds must be tombstoned (or absent)
on the hub before a copy is published, and until then it is listed and no
fact row shows; once the hub's forget lands, the live facts are served."""
from khipu import hub_snapshot as hs, offline_reads, provenance, tombstones
from tests.fact_seed import announce
from tests.hermetic_pg import hub, pg_cluster  # noqa: F401
from tests.replica_copy import replica  # noqa: F401
from tests.test_provenance_forget import env, pg  # noqa: F401


def _seed(h):
    with h.connect() as conn:
        ids = []
        for summary, words in (("FORGOTTEN source", "FORGOTTEN synthetic obligation"),
                               ("live source", "live control obligation")):
            announce(conn)  # each episode its own identity
            eid = conn.execute(
                "INSERT INTO episodes (ts, session_id, summary, raw, harness, project, transcript_range) "
                "VALUES (now(), %s, %s, jsonb_build_object('summary', %s::text), 'codex', 'scratch', '0:1') "
                "RETURNING id", (f"codex:{summary}", summary, summary)).fetchone()[0]
            cid = conn.execute(
                "INSERT INTO commitments (text, project, owner, kind, opened_episode, content_hash) "
                "VALUES (%s, 'scratch', 'user', 'followup', %s, %s) RETURNING id", (words, eid, words)).fetchone()[0]
            provenance.record_claim(conn.cursor(), "commitment", cid, "text", words, episode_id=eid)
            provenance.project(conn.cursor(), "commitment", cid)
            ids.append((eid, cid))
    return ids


def test_full_refresh_scrubs_episode_but_serves_forgotten_commitment(pg, replica):
    replica(pg)
    (forgotten, gone), (_live, kept) = _seed(pg)
    tombstones.publish_episode_ids({forgotten})
    # The hub still holds the episode live: listed, no fact row.
    result = hs.refresh()
    assert result['ok'] and result['counts']['forgotten_scrubbed'] == 1, result
    assert result['facts'].get('unsent') == 1, result['facts']
    waiting = offline_reads.owed('scratch', 'open', 20)
    assert waiting is not None and waiting['results'] == [] and waiting['facts_hidden'], waiting
    # The hub's forget lands: the live commitment is served, the other never.
    with pg.connect() as conn, conn.cursor() as cur:
        identities = provenance.stored_identities(cur, [forgotten])[forgotten]
        provenance.refuse(cur, identities, episode_id=forgotten, also=[forgotten])
        conn.commit()
    result = hs.refresh()
    assert result['ok'] and result['facts'].get('copied'), result
    served = offline_reads.owed('scratch', 'open', 20)
    ids = [r['id'] for r in served['results']]
    assert ids == [kept] and gone not in ids, 'full refresh must not serve the forgotten commitment'
