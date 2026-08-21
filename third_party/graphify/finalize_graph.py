"""
Bite 3.7: Dropbox-sync-safe graph finalizer

Copies a working SQLite (typically /tmp/working.sqlite) back to the workspace
graph at /Volumes/.../UNIFICATION/state/graph.sqlite.

Why: Dropbox-mounted filesystems can revert recently-written files during sync
("conflicted copy" behavior, sync-state races, etc). A naive byte-copy from
/tmp to the workspace can be silently undone seconds later.

This script:
1. Records the source's row counts (nodes, edges, embeddings)
2. Byte-copies source → destination
3. Waits N seconds for Dropbox to settle
4. Reads destination back, verifies counts match
5. Retries up to MAX_RETRIES if verification fails

Usage:
  python finalize_graph.py                       # /tmp/working.sqlite → workspace
  python finalize_graph.py --src /tmp/foo.sqlite
  python finalize_graph.py --no-verify           # skip verify (fast, less safe)
"""
from __future__ import annotations

import argparse
import glob
import os
import sqlite3
import sys
import time
from pathlib import Path


def _find_workspace() -> Path:
    candidates = [Path("/Volumes/Cloud Storage/Claude")]
    candidates += [Path(p) for p in glob.glob("/sessions/*/mnt/Claude")]
    candidates += [Path(p) for p in glob.glob("/sessions/*/mnt/Dropbox--Claude")]
    for c in candidates:
        if c.is_dir() and (c / "skills").is_dir():
            return c
    raise RuntimeError("cannot locate workspace")


WORKSPACE = _find_workspace()
DEFAULT_SRC = Path("/tmp/working.sqlite")
DEFAULT_DST = Path("/Volumes/Cloud Storage/Graph/graph.sqlite")
MAX_RETRIES = 5
SETTLE_SECONDS = 4


def get_counts(db: Path) -> dict:
    """Return {nodes, edges, embeddings} or {error}. Stages Dropbox-mounted
    files to /tmp first (sqlite reads can fail on cloud-mounted filesystems)."""
    if not db.exists():
        return {}
    read_path = db
    if str(db).startswith(("/Volumes/", "/sessions/")):
        import tempfile, shutil
        fd, tmp_str = tempfile.mkstemp(prefix="finalize_verify_", suffix=".sqlite")
        os.close(fd)
        try:
            shutil.copyfile(str(db), tmp_str)
            read_path = Path(tmp_str)
        except Exception as ex:
            return {"error": f"copy-to-tmp: {ex}"}
    try:
        conn = sqlite3.connect(str(read_path))
        try:
            n = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            e = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            try:
                m = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
            except sqlite3.OperationalError:
                m = 0
            return {"nodes": n, "edges": e, "embeddings": m}
        finally:
            conn.close()
    except Exception as ex:
        return {"error": str(ex)}
    finally:
        if read_path != db and read_path.exists():
            read_path.unlink(missing_ok=True)


def byte_copy(src: Path, dst: Path) -> None:
    """Atomic-ish byte copy with fsync. Without fsync, Dropbox can see the file
    metadata as updated while the actual data hasn't flushed, leading to sync
    revert."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as s, open(dst, "wb") as d:
        while True:
            chunk = s.read(1 << 20)
            if not chunk:
                break
            d.write(chunk)
        d.flush()
        os.fsync(d.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=DEFAULT_SRC)
    parser.add_argument("--dst", type=Path, default=DEFAULT_DST)
    parser.add_argument("--retries", type=int, default=MAX_RETRIES)
    parser.add_argument("--settle", type=int, default=SETTLE_SECONDS,
                        help="seconds to wait for Dropbox sync between attempts")
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()

    if not args.src.exists():
        print(f"ERROR: source not found: {args.src}", file=sys.stderr)
        return 1

    src_counts = get_counts(args.src)
    if not src_counts or "error" in src_counts:
        print(f"ERROR: can't read source: {src_counts}", file=sys.stderr)
        return 1
    print(f"src: {args.src}")
    print(f"     nodes={src_counts['nodes']}  edges={src_counts['edges']}  embeddings={src_counts['embeddings']}")
    print(f"dst: {args.dst}")
    print()

    for attempt in range(1, args.retries + 1):
        print(f"attempt {attempt}/{args.retries}: byte-copy → wait {args.settle}s → verify...")
        try:
            byte_copy(args.src, args.dst)
        except Exception as e:
            print(f"  copy failed: {e}", file=sys.stderr)
            time.sleep(args.settle)
            continue

        if args.no_verify:
            print(f"  copied (no verify).")
            return 0

        time.sleep(args.settle)
        dst_counts = get_counts(args.dst)
        if not dst_counts or "error" in dst_counts:
            print(f"  verify FAILED ({dst_counts}), retrying...")
            continue
        if dst_counts == src_counts:
            print(f"  verify OK: dst matches src "
                  f"(nodes={dst_counts['nodes']} edges={dst_counts['edges']} embeddings={dst_counts['embeddings']})")
            return 0
        print(f"  MISMATCH: dst={dst_counts} vs src={src_counts}, retrying...")

    print(f"\nERROR: failed to finalize after {args.retries} attempts.", file=sys.stderr)
    print(f"       source preserved at {args.src}; copy manually if needed:", file=sys.stderr)
    print(f"       cp '{args.src}' '{args.dst}'", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
