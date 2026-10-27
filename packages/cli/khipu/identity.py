# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Stable session identity: repo root + project, resolved from a cwd (W1.2).

``scope`` today is whatever the model wrote in the extraction prompt — a free
label like ``"build"`` or an absolute path — so two episodes from the same
conversation can carry different, unfilterable scope strings, and a worktree
checkout or a dispatched child session (cwd under a scratchpad) looks like an
unrelated project. This module resolves the one thing that IS structured and
available from the hook's own cwd, with no model and no network: which git
checkout this is, and what project it belongs to.

Called from the in-session hook (``session_capture.hook_main``), so it must
stay cheap, dependency-free, and safe to run inside Aegis's sandbox: only
``git`` as a subprocess, a bounded timeout, and no exception ever escapes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

GIT_TIMEOUT_S = 3.0

WORKTREE_MARKERS = ("/.claude/worktrees/", "/.cursor/worktrees/", "/.codex/worktrees/")

_SCRATCHPAD_RE = re.compile(r"/claude-[^/]*/.*scratchpad")


def _is_scratch_cwd(cwd: str) -> bool:
    """cwd that is never a stable project: /tmp, /private/tmp, or a Claude
    scratchpad dir (the harness's own per-session temp area, which can live
    outside /tmp on some installs — matched by name, not just prefix)."""
    p = (cwd or "").rstrip("/")
    if not p:
        return True
    if p == "/tmp" or p == "/private/tmp":
        return True
    if p.startswith("/tmp/") or p.startswith("/private/tmp/"):
        return True
    if _SCRATCHPAD_RE.search(p):
        return True
    return False


def _git(cwd: str, args: list[str], timeout: float = GIT_TIMEOUT_S) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    return out or None


# A project name is untrusted data that reaches model context (the pushed
# slice), so only these shapes are ever produced or rendered: a remote's
# ``owner/repo`` (GitHub's own rules are narrower) or a folder name.
REMOTE_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}")
FOLDER_NAME_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._ -]{0,99}")


def safe_project_name(name: Any) -> bool:
    """True when ``name`` is a remote slug or folder name of the safe shape."""
    return isinstance(name, str) and bool(REMOTE_SLUG_RE.fullmatch(name) or FOLDER_NAME_RE.fullmatch(name))


def _folder_project(name: str) -> str | None:
    """A repo folder's name as a project: anything outside the safe set
    (newlines, backticks, slashes, other punctuation) becomes ``-``."""
    out = re.sub(r"[^A-Za-z0-9._ -]", "-", name or "").strip(" -")[:100].strip(" -")
    return out if out and FOLDER_NAME_RE.fullmatch(out) else None


def _slug_from_remote_url(url: str) -> str | None:
    """``owner/repo`` from an https/ssh/scp-style git remote URL, or None for
    anything that is not exactly that safe shape (the folder name applies)."""
    url = (url or "").strip()
    if not url:
        return None
    url = re.sub(r"\.git$", "", url)
    url = url.rstrip("/")
    m = re.search(r"[:/]([^/:]+/[^/:]+)$", url)
    if not m:
        return None
    slug = m.group(1).strip("/")
    return slug if REMOTE_SLUG_RE.fullmatch(slug) else None


def _remote_slug(repo_root: Path) -> str | None:
    url = _git(str(repo_root), ["remote", "get-url", "origin"])
    if not url:
        return None
    return _slug_from_remote_url(url)


def _resolve_project_alias(cur, project: str) -> str | None:
    """K6: a ``project_aliases`` lookup for ``project`` (migration 0021) — two
    remote URLs or repo-root basenames for the SAME real project (a rename,
    an org-transfer, a second local checkout) can otherwise read as two
    different projects everywhere search/commitments/decisions group by
    project. Older schemas are supported; unexpected failures propagate.
    Callers with no live connection (every hook call — see ``resolve_repo_root``'s own contract)
    simply skip it by not passing ``cur``."""
    try:
        cur.execute("SAVEPOINT project_alias_lookup", ())
        cur.execute("SELECT project FROM project_aliases WHERE alias = %s", (project,))
        row = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT project_alias_lookup", ())
        return str(row[0]) if row and row[0] else None
    except Exception as exc:  # compatibility only for an older schema
        try:
            cur.execute("ROLLBACK TO SAVEPOINT project_alias_lookup", ())
            cur.execute("RELEASE SAVEPOINT project_alias_lookup", ())
        except Exception:  # noqa: BLE001
            pass
        from psycopg.errors import UndefinedColumn, UndefinedTable

        if not isinstance(exc, (UndefinedTable, UndefinedColumn)):
            raise
        return None


def project_canonical_group(cur, project: str | None) -> tuple[str | None, list[str]]:
    """``(canonical, group)`` for ``project`` through ``project_aliases``: the
    canonical project is the alias's target (or ``project`` itself), and the
    group is the canonical plus every alias that points at it, canonical first.
    One round trip. Missing tables/columns on older hubs yield
    ``(project, [project])``; unexpected database failures propagate."""
    if not project:
        return project, [project] if project else []
    try:
        cur.execute("SAVEPOINT project_alias_group", ())
        cur.execute(
            "WITH c AS (SELECT COALESCE((SELECT project FROM project_aliases WHERE alias = %s), %s) AS p) "
            "SELECT c.p, a.alias FROM c LEFT JOIN project_aliases a ON a.project = c.p ORDER BY a.alias",
            (project, project),
        )
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT project_alias_group", ())
        if not rows:
            return project, [project]
        canonical = str(rows[0][0])
        group = [canonical] + [str(r[1]) for r in rows if r[1] and str(r[1]) != canonical]
        return canonical, group
    except Exception as exc:  # compatibility only for an older schema
        try:
            cur.execute("ROLLBACK TO SAVEPOINT project_alias_group", ())
            cur.execute("RELEASE SAVEPOINT project_alias_group", ())
        except Exception:  # noqa: BLE001
            pass
        from psycopg.errors import UndefinedColumn, UndefinedTable

        if not isinstance(exc, (UndefinedTable, UndefinedColumn)):
            raise
        return project, [project]


class AmbiguousProject(ValueError):
    """A typed project name that fits more than one project."""


def pick_project(name: str, known: Iterable[str], canonical_of: Callable[[str], str]) -> str:
    """The stored project name a typed ``name`` means, among ``known`` names.

    An exact known name (or alias) wins as typed. Otherwise a name differing
    only by case, or a bare repo name (no ``/``) equal to the last path
    segment of a known name, resolves to it. Candidates that read as one
    project (same canonical through ``canonical_of``) count once. No match
    returns ``name`` unchanged; several different projects raise
    ``AmbiguousProject`` naming them, never a silent empty answer."""
    return pick_project_known(name, known, canonical_of)[0]


def pick_project_known(name: str, known: Iterable[str],
                       canonical_of: Callable[[str], str]) -> tuple[str, bool]:
    """``pick_project`` and whether the answer is one of the ``known``
    projects (``False``: no match, ``name`` came back unchanged)."""
    known = {k for k in known if k}
    if name in known:
        return name, True
    lowered = name.casefold()
    bare = "/" not in name
    hits = sorted(k for k in known
                  if k.casefold() == lowered or (bare and k.rsplit("/", 1)[-1].casefold() == lowered))
    canon = sorted({canonical_of(k) for k in hits})
    if not canon:
        return name, False
    if len(canon) > 1:
        raise AmbiguousProject(f"project {name!r} is ambiguous: could be {', '.join(canon)}; "
                               "use the full name")
    return canon[0], True


def visible_project_names(visible: Iterable[str], alias_rows: Iterable[tuple[Any, Any]]) -> set[str]:
    """The names a typed project may resolve to: every project a visible
    commitment is filed under, plus the aliases and canonical of each alias
    group one of those names belongs to. An alias group backed only by
    commitments of forgotten (or pending-erasure) episodes adds no name, so
    it never resolves a short name nor appears in an ambiguity error."""
    known = {str(v) for v in visible if v}
    rows = [(str(a), str(p)) for a, p in alias_rows if a and p]
    live_groups = {p for a, p in rows if a in known or p in known}
    known.update(n for a, p in rows if p in live_groups for n in (a, p))
    return known


def resolve_project_name(cur, project: str | None) -> str | None:
    """``pick_project`` over the hub: every project a visible commitment is
    filed under, plus the aliases of the groups those projects belong to."""
    return resolve_project(cur, project)[0]


def resolve_project(cur, project: str | None) -> tuple[str | None, bool]:
    """``resolve_project_name`` and whether the answer is a visible project.
    ``False`` means an unknown name -- including an exact alias of a project
    that exists only through forgotten episodes -- which a caller must not
    widen through ``project_aliases`` (that would name the hidden project)."""
    if not project:
        return project, True
    from khipu import commitments

    # Names come only from commitments whose opening episode is not forgotten,
    # the same filter the owed listing applies (computed outside the savepoint:
    # it may introspect the schema). An alias counts only for a group one of
    # those names belongs to.
    live = commitments._not_forgotten_clause(cur)
    found: list[list[tuple]] = []
    for table_sql in (f"SELECT DISTINCT project FROM commitments WHERE project IS NOT NULL AND {live}",
                      "SELECT alias, project FROM project_aliases"):
        cur.execute("SAVEPOINT project_name_known", ())
        try:
            cur.execute(table_sql, ())
            found.append(list(cur.fetchall()))
            cur.execute("RELEASE SAVEPOINT project_name_known", ())
        except Exception as exc:  # compatibility only for an older schema
            cur.execute("ROLLBACK TO SAVEPOINT project_name_known", ())
            cur.execute("RELEASE SAVEPOINT project_name_known", ())
            from psycopg.errors import UndefinedColumn, UndefinedTable

            if not isinstance(exc, (UndefinedTable, UndefinedColumn)):
                raise
            found.append([])
    known = visible_project_names((r[0] for r in found[0]), found[1])
    return pick_project_known(project, known, lambda n: project_canonical_group(cur, n)[0] or n)


def project_group(cur, project: str | None) -> list[str]:
    """Every key that reads as the same project as ``project`` (the canonical
    plus its aliases); ``[project]`` when it has none or the lookup fails."""
    return project_canonical_group(cur, project)[1]


def list_project_aliases(cur) -> list[dict[str, Any]]:
    cur.execute("SELECT alias, project, created_at FROM project_aliases ORDER BY project, alias", ())
    return [{"alias": r[0], "canonical": r[1], "created_at": r[2]} for r in cur.fetchall()]


def add_project_alias(cur, alias: str, canonical: str) -> dict[str, Any]:
    """Record ``alias`` as another key for ``canonical``. Refuses (``ok`` false,
    ``error`` says why) an alias equal to its canonical, a canonical that is
    itself an alias (no chains), and an alias that already has aliases of its
    own. Re-adding the same pair is a no-op success; an alias that already
    points elsewhere is refused rather than silently re-pointed. Joining
    ``alias`` to a group holding a name the owner keeps separate from it is
    refused (``code`` kept_separate), decided under the group lock."""
    alias, canonical = (alias or "").strip(), (canonical or "").strip()
    if not alias or not canonical:
        return {"ok": False, "error": "alias and canonical must both be non-empty"}
    if alias == canonical:
        return {"ok": False, "error": f"{alias!r} cannot be an alias of itself"}
    group_lock(cur)
    cur.execute("SELECT project FROM project_aliases WHERE alias = %s", (canonical,))
    row = cur.fetchone()
    if row:
        return {"ok": False, "error": f"{canonical!r} is itself an alias of {row[0]!r}; "
                                      f"use {row[0]!r} as the canonical (no alias chains)"}
    cur.execute("SELECT project FROM project_aliases WHERE alias = %s", (alias,))
    row = cur.fetchone()
    if row:
        if row[0] == canonical:
            return {"ok": True, "alias": alias, "canonical": canonical, "changed": False}
        return {"ok": False, "error": f"{alias!r} is already an alias of {row[0]!r}; "
                                      f"remove it first to point it at {canonical!r}"}
    cur.execute("SELECT count(*) FROM project_aliases WHERE project = %s", (alias,))
    if int(cur.fetchone()[0]):
        return {"ok": False, "error": f"{alias!r} is the canonical of other aliases; "
                                      "move or remove those first (no alias chains)"}
    pair = kept_separate_between(cur, {alias}, group_members(cur, canonical))
    if pair:
        return _kept_refusal(pair, "add this name")
    cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s)", (alias, canonical))
    return {"ok": True, "alias": alias, "canonical": canonical, "changed": True}


def remove_project_alias(cur, alias: str) -> dict[str, Any]:
    """Drop ``alias``. Episodes captured while it pointed at a canonical keep
    ``raw.typed_project`` from capture; restore those rows to the typed name
    so undo does not leave them stranded under the former target."""
    alias = (alias or "").strip()
    cur.execute("SELECT project FROM project_aliases WHERE alias = %s", (alias,))
    row = cur.fetchone()
    if not row:
        return {"ok": False, "error": f"{alias!r} is not an alias"}
    canonical = str(row[0])
    # Match every name in the group: after a second rename the row may still
    # sit under an older member, not the newest canonical.
    group = project_group(cur, canonical)
    # Only the project column moves, and a forgotten row (deleted_at set)
    # stays exactly where forget left it.
    cur.execute(
        "UPDATE episodes SET project = %s "
        "WHERE project = ANY(%s) AND raw->>'typed_project' = %s AND deleted_at IS NULL",
        (alias, group, alias),
    )
    restored = int(cur.rowcount or 0)
    cur.execute("DELETE FROM project_aliases WHERE alias = %s", (alias,))
    return {
        "ok": True,
        "alias": alias,
        "changed": True,
        "episodes_restored": restored,
        "note": "Only episodes with raw.typed_project are restored; older rows without it stay under their current project.",
    }


# Advisory lock classes (the first key of the two-key form), so a project
# name and a checkout key never share a lock by hash coincidence.
PROJECT_LOCK_CLASS = 0x4B485052  # one project group, by its canonical name
CHECKOUT_LOCK_CLASS = 0x4B48434B  # one checkout, by its key


def _xact_lock(cur, cls: int, name: str) -> None:
    cur.execute("SELECT pg_advisory_xact_lock(%s::int4, hashtext(%s))", (cls, name))


def group_lock(cur) -> None:
    """The one lock every write to ``project_aliases`` takes before it reads.
    Per-group locks had to be rediscovered as canonicals moved, which could
    take them out of order and deadlock; renames are rare, so serialising
    them all costs nothing. Transaction-level, so taking it twice is fine."""
    _xact_lock(cur, PROJECT_LOCK_CLASS, "*project-groups*")


def _canonical_of(cur, name: str) -> str:
    cur.execute("SELECT project FROM project_aliases WHERE alias = %s", (name,))
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else name


def group_members(cur, name: str) -> set[str]:
    """``name``'s whole group: its canonical and every alias of that canonical."""
    top = _canonical_of(cur, name)
    cur.execute("SELECT alias FROM project_aliases WHERE project = %s", (top,))
    return {name, top, *(str(r[0]) for r in cur.fetchall())}


# The invariant: no two names of a 'kept_separate' pair are ever in one group.
# Every write that joins two groups checks it here, under ``group_lock`` and
# against the two complete groups, never the two names it was handed alone.
KEPT_SEPARATE = "kept_separate"


def kept_separate_between(cur, left: set[str], right: set[str]) -> tuple[str, str] | None:
    """A pair the owner keeps separate with one name in ``left`` and the
    other in ``right``, else None. A hub without ``project_pairs`` has none."""
    cur.execute("SELECT to_regclass('project_pairs') IS NOT NULL", ())
    if not cur.fetchone()[0]:
        return None
    lft, rgt = sorted(left), sorted(right)
    cur.execute("SELECT a, b FROM project_pairs WHERE state = 'kept_separate' AND "
                "((a = ANY(%s) AND b = ANY(%s)) OR (a = ANY(%s) AND b = ANY(%s))) ORDER BY a, b LIMIT 1",
                (lft, rgt, rgt, lft))
    row = cur.fetchone()
    return (str(row[0]), str(row[1])) if row else None


def kept_separate_message(pair: tuple[str, str] | list[str], then: str) -> str:
    return (f"You said {pair[0]} and {pair[1]} are separate projects. Undo that in Kept separate first, "
            f"then {then}.")


def _kept_refusal(pair: tuple[str, str], then: str) -> dict[str, Any]:
    return {"ok": False, "code": KEPT_SEPARATE, "pair": list(pair), "error": kept_separate_message(pair, then)}


def record_rename(cur, old: str, new: str, *, source: str, evidence: dict | None = None,
                  observed_at: float | None = None) -> dict[str, Any]:
    """``old`` is now called ``new``: the newest name becomes canonical for the
    whole group, with no alias chains (A to B to C leaves A->C and B->C).

    Grouping never splits. When ``old`` and ``new`` are already one group the
    only change is relabelling: every member is re-pointed at ``new``. Otherwise
    ``old``'s group and ``new``'s group (each a canonical plus its aliases) merge
    under ``new``. ``new``'s own alias row, if any, is dropped so it can be
    canonical; no member ever leaves. Observation times from different checkouts
    come from different Macs' clocks, so they are never compared: a late, stale
    event can relabel a group but never take a name out of it. ``observed_at``
    is only kept in the evidence of the rows this call creates.

    Every group change takes one transaction-level advisory lock before it
    reads, so two renames never interleave into a chain. Under it, when the
    two groups differ and a pair the owner keeps separate has a name in each,
    nothing is written and ``ok`` is false with ``code`` kept_separate and the
    ``pair``. One savepoint; best-effort like the readers: never raises, any
    failure rolls back and returns ``ok`` false."""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new:
        return {"ok": False, "error": "old and new must both be non-empty"}
    if old == new:
        return {"ok": True, "changed": False, "old": old, "new": new, "canonical": new}
    ev_dict = dict(evidence) if evidence else {}
    if observed_at is not None:
        ev_dict["observed_at"] = float(observed_at)
    ev = json.dumps(ev_dict) if ev_dict else None
    try:
        cur.execute("SAVEPOINT project_record_rename", ())
        group_lock(cur)
        old_group, new_group = group_members(cur, old), group_members(cur, new)
        if new not in old_group:
            pair = kept_separate_between(cur, old_group, new_group)
            if pair:
                cur.execute("RELEASE SAVEPOINT project_record_rename", ())
                return {**_kept_refusal(pair, "merge"), "old": old, "new": new}
        group = old_group | new_group
        removed: list[str] = []
        cur.execute("DELETE FROM project_aliases WHERE alias = %s", (new,))
        if cur.rowcount:
            removed.append(new)
        added: list[str] = []
        repointed: list[str] = []
        for member in sorted(group - {new}):
            cur.execute("SELECT project FROM project_aliases WHERE alias = %s", (member,))
            have = cur.fetchone()
            if have is None:
                cur.execute(
                    "INSERT INTO project_aliases (alias, project, source, evidence) "
                    "VALUES (%s, %s, %s, %s::jsonb)", (member, new, source, ev))
                added.append(member)
            elif str(have[0]) != new:
                cur.execute("UPDATE project_aliases SET project = %s WHERE alias = %s", (new, member))
                repointed.append(member)
        cur.execute("RELEASE SAVEPOINT project_record_rename", ())
        return {"ok": True, "changed": bool(added or repointed or removed), "old": old, "new": new,
                "canonical": new, "added": added, "repointed": repointed, "removed": removed}
    except Exception as exc:  # noqa: BLE001 — recording a rename is best-effort, never fatal
        try:
            cur.execute("ROLLBACK TO SAVEPOINT project_record_rename", ())
            cur.execute("RELEASE SAVEPOINT project_record_rename", ())
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def github_confirms_rename(old: str, new: str) -> bool | None:
    """Whether GitHub says ``old`` is now ``new``: ``gh api repos/<old> --jq
    .full_name`` (GitHub follows its own redirect after a rename), 3 s, no shell.
    True only when the answer equals ``new`` case-insensitively; False when it
    names something else; None when ``gh`` is missing, unauthenticated, slow or
    errors in any way. ``gh`` is found by ``synth_cli.binary`` (PATH, then the
    usual install folders), since the drain runs under launchd's bare PATH."""
    try:
        from khipu.synth_cli import binary

        gh = binary("gh")
    except Exception:  # noqa: BLE001
        gh = None
    if not gh:
        return None
    try:
        proc = subprocess.run([gh, "api", f"repos/{old}", "--jq", ".full_name"], capture_output=True,
                              text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    answer = (proc.stdout or "").strip()
    if not answer:
        return None
    return answer.lower() == (new or "").strip().lower()


def _root_commit(repo_root: str) -> str | None:
    """A root commit of the checkout (``git rev-list --max-parents=0 HEAD``, the
    smallest hash when there are several); every checkout, clone and rename of
    one repo shares it. None for a repo with no commits."""
    out = _git(repo_root, ["rev-list", "--max-parents=0", "HEAD"])
    return min(out.split()) if out else None


def _origin_kind(repo_root: str) -> tuple[str, str | None]:
    """``('remote', slug)`` when origin resolves, ``('folder', None)`` when git
    reports there is genuinely no such remote, ``('unknown', None)`` for anything
    else (timeout, other git error, a URL that carries no owner/repo)."""
    try:
        proc = subprocess.run(["git", "-C", repo_root, "remote", "get-url", "origin"], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S, check=False, env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return "unknown", None
    if proc.returncode == 0:
        slug = _slug_from_remote_url((proc.stdout or "").strip())
        return ("remote", slug) if slug else ("unknown", None)
    if proc.returncode == 2 and "no such remote" in (proc.stderr or "").lower():
        return "folder", None
    return "unknown", None


def _checkout_key(cwd: str) -> str | None:
    """``device:inode`` of the repo's git common dir, so a moved folder keeps
    its key and a deleted-and-recloned one gets a new key."""
    common = _git(cwd, ["rev-parse", "--git-common-dir"])
    if not common:
        return None
    path = Path(common)
    if not path.is_absolute():
        path = Path(cwd) / path
    try:
        st = os.stat(path.resolve())
    except OSError:
        return None
    return f"{st.st_dev}:{st.st_ino}"


def observe_checkout(cwd: str, repo_root: str | None, project: str | None, cache_dir: Path) -> dict[str, Any] | None:
    """Hook-side observation of one checkout. It only reports what it sees
    (``{checkout_key, root_commit, project, project_kind, observed_at}``), never
    remembers a last project and never decides a rename; the hub does that from
    its ``project_checkouts`` row. No database. The root commit is cached in one
    small file per checkout key (written atomically, so concurrent hooks never
    clobber each other); a new key always recomputes it. None when there is no
    repo, no commit yet, or anything fails."""
    if not (cwd and repo_root and project):
        return None
    try:
        key = _checkout_key(cwd)
        if not key:
            return None
        cache = cache_dir / f"{key.replace(':', '_')}.json"
        root = None
        try:
            rec = json.loads(cache.read_text(encoding="utf-8"))
            root = rec.get("root_commit") if isinstance(rec, dict) and rec.get("checkout_key") == key else None
        except (OSError, ValueError):
            pass
        if not root:
            root = _root_commit(repo_root)
            if root:
                cache.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=cache.parent, prefix=".checkout.", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump({"checkout_key": key, "root_commit": root}, fh)
                os.replace(tmp, cache)
        if not root:
            return None
        kind, slug = _origin_kind(repo_root)
        if kind == "remote":
            project = slug or project
        return {"checkout_key": key, "root_commit": root, "project": project, "project_kind": kind,
                "observed_at": time.time()}
    except Exception:  # noqa: BLE001 — identity is best-effort on the hook path
        return None


def _pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def observation_fields(obs: Any) -> tuple[str, str, str, str, float] | None:
    """``(checkout_key, root_commit, project, project_kind, observed_at)`` of a
    well-formed observation, else None."""
    try:
        key, root = str(obs["checkout_key"]), str(obs["root_commit"])
        project, kind = str(obs["project"]), str(obs["project_kind"])
        at = float(obs["observed_at"])
    except (KeyError, TypeError, ValueError):
        return None
    if kind not in ("remote", "folder", "unknown") or not (key and root and project):
        return None
    if not safe_project_name(project):
        return None
    return key, root, project, kind, at


def record_checkout_observation(cur, obs: dict[str, Any]) -> dict[str, Any]:
    """The hub's side: compare one observation with the checkout's stored row
    and record a rename when the rules say it is certain.

    A transition is the same ``checkout_key``, the same ``root_commit``, a
    changed project and an observation not older than the row (both times come
    from the one Mac that owns the checkout, so comparing them is safe; times
    from different checkouts are never compared). folder to folder
    and folder to remote are certain; remote to folder is ignored; remote to
    remote is a rename only if GitHub confirms it, otherwise a ``project_pairs``
    'asked' row is kept for the owner and nothing is merged. A different root
    commit is never a rename. An 'unknown' observation only moves
    ``last_seen_at``. One savepoint, never raises: when anything fails (e.g.
    migration 0039 not applied) nothing is recorded and ``ok`` is false, so the
    caller can keep the observation (capture's local spool) and land it later."""
    fields = observation_fields(obs)
    if fields is None:
        return {"ok": False, "error": "malformed observation"}
    key, root, project, kind, at = fields
    try:
        cur.execute("SAVEPOINT project_checkout_observation", ())
        # Read-evaluate-write under a per-checkout lock: two first sightings of
        # one checkout in concurrent transactions are evaluated one after the
        # other, so the second sees the first's row and its transition counts.
        _xact_lock(cur, CHECKOUT_LOCK_CLASS, key)
        cur.execute("SELECT root_commit, project, project_kind, extract(epoch FROM last_seen_at) "
                    "FROM project_checkouts WHERE checkout_key = %s FOR UPDATE", (key,))
        row = cur.fetchone()
        out: dict[str, Any] = {"ok": True, "action": "recorded"}
        if row is None:
            if kind != "unknown":
                cur.execute("INSERT INTO project_checkouts (checkout_key, root_commit, project, project_kind, "
                            "last_seen_at) VALUES (%s, %s, %s, %s, to_timestamp(%s)) "
                            "ON CONFLICT (checkout_key) DO NOTHING", (key, root, project, kind, at))
            out["action"] = "first" if kind != "unknown" else "ignored"
        else:
            old_root, old_project, old_kind, seen = str(row[0]), str(row[1]), str(row[2]), float(row[3])
            if at < seen:
                out["action"] = "stale"
            elif kind == "unknown":
                cur.execute("UPDATE project_checkouts SET last_seen_at = to_timestamp(%s) WHERE checkout_key = %s",
                            (at, key))
                out["action"] = "seen"
            elif old_root != root:
                cur.execute("UPDATE project_checkouts SET root_commit = %s, project = %s, project_kind = %s, "
                            "last_seen_at = to_timestamp(%s) WHERE checkout_key = %s", (root, project, kind, at, key))
                out["action"] = "new_root"
            elif old_project == project:
                cur.execute("UPDATE project_checkouts SET project_kind = %s, last_seen_at = to_timestamp(%s) "
                            "WHERE checkout_key = %s", (kind, at, key))
            elif old_kind == "remote" and kind == "folder":
                # Origin removed: not a rename, and the remote name stays the
                # last known one so re-adding it later is not a transition.
                cur.execute("UPDATE project_checkouts SET last_seen_at = to_timestamp(%s) WHERE checkout_key = %s",
                            (at, key))
                out["action"] = "ignored_remote_to_folder"
            else:
                evidence = {"old": old_project, "new": project, "root_commit": root, "checkout_key": key}
                source = "checkout"
                if old_kind == "remote" and kind == "remote":
                    if github_confirms_rename(old_project, project):
                        source = "github"
                    else:
                        a, b = _pair(old_project, project)
                        cur.execute("INSERT INTO project_pairs (a, b, root_commit, state) VALUES (%s, %s, %s, 'asked') "
                                    "ON CONFLICT (a, b) DO NOTHING", (a, b, root))
                        source = ""
                if source:
                    # The GitHub lookup above ran before the group lock; whether
                    # the owner keeps these apart is decided by record_rename
                    # under it. Refused: no rename, but the checkout's name is
                    # followed so this is not re-evaluated on every capture.
                    res = record_rename(cur, old_project, project, source=source, evidence=evidence, observed_at=at)
                    if res.get("code") == KEPT_SEPARATE:
                        out.update(action="kept_separate", pair=res["pair"])
                    elif not res.get("ok"):
                        raise RuntimeError(str(res.get("error")))
                    else:
                        out.update(action="renamed", rename=res)
                else:
                    out["action"] = "asked"
                cur.execute("UPDATE project_checkouts SET project = %s, project_kind = %s, "
                            "last_seen_at = to_timestamp(%s) WHERE checkout_key = %s", (project, kind, at, key))
        cur.execute("RELEASE SAVEPOINT project_checkout_observation", ())
        return out
    except Exception as exc:  # noqa: BLE001 — best-effort; the next capture re-derives it
        try:
            cur.execute("ROLLBACK TO SAVEPOINT project_checkout_observation", ())
            cur.execute("RELEASE SAVEPOINT project_checkout_observation", ())
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def resolve_repo_root(cwd: str, *, cur: Any = None) -> dict[str, Any]:
    """{repo_root, project, is_worktree} for a hook's cwd.

    - A path under a Claude/Cursor/Codex worktree tree, or whose git common
      dir lives outside its own toplevel, resolves to the MAIN checkout
      (repo_root is the common dir's parent, not the worktree's own path).
    - project is the git remote's ``owner/repo`` slug when origin is set,
      else the repo root's basename — then, when ``cur`` is given, resolved
      through ``project_aliases`` (K6) so a renamed/aliased project reads as
      one project everywhere instead of splitting silently.
    - A scratchpad / /tmp cwd (dispatched-child pattern) never resolves —
      repo_root and project are both None; the caller decides fallback.

    ``cur`` is OPTIONAL and defaults to None: every hook caller (the actual
    per-turn path, sandboxed in Aegis, no DB) calls this with no cursor and
    gets EXACTLY today's behaviour — the alias lookup only runs when a
    caller that already holds a live cursor (a backfill, a CLI command)
    opts in. Git failures or unexpected git output yield the empty result.
    With a supplied cursor, missing alias-schema tables/columns fall back to
    the resolved project; unexpected database failures propagate.
    """
    out: dict[str, Any] = {"repo_root": None, "project": None, "is_worktree": False}
    cwd = (cwd or "").strip()
    if not cwd or _is_scratch_cwd(cwd):
        return out
    try:
        cwd_path = Path(cwd).resolve()
    except OSError:
        cwd_path = Path(cwd)
    toplevel = _git(cwd, ["rev-parse", "--show-toplevel"])
    if not toplevel:
        return out
    try:
        toplevel_path = Path(toplevel).resolve()
    except OSError:
        toplevel_path = Path(toplevel)

    # git's --git-common-dir, when relative, is relative to the invocation
    # CWD (not the toplevel) — resolving it against toplevel_path instead
    # silently walks to the wrong directory whenever cwd is a subdirectory.
    common = _git(cwd, ["rev-parse", "--git-common-dir"])
    main_root = toplevel_path
    is_worktree = False
    if common:
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = cwd_path / common_path
        try:
            common_path = common_path.resolve()
        except OSError:
            pass
        if common_path.name == ".git" and common_path.parent != toplevel_path:
            main_root = common_path.parent
            is_worktree = True
    if any(marker in str(toplevel_path) for marker in WORKTREE_MARKERS):
        is_worktree = True

    out["repo_root"] = str(main_root)
    out["is_worktree"] = is_worktree
    slug = _remote_slug(main_root)
    project = slug or _folder_project(main_root.name)
    if not project:
        return out
    if cur is not None:
        project = _resolve_project_alias(cur, project) or project
    out["project"] = project
    return out


_SCOPE_PATH_SEP_RE = re.compile(r"[/\\]")
SCOPE_MAX_WORDS = 6


def normalize_scope(raw: str | None) -> str | None:
    """K6: ``episodes.scope`` is a free-text FALLBACK label, not a project —
    the audit found 3,851 distinct values, 2,541 of them over 40 chars,
    including bare worktree paths. Kept only when it still reads like a
    short label (<= 6 words, no path separator); anything else is dropped to
    NULL at write time rather than stored as the junk it is. Never raises."""
    s = (raw or "").strip()
    if not s:
        return None
    if _SCOPE_PATH_SEP_RE.search(s):
        return None
    if len(s.split()) > SCOPE_MAX_WORDS:
        return None
    return s
