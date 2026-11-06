"""Deliberately filed questions. Recording an ask grants no authority."""
from khipu import commitments as co

MARKER = "explicit_ask"


def validate(args):
    allowed = {"action", "requester", "yes", "no", "episode_id", "write_id"}
    if set(args) - allowed:
        raise ValueError("ask takes only requester, yes, no and episode_id")
    values = {}
    for key in ("requester", "yes", "no"):
        value = args.get(key)
        if not isinstance(value, str) or not value.strip() or "\n" in value:
            raise ValueError(f"ask requires one line for {key}")
        values[key] = value.strip()
    eid = args.get("episode_id")
    if type(eid) is not int or eid <= 0:
        raise ValueError("ask requires a positive source episode_id")
    return dict(values, episode_id=eid)


def file_on_hub(args):
    from khipu import db, owner_writes, provenance
    values = validate(args)
    from khipu.redact import redact_secrets
    text = redact_secrets(f"{values['requester']} asks: yes means {values['yes']}; no means {values['no']}.")[0]
    with db.connect() as conn, conn.cursor() as cur:
        def write():
            cur.execute("SELECT project FROM episodes WHERE id = %s AND deleted_at IS NULL FOR SHARE",
                        (values["episode_id"],))
            source = cur.fetchone()
            if source is None:
                raise ValueError("ask source episode is missing or forgotten")
            if not co._fenced_owed_rows([{"opened_episode": values["episode_id"]}]):
                raise ValueError("ask source episode is forgotten or unreadable")
            provenance.declare_writer(cur)
            cur.execute("INSERT INTO commitments (text, project, owner, kind, opened_episode, content_hash, "
                        "origin, owner_reassigned_at, owner_reassigned_by, owner_reassign_reason) "
                        "VALUES (%s, %s, 'user', 'question', %s, %s, 'conversation', now(), %s, %s) "
                        "RETURNING id", (text, source[0], values["episode_id"], co.content_hash(source[0], text),
                                         MARKER, "Deliberately filed ask; no authority to apply anything"))
            cid = cur.fetchone()[0]
            provenance.record_claim(cur, "commitment", cid, "text", text, episode_id=values["episode_id"])
            provenance.project(cur, "commitment", cid)
            return {"ok": True, "id": cid, "action": "ask", "owner": "user", "text": text}
        return owner_writes.apply_on_hub(conn, cur, "owed_update", args, write, {"action": "ask"})


def needs_you(cur):
    from khipu.needs_you import _item
    from khipu.routing import _iso
    cur.execute("SELECT to_jsonb(commitments) FROM commitments WHERE status = 'open' "
                "AND owner_reassigned_by = %s AND (due_after IS NULL OR due_after <= now()) "
                "AND " + co._not_forgotten_clause(cur) + " ORDER BY opened_at, id", (MARKER,))
    rows = co._fenced_owed_rows([raw[0] for raw in cur.fetchall()])
    return [_item("ask", f"ask:{r['id']}", r["text"],
                  by={"kind": "conversation"}, at=_iso(co._as_utc(r["opened_at"])),
                  do="Answer in your own words; this question cannot apply a change.")
            for r in rows if r.get("owner_reassigned_by") == MARKER]
