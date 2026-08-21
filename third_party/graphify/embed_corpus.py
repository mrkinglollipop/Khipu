"""
Bite 3.5: corpus embedder

Embeds text-bearing graph nodes (corpus authors, memory topics, biblical
entries, ticker-wiki bodies) using Voyage voyage-3. Stores
vectors in the `embeddings` table of UNIFICATION/state/graph.sqlite.

Properties:
- Idempotent: skips nodes/files already embedded with the same model
- Atomic: each batch is its own transaction
- Cheap: voyage-3 is $0.06 per 1M tokens (50M free tier). Full corpus ≈ $0.10
- Soft-fail: missing API key, network error, or token-limit overflow logs
  and continues rather than crashing the whole run

Usage:
  python embed_corpus.py                         # embed everything pending
  python embed_corpus.py --types corpus_author   # only this type
  python embed_corpus.py --node ticker:NVDA      # only this node
  python embed_corpus.py --dry-run               # estimate cost, no API calls
  python embed_corpus.py --rebuild               # delete + re-embed all

API key location: API Keys/VoyageAI API Key.txt (one line, the key).
Override via env: VOYAGE_API_KEY.

Embedding model: voyage-3 (1024 dims, $0.06 / 1M tokens paid tier).
Override via env: EMBED_MODEL (must be a Voyage embeddings model id).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# ── workspace + config ────────────────────────────────────────────────────────

def _find_workspace() -> Path:
    raw = (os.environ.get("KHIPU_GRAPHIFY_WORKSPACE") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if path.is_dir():
            return path
    return Path(__file__).resolve().parent / "graphify_workspace"


WORKSPACE = _find_workspace()
DEFAULT_DB = Path(
    os.environ.get(
        "KHIPU_GRAPH_SQLITE",
        str(Path.home() / "Library" / "Application Support" / "Khipu" / "graph" / "graph.sqlite"),
    )
)


def _biblical_data_root() -> Path:
    env = (os.environ.get("BIBLICAL_DATA_DIR") or "").strip()
    if env and Path(env).is_dir():
        return Path(env)
    return WORKSPACE / "biblical_data"


BIBLICAL_DATA_ROOT = _biblical_data_root()


def _portable_relpath(f: Path) -> str:
    """Path stored in embeddings.source_file, relative to whichever known root
    holds the file: the biblical data root (corpus lives there post-extraction)
    or the workspace. Absolute path as a last resort. Prevents the
    relative_to(WORKSPACE) crash on corpus files that no longer live in-tree."""
    for base in (BIBLICAL_DATA_ROOT, WORKSPACE):
        try:
            return str(f.resolve().relative_to(base.resolve()))
        except ValueError:
            continue
    return str(f)

EMBED_MODEL = os.environ.get("EMBED_MODEL", "voyage-3")
EMBED_DIMS = 1024  # voyage-3 default
COST_PER_1M_TOKENS_USD = 0.06  # voyage-3 paid tier; free tier 50M tokens
CHUNK_SIZE_CHARS = 6000  # ~1500 tokens per chunk
BATCH_SIZE = 128         # voyage max per request

VOYAGE_ENDPOINT = "https://api.voyageai.com/v1/embeddings"

# Token-aware batching shared by node-chunk and edge-triplet embed paths.
# Voyage caps a batch at 128 inputs AND 320k tokens. Observed corpus batches
# hit ~1.1 chars/token (Hebrew/Greek lexicon text is extremely token-dense),
# so estimate 1 char ~= 1 token and budget 220k for margin. Also hard-cap any
# single chunk so one monster input can't fail the whole batch.
TOKEN_BUDGET = 220_000
CHARS_PER_TOKEN = 1.0
MAX_CHUNK_CHARS = 200_000

# Edge-embed nightly guardrails (Session C, 2026-07-18). Nightly deltas are
# expected to be tiny (deterministic edge: keys dedupe on rebuild — only
# genuinely new/changed edges are ever pending), but a defer cap protects the
# 900s run_step timeout budget if something upstream ever produces a huge
# batch. Stale-edge prune only fires below this ratio so a real graph
# regression doesn't get silently mass-deleted.
MAX_PENDING_EDGES = 20_000
STALE_PRUNE_MAX_RATIO = 0.10  # applies independently to edge: rows and to node rows


def make_batches(items: list[dict]) -> Iterator[list[dict]]:
    cur: list[dict] = []
    cur_tok = 0.0
    for p in items:
        if len(p["chunk_text"]) > MAX_CHUNK_CHARS:
            p["chunk_text"] = p["chunk_text"][:MAX_CHUNK_CHARS]
        tok = len(p["chunk_text"]) / CHARS_PER_TOKEN
        if cur and (len(cur) >= BATCH_SIZE or cur_tok + tok > TOKEN_BUDGET):
            yield cur
            cur, cur_tok = [], 0.0
        cur.append(p)
        cur_tok += tok
    if cur:
        yield cur


def _load_api_key() -> Optional[str]:
    if k := os.environ.get("VOYAGE_API_KEY"):
        return k.strip()
    for cand in (
        WORKSPACE / "API Keys" / "VoyageAI API Key.txt",
        WORKSPACE / "API Keys" / "Voyage API Key.txt",
        WORKSPACE / "API Keys" / "voyage.txt",
    ):
        if cand.exists():
            return cand.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    return None


# ── text extraction per node type ─────────────────────────────────────────────

def extract_text_chunks(node: dict, doc_lookup: Optional[dict] = None) -> Iterator[tuple[str, str]]:
    """Yield (source_file_relpath, chunk_text) for a node. Skips nodes with no
    discoverable text content.

    doc_lookup: optional {node_id: docstring_text} map for function/module/class
    nodes, sourced from their rationale_for-linked concept doc node (the code
    node itself carries no docstring in payload — see build_graph.py collector).
    """
    # concept nodes carry their text directly in the `name` field (code-graph
    # summaries, function purposes, domain tags, docstring snippets) — no file.
    if node.get("type") == "concept":
        name = (node.get("name") or "").strip()
        if name:
            yield (node.get("source_path") or node["id"], name)
        return

    src = node.get("source_path")
    if not src:
        return

    typ = node["type"]

    if typ in ("function", "module", "class"):
        # No file body read — these nodes carry only a short name (+ relpath
        # for module) in-graph; docstring text (if any) lives on a separate
        # concept:*__doc node reached via a rationale_for edge, resolved by
        # the caller into doc_lookup. Bare names alone are low-signal for
        # semantic search, so fold the docstring in when available.
        parts = [(node.get("name") or "").strip()]
        try:
            payload = json.loads(node.get("payload") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        if payload.get("relpath"):
            parts.append(payload["relpath"])
        doc = (doc_lookup or {}).get(node["id"])
        if doc:
            parts.append(doc.strip())
        text = " — ".join(p for p in parts if p)
        if text:
            yield (src, text)
        return

    abs_src = WORKSPACE / src
    if not abs_src.exists():
        return

    if typ == "corpus_author" and abs_src.is_dir():
        # walk every .txt / .md / .epub in the author dir
        for f in sorted(abs_src.rglob("*")):
            if f.suffix.lower() not in (".txt", ".md"):
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            for chunk in _chunk_text(text):
                yield (_portable_relpath(f), chunk)

    elif typ in ("memory_topic", "biblical_entry") and abs_src.is_file():
        try:
            text = abs_src.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        for chunk in _chunk_text(text):
            yield (src, chunk)

    elif typ == "ticker" and abs_src.is_file():
        # embed the wiki body (skip frontmatter)
        try:
            text = abs_src.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        body = _strip_frontmatter(text)
        if not body.strip():
            return
        for chunk in _chunk_text(body):
            yield (src, chunk)

    elif typ == "skill" and abs_src.is_dir():
        # embed SKILL.md if present (skill description)
        sm = abs_src / "SKILL.md"
        if sm.exists():
            try:
                text = sm.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                return
            body = _strip_frontmatter(text)
            for chunk in _chunk_text(body):
                yield (str(sm.relative_to(WORKSPACE)), chunk)


def _strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    m = re.match(r"^---\n.*?\n---\n", text, re.DOTALL)
    if m:
        return text[m.end():]
    return text


def _chunk_text(text: str) -> Iterator[str]:
    """Split into chunks roughly CHUNK_SIZE_CHARS each, breaking on paragraph
    boundaries when possible."""
    text = text.strip()
    if not text:
        return
    if len(text) <= CHUNK_SIZE_CHARS:
        yield text
        return
    # split by double-newline (paragraphs); pack greedily
    paras = re.split(r"\n\s*\n", text)
    cur = []
    cur_len = 0
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if cur_len + len(p) + 2 > CHUNK_SIZE_CHARS and cur:
            yield "\n\n".join(cur)
            cur, cur_len = [], 0
        if len(p) > CHUNK_SIZE_CHARS:
            # paragraph itself too big — hard split
            for i in range(0, len(p), CHUNK_SIZE_CHARS):
                yield p[i:i + CHUNK_SIZE_CHARS]
        else:
            cur.append(p)
            cur_len += len(p) + 2
    if cur:
        yield "\n\n".join(cur)


# ── Voyage client ─────────────────────────────────────────────────────────────

def embed_batch(texts: list[str], api_key: str, model: str = EMBED_MODEL,
                input_type: str = "document",
                retries: int = 3) -> Optional[list[list[float]]]:
    payload = json.dumps({
        "model": model,
        "input": texts,
        "input_type": input_type,
    }).encode("utf-8")
    req = urllib.request.Request(
        VOYAGE_ENDPOINT, data=payload,
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                # Voyage returns {data: [{embedding: [...], index: N}, ...]}
                # Sort by index to preserve input order
                data = sorted(body["data"], key=lambda d: d.get("index", 0))
                return [d["embedding"] for d in data]
        except urllib.error.HTTPError as e:
            if e.code in (429, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            print(f"  ERROR (HTTP {e.code}): {e.read()[:300].decode('utf-8', 'ignore')}",
                  file=sys.stderr)
            return None
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            print(f"  ERROR: {e}", file=sys.stderr)
            return None
    return None


# ── persistence ───────────────────────────────────────────────────────────────

def pack_vector(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_vector(blob: bytes) -> list[float]:
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def existing_keys(conn: sqlite3.Connection, model: str) -> set[tuple[str, int]]:
    c = conn.cursor()
    c.execute("SELECT node_id, chunk_idx FROM embeddings WHERE model = ?", (model,))
    return {(r[0], r[1]) for r in c.fetchall()}


def insert_embeddings(conn: sqlite3.Connection, rows: list[dict]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO embeddings "
        "(node_id, chunk_idx, source_file, chunk_text, embedding, model, dims, built_at) "
        "VALUES (:node_id, :chunk_idx, :source_file, :chunk_text, :embedding, :model, :dims, :built_at)",
        rows,
    )
    conn.commit()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--types", nargs="*",
                        default=["corpus_author", "memory_topic", "biblical_entry", "ticker",
                                 "skill", "function", "module", "class"],
                        help="node types to embed")
    parser.add_argument("--node", help="embed only this single node id")
    parser.add_argument("--dry-run", action="store_true",
                        help="count chunks + estimate cost without API calls")
    parser.add_argument("--rebuild", action="store_true",
                        help="delete existing embeddings for matched nodes first")
    parser.add_argument("--limit", type=int, default=0,
                        help="limit total chunks (for testing; 0 = unlimited)")
    parser.add_argument("--edges", action="store_true",
                        help="embed edge triplets instead of node text (see --edge-types)")
    parser.add_argument("--edge-types", nargs="*",
                        default=["rationale_for", "concept_link"],
                        help="edge types to embed when --edges is set")
    parser.add_argument("--max-pending-edges", type=int, default=MAX_PENDING_EDGES,
                        help="if --edges finds more than this many pending rows, "
                             "log and defer (skip API calls) instead of risking a "
                             "run_step timeout; bump for a manual catch-up run")
    # Renamed --prune-stale-edges -> --prune-stale (now covers node-row
    # orphans too, not just edge: rows) and kept the old flag as an alias to
    # the same dest so graphify_nightly.py step 3b's hardcoded
    # ["--edges", "--prune-stale-edges"] args list keeps working unchanged.
    parser.add_argument("--prune-stale", "--prune-stale-edges",
                        dest="prune_stale", action="store_true",
                        help="log (and, if the stale ratio is sane, delete) "
                             "orphaned embeddings rows: edge: rows whose "
                             "underlying edge no longer exists in `edges`, "
                             "AND non-edge: node rows whose node_id no "
                             "longer exists in `nodes`. --prune-stale-edges "
                             "is a kept alias for backward compat")
    args = parser.parse_args()

    if not args.db.exists():
        print(f"ERROR: graph not found at {args.db} — run build_graph.py first",
              file=sys.stderr)
        return 1

    api_key = _load_api_key()
    if not args.dry_run and not api_key:
        print("ERROR: VOYAGE_API_KEY not set and 'API Keys/VoyageAI API Key.txt' missing.",
              file=sys.stderr)
        print("       Run with --dry-run to estimate cost without an API key.",
              file=sys.stderr)
        return 1

    # Stage the db on local /tmp to dodge Dropbox-mount sqlite I/O errors.
    # Original is preserved; we copy back at end (even on failure).
    import shutil
    import tempfile
    fd, tmp_str = tempfile.mkstemp(prefix="embed_stage_", suffix=".sqlite")
    os.close(fd)
    staged_db = Path(tmp_str)
    shutil.copyfile(str(args.db), str(staged_db))
    print(f"staged: {staged_db}")
    print()

    try:
        return _run_main(args, staged_db, api_key)
    finally:
        # Byte-copy /tmp → original (works around Dropbox unlink/rename issues)
        try:
            with open(staged_db, "rb") as src, open(args.db, "wb") as dst:
                while True:
                    chunk = src.read(1 << 20)
                    if not chunk:
                        break
                    dst.write(chunk)
            staged_db.unlink(missing_ok=True)
        except Exception as e:
            print(f"\nWARNING: copy-back failed: {e}", file=sys.stderr)
            print(f"         staged db preserved at {staged_db}", file=sys.stderr)
            print(f"         to recover manually: cp '{staged_db}' '{args.db}'", file=sys.stderr)


def _run_main(args, staged_db: Path, api_key: Optional[str]) -> int:
    conn = sqlite3.connect(str(staged_db))
    conn.row_factory = sqlite3.Row

    if args.edges or args.prune_stale:
        rc_edges = 0
        if args.edges:
            rc_edges = _run_edge_embeddings(args, conn, api_key)
        rc_prune = 0
        if args.prune_stale:
            rc_prune = _run_prune_stale(conn, args.dry_run)
        conn.close()
        return rc_edges or rc_prune

    # Select target nodes
    if args.node:
        c = conn.cursor()
        c.execute("SELECT * FROM nodes WHERE id = ?", (args.node,))
        rows = c.fetchall()
    else:
        placeholders = ",".join("?" * len(args.types))
        c = conn.cursor()
        c.execute(f"SELECT * FROM nodes WHERE type IN ({placeholders}) ORDER BY type, id",
                  args.types)
        rows = c.fetchall()
    nodes = [dict(r) for r in rows]
    if not nodes:
        print("no nodes match. exiting.")
        return 0

    # --dry-run must not mutate the database. These DELETEs used to run before
    # the dry-run guard further down, so `--rebuild --dry-run` silently dropped
    # every embedding for the targeted nodes and then reported "no rows written".
    if args.rebuild and args.dry_run:
        print(f"[dry-run] --rebuild would delete existing {EMBED_MODEL} embeddings "
              f"for {1 if args.node else len(nodes)} node(s) before re-embedding.")
    elif args.rebuild and args.node:
        conn.execute("DELETE FROM embeddings WHERE node_id = ? AND model = ?",
                     (args.node, EMBED_MODEL))
        conn.commit()
    elif args.rebuild:
        ids = [n["id"] for n in nodes]
        for batch in [ids[i:i+500] for i in range(0, len(ids), 500)]:
            conn.execute(
                f"DELETE FROM embeddings WHERE model = ? AND node_id IN "
                f"({','.join('?' * len(batch))})",
                [EMBED_MODEL] + batch,
            )
        conn.commit()

    # After a real --rebuild the targeted rows are gone, so nothing is skippable.
    # Treat `existing` as empty whenever --rebuild is set so a dry run reports the
    # same pending count the real run would, without touching the database.
    existing = set() if args.rebuild else existing_keys(conn, EMBED_MODEL)

    # function/module/class nodes carry no docstring themselves — resolve it
    # from the linked concept:*__doc node (rationale_for edge) once, up front.
    doc_lookup: dict = {}
    if {"function", "module", "class"} & set(args.types):
        dc = conn.cursor()
        dc.execute("""
            SELECT e.dst, n.name FROM edges e JOIN nodes n ON n.id = e.src
            WHERE e.type = 'rationale_for' AND n.type = 'concept'
        """)
        for dst_id, doc_name in dc.fetchall():
            doc_lookup.setdefault(dst_id, doc_name)

    # Walk nodes → collect pending (node_id, chunk_idx, source_file, chunk_text)
    pending: list[dict] = []
    skipped_existing = 0
    for n in nodes:
        for chunk_idx, (src_file, chunk) in enumerate(extract_text_chunks(n, doc_lookup)):
            if (n["id"], chunk_idx) in existing and not args.rebuild:
                skipped_existing += 1
                continue
            pending.append({
                "node_id": n["id"],
                "chunk_idx": chunk_idx,
                "source_file": src_file,
                "chunk_text": chunk,
            })
            if args.limit and len(pending) >= args.limit:
                break
        if args.limit and len(pending) >= args.limit:
            break

    total_chars = sum(len(p["chunk_text"]) for p in pending)
    est_tokens = total_chars / 4  # rough chars-to-tokens heuristic
    est_cost = (est_tokens / 1_000_000) * COST_PER_1M_TOKENS_USD

    print(f"workspace:        {WORKSPACE}")
    print(f"db:               {args.db}")
    print(f"model:            {EMBED_MODEL}")
    print(f"matched nodes:    {len(nodes)}")
    print(f"existing chunks:  {len(existing)} (skipped: {skipped_existing})")
    print(f"pending chunks:   {len(pending)}")
    print(f"total chars:      {total_chars:,}")
    print(f"estimated tokens: {int(est_tokens):,}")
    print(f"estimated cost:   ${est_cost:.4f}")

    if args.dry_run:
        print("\n[dry-run] no API calls made, no rows written.")
        return 0

    if not pending:
        print("\nnothing to embed.")
        return 0

    batches = list(make_batches(pending))
    print()
    print(f"embedding {len(pending)} chunks in {len(batches)} token-aware batches...")
    written = 0
    failed = 0
    seen = 0
    for batch in batches:
        seen += len(batch)
        texts = [p["chunk_text"] for p in batch]
        vecs = embed_batch(texts, api_key)
        if vecs is None:
            failed += len(batch)
            continue
        rows = []
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for p, v in zip(batch, vecs):
            rows.append({
                **p,
                "embedding": pack_vector(v),
                "model": EMBED_MODEL,
                "dims": len(v),
                "built_at": now_iso,
            })
        insert_embeddings(conn, rows)
        written += len(rows)
        print(f"  written {written} / {len(pending)} chunks "
              f"(seen {seen})")

    conn.close()

    print()
    print("=== DONE ===")
    print(f"  wrote:  {written}")
    print(f"  failed: {failed}")
    return 0 if failed == 0 else 1


# ── edge-triplet embedding (Bite 3.5b — cognee-style relation embedding) ──────

def _edge_triplet_text(src_name: str, edge_type: str, dst_name: str,
                        dst_path: Optional[str]) -> str:
    """`{src name} —{edge type}→ {dst name}`, plus the target's source path
    as a trimmed signal-adding snippet when present (per scope doc spec)."""
    src_name = (src_name or "").strip()
    dst_name = (dst_name or "").strip()
    text = f"{src_name} —{edge_type}→ {dst_name}"
    if dst_path:
        text += f" ({dst_path})"
    return text


def _run_edge_embeddings(args, conn: sqlite3.Connection, api_key: Optional[str]) -> int:
    """Embed edge triplets (rationale_for / concept_link by default) as
    `edge:{src}|{type}|{dst}` rows, chunk_idx 0. Edges aren't rows in `nodes`,
    so this is a separate code path from the node-chunk walk above — never
    goes through extract_text_chunks."""
    existing = existing_keys(conn, EMBED_MODEL)

    placeholders = ",".join("?" * len(args.edge_types))
    c = conn.cursor()
    c.execute(f"""
        SELECT e.src, e.dst, e.type,
               ns.name AS src_name, nd.name AS dst_name, nd.source_path AS dst_path
        FROM edges e
        JOIN nodes ns ON ns.id = e.src
        JOIN nodes nd ON nd.id = e.dst
        WHERE e.type IN ({placeholders})
        ORDER BY e.type, e.src, e.dst
    """, args.edge_types)
    edge_rows = c.fetchall()

    if args.rebuild:
        ids = [f"edge:{r['src']}|{r['type']}|{r['dst']}" for r in edge_rows]
        for batch in [ids[i:i + 500] for i in range(0, len(ids), 500)]:
            conn.execute(
                f"DELETE FROM embeddings WHERE model = ? AND node_id IN "
                f"({','.join('?' * len(batch))})",
                [EMBED_MODEL] + batch,
            )
        conn.commit()
        existing = existing_keys(conn, EMBED_MODEL)

    pending: list[dict] = []
    skipped_existing = 0
    for r in edge_rows:
        node_id = f"edge:{r['src']}|{r['type']}|{r['dst']}"
        if (node_id, 0) in existing and not args.rebuild:
            skipped_existing += 1
            continue
        text = _edge_triplet_text(r["src_name"], r["type"], r["dst_name"], r["dst_path"])
        if not text.strip():
            continue
        pending.append({
            "node_id": node_id,
            "chunk_idx": 0,
            "source_file": r["dst_path"] or r["dst"],
            "chunk_text": text,
        })
        if args.limit and len(pending) >= args.limit:
            break

    # Dedupe identical triplet texts before hitting the API — rationale_for
    # docstrings repeat verbatim across many code nodes (generic boilerplate
    # like "Return dict matching the Output contract in SKILL.md."); embedding
    # the same string 16x wastes tokens for zero extra signal. Every edge
    # still gets its own row in `embeddings` — rows just share a vector.
    text_to_rows: dict[str, list[dict]] = {}
    for p in pending:
        text_to_rows.setdefault(p["chunk_text"], []).append(p)
    unique_texts = list(text_to_rows.keys())

    total_chars = sum(len(t) for t in unique_texts)
    est_tokens = total_chars / 4
    est_cost = (est_tokens / 1_000_000) * COST_PER_1M_TOKENS_USD

    print(f"edge types:       {args.edge_types}")
    print(f"matched edges:    {len(edge_rows)}")
    print(f"existing rows:    {len(existing)} (skipped: {skipped_existing})")
    print(f"pending rows:     {len(pending)}")
    print(f"unique texts:     {len(unique_texts)} "
          f"(dedup saves {len(pending) - len(unique_texts)} API embeds)")
    print(f"total chars:      {total_chars:,} (unique texts only)")
    print(f"estimated tokens: {int(est_tokens):,}")
    print(f"estimated cost:   ${est_cost:.4f}")

    max_pending = getattr(args, "max_pending_edges", MAX_PENDING_EDGES)
    if len(pending) > max_pending:
        print(f"\n[defer] pending rows ({len(pending)}) exceed --max-pending-edges "
              f"({max_pending}) — nightly run_step timeout guard.")
        if args.dry_run:
            print("        (dry-run: estimate only, not deferring)")
        else:
            print("        skipping API calls this pass — deterministic edge: "
                  "keys mean nothing is lost, the next incremental run (or a "
                  "manual --max-pending-edges override) will pick this up.")
            return 3

    if args.dry_run:
        print("\n[dry-run] no API calls made, no rows written.")
        return 0

    if not pending:
        print("\nnothing to embed.")
        return 0

    unique_stubs = [{"chunk_text": t} for t in unique_texts]
    batches = list(make_batches(unique_stubs))
    print()
    print(f"embedding {len(unique_texts)} unique texts in {len(batches)} token-aware "
          f"batches (covering {len(pending)} edge rows)...")

    # Commit per batch (matches the node path at line ~537) instead of
    # accumulating every row and writing once at the end — a mid-run kill on
    # a large edge backfill previously lost 100% of that run's API spend
    # since nothing was durable until the final insert_embeddings() call.
    # existing_keys() at the top of this function makes a resumed re-run
    # idempotent: already-committed rows are skipped, not re-embedded.
    written = 0
    failed = 0
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for batch in batches:
        texts = [p["chunk_text"] for p in batch]
        vecs = embed_batch(texts, api_key)
        if vecs is None:
            failed += sum(len(text_to_rows[t]) for t in texts)
            continue
        batch_rows = []
        for t, v in zip(texts, vecs):
            packed = pack_vector(v)
            dims = len(v)
            for p in text_to_rows[t]:
                batch_rows.append({
                    **p,
                    "embedding": packed,
                    "model": EMBED_MODEL,
                    "dims": dims,
                    "built_at": now_iso,
                })
        insert_embeddings(conn, batch_rows)  # commits internally — partial progress persists
        written += len(batch_rows)
        print(f"  written {written} / {len(pending)} edge rows")

    print()
    print("=== DONE (edges) ===")
    print(f"  wrote:  {written}")
    print(f"  failed: {failed}")
    return 0 if failed == 0 else 1


# ── stale embedding hygiene (Session C, 2026-07-18; generalized to node
#    orphans in the audit-fix pass on 2026-07-18) ─────────────────────────────
#
# build_graph.py rewrites the entire nodes + edges tables on every rebuild,
# but the embeddings table is additive-only (existing_keys() only ever skips
# or adds; nothing here deletes on a normal embed run). Two independent ways
# a row can go stale:
#   - edge: rows — a rationale_for/concept_link edge disappears from a
#     rebuild (source function deleted, docstring concept dropped, etc.).
#   - non-edge: (node) rows — a node is renamed or removed (e.g. a function
#     rename changes its node id), leaving its old embedding row pointing at
#     a node_id no longer in `nodes`. One real example: a
#     function:...::_run_main._make_batches row orphaned by this session's
#     own rename.
# Both are real rows pointing at nothing, polluting semantic search results
# forever unless something prunes them. This is the ONLY delete this script
# performs, and each category is scoped strictly to rows with no matching
# target in the current graph, with its own ratio cap so a real graph
# regression (mass node/edge loss) gets logged, not silently mass-deleted.

def _stale_edge_report(conn: sqlite3.Connection) -> dict:
    """Every edge: embedding row, split into (still-live, stale, malformed)."""
    c = conn.cursor()
    c.execute("SELECT node_id FROM embeddings WHERE node_id LIKE 'edge:%'")
    edge_node_ids = [r[0] for r in c.fetchall()]

    c.execute("SELECT src || '|' || type || '|' || dst FROM edges")
    live_keys = {r[0] for r in c.fetchall()}

    stale_ids: list[str] = []
    malformed = 0
    for node_id in edge_node_ids:
        key = node_id[len("edge:"):]
        if key.count("|") != 2:
            malformed += 1
            continue
        if key not in live_keys:
            stale_ids.append(node_id)

    return {"total": len(edge_node_ids), "stale_ids": stale_ids, "malformed": malformed}


def _stale_node_report(conn: sqlite3.Connection) -> dict:
    """Every non-edge: embedding row (one per node, per chunk_idx) whose
    node_id has no matching row in `nodes` — i.e. a node embedding orphaned
    by a rename or removal since it was embedded."""
    c = conn.cursor()
    c.execute("SELECT node_id FROM embeddings WHERE node_id NOT LIKE 'edge:%'")
    node_ids = [r[0] for r in c.fetchall()]

    c.execute("SELECT id FROM nodes")
    live_ids = {r[0] for r in c.fetchall()}

    stale_ids = [nid for nid in node_ids if nid not in live_ids]
    return {"total": len(node_ids), "stale_ids": stale_ids}


def _run_prune_stale(conn: sqlite3.Connection, dry_run: bool) -> int:
    edge_report = _stale_edge_report(conn)
    node_report = _stale_node_report(conn)

    edge_total, edge_stale = edge_report["total"], edge_report["stale_ids"]
    node_total, node_stale = node_report["total"], node_report["stale_ids"]
    edge_ratio = len(edge_stale) / edge_total if edge_total else 0.0
    node_ratio = len(node_stale) / node_total if node_total else 0.0

    print()
    print("=== stale embedding check ===")
    print(f"  edge: rows total:            {edge_total}")
    print(f"  edge: rows stale:            {len(edge_stale)} ({edge_ratio:.1%})")
    if edge_report["malformed"]:
        print(f"  edge: rows malformed (skipped, not counted as stale): "
              f"{edge_report['malformed']}")
    print(f"  node rows total (non-edge:): {node_total}")
    print(f"  node rows stale (orphaned):  {len(node_stale)} ({node_ratio:.1%})")

    if not edge_stale and not node_stale:
        print("  nothing stale — no prune needed.")
        return 0

    if dry_run:
        print(f"  [dry-run] edge ratio {edge_ratio:.1%}, node ratio {node_ratio:.1%} "
              f"(cap {STALE_PRUNE_MAX_RATIO:.0%} each); no deletes made.")
        return 0

    to_delete: list[str] = []

    if edge_stale:
        if edge_ratio >= STALE_PRUNE_MAX_RATIO:
            print(f"  edge stale ratio {edge_ratio:.1%} >= cap "
                  f"{STALE_PRUNE_MAX_RATIO:.0%} — logging only, NOT pruning "
                  f"(looks like a real graph regression, not routine drift; "
                  f"needs manual review).")
        else:
            to_delete.extend(edge_stale)

    if node_stale:
        if node_ratio >= STALE_PRUNE_MAX_RATIO:
            print(f"  node stale ratio {node_ratio:.1%} >= cap "
                  f"{STALE_PRUNE_MAX_RATIO:.0%} — logging only, NOT pruning "
                  f"(looks like a real graph regression, not routine drift; "
                  f"needs manual review).")
        else:
            to_delete.extend(node_stale)

    if not to_delete:
        print("  nothing pruned (all stale categories over cap).")
        return 0

    for batch in [to_delete[i:i + 500] for i in range(0, len(to_delete), 500)]:
        conn.execute(
            f"DELETE FROM embeddings WHERE node_id IN "
            f"({','.join('?' * len(batch))})",
            batch,
        )
    conn.commit()
    pruned_edge = sum(1 for nid in to_delete if nid.startswith("edge:"))
    pruned_node = len(to_delete) - pruned_edge
    print(f"  pruned {len(to_delete)} stale rows total "
          f"({pruned_edge} edge:, {pruned_node} node).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
