"""Memory sources registry: which sources are on, and which embed images.

Stored in ``graph_sources.json`` under the Khipu data dir (the file keeps its
old name so an existing registry is read as it is).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA_VERSION = 2
SOURCES_NAME = "graph_sources.json"


class InvalidSourcesDocument(ValueError):
    """An existing source registry cannot safely be changed."""


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


def sources_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / SOURCES_NAME


def conversation_media_root() -> Path:
    """Landing folder for conversation JSONL PNG/JPEG ."""
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
    """Fill in the conversation_memory root when it is missing."""
    if row.get("id") == "conversation_memory" and not row.get("root"):
        row["root"] = str(conversation_media_root())
    return row


def load_sources() -> dict:
    path = sources_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_document()
    except OSError as exc:
        raise InvalidSourcesDocument(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
            raise InvalidSourcesDocument(f"cannot read malformed source registry: {path}")
        data.setdefault("schema_version", SCHEMA_VERSION)
        for s in data["sources"]:
            if not isinstance(s, dict):
                raise InvalidSourcesDocument(f"cannot read malformed source registry: {path}")
            _normalize_source_row(s)
        return data
    except InvalidSourcesDocument:
        raise
    except (OSError, ValueError) as exc:
        raise InvalidSourcesDocument(f"cannot read {path}: {exc}") from exc


def _load_sources_for_mutation() -> dict:
    """Load an existing registry without treating corruption as an empty one."""
    path = sources_file()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default_document()
    except OSError as exc:
        raise InvalidSourcesDocument(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise InvalidSourcesDocument(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("sources"), list):
        raise InvalidSourcesDocument(f"cannot modify malformed source registry: {path}")
    for row in data["sources"]:
        if not isinstance(row, dict):
            raise InvalidSourcesDocument(f"cannot modify malformed source registry: {path}")
        _normalize_source_row(row)
    data.setdefault("schema_version", SCHEMA_VERSION)
    return data


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
    doc = _load_sources_for_mutation()
    row = _find_source(doc, source_id)
    if row is None:
        raise ValueError(f"unknown source id: {source_id}")
    row["enabled"] = bool(enabled)
    save_sources(doc)
    return doc


def remove_user_source(source_id: str) -> dict:
    if source_id in SEEDED_IDS:
        raise ValueError(f"cannot remove seeded source: {source_id}")
    doc = _load_sources_for_mutation()
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
