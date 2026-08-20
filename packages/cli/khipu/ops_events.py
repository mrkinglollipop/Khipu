"""Insert ops_events rows via the Mac DSN (Linode scripts keep record_ops_event.sh)."""

from __future__ import annotations

import json


def record(kind: str, status: str, detail: dict | None = None) -> dict:
    kind = (kind or "").strip()
    status = (status or "").strip()
    if not kind or status not in {"ok", "fail", "mismatch"}:
        raise ValueError("kind required; status must be ok|fail|mismatch")
    payload = detail if isinstance(detail, dict) else {}
    from khipu.db import connect

    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops_events (kind, status, detail) VALUES (%s, %s, %s::jsonb) "
            "RETURNING id, created_at",
            (kind, status, json.dumps(payload)),
        )
        row = cur.fetchone()
        conn.commit()
    return {
        "ok": True,
        "id": row[0],
        "created_at": row[1].isoformat(),
        "kind": kind,
        "status": status,
    }
