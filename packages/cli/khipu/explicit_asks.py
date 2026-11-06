"""Deliberately filed questions. Recording an ask grants no authority."""
from khipu import commitments as co

MARKER = "explicit_ask"


def validate(args):
    allowed = {"action", "requester", "question", "yes", "no", "episode_id", "project", "write_id"}
    if set(args) - allowed:
        raise ValueError("ask takes requester, question, yes, no, optional episode_id and project")
    values = {}
    for key in ("requester", "question", "yes", "no"):
        value = args.get(key)
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            raise ValueError(f"ask requires one line for {key}")
        values[key] = value.strip()
    eid = args.get("episode_id")
    if eid is not None and (type(eid) is not int or eid <= 0):
        raise ValueError("ask requires a positive source episode_id")
    project = args.get("project") or None
    if project is not None and not isinstance(project, str):
        raise ValueError("project must be a project name")
    return dict(values, episode_id=eid, project=project)


def file_on_hub(args):
    from khipu import db, owner_writes
    values = validate(args)
    from khipu.redact import redact_secrets
    text = redact_secrets(f"{values['requester']} asks: {values['question']} Yes: {values['yes']}. No: {values['no']}.")[0]
    with db.connect() as conn, conn.cursor() as cur:
        return owner_writes.apply_on_hub(conn, cur, "owed_update", args,
                                        lambda: _write_ask(cur, values, text), {"action": "ask"})


def _write_ask(cur, values, text):
    """The one wording writer: declared, episode/owner claim, then projection."""
    from khipu import provenance
    from khipu import identity
    project = values["project"]
    episode = values["episode_id"]
    if episode is not None:
        cur.execute("SELECT project FROM episodes WHERE id = %s AND deleted_at IS NULL FOR SHARE",
                    (episode,))
        source = cur.fetchone()
        if source is None:
            raise ValueError("ask source episode is missing or forgotten")
        if not co._fenced_owed_rows([{"opened_episode": episode}]):
            raise ValueError("ask source episode is forgotten or unreadable")
        project = project or source[0]
    if project:
        project, visible = identity.resolve_project(cur, project)
        if visible:
            project = identity.project_canonical_group(cur, project)[0] or project
    content_hash = co.content_hash(project, text)
    # NULL project keys do not collide in the existing unique index.
    # Serialize identical asks as well as handling the indexed conflict.
    cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"khipu:explicit-ask:{project!r}:{content_hash}",))
    def existing():
        cur.execute("SELECT id, opened_episode, owner_reassigned_by FROM commitments "
                    "WHERE project IS NOT DISTINCT FROM %s AND content_hash = %s AND status = 'open'",
                    (project, content_hash))
        row = cur.fetchone()
        if row is None:
            return None
        if row[2] != MARKER or not co._fenced_owed_rows([{"opened_episode": row[1]}]):
            raise ValueError("An existing item prevents filing this ask; no item was changed")
        return {"ok": True, "id": row[0], "action": "ask", "owner": "user", "text": text,
                "already_open": True}
    found = existing()
    if found:
        return found
    provenance.declare_writer(cur)
    cur.execute("INSERT INTO commitments (text, project, owner, kind, opened_episode, content_hash, "
                "origin, owner_reassigned_at, owner_reassigned_by, owner_reassign_reason) "
                "VALUES (%s, %s, 'user', 'question', %s, %s, 'conversation', now(), %s, %s) "
                "ON CONFLICT (project, content_hash) WHERE status = 'open' DO NOTHING "
                "RETURNING id", (text, project, episode, content_hash,
                                 MARKER, "Deliberately filed ask; no authority to apply anything"))
    inserted = cur.fetchone()
    if inserted is None:
        return existing()
    cid = inserted[0]
    provenance.record_claim(cur, "commitment", cid, "text", text, episode_id=episode)
    provenance.project(cur, "commitment", cid)
    return {"ok": True, "id": cid, "action": "ask", "owner": "user", "text": text}


def needs_you(cur):
    from khipu.needs_you import _item
    from khipu.routing import _iso
    cur.execute("SELECT to_jsonb(commitments) FROM commitments WHERE status = 'open' "
                "AND owner_reassigned_by = %s AND (due_after IS NULL OR due_after <= now()) "
                "AND " + co._not_forgotten_clause(cur) + " ORDER BY opened_at, id", (MARKER,))
    rows = co._fenced_owed_rows([raw[0] for raw in cur.fetchall()])
    return [_item("ask", f"ask:{r['id']}", r["text"],
                  by={"kind": "conversation"}, at=_iso(co._as_utc(r["opened_at"])),
                  do=f"The conversation that hears your answer closes this ask with "
                     f"khipu_owed_update(action='close', id={r['id']}); this question cannot apply a change.")
            for r in rows if r.get("owner_reassigned_by") == MARKER]
