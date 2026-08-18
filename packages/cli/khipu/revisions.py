"""Topic revisions + conflict visibility (LWW losers stay queryable)."""
from __future__ import annotations

from pathlib import Path

from khipu.db import connect


def recent_revisions(*, limit: int = 50, slug: str | None = None) -> list[dict]:
    with connect() as conn:
        with conn.cursor() as cur:
            if slug:
                cur.execute(
                    """
                    SELECT id, slug, revised_at, source, note, content_hash,
                           left(body, 200) AS preview
                    FROM topic_revisions
                    WHERE slug = %s
                    ORDER BY revised_at DESC
                    LIMIT %s
                    """,
                    (slug, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT id, slug, revised_at, source, note, content_hash,
                           left(body, 200) AS preview
                    FROM topic_revisions
                    ORDER BY revised_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
            rows = cur.fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "slug": r[1],
                "revised_at": r[2].isoformat() if r[2] else None,
                "source": r[3],
                "note": r[4],
                "content_hash": r[5],
                "preview": r[6],
            }
        )
    return out


def revision_for_id(rev_id: int) -> dict | None:
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, slug, revised_at, source, note, content_hash, body
                FROM topic_revisions
                WHERE id = %s
                """,
                (rev_id,),
            )
            r = cur.fetchone()
    if not r:
        return None
    return {
        "id": r[0],
        "slug": r[1],
        "revised_at": r[2].isoformat() if r[2] else None,
        "source": r[3],
        "note": r[4],
        "content_hash": r[5],
        "body": r[6],
    }


def file_topic_hash(path: Path) -> str | None:
    """One file's content_hash, via the same helpers the writer uses so this can
    never disagree with Postgres over encoding or line endings."""
    from khipu.mirror import read_topic_text, topic_content_hash

    text = read_topic_text(path)
    return None if text is None else topic_content_hash(text)


def conflict_report(
    memory_root: Path | None = None,
    *,
    sample: int | None = None,
) -> dict:
    """
    Visible dual-run / LWW conflict signals (never silent).

    1. file_vs_pg — topic markdown hash ≠ PG content_hash (active dual-run drift)
    2. multi_revision — topics with ≥2 revisions (LWW archive non-empty)

    ``sample`` defaulted to 40 while ``ok`` spoke for the whole corpus, so this
    report cleared 622 topics after comparing the alphabetically-first 40 — the
    same defect the drift check carried, fixed there and missed here (audit
    2026-08-17). None now means every topic; 0 still skips the filesystem walk
    and returns the PG-only revision signals.
    """
    from khipu.drift import file_topic_hashes

    file_mismatches: list[dict] = []
    unreadable: list[str] = []
    file_hashes: dict[str, str] = {}
    walk = memory_root is not None and sample != 0

    with connect() as conn:
        with conn.cursor() as cur:
            if walk:
                file_hashes, unreadable = file_topic_hashes(memory_root, limit=sample)
                pg_by_slug: dict[str, tuple] = {}
                if file_hashes:
                    # One round trip for every slug. This was a query per file,
                    # so a full pass meant 622 of them.
                    cur.execute(
                        "SELECT slug, content_hash, updated_at, deleted_at "
                        "FROM topics WHERE slug = ANY(%s)",
                        (list(file_hashes),),
                    )
                    pg_by_slug = {r[0]: (r[1], r[2], r[3]) for r in cur.fetchall()}
                for slug, digest in file_hashes.items():
                    row = pg_by_slug.get(slug)
                    if row is None:
                        file_mismatches.append(
                            {
                                "slug": slug,
                                "issue": "missing_in_pg",
                                "file_hash": digest[:12],
                            }
                        )
                    elif row[2] is not None:
                        # File exists but PG row is tombstoned — stale
                        # tombstone; next reconcile un-tombstones it.
                        file_mismatches.append(
                            {
                                "slug": slug,
                                "issue": "tombstoned_in_pg",
                                "file_hash": digest[:12],
                            }
                        )
                    elif row[0] != digest:
                        file_mismatches.append(
                            {
                                "slug": slug,
                                "issue": "hash_mismatch",
                                "file_hash": digest[:12],
                                "pg_hash": (row[0] or "")[:12],
                                "pg_updated_at": row[1].isoformat()
                                if row[1]
                                else None,
                            }
                        )

            cur.execute("SELECT COUNT(*) FROM topic_revisions")
            rev_total = int(cur.fetchone()[0])
            cur.execute(
                """
                SELECT slug, COUNT(*) AS n, MAX(revised_at) AS last_rev
                FROM topic_revisions
                GROUP BY slug
                HAVING COUNT(*) >= 2
                ORDER BY last_rev DESC
                LIMIT 50
                """
            )
            multi = [
                {
                    "slug": r[0],
                    "revision_count": int(r[1]),
                    "last_revised_at": r[2].isoformat() if r[2] else None,
                }
                for r in cur.fetchall()
            ]
            cur.execute(
                """
                SELECT COUNT(DISTINCT slug) FROM topic_revisions
                """
            )
            slugs_with_revs = int(cur.fetchone()[0])

    open_conflicts = len(file_mismatches)
    return {
        "open_file_vs_pg": open_conflicts,
        "file_vs_pg": file_mismatches[:40],
        "topics_checked": len(file_hashes),
        # A file we could not read is not a file we cleared, so it fails `ok`
        # the same way a mismatch does.
        "topic_files_unreadable": unreadable,
        "topics_with_multiple_revisions": multi,
        "revision_row_count": rev_total,
        "slugs_with_revisions": slugs_with_revs,
        "ok": open_conflicts == 0 and not unreadable,
        "note": (
            "file_vs_pg = active dual-run drift (fix with reconcile). "
            "multi_revision = LWW archive (expected; open a revision to inspect losers)."
        ),
    }
