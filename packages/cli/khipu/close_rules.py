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
   summary say "PR #N merged" or "closed #N" (:func:`capture_pr_states`; "will
   merge" and "before #N merges" do not count);
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

Every close records ``close_reason``, ``closed_by`` (``BY_RULE``),
``close_rule_key`` and ``close_evidence``. A reopened item keeps its rule key
in ``reopened_rule_keys``; the same rule and evidence never close it again.
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
    BY_RULE,
    _close_columns_ready,
    _jaccard,
    _match_score,
    _not_forgotten_clause,
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
_PR_LIST_RE = re.compile(rf"\b{_PR_WORD}\b([^.;:\n]{{0,60}})", re.I)
_HASH_RE = re.compile(r"(?<![\w&])#(\d{1,7})\b")
_PULL_LINK_RE = re.compile(r"github\.com/([A-Za-z0-9][\w.-]*/[\w.-]+?)/pull/(\d{1,7})\b", re.I)
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
# The narrow "it happened" pattern of a later capture: past tense only, and
# nothing in the words just before it that makes it a plan or a negation.
_PR_REF = rf"(?:{_PR_WORD}\s*#?\s*|#)(\d{{1,7}})"
_DONE_AFTER_RE = re.compile(rf"{_PR_REF}\s+(?:(?:was|is|got|has\s+been|have\s+been)\s+)?(merged|closed)\b",
                            re.I)
_DONE_BEFORE_RE = re.compile(rf"\b(merged|closed)\s+{_PR_REF}", re.I)
_NOT_YET_RE = re.compile(r"\b(?:not|never|will|would|should|could|can|to|before|until|once|after|if|when|"
                         r"unless|yet|planned|plan|going)\b|n't\b", re.I)
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


def _link_repos(text: str) -> dict[int, str]:
    """``{pr: "owner/repo"}`` for each GitHub pull link in ``text``."""
    return {int(m.group(2)): m.group(1) for m in _PULL_LINK_RE.finditer(str(text or ""))}


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


def capture_pr_states(texts) -> dict[int, str]:
    """``{pr: "merged" | "closed"}`` from a capture's own lines: "PR #366
    merged", "PR #366 was merged", "closed #367", "merged PR #368". A line
    that says it will happen, should happen, or has not ("will merge #366",
    "before #366 merges", "not merged PR #366") does not count."""
    out: dict[int, str] = {}
    for raw in texts or []:
        text = str(raw or "")
        hits = [(m.start(), int(m.group(1)), m.group(2)) for m in _DONE_AFTER_RE.finditer(text)]
        hits += [(m.start(), int(m.group(2)), m.group(1)) for m in _DONE_BEFORE_RE.finditer(text)]
        for start, number, state in sorted(hits):
            if not _NOT_YET_RE.search(text[max(0, start - 30):start]):
                out.setdefault(number, state.lower())
    return out


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


def _runner_states(project: str) -> dict[int, str]:
    """``{pr: "merged" | "closed"}`` from the project's registered runner
    summary, read through ``khipu.work`` (the same checks as the Work view)."""
    from khipu import work

    cfg = work.settings()
    entry = next((e for e in cfg["work_projects"] if e["project"] == project), None)
    if entry is None:
        return {}
    contents = work.read_source(entry, cfg, datetime.now(timezone.utc)).get("contents") or {}
    out: dict[int, str] = {}
    for pr in contents.get("prs") or []:
        n = _pr_id(pr)
        if n is not None and pr.get("stage") in DONE_STATES:
            out[n] = pr["stage"]
    return out


def _ledger_states(cur, project: str) -> dict[int, str]:
    """``{pr: "merged"}`` from ledger rows whose ``refs.pr`` names a PR in a
    state that means done (the vocab's ``done``, or a merged word)."""
    cur.execute("SAVEPOINT close_rules_ledger")
    try:
        cur.execute(
            "SELECT r.refs->>'pr', r.state, v.meaning FROM ledger_rows r "
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
    out: dict[int, str] = {}
    for ref, state, meaning in rows:
        m = re.fullmatch(r"\s*(?:pr[-_ ]?|#)?(\d{1,7})\s*", str(ref or ""), re.I)
        word = str(state or "").upper()
        merged = "MERGED" in word and "UNMERGED" not in word
        if m and (meaning == "done" or merged):
            out[int(m.group(1))] = "merged"
    return out


def _episode_states(cur, project: str, after_episode: int) -> dict[int, tuple[str, int]]:
    """``{pr: (state, episode_id)}`` from the project's captures after
    ``after_episode`` (the nightly's view of source 3), earliest first."""
    cur.execute(
        "SELECT e.id, e.summary, e.raw->'decisions', e.raw->'closed_loops' FROM episodes e "
        "WHERE e.project = %s AND e.id > %s "
        "AND (e.summary ~* '(merged|closed)' OR (e.raw->'decisions')::text ~* '(merged|closed)' "
        "OR (e.raw->'closed_loops')::text ~* '(merged|closed)') ORDER BY e.id",
        (project, after_episode),
    )
    out: dict[int, tuple[str, int]] = {}
    for eid, summary, decisions, loops in cur.fetchall():
        texts = [summary or ""]
        for blob in (decisions, loops):
            items = json.loads(blob) if isinstance(blob, str) else blob
            texts += _loop_texts(items if isinstance(items, list) else [])
        for n, state in capture_pr_states(texts).items():
            out.setdefault(n, (state, int(eid)))
    return out


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


# ---- the rules --------------------------------------------------------------


def _pr_reason(found: dict[int, tuple[str, str]]) -> str:
    """'pull request #366 merged (from the runner's summary)', grouped by
    state and source in the item's PR order."""
    groups: dict[tuple[str, str], list[int]] = {}
    for n, (state, source) in found.items():
        groups.setdefault((state, source), []).append(n)
    parts = []
    for (state, source), numbers in groups.items():
        refs = [f"#{n}" for n in numbers]
        named = refs[0] if len(refs) == 1 else ", ".join(refs[:-1]) + f" and {refs[-1]}"
        noun = "pull request" if len(refs) == 1 else "pull requests"
        parts.append(f"{noun} {named} {state} ({SOURCE_WORDS[source]})")
    return "; ".join(parts)


def _close(cur, cid: int, *, reason: str, key: str, evidence: dict, episode: int | None,
           sink: dict | None = None) -> bool:
    """Close one item by rule, or, with ``sink`` (the clean-up planner), only
    record what would close it and write nothing."""
    if sink is not None:
        sink[cid] = {"reason": reason[:300], "key": key, "evidence": evidence, "episode": episode}
        return True
    cur.execute(
        "UPDATE commitments SET status = 'closed', closed_at = now(), closed_episode = %s, "
        "close_reason = %s, closed_by = %s, close_rule_key = %s, close_evidence = %s::jsonb "
        "WHERE id = %s AND status = 'open' AND NOT (%s = ANY(reopened_rule_keys)) "
        f"AND {_not_forgotten_clause(cur)}",
        (episode, reason[:300], BY_RULE, key, json.dumps(evidence), cid, key),
    )
    return cur.rowcount > 0


def _open_items(cur, project: str | None) -> list[dict[str, Any]]:
    where = "status = 'open' AND project IS NOT NULL"
    params: tuple = ()
    if project is not None:
        where += " AND project = %s"
        params = (project,)
    cur.execute(
        "SELECT id, text, project, kind, opened_episode, opened_at, prs, reopened_rule_keys, pr_check, "
        f"settle_suggestion FROM commitments WHERE {where} AND {_not_forgotten_clause(cur)} ORDER BY id",
        params,
    )
    cols = ("id", "text", "project", "kind", "opened_episode", "opened_at", "prs", "reopened",
            "pr_check", "suggestion")
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _pr_rule(cur, items: list[dict], report: dict, *, payload: dict | None, episode_id: int | None,
             github: GitHub, sink: dict | None = None) -> None:
    by_project: dict[str, list[dict]] = {}
    for item in items:
        if item["prs"]:
            by_project.setdefault(item["project"], []).append(item)
    asked_for: set[int] | None = None
    capture: dict[int, str] = {}
    if payload is not None:
        texts = _payload_texts(payload)
        capture = capture_pr_states(texts)
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
        later: dict[int, tuple[str, int]] = {}
        if payload is None:
            later = _episode_states(cur, project, min(int(r["opened_episode"] or 0) for r in rows))
        repo = project if _REPO_RE.match(project) else None
        for item in rows:
            opened = int(item["opened_episode"] or 0)
            before = (item["pr_check"] or {}).get("waiting") or {}
            found: dict[int, tuple[str, str]] = {}
            waiting: dict[str, str] = {}
            capture_episodes: list[int] = []
            links = _link_repos(item["text"])
            for n in item["prs"]:
                if n in runner:
                    found[n] = (runner[n], "runner")
                elif n in ledger:
                    found[n] = (ledger[n], "ledger")
                elif n in capture and episode_id is not None and episode_id > opened:
                    found[n] = (capture[n], "capture")
                    capture_episodes.append(episode_id)
                elif n in later and later[n][1] > opened:
                    found[n] = (later[n][0], "capture")
                    capture_episodes.append(later[n][1])
                elif asked_for is not None and n not in asked_for:
                    waiting[str(n)] = before.get(str(n)) or NOT_ASKED
                else:
                    state, why = github.state(links.get(n) or repo, n)
                    if state in DONE_STATES:
                        found[n] = (state, "github")
                    else:
                        waiting[str(n)] = why or "still open on GitHub"
            if not waiting:
                key = "pr:" + ",".join(f"{n}={found[n][0]}" for n in item["prs"])
                if key in (item["reopened"] or []):
                    report["kept_open_reopened"] += 1
                    continue
                evidence = {"rule": "pr", "prs": {str(n): {"state": s, "source": src}
                                                  for n, (s, src) in found.items()}}
                if _close(cur, item["id"], reason=_pr_reason(found), key=key, evidence=evidence,
                          episode=max(capture_episodes) if capture_episodes else None, sink=sink):
                    report["closed_pr"] += 1
                    item["closed"] = True
                continue
            check = {"states": {str(n): f"{s} ({SOURCE_WORDS[src]})" for n, (s, src) in found.items()},
                     "waiting": waiting}
            if sink is None and check != item["pr_check"]:
                cur.execute("UPDATE commitments SET pr_check = %s::jsonb WHERE id = %s",
                            (json.dumps(check), item["id"]))


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
                if _close(cur, item["id"], reason=f"settled by decision {did}: {text[:150]!r}", key=key,
                          evidence={"rule": "decision", "decision_id": did, "match": how,
                                    "score": round(score, 3)}, episode=dep, sink=sink):
                    report["closed_decision"] += 1
                continue
            _, did, text, near = hit
            if sink is not None:
                continue
            old = item["suggestion"] if isinstance(item["suggestion"], dict) else {}
            if old.get("decision_id") == did or float(old.get("score") or 0) > near:
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
