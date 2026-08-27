"""Graph membership SSOT — which local folders feed graphify collectors.

Stored in ``graph_sources.json`` under the Khipu data dir. Graphify reads the
generated ``graph_sources.resolved.json`` (see ``export_resolved``).
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2
SOURCES_NAME = "graph_sources.json"

COLLECTOR_KEYS = frozenset(
    {
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
    }
)

SEEDED_IDS = frozenset(
    {
        "conversation_memory",
        "code:claude",
        "wiki:claude",
        "skills:claude",
        "agents:claude",
        "reports:claude",
        "memory_topics",
        "frozen_tell",
        "hardcoded",
        "biblical:system",
        "model_call_log",
    }
)

_SOURCE_COLLECTORS: dict[str, frozenset[str]] = {
    "wiki:claude": frozenset({"tickers"}),
    "skills:claude": frozenset({"skills", "predictive_gates"}),
    "agents:claude": frozenset({"agents"}),
    "reports:claude": frozenset({"reports"}),
    "memory_topics": frozenset({"memory_topics"}),
    "frozen_tell": frozenset({"frozen_tell"}),
    "hardcoded": frozenset({"hardcoded_data_sources", "hardcoded_notion_dbs"}),
    "biblical:system": frozenset({"biblical"}),
    "model_call_log": frozenset({"model_call_log"}),
}


def sources_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / SOURCES_NAME


def conversation_media_root() -> Path:
    """Landing folder for conversation JSONL PNG/JPEG (not a graph collector)."""
    from khipu.paths import ensure_data_dir

    p = ensure_data_dir() / "media" / "conversation_memory"
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_document() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "sources": [],
    }


def _normalize_source_row(row: dict) -> dict:
    """Ensure per-source keys exist; missing ``embed_media`` means false."""
    if "embed_media" not in row:
        row["embed_media"] = False
    else:
        row["embed_media"] = bool(row.get("embed_media"))
    if row.get("id") == "conversation_memory" and not row.get("root"):
        row["root"] = str(conversation_media_root())
    return row


def load_sources() -> dict:
    path = sources_file()
    if not path.is_file():
        return default_document()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return default_document()
        if not isinstance(data.get("sources"), list):
            data["sources"] = default_document()["sources"]
        data.setdefault("schema_version", SCHEMA_VERSION)
        for s in data["sources"]:
            if isinstance(s, dict):
                _normalize_source_row(s)
        return data
    except (OSError, ValueError):
        return default_document()


def save_sources(doc: dict) -> Path:
    path = sources_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(doc, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)
    return path


def _find_source(doc: dict, source_id: str) -> dict | None:
    for s in doc.get("sources", []):
        if s.get("id") == source_id:
            return s
    return None


def set_enabled(source_id: str, enabled: bool) -> dict:
    doc = load_sources()
    row = _find_source(doc, source_id)
    if row is None:
        raise ValueError(f"unknown source id: {source_id}")
    row["enabled"] = bool(enabled)
    save_sources(doc)
    return doc


def set_embed_media(source_id: str, embed_media: bool) -> dict:
    """Per-source opt-in for native image embed (default off). Does not purge PG."""
    doc = load_sources()
    row = _find_source(doc, source_id)
    if row is None:
        raise ValueError(f"unknown source id: {source_id}")
    row["embed_media"] = bool(embed_media)
    if source_id == "conversation_memory":
        row["root"] = str(conversation_media_root())
    doc["schema_version"] = SCHEMA_VERSION
    save_sources(doc)
    return doc


def embed_media_enabled(source_id: str) -> bool:
    doc = load_sources()
    row = _find_source(doc, source_id)
    if row is None:
        return False
    return bool(row.get("embed_media", False))


def sources_with_embed_media() -> list[dict]:
    """Sources opted into image embed that have a walkable ``root``."""
    out: list[dict] = []
    for s in load_sources().get("sources", []):
        if not isinstance(s, dict):
            continue
        if not bool(s.get("embed_media", False)):
            continue
        root = s.get("root")
        if not root:
            continue
        out.append(s)
    return out


def _slug_from_path(root: Path) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")
    return slug or "root"


def add_code_root(root: Path) -> dict:
    root = root.resolve()
    if not root.is_absolute():
        raise ValueError(f"code root must be absolute: {root}")
    doc = load_sources()
    for s in doc.get("sources", []):
        existing = s.get("root")
        if existing and Path(str(existing)).resolve() == root:
            raise ValueError(f"code root already registered: {root}")
    source_id = f"code:{_slug_from_path(root)}"
    if _find_source(doc, source_id) is not None:
        raise ValueError(f"source id already exists: {source_id}")
    doc.setdefault("sources", []).append(
        {
            "id": source_id,
            "kind": "code_ast",
            "root": str(root),
            "enabled": True,
            "embed_media": False,
        }
    )
    save_sources(doc)
    from khipu.components_matrix import set_graph_producer

    set_graph_producer(True)
    return doc


def remove_user_source(source_id: str) -> dict:
    if source_id in SEEDED_IDS:
        raise ValueError(f"cannot remove seeded source: {source_id}")
    doc = load_sources()
    sources = doc.get("sources", [])
    idx = next((i for i, s in enumerate(sources) if s.get("id") == source_id), None)
    if idx is None:
        raise ValueError(f"unknown source id: {source_id}")
    sources.pop(idx)
    save_sources(doc)
    return doc


def conversation_memory_enabled() -> bool:
    doc = load_sources()
    row = _find_source(doc, "conversation_memory")
    if row is None:
        return True
    return bool(row.get("enabled", True))


def _path_missing(root: str, kind: str) -> bool:
    p = Path(root)
    if kind == "model_call_log":
        return not p.is_file()
    return not p.is_dir()


def resolve_for_graphify(*, now: datetime | None = None) -> dict:
    doc = load_sources()
    now = now or datetime.now(timezone.utc)
    collectors: dict[str, bool] = {k: True for k in COLLECTOR_KEYS}
    unreachable: list[dict[str, str]] = []
    code_roots: list[str] = []

    for s in doc.get("sources", []):
        sid = str(s.get("id") or "")
        enabled = bool(s.get("enabled", True))
        kind = str(s.get("kind") or "")
        root = s.get("root")
        if root:
            root_str = str(root)
            if enabled and _path_missing(root_str, kind):
                unreachable.append({"id": sid, "root": root_str, "reason": "missing"})
        if not enabled:
            keys = _SOURCE_COLLECTORS.get(sid, frozenset())
            for key in keys:
                collectors[key] = False
        if kind == "code_ast" and enabled and root:
            root_str = str(root)
            if not _path_missing(root_str, kind):
                code_roots.append(root_str)

    has_code = len(code_roots) > 0
    collectors["code_ast"] = has_code and collectors.get("code_ast", True)
    collectors["code_semantic"] = has_code and collectors.get("code_semantic", True)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "conversation_memory": conversation_memory_enabled(),
        "collectors": collectors,
        "code_roots": code_roots,
        "unreachable": unreachable,
    }


def resolved_path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    override = (os.environ.get("KHIPU_GRAPH_SOURCES_RESOLVED") or "").strip()
    if override:
        return Path(override)
    from khipu.paths import ensure_data_dir

    return ensure_data_dir() / "graph_sources.resolved.json"


def export_resolved(path: Path | None = None) -> dict:
    dest = resolved_path(path)
    payload = resolve_for_graphify()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(tmp, dest)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    return payload


def collector_flags_from_resolved(raw: dict) -> dict[str, bool]:
    coll = raw.get("collectors") if isinstance(raw.get("collectors"), dict) else {}
    flags: dict[str, bool] = {}
    for key in COLLECTOR_KEYS:
        flags[key] = False if coll.get(key) is False else True
    return flags


def _code_source_for_path(source_path: str | None, doc: dict) -> str:
    if not source_path:
        return "code:claude"
    matched = _code_source_id_for_path(source_path, doc)
    return matched if matched is not None else "code:claude"


def _code_source_id_for_path(source_path: str | None, doc: dict) -> str | None:
    """Longest registered code_ast root prefix; None when unmatched."""
    if not source_path:
        return None
    sp = source_path.replace("\\", "/")
    best_id: str | None = None
    best_len = -1
    for s in doc.get("sources", []):
        if s.get("kind") != "code_ast":
            continue
        root = s.get("root")
        if not root:
            continue
        root_path = Path(str(root))
        try:
            rel = str(root_path.resolve())
        except OSError:
            rel = str(root_path)
        root_posix = rel.replace("\\", "/")
        if sp.startswith(root_posix) and len(root_posix) > best_len:
            sid = str(s.get("id") or "").strip()
            if sid:
                best_id = sid
                best_len = len(root_posix)
    return best_id


def owned_source_ids(doc: dict | None = None) -> set[str]:
    """Enabled and reachable source ids from graph_sources.json on this Mac."""
    doc = doc or load_sources()
    off = disabled_or_unreachable_ids(doc)
    return {
        str(s.get("id"))
        for s in doc.get("sources", [])
        if s.get("id") and str(s.get("id")) not in off
    }


def source_id_for_graphify_node(
    *,
    node_id: str,
    type: str,
    bucket: str | None,
    source_path: str | None,
    doc: dict | None = None,
) -> str | None:
    doc = doc or load_sources()
    nid = (node_id or "").strip()
    ntype = (type or "").strip()
    bkt = (bucket or "").strip()

    if bkt == "conversation-memory":
        return "conversation_memory"
    sp = (source_path or "").replace("\\", "/")
    if "/Memory/conversations/graph" in sp:
        return "conversation_memory"

    if nid.startswith("memory_topic:"):
        return "memory_topics"
    if nid.startswith("ticker:"):
        return "wiki:claude"
    if nid.startswith("skill:"):
        return "biblical:system" if bkt == "biblical" else "skills:claude"
    if nid.startswith("agent:"):
        return "biblical:system" if bkt == "biblical" else "agents:claude"
    if nid.startswith("report:") or ntype == "report":
        return "biblical:system" if bkt == "biblical" else "reports:claude"
    if nid.startswith("corpus_author:") or bkt == "biblical":
        return "biblical:system"
    if nid.startswith("gate:"):
        return "skills:claude"
    if ntype == "sentiment_run" or nid.startswith("tell:"):
        return "frozen_tell"
    if nid.startswith("data_source:") or nid.startswith("notion_db:"):
        return "hardcoded"
    if ntype in {"module", "function", "class"} or bkt == "code":
        matched = _code_source_id_for_path(sp or None, doc)
        if matched:
            return matched
        # Mac 1 still labels unmatched code as code:claude when that source is
        # in this Mac's membership file. A join Mac that never listed it must
        # not stamp Mac 1's id — scoped delete would then purge those nodes.
        if _find_source(doc, "code:claude") is not None:
            return _code_source_for_path(sp or None, doc)
        return None
    return None


def source_id_for_delete(
    *,
    node_id: str,
    type: str,
    bucket: str | None,
    source_path: str | None,
    doc: dict | None = None,
) -> str | None:
    """Resolve source_id for delete eligibility; never defaults code paths to code:claude."""
    doc = doc or load_sources()
    nid = (node_id or "").strip()
    ntype = (type or "").strip()
    bkt = (bucket or "").strip()

    if bkt == "conversation-memory":
        return "conversation_memory"
    sp = (source_path or "").replace("\\", "/")
    if "/Memory/conversations/graph" in sp:
        return "conversation_memory"

    if nid.startswith("memory_topic:"):
        return "memory_topics"
    if nid.startswith("ticker:"):
        return "wiki:claude"
    if nid.startswith("skill:"):
        return "biblical:system" if bkt == "biblical" else "skills:claude"
    if nid.startswith("agent:"):
        return "biblical:system" if bkt == "biblical" else "agents:claude"
    if nid.startswith("report:") or ntype == "report":
        return "biblical:system" if bkt == "biblical" else "reports:claude"
    if nid.startswith("corpus_author:") or bkt == "biblical":
        return "biblical:system"
    if nid.startswith("gate:"):
        return "skills:claude"
    if ntype == "sentiment_run" or nid.startswith("tell:"):
        return "frozen_tell"
    if nid.startswith("data_source:") or nid.startswith("notion_db:"):
        return "hardcoded"
    if ntype in {"module", "function", "class"} or bkt == "code":
        return _code_source_id_for_path(sp or None, doc)
    return None


def disabled_or_unreachable_ids(doc: dict | None = None) -> set[str]:
    doc = doc or load_sources()
    disabled = {
        str(s.get("id"))
        for s in doc.get("sources", [])
        if s.get("id") and not s.get("enabled", True)
    }
    resolved = resolve_for_graphify()
    unreachable = {
        str(u.get("id")) for u in resolved.get("unreachable", []) if u.get("id")
    }
    return disabled | unreachable


def should_delete_graphify_node(node: dict[str, Any], membership_off: set[str]) -> bool:
    bkt = (node.get("bucket") or "").strip()
    if bkt == "conversation-memory":
        return False
    sp = node.get("source_path")
    if sp and "/Memory/conversations/graph" in str(sp).replace("\\", "/"):
        return False
    pg_sid = node.get("source_id")
    if pg_sid:
        sid = str(pg_sid).strip() or None
    else:
        sid = source_id_for_delete(
            node_id=str(node.get("id") or ""),
            type=str(node.get("type") or ""),
            bucket=node.get("bucket"),
            source_path=sp,
        )
    if not sid:
        return False
    if sid in membership_off:
        return False
    return True


def should_delete_graphify_edge(
    src: dict[str, Any],
    dst: dict[str, Any],
    membership_off: set[str],
) -> bool:
    """Delete a leftover graphify edge only when both endpoints are deleteable.

    Membership-off / unreachable-source leftover edges stay, matching leftover
    nodes: they must not fail graph_drift.
    """
    return should_delete_graphify_node(
        src, membership_off
    ) and should_delete_graphify_node(dst, membership_off)


def _edge_endpoint_node(
    node_id: str, nodes_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    found = nodes_by_id.get(node_id)
    if found is not None:
        return found
    return {
        "id": node_id,
        "type": "",
        "bucket": None,
        "source_path": None,
        "source_id": None,
    }


def drift_failing_pg_extras(
    extras: list[dict[str, Any]],
    membership_off: set[str],
) -> list[dict[str, Any]]:
    return [n for n in extras if should_delete_graphify_node(n, membership_off)]


def drift_failing_pg_extra_edges(
    extras: list[tuple[str, str, str]],
    nodes_by_id: dict[str, dict[str, Any]],
    membership_off: set[str],
) -> list[tuple[str, str, str]]:
    failing: list[tuple[str, str, str]] = []
    for src, dst, etype in extras:
        if should_delete_graphify_edge(
            _edge_endpoint_node(src, nodes_by_id),
            _edge_endpoint_node(dst, nodes_by_id),
            membership_off,
        ):
            failing.append((src, dst, etype))
    return failing
