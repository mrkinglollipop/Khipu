"""Topic wiki links + filesystem paths → Khipu-owned graph nodes/edges.

Search enrich and ``khipu graph`` union-expand live here so MCP and CLI cannot
drift. Minted ids are only ``topic:{slug}`` and ``path:{rel}`` with
``bucket=conversation-memory``. Never INSERT/UPDATE graphify ids
(``memory_topic:…``) — ``graph_sync`` ``ON CONFLICT (id) DO UPDATE SET`` would
overwrite type/bucket/payload on the next nightly.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

KHIPU_BUCKET = "conversation-memory"
TOPIC_PREFIX = "topic:"
PATH_PREFIX = "path:"
MEMORY_TOPIC_PREFIX = "memory_topic:"
WIKI_EDGE = "wiki_link"
LIVES_IN_EDGE = "lives_in"
NEIGHBOR_CAP = 6
PATH_CAP = 12
VOLUME_ROOT = "/Volumes/Cloud Storage"  # wiki-path peeler, not an install default
_ELLIPSIS = ("...", "…")
_VOLUME_PREFIXES = (
    VOLUME_ROOT + "/",
    VOLUME_ROOT,
)
# Token with at least one slash; used after backtick extraction.
_PATHISH = re.compile(
    r"(?:`([^`]+)`)|((?:/~|\.{0,2}/|[A-Za-z0-9_.-]+/)[A-Za-z0-9_./~-]+)"
)


def peel_topic_id(raw: str) -> str:
    s = (raw or "").strip()
    for prefix in (TOPIC_PREFIX, MEMORY_TOPIC_PREFIX):
        if s.startswith(prefix):
            return s[len(prefix) :]
    return s


def topic_aliases(raw: str) -> list[str]:
    """Union expand: ``{slug, topic:slug, memory_topic:slug}`` after peel."""
    slug = peel_topic_id(raw)
    if not slug:
        return [raw] if raw else []
    return [slug, f"{TOPIC_PREFIX}{slug}", f"{MEMORY_TOPIC_PREFIX}{slug}"]


def assert_mintable_id(node_id: str) -> None:
    if node_id.startswith(MEMORY_TOPIC_PREFIX):
        raise ValueError(
            f"refusing to mint graphify id {node_id!r}; use {TOPIC_PREFIX} or {PATH_PREFIX}"
        )
    if not (node_id.startswith(TOPIC_PREFIX) or node_id.startswith(PATH_PREFIX)):
        raise ValueError(
            f"refusing to mint {node_id!r}; stored endpoints are {TOPIC_PREFIX} / {PATH_PREFIX} only"
        )


def parse_frontmatter_links(fm_text: str) -> list[str]:
    """YAML-ish ``links:`` then indented ``- slug`` items. The ``links:`` line
    itself is not a slug."""
    links: list[str] = []
    in_links = False
    for line in (fm_text or "").splitlines():
        if line.startswith("links:"):
            in_links = True
            rest = line.split(":", 1)[1].strip().strip("[]")
            if rest:
                for part in rest.split(","):
                    slug = part.strip().strip("\"'")
                    if slug:
                        links.append(slug)
            continue
        if in_links:
            stripped = line.strip()
            if stripped.startswith("- "):
                slug = stripped[2:].strip().strip("\"'")
                if slug:
                    links.append(slug)
                continue
            if stripped.startswith("-"):
                slug = stripped[1:].strip().strip("\"'")
                if slug:
                    links.append(slug)
                continue
            if not stripped:
                continue
            if not line[:1].isspace() and ":" in line:
                in_links = False
            else:
                continue
    # unique, stable order
    seen: set[str] = set()
    out: list[str] = []
    for slug in links:
        if slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


def _looks_url(text: str) -> bool:
    if "://" in text:
        return True
    parsed = urlparse(text)
    return bool(parsed.scheme and parsed.netloc)


def _has_ellipsis(text: str) -> bool:
    return any(tok in text for tok in _ELLIPSIS)


def _normalize_path(raw: str) -> str | None:
    text = (raw or "").strip().strip("`").strip()
    text = text.strip(".,;:)")
    if not text or _has_ellipsis(text) or _looks_url(text):
        return None
    if "node_modules" in text.split("/"):
        return None
    for prefix in _VOLUME_PREFIXES:
        if text == prefix.rstrip("/") or text == VOLUME_ROOT:
            return None
        if text.startswith(prefix if prefix.endswith("/") else prefix + "/"):
            text = text[len(VOLUME_ROOT) :].lstrip("/")
            break
    if "/" not in text:
        return None
    if text.startswith("/"):
        # leftover absolute that isn't the Cloud Storage volume — skip
        if not any(text.startswith(p) for p in ("sojourn/", "sojourn_art/", "Code/", "Claude/")):
            return None
        text = text.lstrip("/")
    parts = [p for p in text.split("/") if p]
    if len(parts) < 2:
        return None
    return text


def extract_paths(text: str, *, cap: int = PATH_CAP) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _PATHISH.finditer(text or ""):
        raw = match.group(1) or match.group(2) or ""
        norm = _normalize_path(raw)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        found.append(norm)
        if len(found) >= cap:
            break
    return found


def collapse_semantic_topic_hits(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One topic row per slug: highest score, then lowest chunk_idx. Episodes unchanged."""
    best: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if row.get("kind") != "topic":
            continue
        slug = str(row.get("id") or "")
        prev = best.get(slug)
        if prev is None or _topic_row_better(row, prev):
            best[slug] = row
    emitted: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("kind") != "topic":
            out.append(dict(row))
            continue
        slug = str(row.get("id") or "")
        winner = best[slug]
        if row.get("chunk_idx") != winner.get("chunk_idx") or row.get("score") != winner.get("score"):
            continue
        if slug in emitted:
            continue
        emitted.add(slug)
        out.append(dict(winner))
    return out


def _topic_row_better(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    sa = float(a.get("score") or 0)
    sb = float(b.get("score") or 0)
    if sa != sb:
        return sa > sb
    ca = int(a.get("chunk_idx") or 0)
    cb = int(b.get("chunk_idx") or 0)
    return ca < cb


def _path_node_id(rel: str) -> str:
    return PATH_PREFIX + rel.rstrip("/")


def persist_topic_graph(cur, parsed: Mapping[str, Any], *, dry_run: bool = False) -> dict[str, int]:
    """Mint ``topic:`` / ``path:`` nodes and wiki_link / lives_in edges. Never graphify ids."""
    from khipu.sources import conversation_memory_enabled

    stats = {"nodes_minted": 0, "edges_minted": 0}
    if not conversation_memory_enabled():
        return stats
    slug = str(parsed.get("slug") or "")
    if not slug:
        return stats
    topic_id = f"{TOPIC_PREFIX}{slug}"
    assert_mintable_id(topic_id)
    links = [str(x) for x in (parsed.get("links") or []) if str(x).strip()]
    paths = extract_paths(str(parsed.get("body") or ""))
    now = datetime.now(timezone.utc).isoformat()
    wanted_nodes: list[tuple[str, str, str]] = [
        (topic_id, "topic", str(parsed.get("title") or slug)),
    ]
    wanted_edges: list[tuple[str, str, str]] = []
    for other in links:
        if other == slug:
            continue
        oid = f"{TOPIC_PREFIX}{other}"
        assert_mintable_id(oid)
        wanted_nodes.append((oid, "topic", other))
        wanted_edges.append((topic_id, oid, WIKI_EDGE))
    for rel in paths:
        pid = _path_node_id(rel)
        assert_mintable_id(pid)
        wanted_nodes.append((pid, "path", rel))
        wanted_edges.append((topic_id, pid, LIVES_IN_EDGE))

    existing_nodes = _existing_ids(cur, [n[0] for n in wanted_nodes])
    existing_edges = _existing_edges(cur, wanted_edges)
    for nid, ntype, name in wanted_nodes:
        if nid in existing_nodes:
            continue
        stats["nodes_minted"] += 1
        if dry_run:
            continue
        _insert_khipu_node(cur, nid, ntype, name, now)
        existing_nodes.add(nid)
    for src, dst, etype in wanted_edges:
        key = (src, dst, etype)
        if key in existing_edges:
            continue
        stats["edges_minted"] += 1
        if dry_run:
            continue
        _insert_khipu_edge(cur, src, dst, etype, now)
        existing_edges.add(key)
    return stats


def _existing_ids(cur, ids: Sequence[str]) -> set[str]:
    if not ids:
        return set()
    cur.execute("SELECT id FROM nodes WHERE id = ANY(%s)", (list(ids),))
    return {r[0] for r in cur.fetchall()}


def _existing_edges(cur, edges: Sequence[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
    if not edges:
        return set()
    srcs = list({e[0] for e in edges})
    cur.execute(
        "SELECT src, dst, type FROM edges WHERE src = ANY(%s)",
        (srcs,),
    )
    wanted = set(edges)
    return {(a, b, t) for a, b, t in cur.fetchall() if (a, b, t) in wanted}


def _insert_khipu_node(cur, node_id: str, ntype: str, name: str, built_at: str) -> None:
    assert_mintable_id(node_id)
    cur.execute(
        """
        INSERT INTO nodes (id, type, bucket, name, payload, source_path, built_at, frozen)
        VALUES (%s, %s, %s, %s, '{}'::jsonb, NULL, %s::timestamptz, false)
        ON CONFLICT (id) DO UPDATE SET
            type = EXCLUDED.type,
            name = EXCLUDED.name,
            built_at = EXCLUDED.built_at
        WHERE nodes.bucket = %s
        """,
        (node_id, ntype, KHIPU_BUCKET, name, built_at, KHIPU_BUCKET),
    )


def _insert_khipu_edge(cur, src: str, dst: str, etype: str, built_at: str) -> None:
    assert_mintable_id(src)
    assert_mintable_id(dst)
    cur.execute(
        """
        INSERT INTO edges (src, dst, type, weight, payload, built_at)
        VALUES (%s, %s, %s, 1.0, '{}'::jsonb, %s::timestamptz)
        ON CONFLICT (src, dst, type) DO NOTHING
        """,
        (src, dst, etype, built_at),
    )


def enrich_search_results(cur, results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Additive ``paths`` / ``neighbors`` on search rows. Dedupes semantic topic chunks."""
    rows = collapse_semantic_topic_hits(results)
    topic_slugs = [str(r["id"]) for r in rows if r.get("kind") == "topic"]
    bodies: dict[str, str] = {}
    if topic_slugs:
        cur.execute(
            "SELECT slug, body FROM topics WHERE slug = ANY(%s) AND deleted_at IS NULL",
            (topic_slugs,),
        )
        bodies = {s: (b or "") for s, b in cur.fetchall()}
    aliases: list[str] = []
    alias_to_slug: dict[str, str] = {}
    for slug in dict.fromkeys(topic_slugs):
        for alias in topic_aliases(slug):
            alias_to_slug[alias] = slug
            aliases.append(alias)
    neighbors_by_slug: dict[str, list[dict[str, str]]] = defaultdict(list)
    seen_nb: dict[str, set[str]] = defaultdict(set)
    if aliases:
        cur.execute(
            """
            SELECT e.src, e.dst, e.type
            FROM edges e
            WHERE e.src = ANY(%s) OR e.dst = ANY(%s)
            """,
            (aliases, aliases),
        )
        for src, dst, etype in cur.fetchall():
            slug = alias_to_slug.get(src) or alias_to_slug.get(dst)
            if not slug:
                continue
            other = dst if alias_to_slug.get(src) == slug else src
            if other in seen_nb[slug]:
                continue
            if len(neighbors_by_slug[slug]) >= NEIGHBOR_CAP:
                continue
            seen_nb[slug].add(other)
            neighbors_by_slug[slug].append({"id": other, "type": etype})
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        kind = item.get("kind")
        if kind == "topic":
            slug = str(item.get("id") or "")
            item["paths"] = extract_paths(bodies.get(slug, ""))
            item["neighbors"] = neighbors_by_slug.get(slug, [])
        elif kind == "episode":
            item["paths"] = extract_paths(str(item.get("snippet") or ""))
        elif kind == "media":
            # Snippet/label is a relative path or filename from media_assets.
            item["paths"] = extract_paths(str(item.get("snippet") or item.get("label") or ""))
        out.append(item)
    return out


def topic_slug_from_label(raw: str) -> str:
    """Capture topic labels → slug. ``OpenBot`` and ``openbot`` collapse."""
    s = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    return s[:80]


def persist_capture_graph(cur, payload: Mapping[str, Any]) -> dict[str, int]:
    """Mint topic:/path: wiki from a capture payload (no topic markdown file).

    Cloud hub captures never see the file wiki, so this is how an episode's
    ``topics`` array becomes a graph neighborhood.
    """
    slugs: list[str] = []
    for item in payload.get("topics") or []:
        slug = topic_slug_from_label(str(item))
        if slug and slug not in slugs:
            slugs.append(slug)
        if len(slugs) >= 8:
            break
    if not slugs:
        return {"nodes_minted": 0, "edges_minted": 0}
    body = str(payload.get("summary") or "")
    hub = slugs[0]
    totals = {"nodes_minted": 0, "edges_minted": 0}
    for slug in slugs:
        others = [s for s in slugs if s != slug]
        links = others[:5] if slug == hub else [hub]
        stats = persist_topic_graph(
            cur,
            {"slug": slug, "title": slug, "links": links, "body": body},
            dry_run=False,
        )
        totals["nodes_minted"] += stats["nodes_minted"]
        totals["edges_minted"] += stats["edges_minted"]
    return totals


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def graph_query_aliases(node_id: str) -> list[str]:
    """Alias set for ``_graph_query``: topic-shaped ids expand; others stay singleton.

    All-digit ids are episode primary keys, not topic slugs — ``_graph_query``
    resolves those separately. Treating ``9320`` as ``topic:9320`` returned an
    empty neighborhood and looked like a missing graph.
    """
    s = (node_id or "").strip()
    if s.isdigit():
        return []
    if s.startswith(TOPIC_PREFIX) or s.startswith(MEMORY_TOPIC_PREFIX) or (
        s and ":" not in s
    ):
        return topic_aliases(s)
    return [s]
