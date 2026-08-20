"""``khipu capture`` — the one capture entrypoint every harness hook calls (P3 step 2).

Reads a capture payload (same JSON shape ``capture_v2.py`` accepts) from stdin or
``--payload-file`` and routes it by ``capture_mode``:

  legacy  → shell out to capture_v2 only. Khipu writes nothing here; capture_v2's own
            fail-open mirror still runs if KHIPU_MIRROR permits (unchanged behavior).
  dual    → write PG FIRST and DURABLY (a PG failure is a real exit code, not a
            warning — that is what makes Khipu a second *writer* rather than a mirror),
            then shell out to capture_v2 for the file wiki with KHIPU_MIRROR=0 so it
            does not mirror the same episode a second time.
  hub     → write PG only. The file wiki is maintained by the reverse-mirror
            (``khipu regen-memory`` today; full materialize is P3 end-state).

Identity is minted ONCE here (``ts``, seconds precision, ``Z``) and passed to both
writers, so the PG row and the file line share the (ts, md5(summary)) key the
reconcile upserts on — the P2b dual-mint bug can't come back through this path.

Exit codes mirror capture_v2: 0 ok · 64 no payload · 65 bad payload · 70 PG write
failed (dual/hub) · capture_v2's own code if the file leg fails.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu.config import capture_mode



def _capture_v2() -> Path | None:
    from khipu.config import path_setting

    return path_setting("capture_v2")


EX_USAGE, EX_DATAERR, EX_SOFTWARE = 64, 65, 70


def _log(msg: str) -> None:
    print(f"[khipu-capture] {msg}", file=sys.stderr, flush=True)


def _mint_ts() -> str:
    # capture.py's default format: seconds precision, trailing Z.
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_payload(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise SystemExit(EX_USAGE)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _log(f"invalid JSON: {exc}")
        raise SystemExit(EX_DATAERR)
    # Coerce before stripping: a payload whose summary is a list or a number
    # (a model extraction can produce either) used to raise AttributeError here
    # and exit with a traceback instead of the documented EX_DATAERR (audit
    # 2026-08-17).
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), str) \
            or not payload["summary"].strip():
        _log("payload missing a string 'summary'")
        raise SystemExit(EX_DATAERR)
    if not isinstance(payload.get("session_id"), str) or not payload["session_id"].strip():
        sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
        if sid:
            payload["session_id"] = sid
    if not payload.get("ts"):
        payload["ts"] = _mint_ts()
    return payload


def write_pg(payload: dict[str, Any]) -> dict[str, Any]:
    """Durable PG write: episode row + any topic pages already on disk. Raises on failure."""
    from khipu.db import connect
    from khipu.mirror import _upsert_episode, _upsert_topic, parse_topic_file

    topics_written = 0
    with connect() as conn:
        with conn.cursor() as cur:
            inserted = _upsert_episode(cur, payload)
            from khipu.config import path_setting
            from khipu.topic_graph import persist_capture_graph

            cur.execute("SAVEPOINT capture_graph")
            try:
                persist_capture_graph(cur, payload)
            except Exception as exc:  # noqa: BLE001 — episode row stays; graph is additive
                cur.execute("ROLLBACK TO SAVEPOINT capture_graph")
                _log(f"topic graph mint failed ({type(exc).__name__}: {exc})")

            memory_root = path_setting("memory_root")
            # In dual mode capture_v2 has not run yet, so topic pages named in the
            # payload may not exist on disk; upsert whatever is already there and let
            # the nightly reconcile / capture_v2's file write catch the rest.
            # No file wiki configured (hub-only install): the episode is the
            # whole capture; there are no topic files to fold in.
            for tp in (payload.get("topic_pages") or []) if memory_root else []:
                slug = (tp.get("slug") or "").strip() if isinstance(tp, dict) else ""
                if not slug:
                    continue
                path = memory_root / "topics" / f"{slug}.md"
                parsed = parse_topic_file(path)
                if parsed and _upsert_topic(
                    cur, parsed, str(path), source="capture", note="khipu capture"
                ):
                    topics_written += 1
        conn.commit()
    return {"episode_inserted": inserted, "topics_written": topics_written}


def run_capture_v2(payload: dict[str, Any], *, suppress_mirror: bool) -> int:
    capture_v2 = _capture_v2()
    if capture_v2 is None:
        _log("capture_v2 not configured (khipu config --set capture_v2 PATH); "
             "file wiki not written")
        return EX_SOFTWARE
    if not capture_v2.is_file():
        _log(f"capture_v2 not found at {capture_v2} (khipu config --set capture_v2 PATH)")
        return EX_SOFTWARE
    env = dict(os.environ)
    if suppress_mirror:
        env["KHIPU_MIRROR"] = "0"
    try:
        proc = subprocess.run(
            [sys.executable, str(capture_v2)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=env,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        # The PG row is already durable when we get here in dual/hub, and the
        # PG-failure path has already queued to the outbox — so a hung file leg
        # is a bad exit code, never an uncaught traceback out of the hook.
        _log(f"capture_v2 timed out after 300s ({capture_v2})")
        return EX_SOFTWARE
    except OSError as exc:
        _log(f"capture_v2 could not run: {type(exc).__name__}: {exc}")
        return EX_SOFTWARE
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.returncode


def capture(payload: dict[str, Any], *, mode: str | None = None) -> int:
    mode = (mode or capture_mode()).lower()
    if payload.get("scope") == "trivial":
        _log("scope=trivial — skipping per protocol")
        return 0

    if mode == "legacy":
        _log("mode=legacy → capture_v2 only")
        return run_capture_v2(payload, suppress_mirror=False)

    # dual + hub: PG first, durably. If PG is unreachable the payload goes to the
    # outbox (replayed by the next Stop hook / nightly / `khipu outbox drain`)
    # and the file line is still written, so nothing is ever lost and nothing
    # is ever double-written (the replay is the same identity upsert).
    try:
        stats = write_pg(payload)
    except Exception as exc:  # noqa: BLE001 — surfaced, queued, never swallowed
        from khipu.outbox import enqueue

        _log(f"PG write FAILED ({type(exc).__name__}: {exc}) → outbox")
        try:
            enqueue(payload, reason=f"{type(exc).__name__}")
            queued = True
        except Exception as qexc:  # noqa: BLE001 — outbox itself unwritable
            _log(f"outbox enqueue FAILED ({type(qexc).__name__}: {qexc})")
            queued = False
        # The file leg keeps its own mirror ON here: if PG comes back mid-way it
        # lands the row now, and if not it re-queues the same identity (no dup).
        rc = run_capture_v2(payload, suppress_mirror=False)
        if rc != 0:
            return rc
        return 0 if queued else EX_SOFTWARE
    _log(
        f"mode={mode} pg ok episode_inserted={stats['episode_inserted']} "
        f"topics_written={stats['topics_written']} ts={payload.get('ts')}"
    )
    # Vectors ride the capture (P3 step 3). Fail-open by design — the row is
    # already durable and `khipu embed backfill` heals any miss.
    if stats["episode_inserted"]:
        from khipu.embed import embed_on_capture

        embed_on_capture(payload)

    # dual: legacy file wiki is a peer; hub: PG is the record and the file is
    # its reverse mirror (plan: "Hub owns writes ... and can reverse-mirror to
    # files"). Same call either way — the legacy consumers (nightly consolidate,
    # topic amend, MEMORY.md) keep working in both modes. KHIPU_HUB_FILE_MIRROR=0
    # turns the reverse mirror off in hub for a future all-PG world.
    if mode == "hub" and os.environ.get("KHIPU_HUB_FILE_MIRROR", "1").strip().lower() in {"0", "false", "off"}:
        return 0
    return run_capture_v2(payload, suppress_mirror=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="khipu capture")
    ap.add_argument("--payload-file", help="Read JSON payload from a file instead of stdin")
    ap.add_argument(
        "--mode",
        choices=("legacy", "dual", "hub"),
        help="Override capture_mode for this run (default: Hub config)",
    )
    args = ap.parse_args(argv)
    raw = Path(args.payload_file).read_text(encoding="utf-8") if args.payload_file else sys.stdin.read()
    payload = load_payload(raw)
    return capture(payload, mode=args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
