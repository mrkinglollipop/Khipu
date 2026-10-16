"""Every local place capture data can be stored, in one list.

Ten review rounds of forget each found one more folder or file where a copy of
a source could sit. This module is the inventory those rounds were missing:
each store names where it is, what it holds, which code writes it and how a
forget covers it. ``forget.forget_everywhere`` runs the scan each store names,
and a content store whose scan forget does not know keeps the forget open.
``tests/test_capture_store_inventory.py`` fails when any code in the package
writes a file from a function that is neither a store's writer here nor
listed there as writing no capture data. The same list is in
docs/plans/2026-10-11-capture-identity.md ("Durable forget: one store
inventory").

``forget`` values: a scan name from ``FORGET_SCANS``, or ``kept: <why>`` for a
store forget leaves by design (named in the forget result's ``kept`` text or
the scope doc's known boundaries), or ``none: <why>`` for a store that holds
no capture source.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The scans forget runs, by the name a store gives.
FORGET_SCANS = ("native jobs", "outbox", "legacy logs", "replica", "replica temps",
                "legacy writers")


@dataclass(frozen=True)
class Store:
    name: str
    where: str
    holds: str
    forget: str
    writers: frozenset[str]

    @property
    def scanned(self) -> bool:
        return self.forget in FORGET_SCANS


def _w(*names: str) -> frozenset[str]:
    return frozenset(names)


STORES: tuple[Store, ...] = (
    Store("native capture job", "capture home queue/: *.json, *.claimed.<pid>, *.tmp, .extract-*.tmp",
          "transcript text, and the extracted episode once checkpointed", "native jobs",
          _w("session_capture.enqueue", "session_capture._claim", "session_capture._release_unlocked",
             "session_capture._save_claimed_job")),
    Store("native empty archive", "capture home empty/: *.json",
          "transcript text of a window that yielded no episode", "native jobs",
          _w("session_capture._archive_claimed")),
    Store("outbox job", "outbox/, outbox/dead/, outbox/quarantine/: *.json and .*.tmp",
          "whole capture payloads waiting for the hub", "outbox",
          _w("outbox._atomic_write", "outbox._bury", "outbox._quarantine", "outbox.drain",
             "outbox.enqueue", "outbox.retry_dead")),
    Store("legacy log", "memory root: episodes*.jsonl, their .bak copies, .khipu-forget-backups/, "
          "and .tmp files beside them",
          "one capture per line, summary text", "legacy logs",
          _w("forget._rewrite_log", "forget._scrub_copy")),
    Store("replica", "data folder hub_snapshot.sqlite",
          "episode rows, vectors, decisions and deliverables", "replica",
          _w("hub_snapshot.refresh", "hub_snapshot._upsert_episode_unfenced",
             "hub_snapshot.upsert_embeddings", "hub_snapshot.apply_decision_changes",
             "hub_snapshot.sync_decision_changes", "hub_snapshot.ledger_write_snapshot",
             "hub_snapshot.forget_episode_in_snapshot", "hub_snapshot.scrub_forgotten_snapshot",
             "hub_snapshot.publish_imported_snapshot", "hub_snapshot.purge_topic_in_snapshot",
             "hub_snapshot.ensure_edge_indexes")),
    Store("replica temp", "data folder .hub_snapshot.<random>.sqlite and its journal and WAL",
          "a refresh dump or a staged backup replica", "replica temps",
          _w("hub_snapshot.staging_path")),
    Store("legacy writer", "data folder .legacy_writers/<pgid>.json",
          "the process group of a legacy writer that may still append", "legacy writers",
          _w("writer_groups.record")),
    Store("session state", "capture home state/<harness>--<session>.json and .capture-now.json",
          "offsets and hashes; carried text and a capture note that no queued job holds yet",
          "none: carried text and a note belong to the next window, which is not a source until "
          "it is queued, and a forgotten source is refused when it is",
          _w("session_capture.save_state", "session_capture.request_capture_now",
             "session_capture.sweep_idle")),
    Store("capture log", "capture home logs/session-capture.log",
          "drain and hook events: file names, counts and lengths",
          "none: it never holds summary or transcript text",
          _w("session_capture._log")),
    Store("conversation media", "data folder media/conversation_memory/<sha256>.<ext>",
          "images found in transcripts, shared by content hash",
          "kept: images are content-addressed and can belong to several episodes; a known "
          "boundary in the scope doc",
          _w("session_capture.land_transcript_images")),
    Store("extraction scratch", "data folder state/synth-cli/work-*/ and its output temp",
          "a model call's working folder and output, removed when the call returns",
          "kept: removed by the call that made it; one a killed call left behind is a known "
          "boundary in the scope doc",
          _w("extract._generate_claude_cli", "extract._generate_codex_cli")),
    Store("commitments backup", "the hygiene backup folder commitments-<time>/commitments.bin",
          "a binary copy of the commitments table, whose text a capture opened",
          "kept: closed commitments remain by design (forget's kept text)",
          _w("hygiene.backup_commitments")),
    Store("local backup", "a backup folder or archive the owner chose",
          "a copy of the data folder", "kept: backups are kept for recovery (forget's kept text); "
          "an import scrubs them by every identity the registry holds",
          _w("paths.backup_local", "paths._import_local", "paths.stage", "paths.copy_sensitive",
             "paths.write_sensitive_bytes", "paths.sensitive_writer")),
)


def store(name: str) -> Store:
    return next(s for s in STORES if s.name == name)


def native_job_folders() -> list[tuple[str, Path]]:
    """The folders forget's native scan reads, as (place, folder)."""
    from khipu import session_capture as sc

    return [("native capture job", sc.queue_dir()), ("native empty archive", sc.empty_dir())]


def outbox_folders() -> list[Path]:
    """The folders forget's outbox scan reads."""
    from khipu.outbox import dead_dir, outbox_dir, quarantine_dir

    return [outbox_dir(), dead_dir(), quarantine_dir()]
