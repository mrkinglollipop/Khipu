"""
Bite 2: graph builder

Reads workspace state and produces graph.sqlite — the master
graph that the visual map (bite 3) and dashboard (bite 6) consume.

Sources read:
- wiki/{bucket}/*.md                           → ticker nodes
- skills/**/MANIFEST.yaml                       → skill nodes (+ edges from manifest)
- Agents/*/                                     → agent nodes
- Reports/**/*.pdf                              → report nodes (+ edges to tickers)
- memory/conversations/topics/*.md              → memory_topic nodes
- skills/_shared/predictive-gates/state/phase_* → gate nodes (mirrored, frozen)
- frozen_tell/cascade/                          → sentiment_run nodes (mirrored, frozen)

Hardcoded (because they don't live on disk in a graph-shaped form yet):
- Data sources (FMP, Tavily, Bigdata, MMD, Finnhub, FRED, EDGAR, LunarCrush,
  WebSearch, OpenRouter, DeepSeek, Notion API)
- Notion DBs (Reports, Watchlist_State, Screener_Fires, Direction_Changes,
  Tell · Composite Signal, Tell · Market Sentiment Daily, Tell · Stock
  Sentiment Timeline, Tell · Action Items)
- Cross-cutting edges (orchestrator → reviewers, reviewers → shared infra)

Properties:
- Idempotent (rebuilds graph.sqlite from scratch every run; no diff logic)
- Atomic (writes to .tmp, then os.replace)
- Format: SQLite (queryable, portable, no server)

Usage:
  python build_graph.py
  python build_graph.py --summary
  python build_graph.py --output /tmp/test.sqlite
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML required. Install with: pip3 install pyyaml --break-system-packages", file=sys.stderr)
    sys.exit(2)


# ── workspace resolution ──────────────────────────────────────────────────────

def _app_support_dir() -> Path:
    return Path.home() / "Library" / "Application Support" / "Khipu"


def _find_workspace() -> Path:
    raw = (os.environ.get("KHIPU_GRAPHIFY_WORKSPACE") or "").strip()
    if raw:
        path = Path(raw).expanduser()
        if path.is_dir():
            return path
    return _app_support_dir() / "graphify_workspace"


def _default_graph_sqlite() -> Path:
    raw = (os.environ.get("KHIPU_GRAPH_SQLITE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    out = _app_support_dir() / "graph" / "graph.sqlite"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _memory_root() -> Path:
    raw = (os.environ.get("KHIPU_MEMORY_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _app_support_dir() / "memory"


WORKSPACE = _find_workspace()
DEFAULT_OUT = _default_graph_sqlite()
MEMORY_ROOT = _memory_root()
# Subdir under WORKSPACE holding graphify's own state (model call ledger,
# etc). A maintainer whose workspace layout nests this under a differently
# named directory sets KHIPU_GRAPHIFY_STATE_DIR; default is portable.
GRAPHIFY_STATE_SUBDIR = (os.environ.get("KHIPU_GRAPHIFY_STATE_DIR") or "state").strip()
NOW_ISO = datetime.now(timezone.utc).isoformat(timespec="seconds")

RESOLVED_SOURCES = Path(
    os.environ.get(
        "KHIPU_GRAPH_SOURCES_RESOLVED",
        str(_app_support_dir() / "graph_sources.resolved.json"),
    )
)


def _membership() -> dict | None:
    """Load graph_sources.resolved.json when present; else None (all collectors on).

    A missing file is absent → all collectors on. A *present* unreadable or
    non-object file is fail-closed for this build (do not treat as all-on).
    """
    path = Path(RESOLVED_SOURCES)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        msg = f"unreadable graph_sources.resolved.json at {path}: {e}"
        print(f"ERROR: {msg}", file=sys.stderr)
        raise RuntimeError(msg) from e
    if not isinstance(raw, dict):
        msg = f"graph_sources.resolved.json at {path} is not a JSON object"
        print(f"ERROR: {msg}", file=sys.stderr)
        raise RuntimeError(msg)
    coll = raw.get("collectors") if isinstance(raw.get("collectors"), dict) else {}
    flags = {
        key: False if coll.get(key) is False else True
        for key in (
            "tickers",
            "skills",
            "agents",
            "reports",
            "memory_topics",
            "predictive_gates",
            "frozen_tell",
            "hardcoded_data_sources",
            "hardcoded_notion_dbs",
            "biblical",
            "model_call_log",
            "code_ast",
            "code_semantic",
        )
    }
    code_roots = raw.get("code_roots")
    if not isinstance(code_roots, list):
        code_roots = []
    return {"collectors": flags, "code_roots": [str(p) for p in code_roots]}


def _collector_on(m: dict | None, flag: str) -> bool:
    return m is None or m["collectors"].get(flag, True)


# ── schema ────────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  bucket TEXT,
  name TEXT,
  payload TEXT,            -- JSON
  source_path TEXT,
  built_at TEXT,
  frozen INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS edges (
  src TEXT NOT NULL,
  dst TEXT NOT NULL,
  type TEXT NOT NULL,
  weight REAL,
  payload TEXT,            -- JSON
  built_at TEXT,
  FOREIGN KEY (src) REFERENCES nodes(id),
  FOREIGN KEY (dst) REFERENCES nodes(id)
);
CREATE TABLE IF NOT EXISTS model_call_log (
  ts TEXT,
  task_type TEXT,
  model TEXT,
  provider TEXT,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cost_usd REAL,
  duration_ms INTEGER,
  status TEXT,
  caller_script TEXT,
  identifier TEXT
);
CREATE TABLE IF NOT EXISTS embeddings (
  node_id TEXT NOT NULL,
  chunk_idx INTEGER NOT NULL DEFAULT 0,
  source_file TEXT,
  chunk_text TEXT,
  embedding BLOB,           -- packed float32 vector
  model TEXT,
  dims INTEGER,
  built_at TEXT,
  PRIMARY KEY (node_id, chunk_idx)
);
CREATE INDEX IF NOT EXISTS idx_nodes_type   ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_bucket ON nodes(bucket);
CREATE INDEX IF NOT EXISTS idx_edges_src    ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst    ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_edges_type   ON edges(type);
CREATE INDEX IF NOT EXISTS idx_emb_node     ON embeddings(node_id);
CREATE INDEX IF NOT EXISTS idx_emb_model    ON embeddings(model);
"""


# ── helpers ───────────────────────────────────────────────────────────────────

def parse_frontmatter(text: str) -> Optional[dict]:
    """Extract YAML frontmatter from a markdown file. Returns parsed dict or None."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    end = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return None
    fm_text = "\n".join(lines[1:end])
    try:
        return yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return None


def relpath(p: Path) -> str:
    try:
        return str(p.relative_to(WORKSPACE))
    except ValueError:
        return str(p)


def make_node(node_id: str, type_: str, name: str, *,
              bucket: str = None, payload: dict = None,
              source_path: str = None, frozen: bool = False) -> dict:
    return {
        "id": node_id,
        "type": type_,
        "bucket": bucket,
        "name": name,
        "payload": json.dumps(payload or {}),
        "source_path": source_path,
        "built_at": NOW_ISO,
        "frozen": 1 if frozen else 0,
    }


def make_edge(src: str, dst: str, type_: str, *,
              weight: float = None, payload: dict = None) -> dict:
    return {
        "src": src,
        "dst": dst,
        "type": type_,
        "weight": weight,
        "payload": json.dumps(payload or {}),
        "built_at": NOW_ISO,
    }


# ── node collectors ───────────────────────────────────────────────────────────

def collect_tickers() -> list[dict]:
    """Walk wiki/{bucket}/*.md → ticker nodes."""
    out = []
    for bucket in ("equity", "etf", "fi", "portfolio"):
        bd = WORKSPACE / "wiki" / bucket
        if not bd.is_dir():
            continue
        for f in sorted(bd.glob("*.md")):
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            fm = parse_frontmatter(text) or {}
            ident = fm.get("ticker") or fm.get("cusip") or fm.get("identifier") or f.stem
            ident = str(ident)
            payload = {
                "schema_version": fm.get("schema_version"),
                "last_review_date": fm.get("last_review_date") or fm.get("last_review"),
                "business_model_type": (fm.get("data") or {}).get("business_model_type"),
            }
            out.append(make_node(
                f"ticker:{ident}", "ticker", ident,
                bucket=bucket, payload=payload, source_path=relpath(f),
            ))
    return out


def collect_skills() -> tuple[list[dict], list[dict]]:
    """Walk skills/**/MANIFEST.yaml → skill nodes + edges from manifest declarations."""
    nodes, edges = [], []
    for mf in sorted((WORKSPACE / "skills").rglob("MANIFEST.yaml")):
        try:
            data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            continue
        skill_id = data.get("skill_id") or mf.parent.name
        bucket = data.get("bucket") or "shared"
        # cross-domain bucket overrides (e.g. cs-lewis is biblical even though
        # it sits in workspace skills/ root)
        if skill_id in BIBLICAL_BUCKET_OVERRIDES:
            bucket = "biblical"
        status = data.get("status") or "active"
        frozen = bool((data.get("calibration") or {}).get("freeze"))
        node_id = f"skill:{skill_id}"
        nodes.append(make_node(
            node_id, "skill", skill_id,
            bucket=bucket,
            payload={
                "version": data.get("version"),
                "status": status,
                "runs_on": data.get("runs_on") or {},
                "model_calls": data.get("model_calls") or [],
            },
            source_path=relpath(mf.parent),
            frozen=frozen,
        ))
        # edges from manifest declarations (when hand-edited later)
        for inp in data.get("inputs") or []:
            if isinstance(inp, dict):
                if "data_source" in inp:
                    edges.append(make_edge(node_id, f"src:{inp['data_source']}", "reads",
                                           payload={"fields": inp.get("fields", [])}))
                elif "skill" in inp:
                    edges.append(make_edge(node_id, f"skill:{inp['skill']}", "consumes"))
        for out in data.get("outputs") or []:
            if isinstance(out, dict):
                if "notion_db" in out:
                    edges.append(make_edge(node_id, f"notion_db:{out['notion_db']}", "writes"))
        for d in data.get("dispatches_to") or []:
            if isinstance(d, str):
                edges.append(make_edge(node_id, d, "dispatches_to"))
        for d in data.get("depends_on") or []:
            if isinstance(d, dict) and "skill" in d:
                edges.append(make_edge(node_id, f"skill:{d['skill']}", "depends_on"))
    return nodes, edges


def collect_agents() -> list[dict]:
    """Walk Agents/* → agent nodes (one per top-level Agents/ subdir)."""
    out = []
    ad = WORKSPACE / "Agents"
    if not ad.is_dir():
        return out
    for entry in sorted(ad.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        out.append(make_node(
            f"agent:{entry.name}", "agent", entry.name,
            bucket=_agent_bucket(entry.name),
            payload={"has_workflow_md": (entry / "workflow.md").exists()},
            source_path=relpath(entry),
        ))
    return out


def _agent_bucket(name: str) -> str:
    if "equity" in name:
        return "equity"
    if "etf" in name:
        return "etf"
    if "fixed-income" in name or name.startswith("fi-"):
        return "fi"
    if "portfolio" in name:
        return "portfolio"
    return "shared"


def collect_reports() -> tuple[list[dict], list[dict]]:
    """Walk Reports/**/*.pdf → report nodes + edges to tickers + Notion."""
    nodes, edges = [], []
    rd = WORKSPACE / "Reports"
    if not rd.is_dir():
        return nodes, edges
    for f in sorted(rd.rglob("*.pdf")):
        bucket = f.parent.name  # Reports/equity/foo.pdf → "equity"
        # Filename patterns:
        #   {TICKER}_{bucket}_review_{date}.pdf
        #   {TICKER}_covered_call_backtest_{date}.pdf
        #   {ACCOUNT}_portfolio_review_{mode}_{date}.pdf
        m = re.match(r"^([A-Z0-9._\-]+?)_(equity|etf|fi|portfolio|covered_call_backtest|ipo|supply_chain|insider_scan)", f.stem)
        identifier = m.group(1) if m else f.stem.split("_")[0]
        node_id = f"report:{f.stem}"
        nodes.append(make_node(
            node_id, "report", f.stem.replace("_", " "),
            bucket=bucket,
            payload={"filename": f.name, "bytes": f.stat().st_size},
            source_path=relpath(f),
        ))
        # edges
        edges.append(make_edge(node_id, f"ticker:{identifier}", "covers"))
        edges.append(make_edge(node_id, "notion_db:Reports", "cataloged_in"))
    return nodes, edges


def _topic_dir_parts(f: Path, td: Path) -> tuple[str, ...]:
    """Dir components of f relative to the topics root, excluding the filename."""
    return f.relative_to(td).parts[:-1]


def _topic_archive_rank(rel_parts: tuple[str, ...]) -> int:
    """Tie-break among subdir-only copies (both under some `_*` dir). Lower wins.
    _backup/ is a point-in-time snapshot (rank 1); any other `_*` dir
    (_archive/, _retired/, future _*) is a stable intentional archival move
    and outranks a mere snapshot (rank 0)."""
    return 1 if any(p == "_backup" for p in rel_parts) else 0


def collect_memory_topics() -> list[dict]:
    out = []
    td = MEMORY_ROOT / "conversations" / "topics"
    if not td.is_dir():
        return out
    # rglob: topics/_archive/ pages must stay searchable — archive-not-delete
    # only works if archived pages remain indexed (2026-07-06 audit)
    # Precedence fix (2026-07-18, generalized): a live topics/<slug>.md (not
    # under ANY `_*` subdir) must win over EVERY subdir copy — `_backup/`,
    # `_archive/`, `_retired/`, or any future `_*` dir — not just `_backup/`.
    # The original fix special-cased `_backup/` only, so a live file still lost
    # to `_retired/` (e.g. memory_topic:example-topic resolved to
    # topics/_retired/example-topic.md with archived:false even though a live
    # topics/example-topic.md existed). Among subdir-only copies (no live file
    # exists), prefer the stable `_archive/`/`_retired/` copy over a `_backup/`
    # snapshot via _topic_archive_rank. Dedupe explicitly by slug instead of
    # relying on rglob sort order (previously let subdirs silently clobber the
    # live node via the id-keyed seen_ids overwrite downstream).
    best: dict[str, Path] = {}
    for f in sorted(td.rglob("*.md")):
        slug = f.stem
        rel_parts = _topic_dir_parts(f, td)
        is_archived_dir = any(p.startswith("_") for p in rel_parts)
        prior = best.get(slug)
        if prior is None:
            best[slug] = f
            continue
        prior_rel_parts = _topic_dir_parts(prior, td)
        prior_is_archived_dir = any(p.startswith("_") for p in prior_rel_parts)
        if prior_is_archived_dir and not is_archived_dir:
            best[slug] = f  # live file always beats a subdir copy
        elif prior_is_archived_dir and is_archived_dir:
            if _topic_archive_rank(rel_parts) < _topic_archive_rank(prior_rel_parts):
                best[slug] = f  # stable archive/retired beats a backup snapshot
        # else prior is live (or an earlier-sorted equal-rank dup): keep prior
    for slug, f in sorted(best.items()):
        archived = any(p.startswith("_") for p in _topic_dir_parts(f, td))
        out.append(make_node(
            f"memory_topic:{slug}", "memory_topic", f"topic: {slug}",
            bucket="shared",
            payload={"slug": slug, "archived": archived},
            source_path=relpath(f),
        ))
    return out


def collect_predictive_gates() -> list[dict]:
    """Walk predictive-gates state/phase_* → gate nodes (mirrored, frozen)."""
    out = []
    gd = WORKSPACE / "skills" / "_shared" / "predictive-gates" / "state"
    if not gd.is_dir():
        return out
    for entry in sorted(gd.iterdir()):
        if not entry.is_dir() or not entry.name.startswith("phase_"):
            continue
        phase_id = entry.name.replace("phase_", "")  # e.g. "d1", "e1", "f1"
        # try to glean status from a STATE.json or PROGRESS.md
        status = None
        state_json = entry / "STATE.json"
        if state_json.exists():
            try:
                sd = json.loads(state_json.read_text(encoding="utf-8"))
                status = sd.get("status") or sd.get("phase_status")
            except Exception:
                pass
        out.append(make_node(
            f"gate:phase-{phase_id}", "gate", f"predictive-gates · phase {phase_id.upper()}",
            bucket="shared",
            payload={"phase": phase_id, "status": status},
            source_path=relpath(entry),
            frozen=True,
        ))
    # also add the bottoming-gates v2 logical node if its dir exists
    bg = WORKSPACE / "skills" / "_shared" / "predictive-gates"
    if bg.is_dir():
        out.append(make_node(
            "gate:bottoming-v2", "gate", "bottoming-gates v2",
            bucket="shared",
            payload={"version": "v2"},
            source_path=relpath(bg),
            frozen=True,
        ))
    return out


def collect_frozen_tell() -> list[dict]:
    """Walk skills/_shared/news-sentiment/scripts/ → sentiment_run nodes
    (mirrored, frozen). The cascade scripts live there, NOT in frozen_tell/cascade/
    despite the audit's claim. frozen_tell/ holds anchors + queue + meta."""
    out = []
    ns = WORKSPACE / "skills" / "_shared" / "news-sentiment" / "scripts"
    ft = WORKSPACE / "frozen_tell"
    if not (ns.is_dir() or ft.is_dir()):
        return out

    # Cascade parent (logical node — sits over the tier scripts in news-sentiment/)
    if ns.is_dir():
        out.append(make_node(
            "tell:cascade", "sentiment_run", "frozen-tell · cascade",
            bucket="shared",
            payload={"role": "parent"},
            source_path=relpath(ns),
            frozen=True,
        ))

    # Map cascade-tier script files to logical tier nodes. Names per audit 1F.
    tier_scripts = [
        ("tell:t1", "tier 1 · triage",         "haiku_ingest.py"),
        ("tell:t2", "tier 2 · score",          "sonnet_score_runner.py"),
        ("tell:t3", "tier 3 · catalyst judge", "haiku_triage.py"),
        ("tell:t4", "tier 4 · arbiter",        "opus_judge.py"),
    ]
    for tid, tname, fname in tier_scripts:
        f = ns / fname
        if not f.exists():
            continue
        out.append(make_node(
            tid, "sentiment_run", tname,
            bucket="shared",
            payload={"script": fname},
            source_path=relpath(f),
            frozen=True,
        ))

    # Auxiliary scripts
    aux = [
        ("tell:aggregator",   "aggregator",          "aggregator.py"),
        ("tell:orchestrator", "cascade orchestrator","cascade_orchestrator.py"),
    ]
    for tid, tname, fname in aux:
        f = ns / fname
        if not f.exists():
            continue
        out.append(make_node(
            tid, "sentiment_run", tname,
            bucket="shared",
            payload={"script": fname},
            source_path=relpath(f),
            frozen=True,
        ))

    # Anchor set — try the actual locations on disk
    for anchor_path in (
        ft / "anchors" / "anchor.jsonl",
        ft / "labels" / "anchor.jsonl",
        ns.parent / "state" / "labels" / "anchor.jsonl",
    ):
        if anchor_path.exists():
            out.append(make_node(
                "tell:anchor", "sentiment_run", "anchor labeling",
                bucket="shared",
                payload={"file": anchor_path.name},
                source_path=relpath(anchor_path),
                frozen=True,
            ))
            break
    else:
        # No exact filename match; if anchors/ dir exists, point at it
        anchors_dir = ft / "anchors"
        if anchors_dir.is_dir():
            out.append(make_node(
                "tell:anchor", "sentiment_run", "anchor labeling",
                bucket="shared",
                payload={"dir": "frozen_tell/anchors/"},
                source_path=relpath(anchors_dir),
                frozen=True,
            ))

    return out


# ── biblical system collectors ────────────────────────────────────────────────
#
# Biblical system parallels the financial system:
#   Biblical System/Agents/{passage,doctrine,figure,bible-research}-study/
#   Biblical System/wiki/{book,doctrine,figure,passage,question}/*.md
#   Biblical System/Reports/{book,doctrine,figure,passage,research}/*.pdf
#   Biblical System/corpus/{author}/  — author-grouped text corpora
#   memory/bible-expert/  — separate memory namespace
#
# Skills live in plugin path (not workspace skills/), so hardcode them.

BIBLICAL_SKILLS = [
    # (skill_id, bucket, one-line description)
    ("biblical-orchestrator",     "biblical", "Routing entry point for biblical system"),
    ("biblical-section-fetcher",  "biblical", "Per-section research fetcher (biblical)"),
    ("biblical-theology-research","biblical", "Pauline-baseline theology research"),
    ("biblical-historian",        "biblical", "Historical evidence for the Bible"),
    ("biblical-linguistics",      "biblical", "Hebrew · Aramaic · Greek language analysis"),
    ("michael-heiser",            "biblical", "Divine council · supernatural worldview"),
    ("max-lucado",                "biblical", "Pastoral teaching · anxiety · fear · grief"),
    ("bill-creasy-bible-study",   "biblical", "Literary-narrative Bible study method"),
    ("lee-strobel",               "biblical", "Investigative apologetics"),
    ("richard-wurmbrand",         "biblical", "Persecution · martyrdom · Voice of the Martyrs"),
    ("dr-henry-cloud",            "biblical", "Boundaries · character · integrity"),
    ("apologetics",               "biblical", "Christian apologetics"),
    ("systematic-theology",       "biblical", "Locus-based systematic theology"),
    ("church-history",            "biblical", "Church history · Apostolic Fathers onward"),
    ("cs-lewis",                  "biblical", "C.S. Lewis lens (also workspace-resident skill)"),
]

# When build_graph reads workspace skills/ MANIFEST.yaml files, force these
# skill_ids to bucket=biblical regardless of what their manifest says.
BIBLICAL_BUCKET_OVERRIDES = {"cs-lewis"}


def collect_biblical_skills() -> list[dict]:
    """Hardcoded biblical skill nodes (most live in plugin paths, not workspace)."""
    out = []
    for sid, bucket, desc in BIBLICAL_SKILLS:
        # skip cs-lewis if the workspace scaffolder already produced a skill node
        # for it; the bucket override in collect_skills handles its bucket
        if sid == "cs-lewis":
            continue
        out.append(make_node(
            f"skill:{sid}", "skill", sid,
            bucket=bucket,
            payload={"description": desc, "lives_in": "plugin path"},
            source_path=f"plugin:{sid}",
        ))
    return out


# Biblical System split (2026-07-12): code → Code/biblical-system (repo),
# data (wiki/reports/corpus) → Databases/biblical. Old in-workspace tree wins
# only while the repo doesn't exist yet.
_BIB_CODE_CANDIDATES = [
    None,  # optional BIBLICAL_CODE_DIR env at call time
    None,  # placeholder — replaced with WORKSPACE-relative at call time
]


def _biblical_code_root() -> Path:
    env = (os.environ.get("BIBLICAL_CODE_DIR") or "").strip()
    candidates = [Path(env)] if env else []
    candidates.extend([WORKSPACE / "Biblical System"])
    for path in candidates:
        if path.is_dir():
            return path
    return WORKSPACE / "Biblical System"


def _biblical_data_root() -> Path:
    env = (os.environ.get("BIBLICAL_DATA_DIR") or "").strip()
    if env:
        return Path(env)
    return WORKSPACE / "biblical_data"


def collect_biblical_agents() -> list[dict]:
    out = []
    ad = _biblical_code_root() / "Agents"
    if not ad.is_dir():
        return out
    for entry in sorted(ad.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        out.append(make_node(
            f"biblical_agent:{entry.name}", "agent", entry.name,
            bucket="biblical",
            payload={"system": "biblical",
                     "has_workflow_md": (entry / "workflow.md").exists()},
            source_path=relpath(entry),
        ))
    return out


def collect_biblical_wiki() -> list[dict]:
    """Biblical System/wiki/{kind}/*.md → biblical_entry nodes (kind in payload)."""
    out = []
    wd = _biblical_data_root() / "wiki"
    if not wd.is_dir():
        return out
    for kind_dir in sorted(wd.iterdir()):
        if not kind_dir.is_dir():
            continue
        kind = kind_dir.name  # book / doctrine / figure / passage / question
        for f in sorted(kind_dir.glob("*.md")):
            slug = f.stem
            out.append(make_node(
                f"biblical_entry:{kind}:{slug}", "biblical_entry", slug,
                bucket="biblical",
                payload={"kind": kind},
                source_path=relpath(f),
            ))
    return out


def collect_biblical_reports() -> tuple[list[dict], list[dict]]:
    nodes, edges = [], []
    rd = _biblical_data_root() / "reports"
    if not rd.is_dir():
        return nodes, edges
    for kind_dir in sorted(rd.iterdir()):
        if not kind_dir.is_dir():
            continue
        kind = kind_dir.name
        for f in sorted(kind_dir.glob("*.pdf")):
            node_id = f"report:biblical:{f.stem}"
            nodes.append(make_node(
                node_id, "report", f.stem.replace("_", " "),
                bucket="biblical",
                payload={"kind": kind, "filename": f.name, "bytes": f.stat().st_size},
                source_path=relpath(f),
            ))
    return nodes, edges


def collect_biblical_corpus() -> list[dict]:
    """One corpus_author node per top-level corpus dir, with rolled-up file count."""
    out = []
    cd = _biblical_data_root() / "corpus"
    if not cd.is_dir():
        return out
    for author_dir in sorted(cd.iterdir()):
        # skip dot-dirs (.ruff_cache, .git, macOS .DS_Store) — data litter, not authors
        if not author_dir.is_dir() or author_dir.name.startswith("."):
            continue
        # tolerate any text-ish file extension
        files = []
        for ext in ("*.txt", "*.md", "*.pdf", "*.epub"):
            files += list(author_dir.rglob(ext))
        out.append(make_node(
            f"corpus_author:{author_dir.name}", "corpus_author", author_dir.name,
            bucket="biblical",
            payload={"file_count": len(files)},
            source_path=relpath(author_dir),
        ))
    return out


def collect_biblical_memory() -> list[dict]:
    out = []
    md = MEMORY_ROOT / "bible-expert"
    if not md.is_dir():
        return out
    for fname, label in [
        ("skill-registry.md",  "biblical skill registry"),
        ("routing-rules.md",   "biblical routing rules"),
        ("gap-log.md",         "biblical gap log"),
    ]:
        f = md / fname
        if f.exists():
            slug = fname.replace(".md", "")
            out.append(make_node(
                f"biblical_memory:{slug}", "memory_topic", label,
                bucket="biblical",
                payload={"file": fname},
                source_path=relpath(f),
            ))
    if (md / "episodes.jsonl").exists():
        ep = md / "episodes.jsonl"
        try:
            count = sum(1 for _ in open(ep, encoding="utf-8", errors="ignore"))
        except Exception:
            count = -1
        out.append(make_node(
            "biblical_memory:episodes", "memory_topic", "biblical episodes log",
            bucket="biblical",
            payload={"file": "episodes.jsonl", "episode_count": count},
            source_path=relpath(ep),
        ))
    return out


def biblical_pattern_edges() -> list[dict]:
    e = []
    # orchestrator → 4 study agents
    for ag in ("bible-research", "doctrine-study", "figure-study", "passage-study"):
        e.append(make_edge("skill:biblical-orchestrator", f"biblical_agent:{ag}", "dispatches"))
    # study agents → lens skills (every agent can call every lens; orchestrator-driven)
    lens_skills = [
        "biblical-theology-research", "biblical-historian", "biblical-linguistics",
        "michael-heiser", "max-lucado", "bill-creasy-bible-study", "lee-strobel",
        "richard-wurmbrand", "dr-henry-cloud", "apologetics", "systematic-theology",
        "church-history", "cs-lewis", "biblical-section-fetcher",
    ]
    for ag in ("bible-research", "doctrine-study", "figure-study", "passage-study"):
        for s in lens_skills:
            e.append(make_edge(f"biblical_agent:{ag}", f"skill:{s}", "calls"))
    # author skills → their corpora
    skill_to_corpus = {
        "michael-heiser":          "Heiser",
        "max-lucado":              "Lucado",
        "bill-creasy-bible-study": "Creasy",
        "lee-strobel":             "Strobel",
        "richard-wurmbrand":       "Wurmbrand",
        "dr-henry-cloud":          "Cloud",
        "cs-lewis":                "Lewis",
        "biblical-historian":      "BiblicalHistorian",
        "biblical-linguistics":    "Linguistics",
        "apologetics":             "Apologetics",
        "systematic-theology":     "SystematicTheology",
        "church-history":          "ChurchHistory",
    }
    for skill_id, author in skill_to_corpus.items():
        e.append(make_edge(f"skill:{skill_id}", f"corpus_author:{author}",
                           "cites", payload={"corpus": author}))
    # orchestrator reads its registry
    e.append(make_edge("skill:biblical-orchestrator",
                       "biblical_memory:skill-registry", "reads"))
    return e


# ── hardcoded (no live data source on disk) ───────────────────────────────────

def hardcoded_data_sources() -> list[dict]:
    sources = [
        ("FMP",                      "paid"),
        ("Tavily",                   "paid · 4000 cr/mo"),
        ("Bigdata · RavenPack",      "free MCP"),
        ("Massive Market Data",      "free MCP"),
        ("Finnhub",                  "free"),
        ("FRED",                     "free"),
        ("SEC · EDGAR",              "free"),
        ("EDGAR Local Store",        "permanent · edgar-data/ · 9 SQLite databases"),
        ("FMP Local Store",          "permanent · fmp-data/ · 10 SQLite databases"),
        ("MMD Archive",              "permanent · mmd-data/ · deprecated source archive"),
        ("LunarCrush",               "free"),
        ("WebSearch",                "native"),
        ("OpenRouter",               "paid · multi-model"),
        ("DeepSeek API",             "paid · 75% off until 2026-05-31"),
        ("Notion API",               "paid"),
    ]
    return [
        make_node(f"data_source:{name}", "data_source", name,
                  bucket="shared", payload={"tier": tier})
        for (name, tier) in sources
    ]


def hardcoded_notion_dbs() -> list[dict]:
    dbs = [
        ("Reports",                       "024c3dc9-5929-40c9-b3f9-23d703dd0dba"),
        ("Watchlist_State",               None),
        ("Screener_Fires",                None),
        ("Direction_Changes",             None),
        ("Tell · Composite Signal",       "83056b27-e884-46e5-b74f-07633ae723da"),
        ("Tell · Market Sentiment Daily", "bb26b9c6-2465-4095-b38b-c3334173388d"),
        ("Tell · Stock Sentiment Timeline", None),
        ("Tell · Action Items",           None),
        # ── Trend Emergence Radar ────────────────────────────────────────────
        ("TER · Theme Emergence",         "6dade34c-32fe-4a94-bcfc-b4115bb27a28"),
        ("TER · Meme Watch",              "e21f1f5c-7881-4b68-9e00-d39883a5e6a4"),
        ("TER · Universe Log",            "a76d1154-4f49-4479-a6e4-6e729137c25f"),
    ]
    return [
        make_node(f"notion_db:{name}", "notion_db", f"NDB · {name}",
                  bucket="shared", payload={"data_source_id": dsid})
        for (name, dsid) in dbs
    ]


def hardcoded_pattern_edges() -> list[dict]:
    """Cross-cutting edges that don't come from manifests yet."""
    e = []
    # orchestrator → reviewers
    for rev in ("equity-reviewer", "etf-reviewer", "fixed-income-reviewer", "portfolio-reviewer"):
        e.append(make_edge("agent:financial-orchestrator", f"agent:{rev}", "dispatches"))
    # reviewers → shared infra
    shared_skills = ["wiki-reader", "wiki-writer", "section-research-fetcher",
                     "section-narrator", "wiki-to-block-adapter",
                     "financial-pdf-builder", "trader-memory-core", "market-news-scout"]
    for rev in ("equity-reviewer", "etf-reviewer", "fixed-income-reviewer", "portfolio-reviewer"):
        for s in shared_skills:
            e.append(make_edge(f"agent:{rev}", f"skill:{s}", "calls"))
    # bucket-specific reviewer → bucket-specific skills
    bucket_skill_map = {
        "equity-reviewer": ["insider-accumulation-scanner", "moat-lens",
                            "financials-sector-slider", "supply-chain-durability",
                            "ipo-mode-overlay", "valuation-multi", "owner-earnings-calc",
                            "famous-investors-crosscheck", "dividend-profile",
                            "classical-charting"],
        "etf-reviewer": ["methodology-holdings", "distribution-tax-etf", "flows-catalyst-etf",
                         "liquidity-structure-etf", "cef-pvc-nav-lens", "cost-tracking-calc",
                         "macro-sector-regime", "option-income-strategy", "overlap-calc"],
        "fixed-income-reviewer": ["duration-convexity-calc", "credit-quality-read",
                                   "call-schedule-read", "ladder-fit-fi", "liquidity-structure-fi",
                                   "macro-rate-regime", "phantom-income-flag",
                                   "tax-equivalent-yield", "wash-sale-check-fi", "ytm-ytw-calc"],
        "portfolio-reviewer": ["asset-class-mix", "concentration-calc", "drift-calc",
                               "factor-style-exposure", "geographic-currency", "holdings-ingestor",
                               "holdings-quality-aggregator", "improvement-planner",
                               "income-profile-portfolio", "ladder-health",
                               "options-overlay-status", "rebalance-proposer",
                               "sector-tilt", "tax-efficiency-scan", "cash-liquidity"],
    }
    for rev, skills in bucket_skill_map.items():
        for s in skills:
            e.append(make_edge(f"agent:{rev}", f"skill:{s}", "calls"))
    # frozen-tell cascade structural edges
    for tid in ("tell:t1", "tell:t2", "tell:t3", "tell:t4"):
        e.append(make_edge("tell:cascade", tid, "contains"))
    e.append(make_edge("tell:cascade", "tell:anchor", "uses"))
    # tier → provider routing
    e.append(make_edge("tell:t1", "data_source:OpenRouter", "calls"))
    e.append(make_edge("tell:t2", "data_source:DeepSeek API", "calls"))
    e.append(make_edge("tell:t3", "data_source:OpenRouter", "calls"))
    e.append(make_edge("tell:t4", "data_source:OpenRouter", "calls"))
    # cascade → notion outputs
    for ndb in ("Tell · Composite Signal", "Tell · Market Sentiment Daily",
                "Tell · Stock Sentiment Timeline", "Tell · Action Items"):
        e.append(make_edge("tell:cascade", f"notion_db:{ndb}", "writes"))
    # gates → notion
    for gid in ("gate:bottoming-v2",):
        e.append(make_edge(gid, "notion_db:Screener_Fires", "writes"))
    # EDGAR Local Store (2026-05-13) — Phase 6/7 EDGAR stockpile + ctx fetchers
    edgar_readers = [
        "module:edgar_financials_fetch", "module:edgar_institutional_ctx",
        "module:edgar_form144_ctx", "module:edgar_activist_ctx",
        "module:edgar_insider_sqlite",
    ]
    for mod in edgar_readers:
        e.append(make_edge(mod, "data_source:EDGAR Local Store", "reads"))
    edgar_writers = [
        "module:edgar_bulk_download", "module:edgar_extract_fundamentals",
        "module:edgar_extract_frames", "module:edgar_extract_filings_index",
        "module:edgar_parse_form4", "module:edgar_parse_13f",
        "module:edgar_parse_form144", "module:edgar_parse_13dg",
        "module:edgar_parse_formd", "module:edgar_parse_proxy",
        "module:edgar_refresh",
    ]
    for mod in edgar_writers:
        e.append(make_edge(mod, "data_source:EDGAR Local Store", "writes"))
        e.append(make_edge(mod, "data_source:SEC · EDGAR", "calls"))
    # FMP Local Store (2026-05-13) — bulk download + ctx fetchers
    fmp_readers = [
        "module:fmp_quote_profile_fetch", "module:fmp_calendar_fetch",
        "module:fmp_analyst_revision_writeback", "module:fmp_peer_multiples_fetch",
        "module:fmp_cash_runway_fetch", "module:fmp_insider_fetch",
        "module:fmp_technical_signals", "module:_fmp_disk_cache",
    ]
    for mod in fmp_readers:
        e.append(make_edge(mod, "data_source:FMP Local Store", "reads"))
    fmp_writers = [
        "module:fmp_download_fundamentals", "module:fmp_download_price_history",
        "module:fmp_download_earnings", "module:fmp_download_dividends",
        "module:fmp_download_analyst", "module:fmp_download_insiders",
        "module:fmp_download_transcripts", "module:fmp_download_segments",
        "module:fmp_download_etf", "module:fmp_download_macro",
        "module:fmp_download_misc", "module:fmp_extract_all",
        "module:fmp_populate_wiki", "module:fmp_refresh",
    ]
    for mod in fmp_writers:
        e.append(make_edge(mod, "data_source:FMP Local Store", "writes"))
        e.append(make_edge(mod, "data_source:FMP", "calls"))
    return e


# ── model call log ingest (bite 8 · 2026-05-07) ───────────────────────────────

# Map caller_script → skill node id. The gateway logs the caller's filename;
# this resolves to the existing skill node so we can draw skill→task_type
# edges. Unknown callers (test scripts, ad-hoc smoke runs) are skipped — they
# still land in the model_call_log table but no edge is drawn.
CALLER_SCRIPT_TO_SKILL = {
    # Frozen Tell cascade — all live in skills/_shared/news-sentiment/
    "haiku_ingest.py":         "skill:news-sentiment",        # Tier 2
    "sonnet_score_runner.py":  "skill:news-sentiment",        # Tier 3 (future migration)
    "opus_judge.py":           "skill:news-sentiment",        # Tier 4
    "deepseek_client.py":      "skill:news-sentiment",        # mirror via wrapper hook (bite 7.5)
    # Memory synthesis — capture_v2.py lives at memory/conversations/scripts/
    # but is registered as the logical skill `conversation-memory` via the
    # manifest at skills/_shared/conversation-memory/MANIFEST.yaml. Bite 8.1
    # (2026-05-08).
    "capture_v2.py":           "skill:conversation-memory",
    # Test/smoke callers — intentionally not mapped:
    #   smoke_test, gateway_smoke, path_c_smoke
}


def collect_model_call_log() -> list[dict]:
    """Read <workspace state>/model_call_log.jsonl → list of row dicts.

    Returns one dict per JSONL line, with fields matching the SQLite
    `model_call_log` table schema. Soft-fail: missing or unreadable file →
    empty list. Bad JSON lines silently skipped.
    """
    ledger_path = WORKSPACE / GRAPHIFY_STATE_SUBDIR / "model_call_log.jsonl"
    if not ledger_path.exists():
        return []
    rows: list[dict] = []
    try:
        with open(ledger_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Project to the SQLite columns; drop fields the schema doesn't
                # have (error_kind, error_detail, fell_back) so executemany
                # doesn't choke. The Costs surface still reads JSONL directly
                # for those richer fields.
                rows.append({
                    "ts":                r.get("ts"),
                    "task_type":         r.get("task_type"),
                    "model":             r.get("model"),
                    "provider":          r.get("provider"),
                    "prompt_tokens":     r.get("prompt_tokens") or 0,
                    "completion_tokens": r.get("completion_tokens") or 0,
                    "cost_usd":          r.get("cost_usd") or 0,
                    "duration_ms":       r.get("duration_ms") or 0,
                    "status":            r.get("status"),
                    "caller_script":     r.get("caller_script"),
                    "identifier":        r.get("identifier"),
                })
    except OSError:
        return []
    return rows


def collect_task_type_nodes_and_edges(ledger_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Build task_type nodes + skill→task_type edges from the ingested ledger.

    For each unique task_type seen in the ledger, create a node with rolled-up
    stats (calls, cumulative cost, providers used, models used) in its payload.
    For each (skill, task_type) pair observed, draw a 'uses' edge with the
    call count + cost as weight + payload metadata.

    Skill resolution uses CALLER_SCRIPT_TO_SKILL. Unknown callers don't get
    edges (the ledger row is still ingested into the SQLite table for SQL
    queries; only the visual System Map skips them).
    """
    if not ledger_rows:
        return [], []
    # Aggregate per task_type
    task_aggs: dict[str, dict] = {}
    for r in ledger_rows:
        tt = r.get("task_type") or "unknown"
        if tt not in task_aggs:
            task_aggs[tt] = {
                "calls": 0,
                "cost_usd": 0.0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "providers": set(),
                "models": set(),
                "callers": set(),
            }
        a = task_aggs[tt]
        a["calls"] += 1
        a["cost_usd"] += float(r.get("cost_usd") or 0)
        a["prompt_tokens"] += int(r.get("prompt_tokens") or 0)
        a["completion_tokens"] += int(r.get("completion_tokens") or 0)
        if r.get("provider"):
            a["providers"].add(r["provider"])
        if r.get("model"):
            a["models"].add(r["model"])
        if r.get("caller_script"):
            a["callers"].add(r["caller_script"])

    # Build nodes
    task_type_nodes: list[dict] = []
    for tt, a in task_aggs.items():
        task_type_nodes.append(make_node(
            f"task_type:{tt}", "task_type", tt,
            bucket="shared",
            payload={
                "calls":             a["calls"],
                "cost_usd":          round(a["cost_usd"], 6),
                "prompt_tokens":     a["prompt_tokens"],
                "completion_tokens": a["completion_tokens"],
                "providers":         sorted(a["providers"]),
                "models":            sorted(a["models"]),
                "callers":           sorted(a["callers"]),
            },
            source_path=f"{GRAPHIFY_STATE_SUBDIR}/model_call_log.jsonl",
        ))

    # Build skill→task_type edges, aggregated per (skill, task_type) pair
    edge_aggs: dict[tuple[str, str], dict] = {}
    for r in ledger_rows:
        caller = r.get("caller_script")
        skill_id = CALLER_SCRIPT_TO_SKILL.get(caller)
        if not skill_id:
            continue  # unknown caller — skip the edge, ledger row still ingested
        tt = r.get("task_type") or "unknown"
        key = (skill_id, f"task_type:{tt}")
        if key not in edge_aggs:
            edge_aggs[key] = {"calls": 0, "cost_usd": 0.0}
        edge_aggs[key]["calls"] += 1
        edge_aggs[key]["cost_usd"] += float(r.get("cost_usd") or 0)

    edges: list[dict] = []
    for (src, dst), agg in edge_aggs.items():
        edges.append({
            "src": src,
            "dst": dst,
            "type": "uses",
            "weight": float(agg["calls"]),
            "payload": json.dumps({
                "calls":    agg["calls"],
                "cost_usd": round(agg["cost_usd"], 6),
            }),
            "built_at": NOW_ISO,
        })

    return task_type_nodes, edges


# ── persistence ───────────────────────────────────────────────────────────────

def write_atomic(out_path: Path, nodes: list[dict], edges: list[dict],
                  model_call_rows: list[dict] = None) -> None:
    """Write atomically. Stages to local /tmp first because sqlite + cloud-mounted
    filesystems (Dropbox) can fail with disk I/O errors during the write transaction.

    PRESERVES EXISTING EMBEDDINGS: copies existing out_path → /tmp first if it
    exists, so the embeddings table (populated by embed_corpus.py) survives the
    rebuild. Schema uses CREATE TABLE IF NOT EXISTS, and we INSERT OR REPLACE
    nodes/edges, so no data clobbering."""
    import shutil
    import secrets
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Build a staging path manually rather than via tempfile.mkstemp.
    # Python's mkstemp uses O_EXCL which Dropbox-mounted filesystems can fail
    # in some sandbox configurations, AND the sandbox /tmp can fill up with
    # orphan staged copies from prior runs (each ~140 MB). Try /tmp first; on
    # any failure fall back to a `.tmp/` dir co-located with out_path.
    tmp = None
    # We need at least ~200 MB of free space — the staged DB grows to ~140 MB
    # once embeddings are carried forward; leave headroom for the COPY back.
    MIN_STAGE_BYTES = 200 * 1024 * 1024
    stage_candidates = [
        Path("/tmp") / f"graph_build_{secrets.token_hex(8)}.sqlite",
        Path("/var/tmp") / f"graph_build_{secrets.token_hex(8)}.sqlite",
        out_path.parent / ".tmp" / f"graph_build_{secrets.token_hex(8)}.sqlite",
    ]
    for cand in stage_candidates:
        try:
            cand.parent.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(str(cand.parent)).free
            if free < MIN_STAGE_BYTES:
                print(f"  staging skipped at {cand.parent}: only {free / 1024 / 1024:.0f} MB free "
                      f"(need {MIN_STAGE_BYTES / 1024 / 1024:.0f} MB)", file=sys.stderr)
                continue
            # Probe writability with a tiny file write (after disk-space check).
            cand.write_bytes(b"")
            cand.unlink()
            tmp = cand
            print(f"  staging at {cand.parent} ({free / 1024 / 1024 / 1024:.1f} GB free)")
            break
        except OSError as e:
            print(f"  staging probe failed at {cand}: {e}", file=sys.stderr)
            continue
    if tmp is None:
        raise RuntimeError("no writable staging directory available with >=200 MB free")
    if out_path.exists():
        # carry forward existing data (embeddings + anything else)
        try:
            shutil.copyfile(str(out_path), str(tmp))
        except Exception as e:
            print(f"  WARNING: couldn't carry forward existing graph ({e}); rebuilding fresh", file=sys.stderr)
    # If carry-forward worked, clear nodes + edges so the rebuild is authoritative.
    # Both must be truncated: collectors can change node IDs between runs (e.g. the
    # AST extractor's ID scheme differs from the retired graphify bridge), so stale
    # nodes with old IDs would never be overwritten by INSERT OR REPLACE.
    # The embeddings table is left intact — its node_id values reference stable
    # domain-node IDs that are re-inserted unchanged on every build.
    conn = sqlite3.connect(str(tmp))
    if tmp.stat().st_size > 0:
        try:
            conn.execute("DELETE FROM edges")
            conn.execute("DELETE FROM nodes")
            # model_call_log: also truncate, since the JSONL source is
            # authoritative — re-ingesting on each build is the simplest
            # path to correctness (matches the edges pattern).
            try:
                conn.execute("DELETE FROM model_call_log")
            except sqlite3.OperationalError:
                pass  # table doesn't exist on the staged copy yet
            conn.commit()
        except sqlite3.OperationalError:
            pass  # tables might not exist yet on a fresh stage
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT OR REPLACE INTO nodes (id, type, bucket, name, payload, source_path, built_at, frozen) "
            "VALUES (:id, :type, :bucket, :name, :payload, :source_path, :built_at, :frozen)",
            nodes,
        )
        seen = set()
        unique_edges = []
        for e in edges:
            key = (e["src"], e["dst"], e["type"])
            if key in seen:
                continue
            seen.add(key)
            unique_edges.append(e)
        conn.executemany(
            "INSERT INTO edges (src, dst, type, weight, payload, built_at) "
            "VALUES (:src, :dst, :type, :weight, :payload, :built_at)",
            unique_edges,
        )
        # Bite 8 (2026-05-07): ingest the gateway's append-only ledger.
        if model_call_rows:
            conn.executemany(
                "INSERT INTO model_call_log "
                "(ts, task_type, model, provider, prompt_tokens, completion_tokens, "
                " cost_usd, duration_ms, status, caller_script, identifier) "
                "VALUES (:ts, :task_type, :model, :provider, :prompt_tokens, :completion_tokens, "
                ":cost_usd, :duration_ms, :status, :caller_script, :identifier)",
                model_call_rows,
            )
        conn.commit()
    finally:
        conn.close()
    # Diagnostic: count embeddings on staged file before copy-back
    diag_conn = sqlite3.connect(str(tmp))
    try:
        emb_count = diag_conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    except sqlite3.OperationalError:
        emb_count = 0
    diag_conn.close()
    print(f"  staged db has {emb_count} embeddings before copy-back")

    # Byte-copy + fsync (Dropbox-mount sync race needs fsync to commit)
    with open(tmp, "rb") as src, open(out_path, "wb") as dst:
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    tmp.unlink(missing_ok=True)


# ── summary ───────────────────────────────────────────────────────────────────

def print_summary(out_path: Path) -> None:
    # Stage to /tmp for the read — Dropbox-mounted sqlite reads can fail
    read_path = out_path
    if str(out_path).startswith(("/Volumes/", "/sessions/")):
        import glob as _glob
        import shutil
        import tempfile
        # Self-clean: drop prior staged summaries (~140MB each). Patched
        # 2026-05-08 after sandbox-bloat audit. Throwaway copies.
        for stale in _glob.glob("/tmp/graph_summary_*.sqlite"):
            try:
                os.unlink(stale)
            except OSError:
                pass
        fd, tmp_str = tempfile.mkstemp(prefix="graph_summary_", suffix=".sqlite")
        os.close(fd)
        try:
            shutil.copyfile(str(out_path), tmp_str)
            read_path = Path(tmp_str)
        except Exception:
            return  # non-fatal — skip the summary print
    conn = sqlite3.connect(str(read_path))
    try:
        c = conn.cursor()
        c.execute("SELECT type, COUNT(*) FROM nodes GROUP BY type ORDER BY type")
        node_counts = c.fetchall()
        c.execute("SELECT type, COUNT(*) FROM edges GROUP BY type ORDER BY type")
        edge_counts = c.fetchall()
        c.execute("SELECT COUNT(*) FROM nodes")
        total_nodes = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM edges")
        total_edges = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM nodes WHERE frozen = 1")
        frozen_nodes = c.fetchone()[0]
    finally:
        conn.close()

    print()
    print("=== GRAPH SUMMARY ===")
    print(f"  output:        {out_path}")
    print(f"  nodes:         {total_nodes} (frozen: {frozen_nodes})")
    print(f"  edges:         {total_edges}")
    print()
    print("  nodes by type:")
    for t, n in node_counts:
        print(f"    {t:18s} {n}")
    print()
    print("  edges by type:")
    for t, n in edge_counts:
        print(f"    {t:18s} {n}")


def collect_similarity_edges(db_path: Path, top_k: int = 5,
                             threshold: float = 0.70) -> list[dict]:
    """semantically_similar_to edges among concept nodes, via cosine similarity
    over existing embeddings (struct-packed float32 in the embeddings table).
    Pure computation — no API. Soft-fails (returns []) if numpy is missing,
    the DB has no embeddings, or fewer than 2 concept vectors exist."""
    import struct
    try:
        import numpy as np
    except ImportError:
        print("  similarity edges skipped — numpy unavailable", file=sys.stderr)
        return []
    if not db_path.exists():
        return []
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT node_id, embedding FROM embeddings "
            "WHERE chunk_idx = 0 AND node_id LIKE 'concept:%'"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    ids: list[str] = []
    vecs: list[tuple] = []
    for nid, blob in rows:
        if not blob:
            continue
        n = len(blob) // 4
        vecs.append(struct.unpack(f"{n}f", blob))
        ids.append(nid)
    if len(ids) < 2:
        return []
    mat = np.array(vecs, dtype=np.float32)
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    edges: list[dict] = []
    seen: set = set()
    chunk = 512
    for start in range(0, len(ids), chunk):
        block = mat[start:start + chunk]
        sims = block @ mat.T
        for i in range(block.shape[0]):
            gi = start + i
            row = sims[i]
            row[gi] = -1.0  # exclude self
            k = min(top_k, len(ids) - 1)
            top = np.argpartition(row, -k)[-k:]
            for j in top:
                score = float(row[j])
                if score < threshold:
                    continue
                a, b = ids[gi], ids[int(j)]
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(make_edge(key[0], key[1],
                                       "semantically_similar_to",
                                       weight=round(score, 4)))
    print(f"similarity edges: {len(edges)} semantically_similar_to "
          f"(over {len(ids)} concept embeddings)")
    return edges


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", type=Path, default=DEFAULT_OUT,
                        help=f"output sqlite path (default: {DEFAULT_OUT})")
    parser.add_argument("--summary", "-s", action="store_true",
                        help="print summary after build (default: yes)")
    parser.add_argument("--skip-graphify", action="store_true",
                        help="skip auto-import of graphify-out/graph.json")
    args = parser.parse_args()

    print(f"workspace: {WORKSPACE}")
    print(f"output:    {args.output}")
    print(f"built_at:  {NOW_ISO}")
    print()

    # Phase 1: collect nodes
    print("collecting nodes...")
    m = _membership()
    if m is not None and not any(m["collectors"].values()) and not m.get("code_roots"):
        payload = {
            "ok": True,
            "skipped": "no_sources",
            "message": "graph-build skipped: no graph collectors enabled",
        }
        print(json.dumps(payload))
        return 0
    all_nodes: list[dict] = []
    if _collector_on(m, "tickers"):
        all_nodes += collect_tickers()
    skill_nodes: list[dict] = []
    skill_edges: list[dict] = []
    if _collector_on(m, "skills"):
        skill_nodes, skill_edges = collect_skills()
        all_nodes += skill_nodes
    if _collector_on(m, "agents"):
        all_nodes += collect_agents()
    report_nodes: list[dict] = []
    report_edges: list[dict] = []
    if _collector_on(m, "reports"):
        report_nodes, report_edges = collect_reports()
        all_nodes += report_nodes
    if _collector_on(m, "memory_topics"):
        all_nodes += collect_memory_topics()
    if _collector_on(m, "predictive_gates"):
        all_nodes += collect_predictive_gates()
    if _collector_on(m, "frozen_tell"):
        all_nodes += collect_frozen_tell()
    if _collector_on(m, "hardcoded_data_sources"):
        all_nodes += hardcoded_data_sources()
    if _collector_on(m, "hardcoded_notion_dbs"):
        all_nodes += hardcoded_notion_dbs()
    # biblical system
    if _collector_on(m, "biblical"):
        all_nodes += collect_biblical_skills()
        all_nodes += collect_biblical_agents()
        all_nodes += collect_biblical_wiki()
        biblical_report_nodes, biblical_report_edges = collect_biblical_reports()
        all_nodes += biblical_report_nodes
        all_nodes += collect_biblical_corpus()
        all_nodes += collect_biblical_memory()
    else:
        biblical_report_edges = []
    if _collector_on(m, "model_call_log"):
        print("ingesting model call log...")
        model_call_rows = collect_model_call_log()
        print(f"  ledger rows: {len(model_call_rows)}")
        task_type_nodes, task_type_edges = collect_task_type_nodes_and_edges(model_call_rows)
        print(f"  task_type nodes: {len(task_type_nodes)}")
        print(f"  skill→task_type edges: {len(task_type_edges)}")
        all_nodes += task_type_nodes
    else:
        task_type_edges = []

    # custom AST code layer (replaces the retired graphify bridge)
    code_nodes: list[dict] = []
    code_edges: list[dict] = []
    if _collector_on(m, "code_ast"):
        print("extracting code AST...")
        _scripts_dir = str(Path(__file__).resolve().parent)
        if _scripts_dir not in sys.path:
            sys.path.insert(0, _scripts_dir)
        from code_ast_extractor import collect_code_ast

        roots = [Path(p) for p in (m["code_roots"] if m else [str(WORKSPACE)])]
        code_nodes, code_edges = collect_code_ast(roots, NOW_ISO)
        all_nodes += code_nodes

    # semantic layer — reads semantic_layer.json (LLM pass run separately by
    # code_semantic_extractor.py); no API calls during the build. Empty if
    # the semantic layer has not been generated yet.
    sem_nodes: list[dict] = []
    sem_edges: list[dict] = []
    if _collector_on(m, "code_semantic"):
        from code_semantic_extractor import collect_code_semantic

        roots = [Path(p) for p in (m["code_roots"] if m else [str(WORKSPACE)])]
        sem_nodes, sem_edges = collect_code_semantic(roots, NOW_ISO)
        all_nodes += sem_nodes

    # de-dupe nodes by id
    seen_ids = {}
    for n in all_nodes:
        seen_ids[n["id"]] = n
    nodes = list(seen_ids.values())

    # Phase 2: collect edges
    print("collecting edges...")
    all_edges: list[dict] = []
    all_edges += skill_edges
    all_edges += report_edges
    all_edges += biblical_report_edges
    all_edges += hardcoded_pattern_edges()
    all_edges += biblical_pattern_edges()
    all_edges += task_type_edges  # bite 8
    all_edges += code_edges       # custom AST code layer
    all_edges += sem_edges        # semantic layer
    all_edges += collect_similarity_edges(args.output)  # embedding similarity

    # Phase 3: filter edges to those with valid endpoints
    valid_ids = set(seen_ids.keys())
    edges = []
    dropped_edges = 0
    for e in all_edges:
        if e["src"] in valid_ids and e["dst"] in valid_ids:
            edges.append(e)
        else:
            dropped_edges += 1

    # Phase 4: write
    print(f"writing {len(nodes)} nodes, {len(edges)} edges, "
          f"{len(model_call_rows)} model_call_log rows "
          f"(dropped {dropped_edges} dangling edges) → {args.output}")
    write_atomic(args.output, nodes, edges, model_call_rows=model_call_rows)

    # graphify bridge retired — code layer now comes from collect_code_ast()
    # (stdlib AST extractor; --skip-graphify kept as a harmless no-op flag).

    if args.summary or True:  # always print summary by default
        print_summary(args.output)

    return 0


if __name__ == "__main__":
    sys.exit(main())
