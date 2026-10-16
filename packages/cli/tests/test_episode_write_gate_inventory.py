"""Every code path that writes an episode or its source is in the inventory.

A completed forget stays durable only while every writer checks the one
forgotten-source gate in khipu/tombstones.py. Five review rounds each found a
writer nobody had listed, so this test lists them: a new write path fails it
until it is routed through the gate and added to the inventory in
docs/plans/2026-10-11-capture-identity.md ("Durable forget: the write gate").

Two more rounds showed the gate alone does not order a writer against a whole
forget, so every local writer entry point also holds the per-Mac memory lock
(khipu/memory_lock.py, shared for a write, exclusive for a forget). LOCKED
below lists those entry points, and the tests check each one names the lock
(or reaches it through the gate) and that every gated writer is reached only
from a locked one ("Durable forget: one memory lock").
"""
from __future__ import annotations

import ast
import functools
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "khipu"
MIGRATIONS = Path(__file__).resolve().parents[3] / "ops" / "migrations"

SQL_WRITES = [re.compile(p, re.IGNORECASE) for p in (
    r"INSERT\s+(OR\s+\w+\s+)?INTO\s+episodes\b",
    r"INSERT\s+INTO\s+episode_sources\b",
    r"UPDATE\s+episodes\b",
    r"UPDATE\s+episode_sources\b",
    r"REPLACE\s+INTO\s+episodes\b",
)]
WRITER_CALLS = {"_upsert_episode", "_merge_into_episode", "record_source", "upsert_episode",
                "_capture_v2", "_insert_source"}
# Queued capture jobs are payloads too: every job file write in the outbox.
OUTBOX_JOB_WRITES = {"_atomic_write", "_bury", "_quarantine"}
GATE_NAMES = ("write_gate", "write_guard", "hub_source_forgotten", "publish_hub_forget",
              "Gate", "is_forgotten")

# (module, function) -> how it is gated:
#   gate              its body checks the gate itself
#   calls:mod.func    its only write is a call of that gated function
#   inside:mod.f,...  called only from those functions, each gated
#   erase             forget's own tombstoning
#   metadata          columns that carry no source, on rows that already exist
ALLOWED = {
    ("capture", "_claim_unowned_episode"): "metadata",
    ("capture", "_merge_into_episode"): "gate",
    ("capture", "dedup_before_insert"): "calls:capture._merge_into_episode",
    ("capture", "run_capture_v2"): "gate",
    ("capture", "write_pg"): "gate",
    ("capture_identity", "_insert_source"): "inside:capture_identity.record_source",
    ("capture_identity", "record_source"):
        "inside:mirror._upsert_episode,capture._merge_into_episode",
    ("cli", "_cmd_episode_edit"): "metadata",
    ("embed", "embed_on_capture"): "calls:hub_snapshot.upsert_episode",
    ("forget", "forget_episode"): "erase",
    ("hub_snapshot", "_apply_pending_erasures"): "erase",
    ("hub_snapshot", "_insert_episodes"): "inside:hub_snapshot.refresh",
    ("hub_snapshot", "_upsert_episode_unfenced"): "inside:hub_snapshot.upsert_episode",
    ("hub_snapshot", "forget_episode_in_snapshot"): "erase",
    ("hygiene", "apply_backfill_identity"): "metadata",
    ("hygiene", "apply_backfill_project"): "metadata",
    ("mirror", "_upsert_episode"): "gate",
    ("mirror", "mirror_episode"): "calls:mirror._upsert_episode",
    ("mirror", "reconcile_memory_root"): "calls:mirror._upsert_episode",
    ("mirror", "sync_recent_episodes"): "calls:mirror._upsert_episode",
    ("outbox", "_bury"): "inside:outbox.drain",
    ("outbox", "_quarantine"): "inside:outbox.drain,outbox.retry_dead",
    ("outbox", "drain"): "gate",
    ("outbox", "enqueue"): "gate",
    ("outbox", "retry_dead"): "gate",
}

# Every local writer entry point, by module.function, and what it must name.
# "shared" and "exclusive" are memory_lock.shared / memory_lock.exclusive.
# A writer may instead reach the lock through the gate (write_gate takes the
# shared lock first) or write_guard (which does the same). Forget is the one
# exclusive holder.
LOCKED = {
    "capture.capture": "shared",
    "capture.write_pg": "shared",
    "forget.forget_everywhere": "exclusive",
    "hub_snapshot.publish_imported_snapshot": "shared",
    "hub_snapshot.refresh": "shared",
    "hub_snapshot.scrub_forgotten_snapshot": "shared",
    "hub_snapshot.upsert_episode": "shared",
    "join.import_kit": "shared",
    "mirror.mirror_episode": "shared",
    "mirror.reconcile_memory_root": "shared",
    "mirror.sync_recent_episodes": "shared",
    "outbox.drain": "shared",
    "outbox.enqueue": "shared",
    "outbox.retry_dead": "shared",
    "paths.import_local": "shared",
    "session_capture._archive_empty": "shared",
    "session_capture._claim": "shared",
    "session_capture._discard_claim": "shared",
    "session_capture._release": "shared",
    "session_capture._save_claimed_job": "shared",
}
# Exempt on purpose: session_capture.enqueue runs inside the Aegis sandbox,
# which cannot write the data folder (no lock file), and it queues a
# transcript, not an episode. Forget's native-job scan shares its claim lock.
LOCK_VIA_GATE = ("write_gate", "write_guard")

# Migrations that write episodes or their sources. 0030 adds only triggers
# and its own table, and is listed so a later edit that writes rows is seen.
ALLOWED_MIGRATIONS = {"0027_episode_sources.sql", "0029_episode_origin.sql",
                      "0030_forgotten_sources.sql"}

HOW_TO_FIX = ("route it through khipu/tombstones.py's gate (write_gate, write_guard or "
              "hub_source_forgotten) and add it to ALLOWED here and to the inventory in "
              "docs/plans/2026-10-11-capture-identity.md")


class _Module:
    def __init__(self, path: Path):
        self.name = path.relative_to(PACKAGE).with_suffix("").as_posix().replace("/", ".")
        self.source = path.read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)
        self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = [
            n for n in ast.walk(self.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

    def enclosing(self, line: int) -> str:
        best = None
        for fn in self.functions:
            if fn.lineno <= line <= (fn.end_lineno or fn.lineno) and (
                    best is None or fn.lineno >= best.lineno):
                best = fn
        return best.name if best else "<module>"

    def body(self, name: str) -> str:
        fn = next(f for f in self.functions if f.name == name)
        return ast.get_source_segment(self.source, fn) or ""


@functools.cache
def _modules() -> dict[str, _Module]:
    return {m.name: m for m in (_Module(p) for p in sorted(PACKAGE.rglob("*.py")))}


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    return f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None


def _writes(mod: _Module) -> set[tuple[str, str]]:
    docstrings = {id(n.value) for n in ast.walk(mod.tree)
                  if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    found = set()
    for node in ast.walk(mod.tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings and any(p.search(node.value) for p in SQL_WRITES)):
            found.add((mod.name, mod.enclosing(node.lineno)))
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in WRITER_CALLS or (mod.name == "outbox" and name in OUTBOX_JOB_WRITES):
                found.add((mod.name, mod.enclosing(node.lineno)))
    return found


def _gated(modules: dict[str, _Module], qualified: str) -> bool:
    mod, func = qualified.rsplit(".", 1)
    return any(name in modules[mod].body(func) for name in GATE_NAMES)


def _call_sites(modules: dict[str, _Module], func: str) -> set[str]:
    return {f"{m.name}.{m.enclosing(n.lineno)}" for m in modules.values() for n in ast.walk(m.tree)
            if isinstance(n, ast.Call) and _call_name(n) == func}


def test_every_episode_write_path_is_in_the_inventory():
    modules = _modules()
    found = set().union(*(_writes(m) for m in modules.values()))
    unlisted = sorted(found - set(ALLOWED))
    assert not unlisted, f"unlisted episode write path(s) {unlisted}: {HOW_TO_FIX}"
    stale = sorted(set(ALLOWED) - found)
    assert not stale, f"inventory lists {stale}, which no longer write; remove them here and in the doc"


def test_every_inventory_entry_is_gated_the_way_it_says():
    modules = _modules()
    for (mod, func), how in sorted(ALLOWED.items()):
        where = f"{mod}.{func}"
        if how == "gate":
            assert _gated(modules, where), f"{where} is listed as gated but never checks the gate: {HOW_TO_FIX}"
        elif how.startswith("calls:"):
            target = how.split(":", 1)[1]
            assert _gated(modules, target), \
                f"{where} writes through {target}, which is not a gated writer"
        elif how.startswith("inside:"):
            callers = set(how.split(":", 1)[1].split(","))
            sites = _call_sites(modules, func)
            assert sites and sites <= callers, \
                f"{where} is called from {sorted(sites - callers)} outside its gated callers: {HOW_TO_FIX}"
            for caller in callers:
                tag = ALLOWED.get(tuple(caller.rsplit(".", 1)), "")
                assert _gated(modules, caller) or tag.startswith("inside:"), \
                    f"{where} runs inside {caller}, which never checks the gate"
        else:
            assert how in {"erase", "metadata"}, f"{where}: unknown tag {how!r}"


def test_episode_writing_migrations_are_pinned():
    pattern = re.compile(r"INSERT\s+INTO\s+(episodes|episode_sources)\b|UPDATE\s+episodes\b",
                         re.IGNORECASE)
    writing = {p.name for p in MIGRATIONS.glob("*.sql") if pattern.search(p.read_text(encoding="utf-8"))}
    unlisted = sorted(writing - ALLOWED_MIGRATIONS)
    assert not unlisted, (f"migration(s) {unlisted} write episodes or their sources; check them "
                          "against forgotten_sources (0030) and add them to ALLOWED_MIGRATIONS "
                          "and the inventory in docs/plans/2026-10-11-capture-identity.md")
    assert (MIGRATIONS / "0030_forgotten_sources.sql").is_file()


def test_every_local_writer_entry_point_holds_the_memory_lock():
    modules = _modules()
    for qualified, mode in sorted(LOCKED.items()):
        mod, func = qualified.rsplit(".", 1)
        body = modules[mod].body(func)
        if mode == "exclusive":
            assert "memory_lock.exclusive" in body, \
                f"{qualified} is a forget and must hold memory_lock.exclusive for its whole run"
            continue
        assert "memory_lock.shared" in body or any(g in body for g in LOCK_VIA_GATE), \
            f"{qualified} is a local writer and must hold memory_lock.shared (or go through " \
            "write_gate / write_guard, which take it); otherwise a forget can interleave with it"


def test_every_gated_writer_is_reached_only_from_a_locked_entry_point():
    modules = _modules()

    def covered(qualified: str, seen: frozenset = frozenset()) -> bool:
        if qualified in LOCKED:
            return True
        if qualified in seen:
            return False
        func = qualified.rsplit(".", 1)[1]
        sites = _call_sites(modules, func)
        return bool(sites) and all(covered(site, seen | {qualified}) for site in sites)

    for (mod, func), how in sorted(ALLOWED.items()):
        if how == "gate" or how.startswith("calls:"):
            qualified = f"{mod}.{func}"
            # A "calls:" entry's only write is that call, so it is also fine
            # when the function it calls is itself locked.
            via = how.split(":", 1)[1] if how.startswith("calls:") else None
            assert covered(qualified) or (via and covered(via)), (
                f"{qualified} is a gated writer that some caller reaches outside the memory "
                "lock; add that caller to LOCKED (and make it hold memory_lock.shared) or route "
                "it through a locked entry point")


def test_locked_entries_exist_and_the_lock_is_taken_before_the_gate():
    modules = _modules()
    for qualified in LOCKED:
        mod, func = qualified.rsplit(".", 1)
        assert any(f.name == func for f in modules[mod].functions), f"LOCKED lists missing {qualified}"
    gate = modules["tombstones"].body("write_gate")
    assert gate.index("memory_lock.shared()") < gate.index("_locked_registry()"), \
        "write_gate must take the memory lock before the registry lock"
