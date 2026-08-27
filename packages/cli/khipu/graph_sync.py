"""Knowledge-graph mirror: graph.sqlite (graphify's output) → Postgres, with drift.

Why this exists (audit 2026-08-17): the P1 ETL copied graph.sqlite into PG once,
on 2026-08-04, and nothing kept it current — while ``graphify-nightly`` kept
rebuilding graph.sqlite every night. Two weeks later PG was 21 code nodes and
70 shared nodes behind, and still carried ~120 nodes graphify had since dropped
(Reports/…, Agents/etf-reviewer, …). Nothing went red, because ``khipu doctor``
only checked episodes and topics.

Model — the same one episodes use: **the producer keeps producing where it does,
Khipu mirrors at the source, and drift is measured, not assumed.**

  * graphify writes graph.sqlite (unchanged). At the end of its nightly it calls
    :func:`sync_from_sqlite` fail-open, exactly like capture_v2 mirrors episodes.
  * The sync is a full, idempotent mirror of graphify's rows: COPY into temp
    tables, upsert nodes then edges, then delete graphify-owned rows that
    graphify no longer has. One transaction; ~20k nodes / 35k edges in seconds.
  * **Ownership.** Rows Khipu created itself (today: the conversation-memory
    graph loaded from ``Memory/conversations/graph*.jsonl``) are Khipu-owned and
    are never deleted by the sync; everything else in ``nodes``/``edges`` came
    from graphify and is graphify-owned. A graphify-owned node still referenced
    by a Khipu-owned edge is kept (reported as ``kept_referenced``) — never an FK
    surprise.
  * :func:`graph_drift` compares graphify's rows to PG's graphify-owned rows by
    id and by (src,dst,type), and ``khipu doctor`` includes it in ``ok``.

Voyage vectors in graph.sqlite are NOT mirrored (plan lock: Khipu re-embeds with
its own profile in P4); the Graph system's own tools keep reading them locally.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from khipu.sources import (
    _edge_endpoint_node,
    disabled_or_unreachable_ids,
    drift_failing_pg_extra_edges,
    drift_failing_pg_extras,
    load_sources,
    owned_source_ids,
    should_delete_graphify_edge,
    should_delete_graphify_node,
    source_id_for_delete,
    source_id_for_graphify_node,
)


def _default_sqlite() -> Path:
    """graph.sqlite from config (env KHIPU_GRAPH_SQLITE → config.json).

    Raises rather than guessing: a caller that reaches here without a
    configured path is on a machine that never said where the graph lives.
    """
    from khipu.config import path_setting

    v = path_setting("graph_sqlite")
    if v is None:
        raise FileNotFoundError(
            "graph_sqlite is not configured (khipu config --set graph_sqlite PATH)"
        )
    return v


# A PG node/edge is Khipu-owned when it came from Khipu's own loaders rather than
# from graphify. Kept as SQL so the sync and the drift check cannot disagree.
# COALESCE matters: 6,611 graphify nodes have NULL source_path, and without it
# `NOT (false OR NULL)` is NULL — those rows were neither owned nor unowned, so
# the first dry run under-counted PG by exactly that many (2026-08-17).
KHIPU_OWNED_NODE_SQL = (
    "(COALESCE(n.bucket, '') = 'conversation-memory'"
    " OR COALESCE(n.source_path, '') LIKE '%/Memory/conversations/graph%')"
)


def _json_or_wrapped(raw: Any) -> str | None:
    """graph.sqlite payload is TEXT; almost always JSON. Same normalisation the
    P1 export used, so a re-mirror never changes what P1 loaded."""
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        return json.dumps(raw)
    try:
        json.loads(raw)
        return raw
    except json.JSONDecodeError:
        return json.dumps({"_raw": raw})


def _effective_source_id(node: dict[str, Any], doc: dict | None = None) -> str | None:
    pg_sid = node.get("source_id")
    if pg_sid:
        return str(pg_sid).strip() or None
    return source_id_for_delete(
        node_id=str(node.get("id") or ""),
        type=str(node.get("type") or ""),
        bucket=node.get("bucket"),
        source_path=node.get("source_path"),
        doc=doc,
    )


def _node_owned(node: dict[str, Any], owned: set[str], doc: dict | None = None) -> bool:
    sid = _effective_source_id(node, doc)
    return bool(sid and sid in owned)


def _read_sqlite(path: Path) -> tuple[list[tuple], list[tuple]]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        nodes = [
            (
                r["id"],
                r["type"],
                r["bucket"] or None,
                r["name"] or None,
                _json_or_wrapped(r["payload"]),
                r["source_path"] or None,
                r["built_at"] or None,
                bool(r["frozen"]),
            )
            for r in con.execute(
                "SELECT id, type, bucket, name, payload, source_path, built_at, frozen FROM nodes"
            )
        ]
        edges = [
            (
                r["src"],
                r["dst"],
                r["type"],
                r["weight"],
                _json_or_wrapped(r["payload"]),
                r["built_at"] or None,
            )
            for r in con.execute(
                "SELECT src, dst, type, weight, payload, built_at FROM edges"
            )
        ]
    finally:
        con.close()
    return nodes, edges


def sync_from_sqlite(
    sqlite_path: Path | None = None, *, dry_run: bool = False
) -> dict[str, Any]:
    """Mirror graphify's graph.sqlite into PG. Idempotent; one transaction.

    Returns counts plus the post-sync drift so a caller can assert zero."""
    from khipu.db import connect

    path = Path(sqlite_path) if sqlite_path else _default_sqlite()
    if not path.is_file():
        raise FileNotFoundError(f"graph.sqlite not found: {path}")
    t0 = time.time()
    nodes, edges = _read_sqlite(path)
    sources_doc = load_sources()
    nodes_with_source: list[tuple] = []
    for row in nodes:
        nid, ntype, bucket, name, payload, source_path, built_at, frozen = row
        sid = source_id_for_graphify_node(
            node_id=nid,
            type=ntype,
            bucket=bucket,
            source_path=source_path,
            doc=sources_doc,
        )
        nodes_with_source.append(
            (nid, ntype, bucket, name, payload, source_path, built_at, frozen, sid)
        )
    stats: dict[str, Any] = {
        "sqlite": str(path),
        "sqlite_nodes": len(nodes),
        "sqlite_edges": len(edges),
        "dry_run": dry_run,
    }
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE _sync_nodes (id TEXT PRIMARY KEY, type TEXT, bucket TEXT, name TEXT,"
                " payload TEXT, source_path TEXT, built_at TEXT, frozen BOOLEAN, source_id TEXT) ON COMMIT DROP"
            )
            cur.execute(
                "CREATE TEMP TABLE _sync_edges (src TEXT, dst TEXT, type TEXT, weight DOUBLE PRECISION,"
                " payload TEXT, built_at TEXT) ON COMMIT DROP"
            )
            with cur.copy(
                "COPY _sync_nodes (id, type, bucket, name, payload, source_path, built_at, frozen, source_id)"
                " FROM STDIN"
            ) as cp:
                for row in nodes_with_source:
                    cp.write_row(row)
            with cur.copy(
                "COPY _sync_edges (src, dst, type, weight, payload, built_at) FROM STDIN"
            ) as cp:
                for row in edges:
                    cp.write_row(row)
            # graphify's own file is the authority for edge endpoints; drop nothing
            # silently — count what would dangle.
            cur.execute(
                "SELECT COUNT(*) FROM _sync_edges e WHERE NOT EXISTS (SELECT 1 FROM _sync_nodes n WHERE n.id = e.src)"
                " OR NOT EXISTS (SELECT 1 FROM _sync_nodes n WHERE n.id = e.dst)"
            )
            stats["sqlite_dangling_edges"] = cur.fetchone()[0]

            # 1. nodes: insert new, update changed (any column, incl. built_at).
            cur.execute(
                """
                INSERT INTO nodes (id, type, bucket, name, payload, source_path, built_at, frozen, source_id)
                SELECT id, type, bucket, name,
                       CASE WHEN payload IS NULL THEN NULL ELSE payload::jsonb END,
                       source_path,
                       CASE WHEN built_at IS NULL THEN NULL ELSE built_at::timestamptz END,
                       COALESCE(frozen, false),
                       source_id
                FROM _sync_nodes s
                ON CONFLICT (id) DO UPDATE SET
                    type = EXCLUDED.type, bucket = EXCLUDED.bucket, name = EXCLUDED.name,
                    payload = EXCLUDED.payload, source_path = EXCLUDED.source_path,
                    built_at = EXCLUDED.built_at, frozen = EXCLUDED.frozen,
                    source_id = EXCLUDED.source_id
                WHERE (nodes.type, nodes.bucket, nodes.name, nodes.payload, nodes.source_path,
                       nodes.built_at, nodes.frozen, nodes.source_id)
                      IS DISTINCT FROM
                      (EXCLUDED.type, EXCLUDED.bucket, EXCLUDED.name, EXCLUDED.payload,
                       EXCLUDED.source_path, EXCLUDED.built_at, EXCLUDED.frozen, EXCLUDED.source_id)
                """
            )
            stats["nodes_upserted"] = cur.rowcount
            # 2. edges: only where both endpoints exist in PG (they do after step 1).
            cur.execute(
                """
                INSERT INTO edges (src, dst, type, weight, payload, built_at)
                SELECT s.src, s.dst, s.type, s.weight,
                       CASE WHEN s.payload IS NULL THEN NULL ELSE s.payload::jsonb END,
                       CASE WHEN s.built_at IS NULL THEN NULL ELSE s.built_at::timestamptz END
                FROM _sync_edges s
                JOIN nodes a ON a.id = s.src JOIN nodes b ON b.id = s.dst
                ON CONFLICT (src, dst, type) DO UPDATE SET
                    weight = EXCLUDED.weight, payload = EXCLUDED.payload, built_at = EXCLUDED.built_at
                WHERE (edges.weight, edges.payload, edges.built_at)
                      IS DISTINCT FROM (EXCLUDED.weight, EXCLUDED.payload, EXCLUDED.built_at)
                """
            )
            stats["edges_upserted"] = cur.rowcount
            membership_off = disabled_or_unreachable_ids()
            owned = owned_source_ids(sources_doc)
            # 3. delete graphify-owned edges graphify no longer has (an edge is
            #    graphify-owned when neither endpoint is Khipu-owned).
            cur.execute(
                f"""
                SELECT e.src, e.dst, e.type FROM edges e
                WHERE NOT EXISTS (SELECT 1 FROM _sync_edges s
                                  WHERE s.src = e.src AND s.dst = e.dst AND s.type = e.type)
                  AND NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = e.src AND {KHIPU_OWNED_NODE_SQL})
                  AND NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = e.dst AND {KHIPU_OWNED_NODE_SQL})
                """
            )
            edge_delete = 0
            for src, dst, etype in cur.fetchall():
                cur.execute(
                    "SELECT id, type, bucket, source_path, source_id FROM nodes WHERE id = %s",
                    (src,),
                )
                src_row = cur.fetchone()
                cur.execute(
                    "SELECT id, type, bucket, source_path, source_id FROM nodes WHERE id = %s",
                    (dst,),
                )
                dst_row = cur.fetchone()
                src_node = (
                    {
                        "id": src_row[0],
                        "type": src_row[1],
                        "bucket": src_row[2],
                        "source_path": src_row[3],
                        "source_id": src_row[4],
                    }
                    if src_row
                    else {
                        "id": src,
                        "type": "",
                        "bucket": None,
                        "source_path": None,
                        "source_id": None,
                    }
                )
                dst_node = (
                    {
                        "id": dst_row[0],
                        "type": dst_row[1],
                        "bucket": dst_row[2],
                        "source_path": dst_row[3],
                        "source_id": dst_row[4],
                    }
                    if dst_row
                    else {
                        "id": dst,
                        "type": "",
                        "bucket": None,
                        "source_path": None,
                        "source_id": None,
                    }
                )
                if (
                    should_delete_graphify_edge(src_node, dst_node, membership_off)
                    and _node_owned(src_node, owned, sources_doc)
                    and _node_owned(dst_node, owned, sources_doc)
                ):
                    cur.execute(
                        "DELETE FROM edges WHERE src = %s AND dst = %s AND type = %s",
                        (src, dst, etype),
                    )
                    edge_delete += cur.rowcount
            stats["edges_deleted"] = edge_delete
            # 4. delete graphify-owned nodes graphify no longer has — unless some
            #    remaining (Khipu-owned) edge still references them, or membership-off.
            cur.execute(
                f"""
                SELECT n.id, n.type, n.bucket, n.source_path, n.source_id FROM nodes n
                WHERE NOT {KHIPU_OWNED_NODE_SQL}
                  AND NOT EXISTS (SELECT 1 FROM _sync_nodes s WHERE s.id = n.id)
                  AND NOT EXISTS (SELECT 1 FROM edges e WHERE e.src = n.id OR e.dst = n.id)
                """
            )
            node_delete = 0
            for nid, ntype, bucket, source_path, pg_source_id in cur.fetchall():
                node = {
                    "id": nid,
                    "type": ntype,
                    "bucket": bucket,
                    "source_path": source_path,
                    "source_id": pg_source_id,
                }
                if should_delete_graphify_node(node, membership_off) and _node_owned(
                    node, owned, sources_doc
                ):
                    cur.execute("DELETE FROM nodes WHERE id = %s", (nid,))
                    node_delete += cur.rowcount
            stats["nodes_deleted"] = node_delete
            cur.execute(
                f"""
                SELECT COUNT(*) FROM nodes n
                WHERE NOT {KHIPU_OWNED_NODE_SQL}
                  AND NOT EXISTS (SELECT 1 FROM _sync_nodes s WHERE s.id = n.id)
                """
            )
            stats["kept_referenced"] = cur.fetchone()[0]
            if dry_run:
                conn.rollback()
            else:
                conn.commit()
    stats["seconds"] = round(time.time() - t0, 1)
    if not dry_run:
        stats["drift"] = graph_drift(path)
    return stats


def _scheduled_jobs_flag(name: str) -> bool:
    try:
        from khipu.components_matrix import read_versions

        scheduled = read_versions().get("scheduled_jobs")
        if isinstance(scheduled, dict) and scheduled.get(name):
            return True
    except Exception:  # noqa: BLE001
        return False
    return False


def is_graph_producer() -> bool:
    """Does THIS machine build graph.sqlite?

    Default false on portable installs — only env or ``versions.json`` scheduled
    jobs opt in. A leftover LaunchAgents plist must not flip producer on a
    stranger Mac (portable doctor contract).
    """
    env = os.environ.get("KHIPU_GRAPH_PRODUCER", "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    if env in {"0", "false", "no"}:
        return False
    if _scheduled_jobs_flag("graph_build") or _scheduled_jobs_flag("graph"):
        return True
    try:
        from khipu.components_matrix import read_versions

        if read_versions().get("graph_producer") is True:
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def graph_drift(sqlite_path: Path | None = None, *, sample: int = 5) -> dict[str, Any]:
    """graphify's rows vs PG's graphify-owned rows, both directions, by identity.
    Zero on both sides is the only 'ok'."""
    from khipu.db import connect

    path = Path(sqlite_path) if sqlite_path else _default_sqlite()
    out: dict[str, Any] = {"sqlite": str(path), "ok": False}
    if not path.is_file():
        # Missing source is a FAILURE on the producer (it lost its graph) and
        # NOT APPLICABLE anywhere else — otherwise the second Mac's doctor is
        # permanently red for a job it never runs (audit 2026-08-17).
        if is_graph_producer():
            out["error"] = "graph.sqlite not found on the machine that builds it"
            return out
        out.update(
            ok=True,
            skipped="not the graph producer; PG is read-only for the graph here",
        )
        return out
    if not is_graph_producer():
        out.update(
            ok=True,
            skipped="not the graph producer; PG is read-only for the graph here",
        )
        return out
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        s_nodes = {r[0] for r in con.execute("SELECT id FROM nodes")}
        s_edges = {
            (r[0], r[1], r[2]) for r in con.execute("SELECT src, dst, type FROM edges")
        }
    finally:
        con.close()
    membership_off = disabled_or_unreachable_ids()
    sources_doc = load_sources()
    owned = owned_source_ids(sources_doc)
    producer = is_graph_producer()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT n.id, n.type, n.bucket, n.source_path, n.source_id FROM nodes n "
                f"WHERE NOT {KHIPU_OWNED_NODE_SQL}"
            )
            pg_node_rows = [
                {
                    "id": r[0],
                    "type": r[1],
                    "bucket": r[2],
                    "source_path": r[3],
                    "source_id": r[4],
                }
                for r in cur.fetchall()
            ]
            p_nodes = {r["id"] for r in pg_node_rows}
            cur.execute(f"SELECT COUNT(*) FROM nodes n WHERE {KHIPU_OWNED_NODE_SQL}")
            khipu_nodes = cur.fetchone()[0]
            cur.execute(
                f"""
                SELECT e.src, e.dst, e.type FROM edges e
                WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = e.src AND {KHIPU_OWNED_NODE_SQL})
                  AND NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = e.dst AND {KHIPU_OWNED_NODE_SQL})
                """
            )
            p_edges = {(r[0], r[1], r[2]) for r in cur.fetchall()}
    missing_nodes = sorted(s_nodes - p_nodes)
    extra_node_ids = sorted(p_nodes - s_nodes)
    extra_node_rows = [r for r in pg_node_rows if r["id"] in set(extra_node_ids)]
    if producer:
        extra_node_rows = [
            r for r in extra_node_rows if _node_owned(r, owned, sources_doc)
        ]
    failing_extra_nodes = drift_failing_pg_extras(extra_node_rows, membership_off)
    extra_nodes = sorted({r["id"] for r in failing_extra_nodes})
    missing_edges = sorted(s_edges - p_edges)
    extra_edge_tuples = sorted(p_edges - s_edges)
    nodes_by_id = {r["id"]: r for r in pg_node_rows}
    if producer:
        extra_edge_tuples = [
            t
            for t in extra_edge_tuples
            if _node_owned(_edge_endpoint_node(t[0], nodes_by_id), owned, sources_doc)
            and _node_owned(_edge_endpoint_node(t[1], nodes_by_id), owned, sources_doc)
        ]
    extra_edges = drift_failing_pg_extra_edges(
        extra_edge_tuples, nodes_by_id, membership_off
    )
    out.update(
        {
            "sqlite_nodes": len(s_nodes),
            "pg_graphify_nodes": len(p_nodes),
            "pg_khipu_nodes": khipu_nodes,
            "sqlite_edges": len(s_edges),
            "pg_graphify_edges": len(p_edges),
            "nodes_missing_in_pg": len(missing_nodes),
            "nodes_extra_in_pg": len(extra_nodes),
            "edges_missing_in_pg": len(missing_edges),
            "edges_extra_in_pg": len(extra_edges),
            "sample_missing_nodes": missing_nodes[:sample],
            "sample_extra_nodes": extra_nodes[:sample],
            "sqlite_mtime": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(path.stat().st_mtime)
            ),
        }
    )
    out["ok"] = not (missing_nodes or extra_nodes or missing_edges or extra_edges)
    return out
