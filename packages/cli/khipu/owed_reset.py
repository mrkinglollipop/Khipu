"""One-time archival reset, never run automatically. Uses tidy's batch/undo records."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

from khipu import commitments as co, owed_tidy as tidy
from khipu.explicit_asks import MARKER

FORMAT = "khipu-owed-owner-reset"
REASON = "Owner-approved reset of summary-inferred Owed items; original items archived with SHA-256"


def snapshot(cur, *, lock=False):
    cur.execute("SELECT to_jsonb(c), EXISTS (SELECT 1 FROM fact_claims f WHERE f.kind = 'commitment' "
                "AND f.fact_id = c.id AND f.claimant = 'owner' AND f.created_at >= now() - interval '30 days') "
                "FROM commitments c WHERE c.status = 'open' ORDER BY c.id" + (" FOR UPDATE OF c" if lock else ""))
    rows = []
    for row, recent_claim in cur.fetchall():
        if row.get("owner") != co.OWNER_USER and co.row_owner(row)[0] != co.OWNER_USER:
            continue
        row["reset_recent_owner_ask"] = bool(recent_claim or (
            row.get("owner_reassigned_at") and co._as_utc(row["owner_reassigned_at"]) >=
            datetime.now(timezone.utc) - timedelta(days=30)))
        rows.append(row)
    return co._fenced_owed_rows(rows)


def group(row):
    if row.get("owner_reassigned_by") == MARKER:
        return "explicit_asks"
    if row.get("reset_recent_owner_ask"):
        return "recent_owner_asks"
    return "to_close"


def preview(cur):
    rows = snapshot(cur)
    groups = {key: [r["id"] for r in rows if group(r) == key]
              for key in ("to_close", "explicit_asks", "recent_owner_asks")}
    return {"format": FORMAT, "version": 1, "project": tidy.ALL_PROJECTS,
            "planned_at": datetime.now(timezone.utc).isoformat(), "reason": REASON,
            "counts": {k: len(v) for k, v in groups.items()}, "groups": groups,
            "items": [{"row": r, "fingerprint": tidy.fingerprint(r), "group": group(r)} for r in rows]}


def export(doc, path):
    path = Path(path)
    data = (json.dumps(doc, indent=2, sort_keys=True, default=str) + "\n").encode()
    digest = hashlib.sha256(data).hexdigest()
    # Exclusive creation: an existing archive is never overwritten.
    with path.open("xb") as out:
        out.write(data)
    with Path(str(path) + ".sha256").open("x") as out:
        out.write(digest + "  " + path.name + "\n")
    return digest


def apply(doc, *, owner_gate=None):
    from khipu import db
    if doc.get("format") != FORMAT or doc.get("version") != 1 or doc.get("project") != tidy.ALL_PROJECTS:
        raise ValueError("not an owner reset archive")
    if owner_gate not in co.OWNER_GATE_PASSES:
        owner_gate = co.bulk_owner_gate(len(doc["items"]))
    if owner_gate not in co.OWNER_GATE_PASSES:
        return {"ok": False, "changed": 0, "error": co.bulk_owner_refusal()}
    with db.connect() as conn, conn.cursor() as cur:
        if not tidy._ready(cur):
            return {"ok": False, "changed": 0, "error": tidy.NOT_READY}
        current = {r["id"]: r for r in snapshot(cur, lock=True)}
        ids, skipped = [], []
        for entry in doc["items"]:
            cid = entry["row"]["id"]
            row = current.get(cid)
            if entry["group"] != "to_close":
                continue
            if row is None or group(row) != "to_close" or tidy.fingerprint(row) != entry["fingerprint"]:
                skipped.append(cid)
            else:
                ids.append(cid)
        changed, batches = tidy.bulk_batch(cur, ids, "close", lambda cid: co.set_status(
            cur, cid, "closed", reason=REASON, by=co.BY_CONVERSATION),
            reason=REASON, gate=owner_gate, all_projects=True)
    return {"ok": True, "changed": sum(changed.values()), "batches": batches, "skipped": skipped}


def command(args):
    from khipu import db
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
        doc = preview(cur)
    out = {"ok": True, "dry_run": args.dry_run, "counts": doc["counts"], "groups": doc["groups"]}
    if not args.dry_run:
        if not args.export:
            raise ValueError("export requires --export FILE (or use --dry-run)")
        out.update(export=args.export, sha256=export(doc, args.export))
    print(json.dumps(out, indent=2))
    return 0


def add_parser(sub):
    parser = sub.add_parser("reset", help="Preview/export open owner items; does not close them",
                            description="Use --dry-run to print counts and protected asks without writing; "
                            "--export FILE writes full owner items and FILE.sha256. Review the archive, then "
                            "apply through the existing owner gate: khipu owed tidy --project "
                            "'<all-owner-items>' --apply FILE. Undo the resulting batch with the same project "
                            "and --undo BATCH. Explicit asks and recent owner-ask evidence are kept separately; "
                            "new or changed items are not silently closed. No automatic reset is installed.")
    parser.add_argument("--dry-run", action="store_true", help="Print counts and separate protected asks; write nothing")
    parser.add_argument("--export", metavar="FILE", help="Export full archive and FILE.sha256")
    parser.set_defaults(func=command)
