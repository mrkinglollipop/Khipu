"""Auto-close by pull request and by decision (scope
docs/plans/2026-10-16-owed-and-ledger-ssot.md, design (b), slice 2, with the
owner's decision 1).

**The PR rule.** An open item closes when every pull request it names
(``commitments.prs``, parsed at insert by :func:`parse_prs`) is merged or
closed. A PR's fate comes from, in order, all read-only:

1. a registered runner's ``work-summary.json`` (``khipu.work``): stage
   ``merged`` or ``closed``;
2. the ledger: a row whose ``refs.pr`` names the PR in a state that means
   done (merged, verified, done);
3. a later capture in the same project whose decisions, closed loops or
   summary carry a line git or gh printed ("Merge pull request #N from ...",
   "✓ Merged pull request owner/repo#N"; :func:`capture_pr_records`). A
   model's sentence or a question ("Was PR #366 merged?") never counts;
4. GitHub, through ``gh pr view N --repo OWNER/REPO --json state,mergedAt``
   (fixed argv, no shell, a short timeout), on by default and switched off
   with the ``owed_pr_check_github`` setting. Each PR is asked once a run.
   The repository is the item's own GitHub pull link, else a project key of
   the ``owner/repo`` shape; with neither, GitHub is skipped. A missing or
   failing ``gh`` leaves the item open and says why in ``pr_check``.

**The decision rule.** A question or a "decide" item closes when a later
decision of the same project answers it strictly: it names the item's id, or
it matches the item by ``commitments.completion_match`` at
``commitment_close_similarity``. A looser match is kept in
``settle_suggestion`` ("Looks settled by decision N") and never closes it.

**Evidence.** Each source gives one record per pull request, keyed by its
repository and number (a pull link names its own repository; any other
mention is the project's), with the state that source read and when. The
newest record for a PR wins, so a PR that was reopened revokes an earlier
"merged". Two repositories' PR #41 are two PRs.

The owner's own items are never closed by these rules (Matt, 2026-10-17:
"Suggest, don't close"): ``commitments.close_by_rule`` reads the owner under
the row lock and turns the close into a "Looks done" suggestion for him.

Every close goes through ``commitments.close_by_rule`` and records
``close_reason``, ``closed_by`` (``BY_RULE``), ``close_rule_key`` and
``close_evidence``. A reopened item keeps its rule key in
``reopened_rule_keys``; the same rule and evidence never close it again.
The nightly runs both rules over every open item (``jobs``); each capture
runs them for its own project (``capture``), asking GitHub only about the
PRs that capture names. Nothing here writes outside the commitments table.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from khipu.commitments import (
    _close_columns_ready,
    _jaccard,
    _match_score,
    _not_forgotten_clause,
    close_by_rule,
    completion_match,
)

GH_TIMEOUT_S = 10
GITHUB_SETTING = "owed_pr_check_github"
DONE_STATES = ("merged", "closed")
SOURCE_WORDS = {"runner": "from the runner's summary", "ledger": "from the ledger",
                "capture": "from a later conversation", "github": "from GitHub"}
# A decision this close to an item, below the strict bar, is offered as
# "Looks settled by decision N" and never applied.
LOOSE_SETTLE_MIN_SCORE = 0.35
BACKFILL_BATCH = 1000
NOT_ASKED = "not checked on GitHub yet; the nightly asks"

_PR_WORD = r"(?:PRs?|pull\s+requests?)"
_PR_NUMBER_RE = re.compile(rf"\b{_PR_WORD}\s*#?\s*(\d{{1,7}})\b", re.I)
# A PR list runs to the end of its clause, however long: a list cut short
# would let an item close while a later PR it names is still open.
_PR_LIST_RE = re.compile(rf"\b{_PR_WORD}\b([^.;:\n]*)", re.I)
_HASH_RE = re.compile(r"(?<![\w&])#(\d{1,7})\b")
_PULL_LINK_RE = re.compile(r"github\.com/([A-Za-z0-9][\w.-]*/[\w.-]+?)/pull/(\d{1,7})\b", re.I)
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
# A capture's words are evidence of a PR's fate only as a line git or gh
# itself prints: git's merge commit subject, or gh's own merge, close and
# reopen lines. A model's sentence ("PR #366 merged") or a question ("Was PR
# #366 merged?") is never one; GitHub is asked instead.
_GIT_MERGE_LINE_RE = re.compile(r"^\s*(?:[0-9a-f]{7,40}\s+)?Merge\s+pull\s+request\s+#(\d{1,7})\s+from\s+\S+",
                                re.I)
_GH_LINE_RE = re.compile(
    r"^\s*[✓✔]?\s*(Merged|Squashed\s+and\s+merged|Rebased\s+and\s+merged|Closed|Reopened)\s+pull\s+request\s+"
    r"(?:([A-Za-z0-9][\w.-]*/[\w.-]+))?#(\d{1,7})\b")
_GH_WORD_STATE = {"closed": "closed", "reopened": "open"}
_DECIDE_RE = re.compile(r"\bdecid(?:e|es|ing|ed)\b|\bdecision\b", re.I)
_NAMES_ITEM_RE = re.compile(r"\b(?:commitment|owed\s+item|item|question)\s*#?\s*(\d{1,12})\b", re.I)


def _log(msg: str) -> None:
    print(f"[khipu-close-rules] {msg}", file=sys.stderr)


# ---- PR numbers -------------------------------------------------------------


def parse_prs(text: str) -> list[int]:
    """The pull request numbers ``text`` names, in order, each once: ``PR
    #N``, ``pull request N``, a ``#N`` in a list after "PR"/"PRs", and a
    GitHub pull link. A bare ``#N`` with no "PR" before it (an issue) is not
    one."""
    text = str(text or "")
    found: list[tuple[int, int]] = []
    for m in _PR_NUMBER_RE.finditer(text):
        found.append((m.start(1), int(m.group(1))))
    for m in _PR_LIST_RE.finditer(text):
        for h in _HASH_RE.finditer(m.group(1)):
            found.append((m.start(1) + h.start(1), int(h.group(1))))
    for m in _PULL_LINK_RE.finditer(text):
        found.append((m.start(2), int(m.group(2))))
    out: list[int] = []
    for _, n in sorted(found):
        if n > 0 and n not in out:
            out.append(n)
    return out


def _repo(value: str | None) -> str | None:
    return str(value).lower() if value else None


def pr_refs(text: str, project_repo: str | None) -> list[tuple[str | None, int]]:
    """The ``(repository, number)`` of each pull request ``text`` names, in
    the order they appear, each once. A GitHub pull link names its own
    repository; any other mention ("PR #41", "#41" in a PR list) is the
    project's repository, None when that is not known. So "PR #41 and
    github.com/other/repo/pull/41" is two refs, both required."""
    text = str(text or "")
    project = _repo(project_repo)
    found: list[tuple[int, str | None, int]] = []
    for m in _PR_NUMBER_RE.finditer(text):
        found.append((m.start(1), project, int(m.group(1))))
    for m in _PR_LIST_RE.finditer(text):
        for h in _HASH_RE.finditer(m.group(1)):
            found.append((m.start(1) + h.start(1), project, int(h.group(1))))
    for m in _PULL_LINK_RE.finditer(text):
        found.append((m.start(2), _repo(m.group(1)), int(m.group(2))))
    out: list[tuple[str | None, int]] = []
    for _, repo, n in sorted(found, key=lambda f: f[0]):
        if n > 0 and (repo, n) not in out:
            out.append((repo, n))
    return out


def item_refs(item: dict[str, Any], project_repo: str | None) -> list[tuple[str | None, int]]:
    """Every pull request an item was ever said to need: those its text and
    its latest restatement name, every number in its stored ``prs`` and every
    other repository's PR an earlier restatement named (``pr_check["refs"]``;
    a restatement adds to both lists, never replaces them). All are
    required."""
    refs = pr_refs(item.get("text") or "", project_repo)
    check = item.get("pr_check") if isinstance(item.get("pr_check"), dict) else {}
    kept = [(m.group(1), int(m.group(2))) for m in (_QUALIFIED_RE.match(str(r)) for r in check.get("refs") or [])
            if m]
    for ref in pr_refs(item.get("latest_text") or "", project_repo) + kept:
        if ref not in refs:
            refs.append(ref)
    named = {n for _, n in refs}
    refs += [(_repo(project_repo), int(n)) for n in (item.get("prs") or []) if int(n) not in named]
    return refs


_QUALIFIED_RE = re.compile(r"^([A-Za-z0-9][\w.-]*/[\w.-]+)#(\d{1,7})$")


def other_repo_labels(text: str, project: str | None) -> list[str]:
    """``owner/repo#N`` for each PR ``text`` names in a repository other
    than ``project``'s: what a restatement keeps in ``pr_check["refs"]``,
    since ``prs`` holds only numbers."""
    repo = _project_repo(project)
    return [f"{r}#{n}" for r, n in pr_refs(text, repo) if r and r != _repo(repo)]


def ref_label(ref: tuple[str | None, int], project_repo: str | None) -> str:
    """``"41"`` for the project's own repository, else ``"owner/repo#41"``."""
    repo, n = ref
    return str(n) if repo == _repo(project_repo) else f"{repo}#{n}"


def ready(cur) -> bool:
    return _close_columns_ready(cur)


def backfill_prs(cur, *, batch: int = BACKFILL_BATCH) -> int:
    """Fill ``prs`` on rows from before migration 0034 (``prs IS NULL``), in
    batches. Returns the count filled; 0 on a hub without the column."""
    if not ready(cur):
        return 0
    filled = 0
    while True:
        cur.execute("SELECT id, text FROM commitments WHERE prs IS NULL ORDER BY id LIMIT %s", (batch,))
        rows = cur.fetchall()
        for cid, text in rows:
            cur.execute("UPDATE commitments SET prs = %s WHERE id = %s", (parse_prs(text), cid))
            filled += 1
        if len(rows) < batch:
            return filled


# ---- where a PR's fate comes from ---------------------------------------------
#
# Every source gives evidence records keyed by ``(repository, number)``, the
# repository lower-cased and None when the source does not name one (it is
# then the project's own). A record carries the PR's state as that source last
# read it (``merged``, ``closed`` or ``open``), when it was read, and the
# source. For one PR the newest record wins, so a later "open" (a reopened PR)
# revokes an earlier "merged".

EvidenceKey = tuple  # (repository or None, number)
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _record(state: str, read_at: Any, source: str, **extra: Any) -> dict[str, Any]:
    return {"state": state, "read_at": _iso(read_at), "source": source, **extra}


def _iso(value: Any) -> str | None:
    at = _at(value)
    return at.isoformat() if at else None


def _at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def capture_pr_records(texts) -> dict[EvidenceKey, str]:
    """``{(repository, pr): state}`` from the lines git or gh print among a
    capture's texts: "Merge pull request #366 from owner/branch" (merged),
    "✓ Merged pull request owner/repo#366" (merged), "✓ Closed pull request
    #367" (closed), "✓ Reopened pull request #367" (open). The last line about
    a PR wins. Nothing else counts: not a model's sentence, and never a
    question."""
    out: dict[EvidenceKey, str] = {}
    for raw in texts or []:
        for line in str(raw or "").splitlines():
            if line.rstrip().endswith("?"):
                continue
            m = _GIT_MERGE_LINE_RE.match(line)
            if m:
                out[(None, int(m.group(1)))] = "merged"
                continue
            m = _GH_LINE_RE.match(line)
            if m:
                word = m.group(1).split()[-1].lower()
                out[(_repo(m.group(2)), int(m.group(3)))] = _GH_WORD_STATE.get(word, "merged")
    return out


def capture_pr_states(texts) -> dict[int, str]:
    """``{pr: state}`` of :func:`capture_pr_records`, for callers that only
    want the numbers."""
    return {n: state for (_, n), state in capture_pr_records(texts).items()}


def _loop_texts(items) -> list[str]:
    return [str((i.get("text") if isinstance(i, dict) else i) or "") for i in items or []]


def _payload_texts(payload: dict[str, Any]) -> list[str]:
    texts = _loop_texts(payload.get("decisions")) + _loop_texts(payload.get("closed_loops"))
    if payload.get("summary"):
        texts.append(str(payload["summary"]))
    return texts


def _pr_id(item: dict) -> int | None:
    m = re.fullmatch(r"(?:pr[-_ ]?|#)?(\d{1,7})", str(item.get("id") or ""), re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"/pull/(\d{1,7})\b", str(item.get("link") or ""))
    return int(m.group(1)) if m else None


def _runner_states(project: str) -> dict[EvidenceKey, dict[str, Any]]:
    """Evidence from the project's registered runner summary, read through
    ``khipu.work`` (the same checks as the Work view). Only a current summary
    counts: a stale, gone or failed one (whose contents are the last good
    read) is no evidence at all. Every listed PR is a record, the ones not
    merged or closed as ``open``, read when the summary was written."""
    from khipu import work

    cfg = work.settings()
    entry = next((e for e in cfg["work_projects"] if e["project"] == project), None)
    if entry is None:
        return {}
    source = work.read_source(entry, cfg, datetime.now(timezone.utc))
    if source.get("state") != "ok":
        return {}
    contents = source.get("contents") or {}
    out: dict[EvidenceKey, dict[str, Any]] = {}
    for pr in contents.get("prs") or []:
        n = _pr_id(pr)
        if n is None:
            continue
        link = _PULL_LINK_RE.search(str(pr.get("link") or ""))
        stage = pr.get("stage")
        out[(_repo(link.group(1)) if link else None, n)] = _record(
            stage if stage in DONE_STATES else "open", contents.get("written_at"), "runner")
    return out


def _ledger_states(cur, project: str) -> dict[EvidenceKey, dict[str, Any]]:
    """Evidence from ledger rows whose ``refs.pr`` names a PR: ``merged``
    when the row's state means done (the vocab's ``done``, or a merged word),
    else ``open``, read when the row last changed."""
    cur.execute("SAVEPOINT close_rules_ledger")
    try:
        cur.execute(
            "SELECT r.refs->>'pr', r.state, v.meaning, r.updated_at FROM ledger_rows r "
            "LEFT JOIN ledger_vocab v ON v.project = r.project AND v.field = 'state' AND v.word = r.state "
            "WHERE r.project = %s AND r.removed_at IS NULL AND r.refs ? 'pr'",
            (project,),
        )
        rows = cur.fetchall()
        cur.execute("RELEASE SAVEPOINT close_rules_ledger")
    except Exception as exc:  # noqa: BLE001 — no ledger tables yet: this source is empty
        cur.execute("ROLLBACK TO SAVEPOINT close_rules_ledger")
        _log(f"ledger read skipped ({type(exc).__name__})")
        return {}
    out: dict[EvidenceKey, dict[str, Any]] = {}
    for ref, state, meaning, updated_at in rows:
        m = re.fullmatch(r"\s*(?:pr[-_ ]?|#)?(\d{1,7})\s*", str(ref or ""), re.I)
        if not m:
            continue
        word = str(state or "").upper()
        merged = "MERGED" in word and "UNMERGED" not in word
        rec = _record("merged" if (meaning == "done" or merged) else "open", updated_at, "ledger")
        key = (None, int(m.group(1)))
        if _newer(rec, out.get(key)):
            out[key] = rec
    return out


def _episode_states(cur, project: str, after_episode: int) -> dict[EvidenceKey, dict[str, Any]]:
    """Evidence from the git and gh lines in the project's captures after
    ``after_episode`` (the nightly's view of source 3): the newest capture's
    line about each PR, with that capture's id as ``episode``."""
    cur.execute(
        "SELECT e.id, e.ts, e.summary, e.raw->'decisions', e.raw->'closed_loops' FROM episodes e "
        "WHERE e.project = %s AND e.id > %s "
        "AND (e.summary ~* 'pull request' OR (e.raw->'decisions')::text ~* 'pull request' "
        "OR (e.raw->'closed_loops')::text ~* 'pull request') ORDER BY e.id",
        (project, after_episode),
    )
    out: dict[EvidenceKey, dict[str, Any]] = {}
    for eid, ts, summary, decisions, loops in cur.fetchall():
        texts = [summary or ""]
        for blob in (decisions, loops):
            items = json.loads(blob) if isinstance(blob, str) else blob
            texts += _loop_texts(items if isinstance(items, list) else [])
        for key, state in capture_pr_records(texts).items():
            out[key] = _record(state, ts, "capture", episode=int(eid))
    return out


def _newer(rec: dict[str, Any], than: dict[str, Any] | None) -> bool:
    """True when ``rec`` should replace ``than``: it was read later, or at
    the same time and says the PR is still open (the cautious reading). A
    record with no read time is the oldest."""
    if than is None:
        return True
    a, b = _at(rec.get("read_at")) or _EPOCH, _at(than.get("read_at")) or _EPOCH
    return a > b or (a == b and rec["state"] == "open" and than["state"] != "open")


class GitHub:
    """Read-only ``gh pr view`` lookups for one run, each PR asked once."""

    def __init__(self) -> None:
        from khipu.config import choice_setting

        self.on = choice_setting(GITHUB_SETTING)[0] == "on"
        self.cache: dict[tuple[str, int], tuple[str | None, str | None]] = {}
        self.unavailable: str | None = None if self.on else (
            f"the GitHub check is turned off ({GITHUB_SETTING})")
        self.asked = 0

    def state(self, repo: str | None, number: int) -> tuple[str | None, str | None]:
        """``(state, None)`` with state merged, closed or open, or ``(None,
        why)``."""
        if self.unavailable:
            return None, self.unavailable
        if not repo:
            return None, "the project's GitHub repository is not known"
        key = (repo, number)
        if key not in self.cache:
            self.cache[key] = self._ask(repo, number)
        return self.cache[key]

    def _ask(self, repo: str, number: int) -> tuple[str | None, str | None]:
        from khipu.synth_cli import binary

        # PATH first, then the usual install folders: the nightly runs under
        # launchd's bare PATH (KHIPU_GH_CLI names one explicitly).
        gh = binary("gh")
        if gh is None:
            self.unavailable = "gh is not installed or not on PATH"
            return None, self.unavailable
        self.asked += 1
        argv = [gh, "pr", "view", str(number), "--repo", repo, "--json", "state,mergedAt"]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=GH_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL, check=False, env=os.environ.copy())
        except subprocess.TimeoutExpired:
            return None, f"GitHub did not answer within {GH_TIMEOUT_S} seconds"
        except OSError as exc:
            self.unavailable = f"gh could not run ({type(exc).__name__})"
            return None, self.unavailable
        if proc.returncode != 0:
            first = (proc.stderr or "").strip().splitlines()[:1]
            return None, "GitHub could not say" + (f": {first[0][:120]}" if first else "")
        try:
            state = str(json.loads(proc.stdout or "{}").get("state") or "").lower()
        except (ValueError, AttributeError):
            return None, "GitHub could not say: the answer was not readable"
        if state not in ("merged", "closed", "open"):
            return None, "GitHub could not say: no state in the answer"
        return state, None


# ---- the evidence check every close path shares --------------------------------

_QUESTION_START_RE = re.compile(r"^\s*(?:was|were|is|are|did|does|do|has|have|had|can|could|should|will|"
                                r"would|why|how|what|when|whether)\b", re.I)


def evidence_problem(text: str, item_text: str = "", project: str | None = None, *,
                     item: dict[str, Any] | None = None) -> str | None:
    """Why ``text`` (a closed loop, a ``done:`` line, a decision or a session
    mention) is not evidence that closes an item reading ``item_text`` in
    ``project``, or None when it is. The same check for every close path
    (``commitments.auto_close``, ``commitments.close_session_plan`` and the
    decision rule here):

    * a question is never evidence ("Was PR #366 merged?");
    * an item that names pull requests closes only when EVERY one of them,
      each resolved to its repository, is merged or closed in the newest
      line about it in this evidence, and only a line git or gh printed
      (:func:`capture_pr_records`) counts. A reopened PR, or any named PR the
      evidence does not show finished, keeps the item open; the PR rule asks
      the PR's own sources, GitHub included, and closes it when they are.
      With ``item`` (its row), the PRs are every one it was ever said to
      need (:func:`item_refs`), not only those ``item_text`` names."""
    s = str(text or "").strip()
    if s.endswith("?") or _QUESTION_START_RE.match(s):
        return "a question is not evidence"
    repo = _project_repo(project)
    refs = item_refs(item, repo) if item is not None else pr_refs(item_text, repo)
    if refs:
        states = {_key(key, repo): state for key, state in capture_pr_records([s]).items()}
        unfinished = [ref_label(ref, repo) for ref in refs if states.get(_key(ref, repo)) not in DONE_STATES]
        if unfinished:
            named = ", ".join(n if "#" in n else f"#{n}" for n in unfinished)
            return (f"the item waits on every pull request it names, and {named} is not shown merged or "
                    "closed by a git or gh line here")
    return None


def _project_repo(project: str | None) -> str | None:
    """The project key as a GitHub repository, or None when it is not one."""
    return project if project and _REPO_RE.match(project) else None


# ---- the rules --------------------------------------------------------------


def _pr_reason(found: dict[str, tuple[str, str]]) -> str:
    """'pull request #366 merged (from the runner's summary)', grouped by
    state and source in the item's PR order."""
    groups: dict[tuple[str, str], list[str]] = {}
    for label, (state, source) in found.items():
        groups.setdefault((state, source), []).append(label)
    parts = []
    for (state, source), labels in groups.items():
        refs = [label if "#" in label else f"#{label}" for label in labels]
        named = refs[0] if len(refs) == 1 else ", ".join(refs[:-1]) + f" and {refs[-1]}"
        noun = "pull request" if len(refs) == 1 else "pull requests"
        parts.append(f"{noun} {named} {state} ({SOURCE_WORDS[source]})")
    reason = "; ".join(parts)
    if len(reason) > 300:
        # A stored reason is capped at 300 characters; a list cut there would
        # read as complete. The evidence names every pull request.
        reason = f"all {len(found)} pull requests it names are merged or closed (each one is in the evidence)"
    return reason


def _close(cur, cid: int, *, rule: str, reason: str, key: str, evidence: dict,
           episode: int | None, sink: dict | None = None) -> bool:
    """Close one item by rule (``commitments.close_by_rule``: the reopen
    guard every automatic close shares), or, with ``sink`` (the clean-up
    planner), only record what would close it and write nothing."""
    if sink is not None:
        sink[cid] = {"reason": reason[:300], "key": key, "evidence": evidence, "episode": episode}
        return True
    return close_by_rule(cur, cid, rule=rule, reason=reason, key=key, evidence=evidence, episode=episode)


def _open_items(cur, project: str | None) -> list[dict[str, Any]]:
    """The open items the rules read, each with its ``owner`` as
    ``commitments.row_owner`` reads it (the owner guard's input)."""
    from khipu.commitments import (
        _future_trigger_ready, _lineage_columns_ready, _reassigned_column_ready, has_future_trigger, row_owner,
    )

    where = "status = 'open' AND project IS NOT NULL"
    params: tuple = ()
    if project is not None:
        where += " AND project = %s"
        params = (project,)
    # The owner columns (migration 0033) come before the close columns (0034).
    extra = ["owner", "origin", "owner_label"]
    if _future_trigger_ready(cur):
        extra.append("future_trigger")
    if _reassigned_column_ready(cur):
        extra.append("owner_reassigned_at")
    if _lineage_columns_ready(cur):
        extra.append("latest_text")
    cur.execute(
        "SELECT id, text, project, kind, opened_episode, opened_at, prs, reopened_rule_keys, pr_check, "
        f"settle_suggestion, {', '.join(extra)} FROM commitments WHERE {where} AND {_not_forgotten_clause(cur)} "
        "ORDER BY id",
        params,
    )
    cols = ("id", "text", "project", "kind", "opened_episode", "opened_at", "prs", "reopened",
            "pr_check", "suggestion", *extra)
    out = []
    for r in cur.fetchall():
        item = dict(zip(cols, r))
        trigger = has_future_trigger(item.get("text") or "") or bool(item.get("future_trigger"))
        item["owner"], item["owner_label"] = row_owner(item, future_trigger=trigger)
        out.append(item)
    return out


def _key(ref: tuple[str | None, int], project_repo: str | None) -> EvidenceKey:
    return (ref[0] or _repo(project_repo), ref[1])


def _newest_for(ref: tuple[str | None, int], project_repo: str | None, opened: int,
                sources: list[dict[EvidenceKey, dict[str, Any]]]) -> dict[str, Any] | None:
    """The newest record any source holds for one PR. A capture's record
    counts only when that capture came after the one that opened the item."""
    want = _key(ref, project_repo)
    best = None
    for source in sources:
        for key, rec in source.items():
            if _key(key, project_repo) != want:
                continue
            if rec.get("episode") is not None and int(rec["episode"]) <= opened:
                continue
            if _newer(rec, best):
                best = rec
    return best


def _pr_rule(cur, items: list[dict], report: dict, *, payload: dict | None, episode_id: int | None,
             github: GitHub, sink: dict | None = None) -> None:
    by_project: dict[str, list[dict]] = {}
    for item in items:
        if item["prs"] or parse_prs(item.get("latest_text") or "") or (item.get("pr_check") or {}).get("refs"):
            by_project.setdefault(item["project"], []).append(item)
    asked_for: set[int] | None = None
    capture: dict[EvidenceKey, dict[str, Any]] = {}
    if payload is not None:
        texts = _payload_texts(payload)
        now = datetime.now(timezone.utc)
        if episode_id is not None:
            capture = {key: _record(state, now, "capture", episode=int(episode_id))
                       for key, state in capture_pr_records(texts).items()}
        # A capture asks GitHub only about the PRs it names; the nightly asks
        # about every open item's.
        asked_for = set(parse_prs("\n".join(texts + _loop_texts(payload.get("open_loops")))))
    for project, rows in by_project.items():
        try:
            runner = _runner_states(project)
        except Exception as exc:  # noqa: BLE001 — a summary that can't be read costs only this source
            _log(f"runner summary skipped ({type(exc).__name__})")
            runner = {}
        ledger = _ledger_states(cur, project)
        later: dict[EvidenceKey, dict[str, Any]] = {}
        if payload is None:
            later = _episode_states(cur, project, min(int(r["opened_episode"] or 0) for r in rows))
        repo = _project_repo(project)
        for item in rows:
            opened = int(item["opened_episode"] or 0)
            before = (item["pr_check"] or {}).get("waiting") or {}
            found: dict[str, tuple[str, str]] = {}
            records: dict[str, dict[str, Any]] = {}
            waiting: dict[str, str] = {}
            capture_episodes: list[int] = []
            for ref in item_refs(item, repo):
                label = ref_label(ref, repo)
                rec = _newest_for(ref, repo, opened, [runner, ledger, capture, later])
                if rec is None or rec["state"] not in DONE_STATES:
                    if asked_for is not None and ref[1] not in asked_for:
                        waiting[label] = before.get(label) or (
                            f"still open ({SOURCE_WORDS[rec['source']]})" if rec else NOT_ASKED)
                        continue
                    state, why = github.state(ref[0] or _repo(repo), ref[1])
                    if state is None:
                        waiting[label] = (f"still open ({SOURCE_WORDS[rec['source']]}); {why}" if rec
                                          else why or "GitHub could not say")
                        continue
                    rec = _record(state, datetime.now(timezone.utc), "github")
                    if state not in DONE_STATES:
                        waiting[label] = "still open on GitHub"
                        continue
                found[label] = (rec["state"], rec["source"])
                records[label] = {k: v for k, v in rec.items() if k != "episode"}
                if rec.get("episode") is not None:
                    capture_episodes.append(int(rec["episode"]))
            if not waiting and found:
                key = "pr:" + ",".join(f"{label}={state}" for label, (state, _) in found.items())
                if key in (item["reopened"] or []):
                    report["kept_open_reopened"] += 1
                    continue
                evidence = {"rule": "pr", "prs": records}
                if _close(cur, item["id"], rule="pr", reason=_pr_reason(found),
                          key=key, evidence=evidence,
                          episode=max(capture_episodes) if capture_episodes else None, sink=sink):
                    report["closed_pr"] += 1
                    item["closed"] = True
                elif sink is None:
                    report["kept_open_reopened"] += 1
                continue
            check = {"states": {label: f"{s} ({SOURCE_WORDS[src]})" for label, (s, src) in found.items()},
                     "waiting": waiting}
            if (item["pr_check"] or {}).get("refs"):
                check["refs"] = item["pr_check"]["refs"]
            if sink is None and check != item["pr_check"]:
                cur.execute("UPDATE commitments SET pr_check = %s::jsonb WHERE id = %s",
                            (json.dumps(check), item["id"]))
            suggestion = item.get("suggestion") if isinstance(item.get("suggestion"), dict) else {}
            if sink is None and suggestion.get("looks_done") and suggestion.get("rule") == "pr":
                # A PR the owner's item names is not finished any more (reopened,
                # or added by a restatement): it no longer looks done.
                cur.execute("UPDATE commitments SET settle_suggestion = NULL WHERE id = %s AND status = 'open'",
                            (item["id"],))


def _is_decide_item(item: dict) -> bool:
    return item["kind"] == "question" or bool(_DECIDE_RE.search(item["text"] or ""))


def _later(item: dict, decision_episode: int | None, decided_at: Any) -> bool:
    """A decision counts only from a capture after the one that opened the
    item: by episode order when both are known, else by time."""
    opened = item["opened_episode"]
    if decision_episode is not None and opened is not None:
        return int(decision_episode) > int(opened)
    return decided_at is not None and decided_at > item["opened_at"]


def _settles(item: dict, decisions: list[tuple], threshold: float):
    """``("strict", did, text, how, score, episode)`` for the best strict
    match, else ``("loose", did, text, score)`` for the best looser one, else
    None."""
    loose = None
    strict = None
    for did, text, dep, decided_at in decisions:
        if not _later(item, dep, decided_at):
            continue
        text = text or ""
        if evidence_problem(text, item["text"], item.get("project")):
            continue
        if any(int(m.group(1)) == item["id"] for m in _NAMES_ITEM_RE.finditer(text)):
            return ("strict", did, text, "names the item", 1.0, dep)
        score, via, _ = completion_match(text, item["text"], kind="loop", threshold=threshold)
        if via:
            if strict is None or score > strict[4]:
                strict = ("strict", did, text, via, score, dep)
            continue
        near = max(_jaccard(text, item["text"]), _match_score(text, item["text"]) if len(text.split()) >= 3
                   else 0.0)
        if near >= LOOSE_SETTLE_MIN_SCORE and (loose is None or near > loose[3]):
            loose = ("loose", did, text, near)
    return strict or loose


def _decision_rule(cur, items: list[dict], report: dict, *, episode_id: int | None,
                   sink: dict | None = None) -> None:
    from khipu.config import float_setting
    from khipu.db import has_columns

    eligible = [i for i in items if not i.get("closed") and _is_decide_item(i)]
    if not eligible:
        return
    threshold = float_setting("commitment_close_similarity")
    live = "d.superseded_by IS NULL"
    if has_columns(cur, "decisions", "retracted_at"):
        live += " AND d.retracted_at IS NULL"
    by_project: dict[str, list[dict]] = {}
    for item in eligible:
        by_project.setdefault(item["project"], []).append(item)
    for project, rows in by_project.items():
        # A capture settles with its own decisions; the nightly with all of
        # the project's since its oldest open item.
        scope = " AND d.episode_id = %s" if episode_id is not None else ""
        params = (project, episode_id) if episode_id is not None else (project,)
        cur.execute(
            f"SELECT d.id, d.text, d.episode_id, d.decided_at FROM decisions d WHERE d.project = %s{scope} "
            f"AND {live} AND d.decided_at >= %s ORDER BY d.id",
            (*params, min(r["opened_at"] for r in rows)),
        )
        decisions = cur.fetchall()
        if not decisions:
            continue
        for item in rows:
            hit = _settles(item, decisions, threshold)
            if hit is None:
                continue
            if hit[0] == "strict":
                _, did, text, how, score, dep = hit
                key = f"decision:{did}"
                if key in (item["reopened"] or []):
                    report["kept_open_reopened"] += 1
                    continue
                # The owner's own item is refused under the row lock and kept as
                # "Looks done" for him to close (commitments.close_by_rule).
                if _close(cur, item["id"], rule="decision",
                          reason=f"settled by decision {did}: {text[:150]!r}", key=key,
                          evidence={"rule": "decision", "decision_id": did, "match": how,
                                    "score": round(score, 3)}, episode=dep, sink=sink):
                    report["closed_decision"] += 1
                continue
            _, did, text, near = hit
            if sink is not None:
                continue
            old = item["suggestion"] if isinstance(item["suggestion"], dict) else {}
            if old.get("looks_done") or old.get("decision_id") == did or float(old.get("score") or 0) > near:
                continue
            cur.execute("UPDATE commitments SET settle_suggestion = %s::jsonb WHERE id = %s",
                        (json.dumps({"decision_id": did, "text": text[:200], "score": round(near, 3)}),
                         item["id"]))
            report["suggested"] += 1


def run(cur, *, payload: dict[str, Any] | None = None, episode_id: int | None = None) -> dict[str, Any]:
    """Run the PR and decision rules. With ``payload`` (a capture), over its
    project's open items with that capture as the later evidence; without,
    over every open item (the nightly). Returns the counts and, when GitHub
    could not be asked, why."""
    if not ready(cur):
        return {"ok": True, "skipped": "the hub has no close-rule columns yet (migration 0034)"}
    project = None
    if payload is not None:
        project = str(payload.get("project") or "") or None
        if project is None:
            return {"ok": True, "skipped": "a capture with no project"}
    report: dict[str, Any] = {"ok": True, "closed_pr": 0, "closed_decision": 0, "suggested": 0,
                              "kept_open_reopened": 0}
    items = _open_items(cur, project)
    github = GitHub()
    _pr_rule(cur, items, report, payload=payload, episode_id=episode_id, github=github)
    _decision_rule(cur, items, report, episode_id=episode_id if payload is not None else None)
    report["github_asked"] = github.asked
    report["github"] = github.unavailable
    if report["closed_pr"] or report["closed_decision"]:
        _log(f"closed {report['closed_pr']} by pull request, {report['closed_decision']} by decision")
    return report


def plan_closes(cur, items: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """What the PR and decision rules would close among ``items`` (rows as
    ``_open_items`` gives them), as ``{id: {reason, key, evidence, episode}}``,
    writing nothing: the clean-up planner's read-only view of the nightly run
    (``khipu.owed_tidy``). Items whose ``prs`` is not parsed yet are parsed
    here, in memory."""
    for item in items:
        if item.get("prs") is None:
            item["prs"] = parse_prs(item.get("text") or "")
    report: dict[str, Any] = {"closed_pr": 0, "closed_decision": 0, "suggested": 0, "kept_open_reopened": 0}
    sink: dict[int, dict[str, Any]] = {}
    _pr_rule(cur, items, report, payload=None, episode_id=None, github=GitHub(), sink=sink)
    _decision_rule(cur, items, report, episode_id=None, sink=sink)
    return sink
