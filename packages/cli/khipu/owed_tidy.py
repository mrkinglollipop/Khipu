"""The one-time clean-up of a project's Owed list (scope
docs/plans/2026-10-16-owed-and-ledger-ssot.md, design (d), slice 4, with the
owner's decision 4).

``khipu owed tidy --project P`` plans, and the plan writes nothing. For each
open item, cheapest first, the way ``hygiene.run_commitments_hygiene`` orders
its work:

1. **close** when every pull request it names is merged or closed, or a later
   decision settles it: the nightly's own rules (``close_rules.plan_closes``),
   read-only;
2. **close** as a duplicate of an older item that stays open
   (``hygiene._duplicate_groups``: token containment, then stored cosine);
3. **reassign** when the owner rules (``commitments.resolve_owner_and_label``),
   re-run on the stored text and the origin, give another owner than the one
   shown today. The origin is the row's, or ``job`` when the capture that
   opened it has a parent session (the lineage a pre-0033 row never stored);
4. a **model pass** over the items no owner rule decided (the capture's
   declared owner, or no one named), never the user's own and never an item
   that names the user by any alias, in batches of ``JUDGE_BATCH``, at most
   ``owed_tidy_calls`` calls (a count setting, 0 for no limit). Past the cap
   an item is kept as it is and marked ``unjudged``. The model only moves an
   item to another owner; it never closes one. A failed or unreadable answer
   keeps the item as it is.

Everything else is **keep**. Nothing is ever deleted: a close keeps the row,
its text and its reason.

**The owner's own items** (``commitments.owner_guard``): the clean-up only
proposes a change to an item the user owns, by the owner rules read with the
item's lineage (a job's item shown as the user's is the orchestrator's, as
the rules of step 3 say, so it moves). Its action stays ``keep`` and the
change the rules would make is kept under ``proposed``; Apply never makes it.
The one exception is a close because every pull request the item itself
names is merged or closed.

**Applying** a plan (``apply``) writes every change in one transaction, under
one ``tidy_batch`` id, with each item's reason and its state before
(``commitment_tidy_items``, migration 0036). An item that changed after the
plan was made (another fingerprint, or no longer open) is skipped and
listed. A plan that changes any of the user's own items passes the owner gate
(``commitments.bulk_owner_gate``) first, or nothing is written.

**Undo** (``undo``) restores owner, label, origin, status and the close
fields of every item in the batch that has not changed since the apply; an
item changed since is left alone and listed. A close by a PR or decision
rule that is undone is remembered in ``reopened_rule_keys``, so the nightly
never closes it again by the same evidence. Undo passes the gate too when it
touches the user's items.

**The plan document** (what the app's preview reads, slice 5)::

    {
      "format": "khipu-owed-tidy-plan", "version": 1,
      "project": "owner/repo", "planned_at": "<hub time, ISO>",
      "totals": {
        "open": N,                      # = stay_user + keep_other + move.total + close.total
        "stay_user": n, "keep_other": n,
        "move": {"total": n, "agent": n, "assistant": n, "user": n},
        "close": {"total": n, "pr": n, "decision": n, "duplicate": n}
      },
      "model": {"used": bool, "calls": n, "cap": n (0 = no limit), "candidates": n, "unjudged": n},
      "samples": {"stay_user" | "keep_other" | "move" | "close_pr" | "close_decision"
                  | "close_duplicate": [{"id", "kind", "text", "reason"}, ... at most 20]},
      "notes": ["why a rule could not run", ...],
      "items": [{
        "id": n, "kind": "question", "text": "...",
        "action": "keep" | "reassign" | "close",
        "close": "pr" | "decision" | "duplicate",     # with action close
        "reason": "plain words", "rule": "which rule decided",
        "evidence": {...},                            # what the rule saw
        "key": "pr:366=merged",                       # the close rule key, with action close
        "episode": n | null,                          # the capture that showed it, with action close
        "before": {"owner", "owner_label", "origin"}, # as shown today
        "after": {"owner", "owner_label", "origin"},  # with action reassign
        "unjudged": true,                             # past the model call cap
        "proposed": {"action", "close", "after", "reason", "rule"},  # the user's item: never applied
        "fingerprint": "sha256 of the item's state when planned"
      }, ...]
    }

A reader must check ``format`` and ``version``; a later version adds fields
and never changes these.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
from datetime import datetime, timezone
from typing import Any

PLAN_FORMAT = "khipu-owed-tidy-plan"
PLAN_VERSION = 1
CALLS_SETTING = "owed_tidy_calls"
DEFAULT_CALLS = 25
JUDGE_BATCH = 40
SAMPLES = 20
GROUPS = ("stay_user", "keep_other", "move", "close_pr", "close_decision", "close_duplicate", "proposed")
# The rules the model pass may stand in for: no owner rule decided the item.
_UNDECIDED_RULES = ("the owner the capture gave it", "no one named: the assistant's")

# The item's state that an apply checks has not changed since planning and an
# undo since applying, and the part of it an undo restores.
FINGERPRINT_FIELDS = ("status", "owner", "owner_label", "origin", "owner_reassigned_at", "closed_at",
                      "closed_episode", "close_reason", "closed_by", "close_rule_key", "close_evidence",
                      "tidy_batch", "text", "kind", "due_after")
# Who set the owner by hand, why, and the owner before (migration 0037):
# kept with each change and restored by an undo, never part of the fingerprint.
LINEAGE_FIELDS = ("owner_reassigned_by", "owner_reassign_reason", "owner_prior", "owner_prior_label")
PRIOR_FIELDS = (*FINGERPRINT_FIELDS[:12], *LINEAGE_FIELDS)
_TIMESTAMPS = ("owner_reassigned_at", "closed_at")
NOT_READY = ("This hub has no clean-up tables yet (migrations 0033 to 0036). Planning works; applying and "
             "undoing need the migrations. Nothing was changed.")


def _log(msg: str) -> None:
    print(f"[khipu-owed-tidy] {msg}", file=sys.stderr)


# ---- reading ----------------------------------------------------------------


def _select(cur, project: str, *, ids: list[int] | None = None, lock: bool = False) -> dict[int, dict]:
    """``{id: row}`` for the project's open items (or for ``ids``, any
    status), with the opening capture's parent session as ``lineage``.
    Columns a pre-migration hub lacks read as None."""
    from khipu.commitments import _not_forgotten_clause
    from khipu.db import has_columns, table_columns

    have = table_columns(cur, "commitments")
    wanted = ("id", "project", "opened_episode", "opened_at", "future_trigger", *FINGERPRINT_FIELDS,
              *LINEAGE_FIELDS)
    cols = [c for c in wanted if c in have]
    lineage = "NULL"
    if has_columns(cur, "episodes", "parent_session_id"):
        lineage = ("(SELECT NULLIF(e.parent_session_id, '') FROM episodes e "
                   "WHERE e.id = commitments.opened_episode)")
    where = "project IS NOT DISTINCT FROM %s AND " + ("id = ANY(%s)" if ids is not None else "status = 'open'")
    params: tuple = (project, list(ids)) if ids is not None else (project,)
    cur.execute(f"SELECT {', '.join(cols)}, {lineage} FROM commitments WHERE {where} "
                f"AND {_not_forgotten_clause(cur)} ORDER BY id{' FOR UPDATE' if lock else ''}", params)
    out = {}
    for raw in cur.fetchall():
        row = {c: None for c in wanted}
        row.update(zip(cols, raw[:-1]))
        row["lineage"] = raw[-1]
        out[int(row["id"])] = row
    return out


def _plain(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


def fingerprint(row: dict[str, Any]) -> str:
    """A hash of the item's state (``FINGERPRINT_FIELDS``)."""
    state = {f: _plain(row.get(f)) for f in FINGERPRINT_FIELDS}
    return hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()


def _names_user(text: str) -> bool:
    """True when the text names the user by any alias (the generic words
    'the user', 'you' and 'owner' included): such an item never reaches the
    model."""
    from khipu.commitments import user_aliases

    names = sorted({re.escape(a) for a in user_aliases() if a}, key=len, reverse=True)
    return bool(names) and re.search(rf"\b(?:{'|'.join(names)})\b", text or "", re.I) is not None


# ---- the owner rules --------------------------------------------------------


def _owner_rule(row: dict[str, Any], origin: str, trigger: bool) -> str:
    """Which owner rule decides this row, in plain words (the order of
    ``commitments.resolve_owner_and_label``)."""
    from khipu import commitments as co

    text = row.get("text") or ""
    job = origin == co.ORIGIN_JOB
    label = co.agent_actor(co._without_trigger_clause(text) if trigger else text) if job else None
    if label:
        return f"a job's item naming the {label} as the one to act"
    if co._user_signal(text, kind=row.get("kind"), future_trigger=trigger):
        if job:
            return "a job's question or ask for a decision is the orchestrator's (a job's user is its dispatcher)"
        return "names the user as the one to act, or is a question or decision for them"
    if not trigger and co._declared(row.get("owner")):
        if job and co._declared(row.get("owner")) == co.OWNER_USER:
            return "a job's item for its user is the orchestrator's"
        return "the owner the capture gave it"
    return "no one named: the assistant's"


def _owners(row: dict[str, Any]) -> tuple[dict, dict, str]:
    """``(before, after, rule)``: the owner shown today, the owner the rules
    give with the origin the lineage shows, and the rule that decided."""
    from khipu import commitments as co

    trigger = co.has_future_trigger(row.get("text") or "") or bool(row.get("future_trigger"))
    stored_origin = row.get("origin") or co.ORIGIN_CONVERSATION
    owner, label = co.row_owner(row, future_trigger=trigger)
    before = {"owner": owner, "owner_label": label, "origin": stored_origin}
    if str(row.get("owner") or "").lower() == co.OWNER_AGENT:
        return before, dict(before), "the orchestrator's or a third party's, as stored"
    if row.get("owner_reassigned_at") and row.get("owner") in (co.OWNER_USER, co.OWNER_ASSISTANT):
        return before, dict(before), "set by hand"
    origin = co.ORIGIN_JOB if (stored_origin == co.ORIGIN_JOB or row.get("lineage")) else stored_origin
    new_owner, new_label = co.resolve_owner_and_label(
        row.get("text") or "", kind=row.get("kind"), declared=row.get("owner"), future_trigger=trigger,
        origin=origin, declared_label=row.get("owner_label"))
    after = {"owner": new_owner, "owner_label": new_label, "origin": origin}
    rule = _owner_rule(row, origin, trigger)
    if origin != stored_origin:
        rule += "; the capture that opened it was a dispatched job's"
    return before, after, rule


# ---- the model pass ---------------------------------------------------------


def _owner_judge_prompt(texts: list[str]) -> str:
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(texts))
    return (
        "You are sorting a project's open obligations kept by an assistant's memory. In this project "
        "an orchestrator conversation dispatches jobs, and a job's questions and asks were often stored "
        "as the person's own. For each numbered item decide who must act on it:\n"
        "- \"user\": the person who owns this memory must do, decide, answer or approve it;\n"
        "- \"assistant\": the AI assistant working with them must do it;\n"
        "- \"agent\": a third party must: the orchestrator, a reviewer, a builder, a job, a PR author "
        "(give a short label).\n"
        "When unsure, answer \"user\".\n\n"
        'Output ONLY a JSON object: {"owners": [{"i": <index>, "owner": "user"|"assistant"|"agent", '
        '"label": "<2 words, for agent>", "reason": "<8 words max>"}]} with one entry per item.\n\n'
        f"Items:\n{numbered}\n"
    )


def _model_owner_judge(texts: list[str]) -> list[dict[str, Any]]:
    """One batch through the summariser model (``khipu.extract._generate``,
    the routing ``hygiene._model_judge`` uses), as exactly one provider
    attempt: one unit of ``owed_tidy_calls``. A retry is the plan's, and
    costs another unit. Fail-open: a failed call (marked ``failed``) or an
    unreadable answer keeps every item as the user's."""
    from khipu.extract import _generate, parse_model_json

    out: list[dict[str, Any]] = [{"owner": "user", "reason": "model returned no answer"} for _ in texts]
    try:
        raw = _generate(_owner_judge_prompt(texts), timeout=90, retries=0, caller="hygiene")
    except Exception as exc:  # noqa: BLE001 — keep everything on a failed call
        return [{"owner": "user", "reason": f"model unavailable: {type(exc).__name__}", "failed": True}
                for _ in texts]
    parsed = parse_model_json(raw) or {}
    for item in parsed.get("owners") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(out):
            out[idx] = {"owner": str(item.get("owner") or "user").strip().lower(),
                        "label": item.get("label"),
                        "reason": str(item.get("reason") or "").strip()[:120] or "model answer"}
    return out


class _Calls:
    """The model budget for one plan (``owed_tidy_calls``, 0 = no limit), in
    provider attempts: every model call here makes one attempt, and a retry
    takes another unit."""

    def __init__(self, cap: int) -> None:
        self.cap, self.used = cap, 0

    def take(self) -> bool:
        if self.cap and self.used >= self.cap:
            return False
        self.used += 1
        return True


# ---- the plan ---------------------------------------------------------------


def plan(cur, project: str, *, judge=None, use_model: bool = True) -> dict[str, Any]:
    """The clean-up plan for ``project`` (see the module docstring). Runs in
    a transaction that is always rolled back, so it writes nothing."""
    import psycopg

    out: dict[str, Any] = {}
    with cur.connection.transaction():
        out = _plan(cur, project, judge=judge, use_model=use_model)
        raise psycopg.Rollback()
    return out


def _plan(cur, project: str, *, judge, use_model: bool) -> dict[str, Any]:
    from khipu import close_rules, hygiene
    from khipu import commitments as co
    from khipu.config import count_setting

    judge = judge or _model_owner_judge
    calls = _Calls(count_setting(CALLS_SETTING))
    cur.execute("SELECT now()")
    planned_at = cur.fetchone()[0]
    rows = _select(cur, project)
    notes: list[str] = []
    items: dict[int, dict[str, Any]] = {}
    for cid, row in rows.items():
        before, after, rule = _owners(row)
        items[cid] = {"id": cid, "kind": row.get("kind"), "text": row.get("text"), "action": "keep",
                      "reason": "", "rule": rule, "before": before, "fingerprint": fingerprint(row),
                      "_after": after, "_owner": after["owner"]}

    # 1. The PR and decision rules, read-only.
    if close_rules.ready(cur):
        cur.execute("SAVEPOINT owed_tidy_rules")
        try:
            closes = close_rules.plan_closes(cur, close_rules._open_items(cur, project))
            cur.execute("RELEASE SAVEPOINT owed_tidy_rules")
        except Exception as exc:  # noqa: BLE001 — the other rules still run
            cur.execute("ROLLBACK TO SAVEPOINT owed_tidy_rules")
            closes = {}
            notes.append(f"the pull request and decision rules could not run ({type(exc).__name__})")
        for cid, hit in closes.items():
            if cid in items:
                kind = "pr" if hit["key"].startswith("pr:") else "decision"
                items[cid].update(action="close", close=kind, reason=hit["reason"], rule=kind,
                                  evidence=hit["evidence"], key=hit["key"], episode=hit["episode"])
    else:
        notes.append("the pull request and decision rules need migration 0034; none ran")

    # 2. Duplicates of an older item that stays open.
    remaining = [{"id": cid, "project": project, "text": it["text"] or "",
                  "owner": it["_after"]["owner"], "owner_label": it["_after"]["owner_label"]}
                 for cid, it in items.items() if it["action"] == "keep"]
    by_text = {r["id"]: r["text"] for r in remaining}

    def _pair_scores(ids: list[int]) -> dict[tuple[int, int], float]:
        cur.execute("SAVEPOINT owed_tidy_cosine")
        scores = hygiene._commitment_pair_scores(cur, ids)
        cur.execute("ROLLBACK TO SAVEPOINT owed_tidy_cosine" if not scores else
                    "RELEASE SAVEPOINT owed_tidy_cosine")
        return scores

    def _adjudicate(pairs: list[tuple[str, str]]) -> list[bool]:
        if not use_model:
            return [False] * len(pairs)
        allowed = [i for i, (a, b) in enumerate(pairs) if not (_names_user(a) or _names_user(b))]
        answers = [False] * len(pairs)
        if allowed and calls.take():
            got = hygiene._model_adjudicate_pairs([pairs[i] for i in allowed], retries=0)
            for i, same in zip(allowed, got):
                answers[i] = bool(same)
        return answers

    for dup, keeper in hygiene._duplicate_groups(remaining, pair_scores=_pair_scores,
                                                 adjudicate=_adjudicate).items():
        items[dup].update(action="close", close="duplicate", rule="duplicate",
                          reason=f"duplicate of #{keeper}, which stays open",
                          evidence={"rule": "duplicate", "duplicate_of": keeper,
                                    "keeper_text": (by_text.get(keeper) or "")[:200]},
                          key=f"duplicate:{keeper}", episode=None)

    # 3. The owner rules, for what stays open.
    for it in items.values():
        after = it.pop("_after")
        if it["action"] != "keep":
            continue
        if (after["owner"], after["owner_label"]) != (it["before"]["owner"], it["before"]["owner_label"]):
            it.update(action="reassign", after=after, reason=f"to the {_owner_words(after)}: {it['rule']}")

    # 4. The model, for what no owner rule decided: never the user's own item
    # (it never overrides the deterministic owner) and never one naming him.
    candidates = [it for it in items.values()
                  if it["action"] == "keep" and co.OWNER_USER not in (it["before"]["owner"], it["_owner"])
                  and it["rule"] in _UNDECIDED_RULES and not _names_user(it["text"] or "")]
    unjudged = 0
    if use_model:
        for start in range(0, len(candidates), JUDGE_BATCH):
            batch = candidates[start:start + JUDGE_BATCH]
            if not calls.take():
                for it in batch:
                    it["unjudged"] = True
                unjudged += len(batch)
                continue
            texts = [it["text"] or "" for it in batch]
            answers = list(judge(texts))
            if answers and all((a or {}).get("failed") for a in answers) and calls.take():
                answers = list(judge(texts))
            answers += [{"owner": "user"}] * (len(batch) - len(answers))
            for it, ans in zip(batch, answers):
                owner = str((ans or {}).get("owner") or "user").lower()
                if owner not in (co.OWNER_ASSISTANT, co.OWNER_AGENT) or co.owner_guard(it["_owner"], "model"):
                    continue
                label = (co.clean_owner_label(ans.get("label")) or co.ORCHESTRATOR) if owner == co.OWNER_AGENT \
                    else None
                after = {"owner": owner, "owner_label": label, "origin": it["before"]["origin"]}
                if (after["owner"], after["owner_label"]) == (it["before"]["owner"], it["before"]["owner_label"]):
                    continue
                it.update(action="reassign", after=after,
                          rule=f"model: {(ans.get('reason') or 'not the user').strip()}",
                          reason=f"to the {_owner_words(after)}: the model's reading, no rule decided it")

    # 5. The owner's own items: every change but a close on its own pull
    # requests is a proposal, never applied (commitments.owner_guard).
    for it in items.values():
        if it["action"] == "keep" or not co.owner_guard(it.pop("_owner"), _rule_of(it)):
            it.pop("_owner", None)
            continue
        it["proposed"] = {k: it[k] for k in ("action", "close", "after", "reason", "rule") if k in it}
        for k in ("close", "after", "evidence", "key", "episode"):
            it.pop(k, None)
        it.update(action="keep", reason=f"suggested, yours to decide: {it['proposed']['reason']}")

    return _document(project, planned_at, items, calls=calls, used=use_model,
                     candidates=len(candidates), unjudged=unjudged, notes=notes)


def _rule_of(it: dict) -> str:
    """The automatic rule (``commitments.AUTOMATIC_RULES``) behind a planned
    change."""
    if it["action"] == "close":
        return it.get("close") or "duplicate"
    return "model" if str(it.get("rule") or "").startswith("model:") else "owner-rules"


def _owner_words(owner: dict) -> str:
    if owner["owner"] == "agent":
        return owner.get("owner_label") or "orchestrator"
    return owner["owner"]


def _group(it: dict) -> str:
    if it["action"] == "close":
        return f"close_{it['close']}"
    if it["action"] == "reassign":
        return "move"
    if it.get("proposed"):
        return "proposed"
    return "stay_user" if it["before"]["owner"] == "user" else "keep_other"


def _document(project, planned_at, items, *, calls, used, candidates, unjudged, notes) -> dict[str, Any]:
    ordered = [items[k] for k in sorted(items)]
    totals: dict[str, Any] = {"open": len(ordered), "stay_user": 0, "keep_other": 0,
                              "move": {"total": 0, "agent": 0, "assistant": 0, "user": 0},
                              "close": {"total": 0, "pr": 0, "decision": 0, "duplicate": 0},
                              # A part of stay_user: changes only suggested for the user's own items.
                              "proposed": {"total": 0, "move": 0, "close": 0}}
    samples: dict[str, list] = {g: [] for g in GROUPS}
    for it in ordered:
        group = _group(it)
        if group == "move":
            totals["move"]["total"] += 1
            totals["move"][it["after"]["owner"]] += 1
        elif group.startswith("close_"):
            totals["close"]["total"] += 1
            totals["close"][it["close"]] += 1
        elif group == "proposed":
            totals["stay_user"] += 1
            totals["proposed"]["total"] += 1
            totals["proposed"]["move" if it["proposed"]["action"] == "reassign" else "close"] += 1
        else:
            totals[group] += 1
        if len(samples[group]) < SAMPLES:
            samples[group].append({"id": it["id"], "kind": it["kind"], "text": (it["text"] or "")[:200],
                                   "reason": it["reason"] or it["rule"]})
    return {"format": PLAN_FORMAT, "version": PLAN_VERSION, "project": project,
            "planned_at": _plain(planned_at), "totals": totals,
            "model": {"used": used, "calls": calls.used, "cap": calls.cap, "candidates": candidates,
                      "unjudged": unjudged},
            "samples": samples, "notes": notes, "items": ordered}


# ---- apply and undo ---------------------------------------------------------


def _ready(cur) -> bool:
    from khipu.db import has_columns, has_table

    return (has_columns(cur, "commitments", "origin", "owner_label", "owner_reassigned_at", "close_rule_key",
                        "reopened_rule_keys", "tidy_batch")
            and has_table(cur, "commitment_tidy_items") and has_table(cur, "commitment_tidy_batches"))


def _check_plan(doc: Any) -> str | None:
    if not isinstance(doc, dict) or doc.get("format") != PLAN_FORMAT:
        return "this file is not a clean-up plan from khipu owed tidy"
    if doc.get("version") != PLAN_VERSION:
        return f"this plan is version {doc.get('version')!r}; this Khipu reads version {PLAN_VERSION}"
    if not doc.get("project") or not isinstance(doc.get("items"), list):
        return "this plan has no project or no items"
    return None


def _owner_now(row: dict) -> str:
    from khipu import commitments as co

    trigger = co.has_future_trigger(row.get("text") or "") or bool(row.get("future_trigger"))
    return co.row_owner(row, future_trigger=trigger)[0]


def _is_users(row: dict) -> bool:
    from khipu import commitments as co

    return _owner_now(row) == co.OWNER_USER


def _gate(cur, rows: dict[int, dict], ids: list[int], extra_users: set[int] | None = None):
    """``(passed, gate, mine)``: the owner gate when any of ``ids`` is the
    user's item now (or in ``extra_users``)."""
    from khipu import commitments as co

    mine = [cid for cid in ids if _is_users(rows[cid]) or cid in (extra_users or set())]
    if not mine:
        return True, None, mine
    gate = co.bulk_owner_gate(len(ids))
    return gate is not None, gate, mine


def apply(doc: dict[str, Any]) -> dict[str, Any]:
    """Apply a plan: one transaction, one batch (see the module docstring)."""
    from khipu import commitments as co, offline
    from khipu.db import connect

    why = _check_plan(doc)
    if why:
        return {"ok": False, "changed": 0, "error": why + "; nothing was changed"}
    project = doc["project"]
    planned = {int(it["id"]): it for it in doc["items"] if it.get("action") in ("close", "reassign")}
    with connect(statement_timeout_s=offline.setting("hub_statement_timeout")) as conn:
        with conn.cursor() as cur:
            if not _ready(cur):
                return {"ok": False, "changed": 0, "error": NOT_READY}

            def current(lock: bool) -> tuple[dict[int, dict], list[dict], list[int]]:
                rows = _select(cur, project, ids=list(planned), lock=lock)
                skipped, live = [], []
                for cid, it in planned.items():
                    row = rows.get(cid)
                    if row is None:
                        skipped.append({"id": cid, "why": "no longer on the hub, or forgotten"})
                    elif row["status"] != "open":
                        skipped.append({"id": cid, "why": f"now {row['status']}, not open"})
                    elif fingerprint(row) != it.get("fingerprint"):
                        skipped.append({"id": cid, "why": "changed after the plan was made"})
                    else:
                        live.append(cid)
                return rows, skipped, live

            rows, skipped, live = current(lock=False)
            passed, gate, mine = _gate(cur, rows, live)
            if not passed:
                return {"ok": False, "changed": 0, "needs_owner": mine, "error": co.BULK_OWNER_REFUSAL}
            rows, skipped, live = current(lock=True)
            if not gate and any(_is_users(rows[cid]) for cid in live):
                return {"ok": False, "changed": 0, "error": co.BULK_OWNER_REFUSAL}
            batch = _batch_id("tidy")
            counts = {"close": 0, "reassign": 0}
            cur.execute("INSERT INTO commitment_tidy_batches (id, project, plan_version, planned_at, owner_gate) "
                        "VALUES (%s, %s, %s, %s::timestamptz, %s)",
                        (batch, project, PLAN_VERSION, doc.get("planned_at"), gate))
            for cid in live:
                it = planned[cid]
                prior = {f: _plain(rows[cid].get(f)) for f in PRIOR_FIELDS}
                now_owner = _owners(rows[cid])[1]["owner"]
                if co.owner_guard(now_owner, _rule_of(it)):
                    skipped.append({"id": cid, "why": "the owner's own item: the clean-up only suggests a change"})
                    continue
                if it["action"] == "close":
                    if not co.close_by_rule(cur, cid, rule=_rule_of(it), owner=now_owner,
                                            reason=it.get("reason") or "closed by the clean-up",
                                            key=it.get("key") or f"tidy:{cid}", evidence=it.get("evidence") or {},
                                            episode=it.get("episode"), set_extra=", tidy_batch = %s",
                                            extra_params=(batch,)):
                        skipped.append({"id": cid, "why": "the owner reopened it after the same evidence closed it"})
                        continue
                else:
                    after = it.get("after") or {}
                    owner = after.get("owner")
                    if owner not in co.OWNERS:
                        skipped.append({"id": cid, "why": f"the plan's owner {owner!r} is not one Khipu knows"})
                        continue
                    label = (co.clean_owner_label(after.get("owner_label")) or co.ORCHESTRATOR) \
                        if owner == co.OWNER_AGENT else None
                    origin = after.get("origin") if after.get("origin") in co.ORIGINS else prior["origin"]
                    lineage = ""
                    lineage_params: tuple = ()
                    if co._lineage_columns_ready(cur):
                        # The right-hand sides read the row before this UPDATE.
                        lineage = ("owner_prior = owner, owner_prior_label = owner_label, "
                                   "owner_reassigned_by = %s, owner_reassign_reason = %s, ")
                        lineage_params = (f"khipu owed tidy ({batch})",
                                          (it.get("reason") or it.get("rule") or "")[:400] or None)
                    co.auto_reassign(
                        cur, cid, rule=_rule_of(it), owner_before=now_owner,
                        sets=[f"{lineage}owner = %s", "owner_label = %s", "origin = %s", "owner_reassigned_at = now()",
                              "tidy_batch = %s"],
                        params=(*lineage_params, owner, label, origin, batch))
                counts[it["action"]] += 1
                done = _select(cur, project, ids=[cid])[cid]
                cur.execute(
                    "INSERT INTO commitment_tidy_items (batch_id, commitment_id, action, reason, prior, "
                    "after_fingerprint) VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
                    (batch, cid, it["action"], (it.get("reason") or it.get("rule") or "")[:400],
                     json.dumps(prior, default=str), fingerprint(done)))
            cur.execute("UPDATE commitment_tidy_batches SET counts = %s::jsonb, skipped = %s::jsonb WHERE id = %s",
                        (json.dumps(counts), json.dumps(skipped), batch))
    changed = counts["close"] + counts["reassign"]
    _log(f"{batch}: closed {counts['close']}, reassigned {counts['reassign']}, skipped {len(skipped)}")
    return {"ok": True, "batch": batch, "project": project, "changed": changed, "counts": counts,
            "skipped": skipped, "owner_gate": gate}


def _batch_id(prefix: str) -> str:
    return f"{prefix}-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)


# Every action a bulk owed change can take; each one is kept as an undoable
# batch with the same record shape (``bulk_batch``).
BULK_ACTIONS = ("close", "reopen", "snooze", "reassign")
# The state an undo of a bulk change restores: the clean-up's, plus the due
# date a snooze moves.
BULK_PRIOR_FIELDS = (*PRIOR_FIELDS, "due_after")


def bulk_batch(cur, ids: list[int], action: str, change, *, reason: str,
               gate: str | None) -> tuple[dict[int, bool], list[str]]:
    """Apply ``change(cid) -> bool`` (a close, reopen, snooze or reassign) to
    ``ids`` as undoable batches, one per project, in the caller's
    transaction. Every bulk action keeps the same record: the batch, and per
    changed item in ``commitment_tidy_items`` its action, the reason and its
    state before (``BULK_PRIOR_FIELDS``, the due date included), so ``undo``
    (and ``khipu owed tidy --undo``) restores each item exactly. Returns
    ``({id: changed}, [batch ids])``."""
    if action not in BULK_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(BULK_ACTIONS)}")
    cur.execute("SELECT id, project FROM commitments WHERE id = ANY(%s) ORDER BY id", (list(ids),))
    by_project: dict[str | None, list[int]] = {}
    for cid, project in cur.fetchall():
        by_project.setdefault(project, []).append(int(cid))
    changed: dict[int, bool] = {}
    batches: list[str] = []
    for project, pids in by_project.items():
        before = _select(cur, project, ids=pids, lock=True)
        if not before:
            continue
        batch = _batch_id(action)
        cur.execute("INSERT INTO commitment_tidy_batches (id, project, plan_version, planned_at, owner_gate) "
                    "VALUES (%s, %s, %s, NULL, %s)", (batch, project or "", PLAN_VERSION, gate))
        count = 0
        for cid in pids:
            row = before.get(cid)
            if row is None or not change(cid):
                changed[cid] = False
                continue
            cur.execute("UPDATE commitments SET tidy_batch = %s WHERE id = %s", (batch, cid))
            after = _select(cur, project, ids=[cid])[cid]
            cur.execute(
                "INSERT INTO commitment_tidy_items (batch_id, commitment_id, action, reason, prior, "
                "after_fingerprint) VALUES (%s, %s, %s, %s, %s::jsonb, %s)",
                (batch, cid, action, reason[:400],
                 json.dumps({f: _plain(row.get(f)) for f in BULK_PRIOR_FIELDS}, default=str), fingerprint(after)))
            changed[cid] = True
            count += 1
        cur.execute("UPDATE commitment_tidy_batches SET counts = %s::jsonb WHERE id = %s",
                    (json.dumps({action: count}), batch))
        batches.append(batch)
    return changed, batches


def batch_project(batch: str) -> str | None:
    """The project a batch belongs to ("" for items with none), or None when
    there is no such batch. Read only."""
    from khipu import offline
    from khipu.db import connect

    with connect(statement_timeout_s=offline.setting("hub_statement_timeout")) as conn:
        with conn.cursor() as cur:
            if not _ready(cur):
                return None
            cur.execute("SELECT project FROM commitment_tidy_batches WHERE id = %s", (str(batch or "").strip(),))
            found = cur.fetchone()
    return None if found is None else str(found[0])


def undo(batch: str, *, project: str | None = None) -> dict[str, Any]:
    """Undo one applied batch (see the module docstring). With ``project``,
    a batch of another project is refused before anything changes."""
    from khipu import commitments as co, offline
    from khipu.db import connect

    batch = str(batch or "").strip()
    with connect(statement_timeout_s=offline.setting("hub_statement_timeout")) as conn:
        with conn.cursor() as cur:
            if not _ready(cur):
                return {"ok": False, "restored": 0, "error": NOT_READY}
            cur.execute("SELECT project FROM commitment_tidy_batches WHERE id = %s FOR UPDATE", (batch,))
            found = cur.fetchone()
            if found is None:
                return {"ok": False, "restored": 0, "error": f"no clean-up batch {batch!r}; nothing was changed"}
            if project is not None and found[0] != project:
                return {"ok": False, "restored": 0, "batch": batch, "project": found[0],
                        "error": f"batch {batch!r} is for project {found[0]!r}, not {project!r}; "
                                 "nothing was changed"}
            project = found[0] or None
            cur.execute("SELECT commitment_id, action, prior, after_fingerprint FROM commitment_tidy_items "
                        "WHERE batch_id = %s AND undone_at IS NULL ORDER BY commitment_id", (batch,))
            entries = [(int(c), a, p if isinstance(p, dict) else json.loads(p), fp) for c, a, p, fp in cur.fetchall()]
            rows = _select(cur, project, ids=[e[0] for e in entries], lock=True)
            left_alone, todo = [], []
            for cid, action, prior, fp in entries:
                row = rows.get(cid)
                if row is None:
                    left_alone.append({"id": cid, "why": "no longer on the hub, or forgotten"})
                elif fingerprint(row) != fp:
                    left_alone.append({"id": cid, "why": "changed after the clean-up"})
                else:
                    todo.append((cid, action, prior))
            prior_users = {cid for cid, _, prior in todo if prior.get("owner") == co.OWNER_USER}
            passed, gate, mine = _gate(cur, rows, [t[0] for t in todo], prior_users)
            if not passed:
                return {"ok": False, "restored": 0, "batch": batch, "needs_owner": mine,
                        "error": co.BULK_OWNER_REFUSAL}
            restored = 0
            lineage_ready = co._lineage_columns_ready(cur)
            for cid, action, prior in todo:
                cur.execute("SAVEPOINT owed_tidy_undo")
                try:
                    cur.execute(
                        "UPDATE commitments SET status = %s, owner = %s, owner_label = %s, origin = %s, "
                        "owner_reassigned_at = %s::timestamptz, closed_at = %s::timestamptz, closed_episode = %s, "
                        "close_reason = %s, closed_by = %s, close_rule_key = %s, close_evidence = %s::jsonb, "
                        "tidy_batch = %s, reopened_rule_keys = CASE WHEN %s AND close_rule_key IS NOT NULL "
                        "AND NOT (close_rule_key = ANY(reopened_rule_keys)) "
                        "THEN array_append(reopened_rule_keys, close_rule_key) ELSE reopened_rule_keys END, "
                        # A clean-up batch kept no due date and never moved one.
                        "due_after = CASE WHEN %s THEN %s::timestamptz ELSE due_after END"
                        + (", owner_reassigned_by = %s, owner_reassign_reason = %s, owner_prior = %s, "
                           "owner_prior_label = %s" if lineage_ready else "")
                        + " WHERE id = %s",
                        (prior["status"], prior["owner"], prior["owner_label"], prior["origin"],
                         prior["owner_reassigned_at"], prior["closed_at"], prior["closed_episode"],
                         prior["close_reason"], prior["closed_by"], prior["close_rule_key"],
                         None if prior["close_evidence"] is None else json.dumps(prior["close_evidence"]),
                         prior["tidy_batch"], action == "close", "due_after" in prior, prior.get("due_after"),
                         *([prior.get(f) for f in LINEAGE_FIELDS] if lineage_ready else []),
                         cid))
                    cur.execute("RELEASE SAVEPOINT owed_tidy_undo")
                except Exception as exc:  # noqa: BLE001 — e.g. the same text is open again
                    cur.execute("ROLLBACK TO SAVEPOINT owed_tidy_undo")
                    left_alone.append({"id": cid, "why": f"could not be restored ({type(exc).__name__}); "
                                                         "an open item with the same text may exist"})
                    continue
                cur.execute("UPDATE commitment_tidy_items SET undone_at = now() WHERE batch_id = %s "
                            "AND commitment_id = %s", (batch, cid))
                restored += 1
            report = {"restored": restored, "left_alone": left_alone, "owner_gate": gate}
            cur.execute("UPDATE commitment_tidy_batches SET undone_at = now(), undo_report = %s::jsonb "
                        "WHERE id = %s", (json.dumps(report), batch))
    _log(f"{batch}: undo restored {restored}, left alone {len(left_alone)}")
    return {"ok": True, "batch": batch, "project": project or "", **report}
