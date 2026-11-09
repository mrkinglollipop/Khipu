"""What is left of the removed job-origin correction: forget scrubs old request records.

The correction command is gone. Old `commitment_origin_requests` rows can still hold
wording from a preview, so forget rewrites each one down to its bare references.
"""
from __future__ import annotations

import hashlib
import json


def _request_references(doc):
    """Keep exact review bindings without retaining wording or display metadata."""
    entries = doc.get("items") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return []
    refs, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        cid, fingerprint = entry.get("id"), entry.get("fingerprint")
        if type(cid) is not int or cid <= 0 or cid in seen:
            continue
        proposal = entry.get("proposal_fingerprint")
        if proposal is None and isinstance(entry.get("after"), dict):
            proposal = _fingerprint(entry["after"])
        if any(not isinstance(value, str) or len(value) != 64
               or any(char not in "0123456789abcdef" for char in value)
               for value in (fingerprint, proposal)):
            continue
        seen.add(cid)
        refs.append({"id": cid, "fingerprint": fingerprint, "proposal_fingerprint": proposal})
    return refs


def _request_document(doc):
    return {"format": "khipu-job-origin-preview", "version": 2,
            "items": _request_references(doc)}


def scrub_request_previews(cur):
    """Forget also removes wording from pre-reference request records."""
    from khipu.db import has_columns, has_table

    if not has_table(cur, "commitment_origin_requests"):
        return
    if not has_columns(cur, "commitment_origin_requests", "id", "preview"):
        raise ValueError("Job-origin request records cannot be safely scrubbed")
    cur.execute("SELECT id, preview FROM commitment_origin_requests ORDER BY id FOR UPDATE")
    for rid, doc in cur.fetchall():
        safe = _request_document(doc)
        if doc != safe:
            cur.execute("UPDATE commitment_origin_requests SET preview = %s::jsonb WHERE id = %s",
                        (json.dumps(safe), rid))


def _fingerprint(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, default=str).encode()).hexdigest()
