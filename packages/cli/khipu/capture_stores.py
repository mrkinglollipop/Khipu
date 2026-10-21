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

Backup import is an allow-list read from here. A store whose ``restore`` is
set may be restored from a backup: only files matching its patterns, and
each through the scrub it names (``RESTORE_SCRUBS``, implemented in
khipu/paths.py). ``OTHER_DATA`` lists the data-folder files that hold no
capture text but are worth restoring (settings, the forget registry, waiting
ledger writes). Any other file in a backup (a temp, a sidecar, a lock, a
writer record, a name nobody claims) is left out and listed in the import
report.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path

# The scans forget runs, by the name a store gives.
FORGET_SCANS = ("native jobs", "outbox", "legacy logs", "replica", "replica temps",
                "legacy writers", "recall golden", "work view", "owner words")


# The scrubs a restored file may go through, by the name an entry gives.
RESTORE_SCRUBS = ("queued job", "replica staging", "content addressed", "registry merge",
                  "recall golden", "no capture text")


@dataclass(frozen=True)
class Restore:
    """Which files of a backup a store owns, as data-folder-relative paths
    with ``*`` wildcards per part, and the scrub each goes through."""
    patterns: tuple[str, ...]
    scrub: str

    def claims(self, parts: tuple[str, ...]) -> bool:
        # A hidden part (a temp, a lock, a record folder) is never claimed,
        # whatever a wildcard would match.
        if any(part.startswith(".") for part in parts):
            return False
        for pattern in self.patterns:
            want = tuple(pattern.split("/"))
            if len(want) == len(parts) and all(map(fnmatchcase, parts, want)):
                return True
        return False


@dataclass(frozen=True)
class Store:
    name: str
    where: str
    holds: str
    forget: str
    writers: frozenset[str]
    restore: Restore | None = None

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
             "outbox.enqueue", "outbox.retry_dead"),
          Restore(("outbox/*.json", "outbox/dead/*.json", "outbox/quarantine/*.json"),
                  "queued job")),
    Store("legacy log", "memory root: episodes*.jsonl, their .bak copies, .khipu-forget-backups/, "
          "and .tmp files beside them",
          "one capture per line, summary text", "legacy logs",
          _w("forget._rewrite_log", "forget._scrub_copy")),
    Store("replica", "data folder hub_snapshot.sqlite",
          "episode rows, vectors, decisions, deliverables, topics and commitments", "replica",
          _w("hub_snapshot._refresh_locked", "hub_snapshot._upsert_episode_unfenced",
             "hub_snapshot.upsert_embeddings", "hub_snapshot.apply_decision_changes",
             "hub_snapshot.sync_decision_changes", "hub_snapshot.ledger_write_snapshot",
             "hub_snapshot.forget_episode_in_snapshot", "hub_snapshot.scrub_forgotten_snapshot",
             "hub_snapshot.publish_imported_snapshot", "hub_snapshot.purge_topic_in_snapshot",
             "hub_snapshot.ensure_edge_indexes", "hub_snapshot._write_pulled_tables",
             "hub_snapshot.refresh_ledger"),
          Restore(("hub_snapshot.sqlite",), "replica staging")),
    Store("replica temp", "data folder .hub_snapshot.<random>.sqlite and its journal and WAL",
          "a refresh dump or a staged backup replica", "replica temps",
          _w("hub_snapshot.staging_path")),
    Store("legacy writer", "data folder .legacy_writers/<pgid>.json",
          "the process group of a legacy writer that may still append", "legacy writers",
          _w("writer_groups.record")),
    Store("recall golden set", "data folder recall-golden.jsonl, or the file KHIPU_RECALL_GOLDEN names, "
          "and .recall-golden.*.tmp rewrites beside it",
          "recall test queries, which usually quote a captured episode's private text, each "
          "with the episode ids it expects", "recall golden",
          _w("forget._scrub_golden_file"),
          Restore(("recall-golden.jsonl",), "recall golden")),
    Store("work view", "data folder state/work/view.json and .view.json.*.tmp rewrites beside it; "
          "the same writer saves state/work/last/*.json, links.json and hidden.json there, which "
          "hold job summaries, ids and hashes only",
          "the Work view's read model, which lists open commitments' text", "work view",
          _w("work._replace_json")),
    Store("recall eval record", "a file the owner names with khipu recall eval --record",
          "the golden queries, quoted text included, and the ids each one returned",
          "kept: an owner-chosen file outside the data folder that forget cannot find; a known "
          "boundary in the scope doc",
          _w("recall_eval.write_record")),
    Store("session state", "capture home state/<harness>--<session>.json and .capture-now.json",
          "offsets and hashes; carried text and a capture note that no queued job holds yet",
          "none: carried text and a note belong to the next window, which is not a source until "
          "it is queued, and a forgotten source is refused when it is",
          _w("session_capture.save_state", "session_capture.request_capture_now",
             "session_capture.sweep_idle", "session_capture._write_at")),
    Store("capture log", "capture home logs/session-capture.log and its one older copy .log.1",
          "drain and hook events: file names, counts and lengths",
          "none: it never holds summary or transcript text",
          _w("session_capture._log")),
    Store("owner turns", "capture home owner-turns/: <session>.jsonl, briefs.jsonl, settings.json and "
          "their .tmp rewrites (owner_files.PATTERNS)",
          "the typed text of each prompt and the briefs of dispatched jobs, for the owner-direction check",
          "owner words",
          _w("owner_turns._record", "owner_turns._write_private")),
    Store("owner actions", "data folder owner-actions.jsonl, its head owner-actions.head.json and lock, "
          "owner-direction-pending.json and their .tmp rewrites (owner_files.PATTERNS)",
          "each owner action with the owner's quoted words, and widenings waiting for a second turn",
          "owner words",
          _w("owner_direction._append", "owner_direction._log_locked", "owner_direction._write_private")),
    Store("routing delivery words", "capture home state/recall--*.routing.json and their rewrite temps",
          "cached routing rules, proposals and notices, including owner words",
          "kept: routing words have no episode provenance; they are the owner's rules, notes, holds "
          "and conversations' proposals, removed through the routing commands that manage them "
          "(khipu route rule retire, khipu route note end, khipu route hold end, "
          "khipu route priority clear) or in Settings → Routing, not by forgetting an episode; "
          "forget lists each copy as kept and an unreadable routing folder keeps the forget open",
          _w("routing_delivery._transaction")),
    Store("routing replication words", "data folder state/routing/remote.jsonl and .replication-* rewrites",
          "replicated routing rule, note and proposal text, including owner words; the same writer "
          "also saves replication cursors and the Mac id",
          "kept: routing words have no episode provenance; they are the owner's rules, notes, holds "
          "and conversations' proposals, removed through the routing commands that manage them "
          "(khipu route rule retire, khipu route note end, khipu route hold end, "
          "khipu route priority clear) or in Settings → Routing, not by forgetting an episode; "
          "forget lists each copy as kept and an unreadable routing folder keeps the forget open",
          _w("routing_replication._atomic")),
    Store("routing scan report", "data folder state/routing/scan-report.json and its .json.tmp rewrite",
          "routing copies found in files: file paths shown from home, line numbers and clipped "
          "quoted owner words",
          "kept: routing copies have no episode provenance; they are the scanner's report of "
          "words in the owner's own files, replaced on the next scan and removed by deleting "
          "state/routing/scan-report.json, not by forgetting an episode",
          _w("routing_scan_report._write_report")),
    Store("conversation media", "data folder media/conversation_memory/<sha256>.<ext>",
          "images found in transcripts, shared by content hash",
          "kept: images are content-addressed and can belong to several episodes; a known "
          "boundary in the scope doc",
          _w("session_capture.land_transcript_images"),
          Restore(("media/conversation_memory/*",), "content addressed")),
    Store("extraction scratch", "data folder state/synth-cli/work-*/ and its output temp",
          "a model call's working folder and output, removed when the call returns",
          "kept: removed by the call that made it; one a killed call left behind is a known "
          "boundary in the scope doc",
          _w("extract._generate_claude_cli", "extract._generate_codex_cli")),
    Store("commitments backup", "the hygiene backup folder commitments-<time>/commitments.bin",
          "a binary copy of the commitments table, whose text a capture opened",
          "kept: closed commitments remain by design (forget's kept text)",
          _w("hygiene.backup_commitments")),
    Store("owed tidy plan", "the file named with khipu owed tidy --plan-out; the app keeps it at "
          "data folder state/owed-tidy/plan-<project>-<hash>.json",
          "a clean-up plan: open commitments' text, owners and the evidence for each change",
          "kept: commitments' text remains by design (forget's kept text); each plan is rewritten "
          "on the next Tidy this list",
          _w("cli.cmd_owed_tidy")),
    Store("local backup", "a backup folder or archive the owner chose",
          "a copy of the data folder", "kept: backups are kept for recovery (forget's kept text); "
          "an import restores only what a store here says it may, each file through that "
          "store's scrub, which checks every identity the registry holds",
          _w("paths.backup_local", "paths._import_local", "paths._write_staged", "paths.copy_sensitive",
             "paths.write_sensitive_bytes", "paths.sensitive_writer")),
)


@dataclass(frozen=True)
class DataFiles:
    """Data-folder files that hold no capture text and that a backup may
    restore."""
    name: str
    holds: str
    restore: Restore


OTHER_DATA: tuple[DataFiles, ...] = (
    DataFiles("settings", "settings, credentials and source lists",
              Restore(("config.json", "dsn", "root.crt", "gateway-token", "graph_sources.json",
                       "graph_sources.resolved.json", "migrate_lock_notes.json"),
                      "no capture text")),
    DataFiles("forget registry", "identities and ids of forgotten sources, never their text",
              Restore(("forgotten_sources.json",), "registry merge")),
    DataFiles("ledger outbox", "ledger writes waiting for the hub, the owner's work rows",
              Restore(("ledger-outbox/*.json",), "no capture text")),
    DataFiles("owner writes", "owner writes waiting for the hub: closing an owed item, superseding a decision",
              Restore(("owner-writes/*.json",), "no capture text")),
)


@dataclass(frozen=True)
class Restorable:
    name: str
    patterns: tuple[str, ...]
    scrub: str
    restore: Restore


def restorable() -> list[Restorable]:
    """Every entry a backup may restore, capture stores first."""
    entries = [(s.name, s.restore) for s in STORES if s.restore is not None]
    entries += [(d.name, d.restore) for d in OTHER_DATA]
    return [Restorable(name, r.patterns, r.scrub, r) for name, r in entries]


def restore_entry(parts: tuple[str, ...]) -> Restorable | None:
    """The entry that claims a backed-up file, or None when none does."""
    return next((e for e in restorable() if e.restore.claims(parts)), None)


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
