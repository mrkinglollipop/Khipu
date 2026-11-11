"""Everything a Stop hook does after its capture is queued, in a detached process.

The hook used to drain the shared queue inline, inside the harness's hook
timeout (20 s in Claude Code): on 2026-10-13, on Gemini Flash, 262 inline
drains held a turn's end for a median 10 s, 72 of them over 15 s, and a run
that reached the timeout was killed mid-summary ("Hook cancelled"). Nothing
waits on a summary, so the hook now queues, starts this module in its own
session, and returns. Summariser latency then only decides how soon a memory
is searchable.

Drainers started this way take one of ``drain_slots()`` slots; a hook that
finds none free leaves the queue to the ones running (they re-scan before
they exit) and to the scheduled drain job.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone

DRAIN_SLOTS_DEFAULT = 4
DRAIN_PASSES_MAX = 50


def drain_slots() -> int:
    """How many hook-started drainers may summarise at once (``KHIPU_DRAIN_SLOTS``)."""
    try:
        return max(1, int(os.environ.get("KHIPU_DRAIN_SLOTS") or DRAIN_SLOTS_DEFAULT))
    except ValueError:
        return DRAIN_SLOTS_DEFAULT


def _say(sid: str, harness: str, msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tag = f" harness={harness}" if harness else ""
    print(f"{stamp} [khipu-stop-hook] session={sid or '?'}{tag} {msg}", file=sys.stderr, flush=True)


@contextmanager
def _slot():
    """Hold one drain slot, or yield None when every slot is taken."""
    from khipu.session_capture import queue_dir

    held = None
    try:
        queue_dir().mkdir(parents=True, exist_ok=True)
        for i in range(drain_slots()):
            stream = (queue_dir() / f".drain-slot-{i}.lock").open("a+")
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                stream.close()
                continue
            held = stream
            break
    except OSError:
        held = None
    try:
        yield held
    finally:
        if held is not None:
            held.close()


def spawn(mem_root: str, harness: str, sid: str) -> int:
    """Start ``run`` in a new session with no pipe back to the harness; returns
    the pid. A fresh interpreter, not a fork: a forked child on macOS dies in
    the system proxy lookup the moment it opens an HTTPS connection."""
    env = {k: v for k, v in os.environ.items() if k != "KHIPU_HOOK_STDIN"}
    proc = subprocess.Popen(  # noqa: S603 — our own interpreter and module
        [sys.executable, "-m", "khipu.hook_background", mem_root or "", harness or "", sid or ""],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=None,
        start_new_session=True, close_fds=True, env=env)
    return proc.pid


def _drain(sid: str, harness: str) -> None:
    from khipu.session_capture import drain, queued_jobs

    if not queued_jobs() or os.environ.get("KHIPU_CAPTURE_NO_DRAIN") == "1":
        return
    with _slot() as held:
        if held is None:
            _say(sid, harness, f"drain left to the {drain_slots()} drainers already running")
            return
        for _ in range(DRAIN_PASSES_MAX):
            out = drain()
            _say(sid, harness, f"drain {out}")
            # Jobs queued while this pass ran are picked up by the next one;
            # a pass that captured nothing means the rest are waiting on
            # something a retry now would not change.
            if not (out.get("captured") or out.get("empty")) or not queued_jobs():
                break


def run(mem_root: str, harness: str, sid: str) -> None:
    """Drain, replay the outbox, catch up embeddings.
    Every step fails open and is logged with the session id.

    ``mem_root`` is accepted for the hook's existing argument list and no
    longer read: the files → hub tail copy of episodes.jsonl is gone (row 11;
    the hub writes the files now, through the nightly export).

    The promotion gate's own capture (``KHIPU_GATE=1``, khipu.promote) drains
    its isolated queue and stops there: the outbox replay and the embed
    catch-up both act on live state that is not the gate's."""
    try:
        _drain(sid, harness)
    except Exception as e:  # noqa: BLE001 — fail-open, always
        _say(sid, harness, f"drain skipped: {type(e).__name__}: {e}")
    if os.environ.get("KHIPU_GATE") == "1":
        _say(sid, harness, "gate run: outbox drain and embed catch-up skipped")
        return
    try:
        # Captures whose PG write failed wait in the outbox; replay them the
        # moment PG is reachable again. One listdir when empty.
        from khipu.outbox import drain as outbox_drain
        from khipu.outbox import jobs as outbox_jobs

        # Bounded like the scheduled drain (session_capture.drain): a
        # reconcile backlog must not hold the drain lock for a whole session.
        if outbox_jobs():
            _say(sid, harness, f"outbox drain {outbox_drain(limit=20, time_budget_s=60)}")
    except Exception as e:  # noqa: BLE001
        _say(sid, harness, f"outbox drain skipped: {type(e).__name__}: {e}")
    try:
        # Owner writes made while the hub was away. One listdir when empty.
        from khipu import owner_writes

        if owner_writes.waiting():
            _say(sid, harness, f"owner writes drain {owner_writes.drain()}")
    except Exception as e:  # noqa: BLE001
        _say(sid, harness, f"owner writes drain skipped: {type(e).__name__}: {e}")
    try:
        # Ledger writes made while the hub was away. One listdir when empty.
        from khipu.ledger import _outbox_jobs as ledger_jobs
        from khipu.ledger import drain as ledger_drain

        if ledger_jobs(strict=True):  # a folder that cannot be listed is reported, not skipped
            _say(sid, harness, f"ledger outbox drain {ledger_drain()}")
        from khipu.ledger import catch_up_offline_copy, replica_pending

        if replica_pending():
            _say(sid, harness, f"ledger offline copy catch-up {catch_up_offline_copy()}")
    except Exception as e:  # noqa: BLE001
        _say(sid, harness, f"ledger outbox drain skipped: {type(e).__name__}: {e}")
    try:
        # Rows the legacy mirror inserted would otherwise wait for the nightly
        # to be embedded. Bounded catch-up.
        from khipu.embed import embed_recent_missing

        r = embed_recent_missing(limit=5)
        if r["embedded"]:
            _say(sid, harness, f"embed catch-up {r}")
    except Exception as e:  # noqa: BLE001
        _say(sid, harness, f"embed catch-up skipped: {type(e).__name__}: {e}")


if __name__ == "__main__":
    _args = (sys.argv[1:] + ["", "", ""])[:3]
    run(*_args)
