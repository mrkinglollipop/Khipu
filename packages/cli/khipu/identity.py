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


def _slug_from_remote_url(url: str) -> str | None:
    """``owner/repo`` from an https/ssh/scp-style git remote URL, or None."""
    url = (url or "").strip()
    if not url:
        return None
    url = re.sub(r"\.git$", "", url)
    url = url.rstrip("/")
    m = re.search(r"[:/]([^/:]+/[^/:]+)$", url)
    if not m:
        return None
    slug = m.group(1).strip("/")
    return slug or None


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
    project. Best-effort only: never raises, and callers with no live
    connection (every hook call — see ``resolve_repo_root``'s own contract)
    simply skip it by not passing ``cur``."""
    try:
        cur.execute("SAVEPOINT project_alias_lookup", ())
        cur.execute("SELECT project FROM project_aliases WHERE alias = %s", (project,))
        row = cur.fetchone()
        cur.execute("RELEASE SAVEPOINT project_alias_lookup", ())
        return str(row[0]) if row and row[0] else None
    except Exception:  # noqa: BLE001 — alias lookup is best-effort only
        try:
            cur.execute("ROLLBACK TO SAVEPOINT project_alias_lookup", ())
            cur.execute("RELEASE SAVEPOINT project_alias_lookup", ())
        except Exception:  # noqa: BLE001
            pass
        return None


def project_canonical_group(cur, project: str | None) -> tuple[str | None, list[str]]:
    """``(canonical, group)`` for ``project`` through ``project_aliases``: the
    canonical project is the alias's target (or ``project`` itself), and the
    group is the canonical plus every alias that points at it, canonical first.
    One round trip. Best-effort exactly like :func:`_resolve_project_alias`:
    never raises, and any failure (no table on a pre-0021 hub, a dead cursor)
    yields ``(project, [project])``."""
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
    except Exception:  # noqa: BLE001 — alias expansion is best-effort only
        try:
            cur.execute("ROLLBACK TO SAVEPOINT project_alias_group", ())
            cur.execute("RELEASE SAVEPOINT project_alias_group", ())
        except Exception:  # noqa: BLE001
            pass
        return project, [project]


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
    points elsewhere is refused rather than silently re-pointed."""
    alias, canonical = (alias or "").strip(), (canonical or "").strip()
    if not alias or not canonical:
        return {"ok": False, "error": "alias and canonical must both be non-empty"}
    if alias == canonical:
        return {"ok": False, "error": f"{alias!r} cannot be an alias of itself"}
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
    cur.execute("INSERT INTO project_aliases (alias, project) VALUES (%s, %s)", (alias, canonical))
    return {"ok": True, "alias": alias, "canonical": canonical, "changed": True}


def remove_project_alias(cur, alias: str) -> dict[str, Any]:
    alias = (alias or "").strip()
    cur.execute("DELETE FROM project_aliases WHERE alias = %s", (alias,))
    if not cur.rowcount:
        return {"ok": False, "error": f"{alias!r} is not an alias"}
    return {"ok": True, "alias": alias, "changed": True}


RENAME_LOCK_KEY = 0x4B48495055524E4D  # fixed: renames are rare, one at a time is fine


def _evidence_times(rows) -> list[float]:
    out: list[float] = []
    for (ev,) in rows:
        if isinstance(ev, str):
            try:
                ev = json.loads(ev)
            except ValueError:
                continue
        if isinstance(ev, dict) and ev.get("observed_at") is not None:
            try:
                out.append(float(ev["observed_at"]))
            except (TypeError, ValueError):
                continue
    return out


def record_rename(cur, old: str, new: str, *, source: str, evidence: dict | None = None,
                  observed_at: float | None = None) -> dict[str, Any]:
    """``old`` is now called ``new``: the newest name becomes canonical for the
    whole group, with no alias chains (A to B to C leaves A->C and B->C). The
    group is ``old``'s canonical plus every alias of it; each member is
    re-pointed at ``new``. If ``new`` was itself an alias (a rename back,
    A->B->A) its row is dropped first so it can be canonical again. One
    savepoint; best-effort exactly like the readers: never raises, any failure
    rolls back and returns ``ok`` false. ``source`` and ``evidence`` are kept on
    the rows this call creates, so an automatic alias can be shown and undone.

    Renames serialise on a transaction-level advisory lock. ``observed_at``
    (epoch seconds, kept in the evidence) orders them: an event older than the
    newest ``observed_at`` already recorded for any alias in the affected group
    is refused (``ok`` true, ``changed`` false, ``refused`` says why), so a
    delayed observation never undoes a newer rename."""
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
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (RENAME_LOCK_KEY,))
        cur.execute("SELECT project FROM project_aliases WHERE alias = %s", (old,))
        row = cur.fetchone()
        top = str(row[0]) if row and row[0] else old
        cur.execute("SELECT alias FROM project_aliases WHERE project = %s", (top,))
        group = {top, new, *(str(r[0]) for r in cur.fetchall())}
        if observed_at is not None:
            cur.execute("SELECT evidence FROM project_aliases WHERE alias = ANY(%s) OR project = ANY(%s)",
                        (sorted(group), sorted(group)))
            newest = max(_evidence_times(cur.fetchall()), default=None)
            if newest is not None and float(observed_at) < newest:
                cur.execute("RELEASE SAVEPOINT project_record_rename", ())
                return {"ok": True, "changed": False, "old": old, "new": new, "refused":
                        f"older than a rename already recorded for this group ({observed_at} < {newest})"}
        members = group - {new}
        removed: list[str] = []
        cur.execute("DELETE FROM project_aliases WHERE alias = %s", (new,))
        if cur.rowcount:
            removed.append(new)
        added: list[str] = []
        repointed: list[str] = []
        for member in sorted(members):
            cur.execute("SELECT project FROM project_aliases WHERE alias = %s", (member,))
            have = cur.fetchone()
            if have is None:
                cur.execute(
                    "INSERT INTO project_aliases (alias, project, source, evidence) "
                    "VALUES (%s, %s, %s, %s::jsonb)", (member, new, source, ev))
                added.append(member)
            elif str(have[0]) != new:
                cur.execute(
                    "UPDATE project_aliases SET project = %s, evidence = "
                    "CASE WHEN %s::float8 IS NULL THEN evidence ELSE COALESCE(evidence, '{}'::jsonb) "
                    "|| jsonb_build_object('observed_at', %s::float8) END WHERE alias = %s",
                    (new, observed_at, observed_at, member))
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
    errors in any way."""
    try:
        proc = subprocess.run(["gh", "api", f"repos/{old}", "--jq", ".full_name"], capture_output=True,
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


def record_checkout_observation(cur, obs: dict[str, Any]) -> dict[str, Any]:
    """The hub's side: compare one observation with the checkout's stored row
    and record a rename when the rules say it is certain.

    A transition is the same ``checkout_key``, the same ``root_commit``, a
    changed project and an observation not older than the row. folder to folder
    and folder to remote are certain; remote to folder is ignored; remote to
    remote is a rename only if GitHub confirms it, otherwise a ``project_pairs``
    'asked' row is kept for the owner and nothing is merged. A different root
    commit is never a rename. An 'unknown' observation only moves
    ``last_seen_at``. One savepoint, never raises: when anything fails (e.g.
    migration 0039 not applied) nothing is recorded and the next capture's
    observation re-derives it."""
    try:
        key, root = str(obs["checkout_key"]), str(obs["root_commit"])
        project, kind = str(obs["project"]), str(obs["project_kind"])
        at = float(obs["observed_at"])
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "malformed observation"}
    if kind not in ("remote", "folder", "unknown") or not (key and root and project):
        return {"ok": False, "error": "malformed observation"}
    try:
        cur.execute("SAVEPOINT project_checkout_observation", ())
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
                    res = record_rename(cur, old_project, project, source=source, evidence=evidence, observed_at=at)
                    if not res.get("ok"):
                        raise RuntimeError(str(res.get("error")))
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
    opts in. Never raises: any git failure or unexpected shape yields the
    empty result.
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
    project = slug or main_root.name
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
