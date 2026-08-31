"""Local graph.sqlite snapshot health, ops_events recording, offsite copy, restore drill."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from khipu.drift import BACKUP_MAX_AGE_HOURS

DEST_REMOTE = "r2:matt-db-backups/khipu-graph"

OFFSITE_MAX_AGE_DAYS = 8
INTEGRITY_TIMEOUT_SECONDS = 60


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _resolve_producer(producer: bool | None) -> bool:
    if producer is not None:
        return producer
    from khipu.graph_sync import is_graph_producer

    return is_graph_producer()


def _live_graph() -> Path | None:
    from khipu.config import path_setting

    return path_setting("graph_sqlite")


def _default_snapshot_dir() -> Path:
    raw = (os.environ.get("KHIPU_GRAPH_SNAPSHOT_DIR") or "").strip()
    if raw:
        return Path(raw)
    # The launchd jobs carry KHIPU_GRAPH_SNAPSHOT_DIR in their plist env, but
    # doctor also runs from shells and from the app, which do not. Read the
    # installed plist so every context sees the same snapshot dir (same
    # fallback jobs.py uses for KHIPU_GRAPHIFY_NIGHTLY).
    from khipu.jobs import PLIST_GRAPH, _plist_env_path

    plist_dir = _plist_env_path(PLIST_GRAPH, "KHIPU_GRAPH_SNAPSHOT_DIR")
    if plist_dir is not None:
        return plist_dir
    from khipu.paths import ensure_data_dir

    return ensure_data_dir() / "backups" / "graph"


def latest_snapshot(snapshot_dir: Path | None = None) -> Path | None:
    root = Path(snapshot_dir) if snapshot_dir else _default_snapshot_dir()
    if not root.is_dir():
        return None
    candidates = sorted(
        root.glob("graph-*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def local_health(
    *,
    now: datetime | None = None,
    snapshot_dir: Path | None = None,
    producer: bool | None = None,
) -> dict:
    """Non-producer → ok skipped. Producer → latest snapshot mtime ≤ 36h and size > 0."""
    if not _resolve_producer(producer):
        return {
            "ok": True,
            "skipped": True,
            "reason": "not the graph producer",
            "max_age_hours": BACKUP_MAX_AGE_HOURS,
        }

    now = now or _utcnow()
    root = Path(snapshot_dir) if snapshot_dir else _default_snapshot_dir()
    snap = latest_snapshot(root)
    max_age_seconds = BACKUP_MAX_AGE_HOURS * 3600

    if snap is None:
        return {
            "ok": False,
            "skipped": False,
            "reason": f"no graph-*.sqlite in {root}",
            "snapshot_dir": str(root),
            "max_age_hours": BACKUP_MAX_AGE_HOURS,
        }

    st = snap.stat()
    if st.st_size <= 0:
        return {
            "ok": False,
            "skipped": False,
            "reason": "latest snapshot is empty",
            "path": str(snap),
            "bytes": st.st_size,
            "max_age_hours": BACKUP_MAX_AGE_HOURS,
        }

    mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
    age_seconds = (now - mtime).total_seconds()
    fresh = age_seconds <= max_age_seconds
    out: dict = {
        "ok": fresh,
        "skipped": False,
        "path": str(snap),
        "bytes": st.st_size,
        "mtime": mtime.isoformat(),
        "age_seconds": age_seconds,
        "max_age_hours": BACKUP_MAX_AGE_HOURS,
    }
    if not fresh:
        out["reason"] = (
            f"latest snapshot age {age_seconds:.0f}s exceeds {BACKUP_MAX_AGE_HOURS}h"
        )
    return out


def offsite_due(
    *, last_ok: datetime | None, now: datetime, period_days: int = 7
) -> bool:
    if last_ok is None:
        return True
    return (now - last_ok).total_seconds() >= period_days * 86400


def drill_due(*, last_ok: datetime | None, now: datetime, period_days: int = 8) -> bool:
    if last_ok is None:
        return True
    return (now - last_ok).total_seconds() >= period_days * 86400


def _latest_ops_event(kind: str) -> dict | None:
    from khipu.db import connect

    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT status, detail, created_at, now() - created_at
                FROM ops_events
                WHERE kind = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (kind,),
            )
            row = cur.fetchone()
    except Exception:
        return None
    if not row:
        return None
    status, detail, created_at, age = row
    return {
        "status": status,
        "detail": detail,
        "created_at": created_at,
        "age_seconds": age.total_seconds() if age is not None else None,
    }


def _last_ok_time(kind: str) -> datetime | None:
    ev = _latest_ops_event(kind)
    if not ev or ev.get("status") != "ok" or ev.get("created_at") is None:
        return None
    created = ev["created_at"]
    if created.tzinfo is None:
        return created.replace(tzinfo=timezone.utc)
    return created


def resolve_rclone(rclone_bin: str = "rclone") -> str | None:
    """A launchd job's PATH has no /opt/homebrew/bin, so a bare "rclone"
    raised OSError there and read as "remote not configured" — every nightly
    offsite attempt 08-27..08-31 failed that way while the same call worked
    from a login shell. Resolve to an absolute path before running."""
    import shutil

    if "/" in rclone_bin:
        return rclone_bin if Path(rclone_bin).is_file() else None
    found = shutil.which(rclone_bin)
    if found:
        return found
    for cand in ("/opt/homebrew/bin/rclone", "/usr/local/bin/rclone"):
        if Path(cand).is_file():
            return cand
    return None


def has_r2_remote(*, rclone_bin: str = "rclone") -> bool:
    resolved = resolve_rclone(rclone_bin)
    if resolved is None:
        return False
    rclone_bin = resolved
    try:
        r = subprocess.run(
            [rclone_bin, "listremotes"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if r.returncode != 0:
        return False
    for line in (r.stdout or "").splitlines():
        remote = line.strip()
        if remote == "r2:" or remote.startswith("r2:"):
            return True
    return False


def _integrity_check(path: Path, *, timeout: int = INTEGRITY_TIMEOUT_SECONDS) -> str:
    script = (
        "import sqlite3, sys; "
        f"c=sqlite3.connect({str(path)!r}); "
        "print(c.execute('PRAGMA integrity_check').fetchone()[0])"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"timeout after {timeout}s"
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip()
        return f"error: {tail or r.returncode}"
    return (r.stdout or "").strip() or "unknown"


def _embeddings_table(con: sqlite3.Connection) -> str | None:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%embedding%' "
        "ORDER BY name LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def _count_embeddings(path: Path) -> int:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        table = _embeddings_table(con)
        if not table:
            return 0
        return int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        con.close()


def _detach(argv: list[str]) -> None:
    subprocess.Popen(
        argv,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=os.environ.copy(),
    )


def record_local(*, snapshot: Path | None = None) -> dict:
    snap = Path(snapshot) if snapshot else latest_snapshot()
    if snap is None or not snap.is_file():
        return {"ok": False, "reason": "no snapshot found"}

    integrity = _integrity_check(snap)
    ok = integrity.lower() == "ok"
    try:
        nbytes = snap.stat().st_size
    except OSError as exc:
        return {"ok": False, "reason": f"stat failed: {exc}"}

    try:
        embeddings = _count_embeddings(snap)
    except sqlite3.Error as exc:
        embeddings = -1
        ok = False
        integrity = f"embeddings probe failed: {exc}"

    detail = {
        "path": str(snap),
        "bytes": nbytes,
        "embeddings": embeddings,
        "integrity": integrity,
    }
    from khipu import ops_events

    try:
        ev = ops_events.record("graph_snapshot", "ok" if ok else "fail", detail)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"ops_events insert failed: {exc}", **detail}

    out = {"ok": ok, "event": ev, **detail}
    if ok and _resolve_producer(None):
        now = _utcnow()
        if drill_due(last_ok=_last_ok_time("graph_restore_drill"), now=now):
            _detach([sys.executable, "-m", "khipu.cli", "graph-backup", "drill"])
            out["drill_spawned"] = True
    return out


def run_offsite(
    *,
    dest_remote: str = DEST_REMOTE,
    keep: int = 3,
    rclone_bin: str = "rclone",
) -> dict:
    resolved = resolve_rclone(rclone_bin)
    if resolved is None:
        return {
            "ok": False,
            "reason": (
                f"rclone binary not found (looked for {rclone_bin!r}, PATH and "
                "Homebrew locations) — install rclone or pass rclone_bin"
            ),
        }
    rclone_bin = resolved
    if not has_r2_remote(rclone_bin=rclone_bin):
        return {
            "ok": False,
            "reason": (
                "rclone remote r2: not configured on this Mac — "
                f"offsite dest remains {dest_remote}; supply an r2 remote to enable copy"
            ),
        }

    snap = latest_snapshot()
    if snap is None:
        return {"ok": False, "reason": "no snapshot found"}

    live = _live_graph()
    if live is None:
        return {"ok": False, "reason": "graph_sqlite is not configured"}
    live = live.resolve()
    snap_resolved = snap.resolve()
    if snap_resolved == live:
        return {"ok": False, "reason": "refusing to upload live graph.sqlite"}

    dest = f"{dest_remote.rstrip('/')}/{snap.name}"
    argv = [rclone_bin, "copyto", str(snap), dest]
    if str(live) in argv:
        return {"ok": False, "reason": "live graph path must not appear in rclone argv"}

    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        detail = {"path": str(snap), "dest": dest, "error": str(exc)}
        try:
            from khipu import ops_events

            ops_events.record("graph_snapshot_offsite", "fail", detail)
        except Exception as rec_exc:  # noqa: BLE001
            detail["ops_error"] = str(rec_exc)
        return {"ok": False, "reason": str(exc), **detail}

    if r.returncode != 0:
        detail = {
            "path": str(snap),
            "dest": dest,
            "stderr": (r.stderr or "")[-500:],
            "returncode": r.returncode,
        }
        try:
            from khipu import ops_events

            ops_events.record("graph_snapshot_offsite", "fail", detail)
        except Exception as rec_exc:  # noqa: BLE001
            detail["ops_error"] = str(rec_exc)
        return {"ok": False, **detail}

    pruned: list[str] = []
    try:
        pruned = _prune_remote_snapshots(dest_remote, keep=keep, rclone_bin=rclone_bin)
    except Exception as exc:  # noqa: BLE001 — copy succeeded; prune is best-effort
        detail = {
            "path": str(snap),
            "dest": dest,
            "prune_error": str(exc),
            "pruned": pruned,
        }
        from khipu import ops_events

        ev = ops_events.record("graph_snapshot_offsite", "ok", detail)
        return {
            "ok": True,
            "event": ev,
            "pruned": pruned,
            "prune_error": str(exc),
            **detail,
        }

    detail = {"path": str(snap), "dest": dest, "pruned": pruned}
    from khipu import ops_events

    ev = ops_events.record("graph_snapshot_offsite", "ok", detail)
    return {"ok": True, "event": ev, **detail}


def _prune_remote_snapshots(
    dest_remote: str, *, keep: int, rclone_bin: str
) -> list[str]:
    prefix = f"{dest_remote.rstrip('/')}/"
    r = subprocess.run(
        [rclone_bin, "lsf", prefix],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        raise RuntimeError(
            (r.stderr or r.stdout or "").strip() or f"lsf rc={r.returncode}"
        )

    names = sorted(
        [
            line.strip()
            for line in (r.stdout or "").splitlines()
            if line.strip().endswith(".sqlite")
        ],
        reverse=True,
    )
    deleted: list[str] = []
    for name in names[keep:]:
        target = f"{prefix}{name}"
        live = _live_graph()
        if live is not None and str(live.resolve()) in target:
            continue
        dr = subprocess.run(
            [rclone_bin, "deletefile", target],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if dr.returncode == 0:
            deleted.append(name)
    return deleted


def offsite_health(
    *,
    now: datetime | None = None,
    producer: bool | None = None,
) -> dict:
    if not _resolve_producer(producer):
        return {
            "ok": True,
            "skipped": True,
            "reason": "not the graph producer",
            "dest_remote": DEST_REMOTE,
        }

    if not has_r2_remote():
        return {
            "ok": False,
            "skipped": False,
            "reason": (
                "rclone remote r2: not configured on this Mac — "
                f"offsite dest remains {DEST_REMOTE}; supply an r2 remote to enable copy"
            ),
            "dest_remote": DEST_REMOTE,
        }

    now = now or _utcnow()
    ev = _latest_ops_event("graph_snapshot_offsite")
    if not ev or ev.get("status") != "ok":
        reason = "no successful graph_snapshot_offsite event recorded yet"
        if ev and ev.get("status") != "ok":
            reason = f"last graph_snapshot_offsite status={ev.get('status')}"
        return {
            "ok": False,
            "skipped": False,
            "reason": reason,
            "dest_remote": DEST_REMOTE,
            "latest": ev,
            "max_age_days": OFFSITE_MAX_AGE_DAYS,
        }

    age = ev.get("age_seconds")
    if age is None:
        return {
            "ok": False,
            "skipped": False,
            "reason": "graph_snapshot_offsite event has no age",
            "dest_remote": DEST_REMOTE,
            "latest": ev,
            "max_age_days": OFFSITE_MAX_AGE_DAYS,
        }

    fresh = age <= OFFSITE_MAX_AGE_DAYS * 86400
    out = {
        "ok": fresh,
        "skipped": False,
        "dest_remote": DEST_REMOTE,
        "latest": {
            "status": ev.get("status"),
            "created_at": ev["created_at"].isoformat()
            if ev.get("created_at")
            else None,
            "age_seconds": age,
        },
        "max_age_days": OFFSITE_MAX_AGE_DAYS,
    }
    if not fresh:
        out["reason"] = (
            f"last offsite copy age {age:.0f}s exceeds {OFFSITE_MAX_AGE_DAYS}d"
        )
    return out


def scratch_drill(
    *, snapshot: Path | None = None, dest_dir: Path | None = None
) -> dict:
    snap = Path(snapshot) if snapshot else latest_snapshot()
    if snap is None or not snap.is_file():
        return {"ok": False, "reason": "no snapshot found"}

    live = _live_graph()
    if live is None:
        return {"ok": False, "reason": "graph_sqlite is not configured"}
    live = live.resolve()
    if snap.resolve() == live:
        return {"ok": False, "reason": "refusing to drill from live graph.sqlite"}

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    temp = (dest_dir or Path("/tmp")) / f"khipu-graph-drill-{ts}.sqlite"
    try:
        shutil.copy2(snap, temp)
    except OSError as exc:
        return {"ok": False, "reason": f"copy failed: {exc}"}

    integrity = _integrity_check(temp)
    ok = integrity.lower() == "ok"
    nodes = 0
    embeddings = 0
    emb_table: str | None = None
    try:
        con = sqlite3.connect(f"file:{temp}?mode=ro", uri=True)
        try:
            nodes = int(con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
            emb_table = _embeddings_table(con)
            if emb_table:
                embeddings = int(
                    con.execute(f"SELECT COUNT(*) FROM {emb_table}").fetchone()[0]
                )
        finally:
            con.close()
    except sqlite3.Error as exc:
        ok = False
        integrity = f"query failed: {exc}"

    detail = {
        "source": str(snap),
        "temp": str(temp),
        "integrity": integrity,
        "nodes": nodes,
        "embeddings": embeddings,
        "embeddings_table": emb_table,
    }
    from khipu import ops_events

    try:
        ev = ops_events.record("graph_restore_drill", "ok" if ok else "fail", detail)
    except Exception as exc:  # noqa: BLE001
        detail["ops_error"] = str(exc)
        ev = None
        ok = False

    try:
        temp.unlink(missing_ok=True)
    except OSError:
        pass

    out = {"ok": ok, "event": ev, **detail}
    if not ok and integrity.lower() != "ok":
        out.setdefault("reason", integrity)
    return out


def status_payload() -> dict:
    local = local_health()
    offsite = offsite_health()
    live = _live_graph()
    return {
        "local": local,
        "offsite": offsite,
        "latest_snapshot": str(latest_snapshot()) if latest_snapshot() else None,
        "dest_remote": DEST_REMOTE,
        "live_graph": str(live) if live is not None else None,
        "has_r2_remote": has_r2_remote(),
        "last_graph_snapshot": _latest_ops_event("graph_snapshot"),
        "last_graph_snapshot_offsite": _latest_ops_event("graph_snapshot_offsite"),
        "last_graph_restore_drill": _latest_ops_event("graph_restore_drill"),
    }
