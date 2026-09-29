# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Golden-query recall evaluation (W6.3) — ``khipu recall eval``.

``<config dir>/recall-golden.jsonl`` (maintainer-local; ``KHIPU_RECALL_GOLDEN`` overrides) holds hand- and evidence-derived queries with
their expected hit ids: ``{query, mode?, expect: [ids], k, note}``. Each line
runs through ``khipu.embed.hybrid_search`` (or the named ``mode``) and scores
hit@k — 1 if ANY id in ``expect`` appears among the top ``k`` results, else 0.
Not a CI gate (GitHub Actions is not used here); it is a manual/soak check
that turns "does default search still find the things it used to" into one
command instead of a memory of a demo that once worked. ``run_eval``/
``eval_one``/``load_golden`` above are the original W6.3 surface and stay
byte-identical: ``khipu recall eval`` with no new flag prints exactly what it
always has.

Phase 1 session A extends this into a regression control that covers every
query-driven recall path, not just ``hybrid_search`` in hybrid mode (the
per-prompt hook and ``khipu_status`` lanes were previously unscored — see
``docs/research/hindsight-plan-review-2026-09-28.md`` finding on the
evaluator gap):

- ``--path explicit|prompt|status|all`` (default ``explicit``, the legacy
  behavior) selects which retrieval implementation(s) score each entry:
  ``explicit`` is ``embed.hybrid_search`` (unchanged); ``prompt`` is
  ``recall_prompt.prior_work_for_prompt`` unbudgeted (the UserPromptSubmit
  hook's own path); ``status`` is the same function with ``budget_ms`` (the
  gateway/``khipu_status`` lane). A golden entry's own ``paths: [...]`` key
  restricts which of these it is scored against.
- ``expect_none: true`` marks an abstention case (no relevant memory should
  come back); ``stale: [ids]`` names ids that must NOT appear in the top k
  (a superseded/forgotten row leaking back into results).
- ``--record FILE`` snapshots the ordered ids every (entry, path) pair
  returns, as a control; ``--compare FILE`` reruns and diffs against it
  (identical / reordered / changed / new), exit 1 on any difference unless
  ``--allow-changes``. Latency is never part of the diff.
- ``--rerank on|off`` scores the explicit path with the optional reranker
  switch set for this process only; ``on`` scores every entry both ways in the
  one run and reports the expected id's rank with and without the stage.
- ``--relevance-floor on|off`` scores the chosen ``--path``(s) with the absolute
  relevance floor switch set for this process only; ``on`` scores every entry
  both ways and reports abstention correctness per path and each golden
  positive whose query the gate emptied, by query.
- ``--replay LOG [--sample N] [--seed S]`` builds no-expectation entries from
  a ``query_log.jsonl`` for realistic-traffic record/compare runs; only valid
  together with ``--record`` or ``--compare``.

Every metric here is computed from the search functions' own return values,
mocked in tests — this module never opens a database connection itself.
"""
from __future__ import annotations

import json
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_K = 3


def default_golden_path() -> Path:
    # The golden set quotes real captured content (queries that must find a
    # specific private episode), so it lives in the maintainer's config dir,
    # never in the public repo. KHIPU_RECALL_GOLDEN overrides for CI/soak.
    import os

    from khipu.paths import data_dir

    env = os.environ.get("KHIPU_RECALL_GOLDEN")
    return Path(env) if env else data_dir() / "recall-golden.jsonl"


def load_golden(path: Path) -> list[dict[str, Any]]:
    """Parse a recall-golden JSONL file. Blank lines and ``#``-prefixed
    comment lines are skipped; a malformed line raises with its 1-based line
    number so a broken golden file fails loudly rather than silently
    dropping a case.

    A line needs ``query`` and one of ``expect`` (a positive case) /
    ``expect_none: true`` (an abstention case) — every other key
    (``mode``, ``k``, ``note``, ``paths``, ``stale``, ``project``, ``cwd``,
    ``since``, ``until``) is optional and read by the scorer, not here, so an
    old golden file with only ``query``/``expect``/``k``/``note`` lines keeps
    loading exactly as before.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{path}:{i}: invalid JSON: {exc}") from exc
        if not isinstance(entry, dict) or not entry.get("query"):
            raise ValueError(f"{path}:{i}: entry needs at least 'query' and one of 'expect'/'expect_none'")
        if not entry.get("expect") and not entry.get("expect_none"):
            raise ValueError(f"{path}:{i}: entry needs at least 'query' and one of 'expect'/'expect_none'")
        out.append(entry)
    return out


def eval_one(entry: dict[str, Any]) -> dict[str, Any]:
    """Run one golden entry through hybrid_search; returns the scored row.
    Any search failure (hub unreachable, bad mode) is recorded as a miss
    rather than raised, so one bad line does not abort the whole eval."""
    from khipu.embed import hybrid_search

    query = str(entry["query"])
    mode = str(entry.get("mode") or "hybrid")
    k = int(entry.get("k") or DEFAULT_K)
    expect = {str(x) for x in (entry.get("expect") or [])}
    row: dict[str, Any] = {
        "query": query, "mode": mode, "k": k, "expect": sorted(expect),
        "note": entry.get("note"),
    }
    try:
        out = hybrid_search(query, mode=mode, limit=k)
        # Every kind counts. Scoring episodes only (audit 2026-09-04) meant a
        # golden line whose expected id was a topic slug or a graph node could
        # never hit, and the topic rows it did return silently consumed the
        # top-k slots the episode filter then discarded.
        got = [str(r.get("id")) for r in out.get("results", [])[:k]]
        row["got"] = got
        row["hit"] = bool(expect & set(got))
        # R4 (2026-09-14): a golden entry is a KNOWN positive by definition —
        # if search finds it (hit=True) but still reports confidence="none",
        # the two thresholds (embed.CONFIDENCE_COSINE_STRONG/WEAK) are
        # miscalibrated for this corpus/profile, not just a coincidence.
        row["confidence"] = out.get("confidence")
    except Exception as exc:  # noqa: BLE001 — a broken line scores a miss, not a crash
        row["got"] = []
        row["hit"] = False
        row["confidence"] = None
        row["error"] = f"{type(exc).__name__}: {exc}"
    return row


def run_eval(path: Path | None = None) -> dict[str, Any]:
    """Score every golden entry and summarize hit@k overall."""
    golden_path = path or default_golden_path()
    entries = load_golden(golden_path)
    rows = [eval_one(e) for e in entries]
    total = len(rows)
    hits = sum(1 for r in rows if r["hit"])
    overall = (hits / total) if total else 0.0
    # R4: positives (hit=True) whose confidence still reads "none" — the
    # thing confidence exists to never do to a real match. Zero is the bar.
    hit_none_confidence = [r["query"] for r in rows if r["hit"] and r.get("confidence") == "none"]
    return {
        "path": str(golden_path),
        "total": total,
        "hits": hits,
        "overall_hit_rate": round(overall, 4),
        "hit_none_confidence": hit_none_confidence,
        "rows": rows,
    }


# ---- Phase 1 session A: multi-path scoring ---------------------------------
# The evaluator above only ever exercised embed.hybrid_search in hybrid mode.
# Two more query-driven recall paths exist with their own implementations and
# had no evaluation coverage at all before this: recall_prompt.
# prior_work_for_prompt with no budget (the UserPromptSubmit hook) and the
# same function WITH budget_ms (khipu_status / the gateway lane). Everything
# below extends scoring to all three without changing a byte of what's above.

PATH_EXPLICIT = "explicit"
PATH_PROMPT = "prompt"
PATH_STATUS = "status"
ALL_PATHS = (PATH_EXPLICIT, PATH_PROMPT, PATH_STATUS)

# khipu_status's own default budget for the same call (mirrors the gateway
# lane; recall_prompt.prior_work_for_prompt's budget_ms is caller-supplied).
DEFAULT_STATUS_BUDGET_MS = 600


def _prompt_path_k_cap() -> int:
    """Both prompt and status paths render at most recall_prompt.TOP_N hits
    regardless of an entry's own k (render_block truncates upstream) — a
    golden k of 5 can never be satisfied by either path, so the scoring
    window is capped to match rather than report a phantom miss. Imported
    lazily and read-only; this module never edits recall_prompt."""
    try:
        from khipu.recall_prompt import TOP_N

        return int(TOP_N)
    except Exception:  # noqa: BLE001 — a broken import must not sink scoring
        return 3


def _entry_applies(entry: dict[str, Any], path: str) -> bool:
    """True when `path` is one of this entry's own `paths` restriction, or
    the entry carries no restriction (applies to every requested path)."""
    restrict = entry.get("paths")
    return not restrict or path in restrict


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list. Not statistics.
    quantiles: nearest-rank is well-defined for the tiny (often single-digit)
    sample sizes one golden run produces, where interpolation schemes
    disagree most."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _call_explicit(query: str, entry: dict[str, Any], *, mode: str, k: int) -> dict[str, Any]:
    from khipu.embed import hybrid_search

    return hybrid_search(
        query, mode=mode, limit=k,
        project=entry.get("project"), since=entry.get("since"), until=entry.get("until"),
    )


def _call_prompt_or_status(
    query: str, entry: dict[str, Any], *, path: str, budget_ms: int
) -> dict[str, Any]:
    from khipu.recall_prompt import prior_work_for_prompt

    return prior_work_for_prompt(
        query, cwd=entry.get("cwd"), session_id=None,
        budget_ms=budget_ms if path == PATH_STATUS else None,
    )


def _extract_got(payload: dict[str, Any], path: str, k: int) -> list[str]:
    rows = payload.get("results" if path == PATH_EXPLICIT else "hits") or []
    return [str(r.get("id")) for r in rows[:k]]


def _outcome(payload: dict[str, Any], path: str) -> tuple[bool, bool, str | None, str]:
    """(timeout, error, degraded, reason) read off one path's raw return
    value. explicit (embed.hybrid_search) has no deadline today (scope:
    "Retrieval and multi-harness contract") so it never reports a timeout;
    prompt/status (recall_prompt.prior_work_for_prompt) are fail-open and
    describe every outcome, including a timeout, in their own `reason`
    string rather than raising."""
    if path == PATH_EXPLICIT:
        err = payload.get("error")
        reason = f"error: {err}" if err else "ok"
        return False, bool(err), payload.get("degraded"), reason
    reason = str(payload.get("reason") or "")
    lowered = reason.lower()
    timeout = lowered.startswith("timeout")
    error = bool(payload.get("error")) or "error" in lowered
    degraded = None
    if path == PATH_STATUS:
        degraded = (payload.get("prior_work_meta") or {}).get("degraded")
    return timeout, error, degraded, reason


def eval_one_path(
    entry: dict[str, Any], path: str, *, budget_ms: int = DEFAULT_STATUS_BUDGET_MS
) -> dict[str, Any]:
    """Score one golden entry against one retrieval path (explicit / prompt /
    status). Same fail-open contract as eval_one — a broken line/path is a
    miss, never a crash — plus the abstention, staleness and outcome
    accounting a single hybrid_search score has no way to express."""
    query = str(entry["query"])
    mode = str(entry.get("mode") or "hybrid")
    k = int(entry.get("k") or DEFAULT_K)
    if path != PATH_EXPLICIT:
        k = min(k, _prompt_path_k_cap())
    expect = {str(x) for x in (entry.get("expect") or [])}
    expect_none = bool(entry.get("expect_none"))
    stale = {str(x) for x in (entry.get("stale") or [])}

    t0 = time.monotonic()
    try:
        if path == PATH_EXPLICIT:
            payload = _call_explicit(query, entry, mode=mode, k=k)
        else:
            payload = _call_prompt_or_status(query, entry, path=path, budget_ms=budget_ms)
    except Exception as exc:  # noqa: BLE001 — a broken line/path scores a miss, not a crash
        payload = {"error": f"{type(exc).__name__}: {exc}"}
    ms = round((time.monotonic() - t0) * 1000, 1)
    payload_chars = len(json.dumps(payload, default=str))

    got = _extract_got(payload, path, k)
    timeout, error, degraded, reason = _outcome(payload, path)

    hit = False
    reciprocal_rank = 0.0
    abstain_correct: bool | None = None
    if expect_none:
        # A timed-out/errored call also returns an empty `got` — without this
        # guard it would read as a correct abstention instead of the
        # infrastructure failure it actually is: the exact shape the review's
        # B10 finding warns about, a miss that looks like "no relevant
        # memory" instead of a lane that missed its own deadline.
        abstain_correct = (not got) and not timeout and not error
    else:
        hit = bool(expect & set(got))
        for rank, gid in enumerate(got, start=1):
            if gid in expect:
                reciprocal_rank = 1.0 / rank
                break

    return {
        "query": query, "path": path, "mode": mode if path == PATH_EXPLICIT else None,
        "k": k, "expect": sorted(expect), "expect_none": expect_none,
        "stale": sorted(stale), "note": entry.get("note"),
        "got": got, "hit": hit, "reciprocal_rank": reciprocal_rank,
        "abstain_correct": abstain_correct,
        "stale_violation": bool(stale & set(got)),
        "timeout": timeout, "error": error, "degraded": degraded, "reason": reason,
        "confidence": payload.get("confidence") if path == PATH_EXPLICIT else None,
        "ms": ms, "payload_chars": payload_chars,
    }


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate eval_one_path rows for ONE path into its metrics block:
    total, hits, hit_rate, mrr, abstain_total, abstain_correct,
    stale_violations, timeouts, errors, degraded, latency p50/p95 (ms), and
    mean result payload size (chars)."""
    positive = [r for r in rows if not r["expect_none"]]
    abstain = [r for r in rows if r["expect_none"]]
    hits = sum(1 for r in positive if r["hit"])
    mrr = (sum(r["reciprocal_rank"] for r in positive) / len(positive)) if positive else 0.0
    latencies = [r["ms"] for r in rows]
    sizes = [r["payload_chars"] for r in rows]
    return {
        "total": len(rows),
        "hits": hits,
        "hit_rate": round(hits / len(positive), 4) if positive else 0.0,
        "mrr": round(mrr, 4),
        "abstain_total": len(abstain),
        "abstain_correct": sum(1 for r in abstain if r["abstain_correct"]),
        "stale_violations": sum(1 for r in rows if r["stale_violation"]),
        "timeouts": sum(1 for r in rows if r["timeout"]),
        # A timeout/error on a positive entry is already folded into `hits`
        # as a miss (see eval_one_path) AND counted here, so this lane can
        # never look like it simply "found nothing" (review finding B10).
        "errors": sum(1 for r in rows if r["error"]),
        "degraded": sum(1 for r in rows if r["degraded"]),
        "latency_p50_ms": round(_percentile(latencies, 50), 1),
        "latency_p95_ms": round(_percentile(latencies, 95), 1),
        "mean_payload_chars": round(sum(sizes) / len(sizes), 1) if sizes else 0.0,
    }


def run_eval_paths(
    entries: list[dict[str, Any]], paths: tuple[str, ...],
    *, budget_ms: int = DEFAULT_STATUS_BUDGET_MS,
) -> dict[str, Any]:
    """Score every entry against every requested path, subject to that
    entry's own `paths` restriction, and summarize per path. The `--path
    prompt|status|all` counterpart to run_eval (which stays explicit-only)."""
    rows: list[dict[str, Any]] = []
    by_path: dict[str, list[dict[str, Any]]] = {p: [] for p in paths}
    for entry in entries:
        for path in paths:
            if not _entry_applies(entry, path):
                continue
            row = eval_one_path(entry, path, budget_ms=budget_ms)
            rows.append(row)
            by_path[path].append(row)
    return {"paths": {p: summarize_rows(by_path[p]) for p in paths}, "rows": rows}


def _stamp(paths: tuple[str, ...]) -> dict[str, Any]:
    """khipu version, the path list, wall-clock UTC, and the replica's own
    refreshed_at. Best-effort: a stamp is metadata, not a scored result, so a
    snapshot read failure degrades to None rather than sinking a record."""
    import khipu

    out: dict[str, Any] = {
        "khipu_version": getattr(khipu, "__version__", None),
        "paths": list(paths),
        "utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "replica_refreshed_at": None,
    }
    try:
        from khipu.hub_snapshot import snapshot_health

        out["replica_refreshed_at"] = snapshot_health().get("refreshed_at")
    except Exception:  # noqa: BLE001 — best-effort stamp metadata only
        pass
    return out


def build_record(
    entries: list[dict[str, Any]], paths: tuple[str, ...],
    *, budget_ms: int = DEFAULT_STATUS_BUDGET_MS,
) -> dict[str, Any]:
    """A control: the ordered ids every (entry, path) pair returns right now,
    plus each pair's reason/degraded state and a stamp. `--compare` diffs a
    later run against exactly this."""
    report = run_eval_paths(entries, paths, budget_ms=budget_ms)
    return {
        "stamp": _stamp(paths),
        "entries": [
            {"query": r["query"], "path": r["path"], "got": r["got"],
             "reason": r["reason"], "degraded": r["degraded"]}
            for r in report["rows"]
        ],
    }


def write_record(path: Path, record: dict[str, Any]) -> None:
    path.write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")


def read_record(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "entries" not in data:
        raise ValueError(f"{path}: not a recall-eval control file (missing 'entries')")
    return data


def _diff_status(control_got: list[str] | None, live_got: list[str]) -> str:
    if control_got is None:
        return "new"
    if control_got == live_got:
        return "identical"
    if sorted(control_got) == sorted(live_got):
        return "reordered"
    return "changed"


def compare_record(
    entries: list[dict[str, Any]], paths: tuple[str, ...], control: dict[str, Any],
    *, budget_ms: int = DEFAULT_STATUS_BUDGET_MS,
) -> dict[str, Any]:
    """Rerun `entries` against `paths` and diff each (query, path) pair's
    ordered ids against `control`. Latency is deliberately never part of the
    diff: only `ms` changing between the two runs still reads as
    "identical", matching the scope's "differences in latency never fail a
    comparison". A pair the live run has but the control lacks (paths=all
    added an entry after the control was recorded, or --replay drew a
    different sample) reports "new" rather than silently matching."""
    report = run_eval_paths(entries, paths, budget_ms=budget_ms)
    control_by_key = {(e.get("query"), e.get("path")): e for e in control.get("entries", [])}
    diffs = []
    changed = 0
    for row in report["rows"]:
        prior = control_by_key.get((row["query"], row["path"]))
        status = _diff_status(prior.get("got") if prior else None, row["got"])
        if status in ("changed", "new"):
            changed += 1
        diffs.append({
            "query": row["query"], "path": row["path"], "status": status,
            "control_got": prior.get("got") if prior else None, "live_got": row["got"],
            "control_reason": prior.get("reason") if prior else None, "live_reason": row["reason"],
        })
    return {"stamp": _stamp(paths), "diffs": diffs, "changed": changed}


def load_replay_entries(
    log_path: Path, *, sample: int | None = None, seed: int = 0,
) -> list[dict[str, Any]]:
    """No-expectation entries built from a query_log.jsonl (query_log.
    log_query's own shape) for a realistic-traffic --record/--compare run.
    Only valid with --record or --compare — there is no golden `expect` to
    score against, by design. A redacted line (query=None — the gateway
    never logs query text, see query_log.log_query's `redact`) has nothing
    to replay and is skipped, same as a line with no query at all; a
    `slice:`-prefixed query is the session-start push's own log marker
    (recall_rule.py), not something a person typed. Sampling is seeded so a
    replay run is reproducible, not a fresh random subset every time.
    """
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for raw in log_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            line = json.loads(raw)
        except ValueError:
            continue
        query = line.get("query")
        if not query or not isinstance(query, str) or query.startswith("slice:"):
            continue
        if query in seen:
            continue
        seen.add(query)
        entries.append({"query": query, "mode": line.get("mode") or "hybrid"})
    if sample is not None and sample < len(entries):
        entries = random.Random(seed).sample(entries, sample)
    return entries


# Rank is read over a window wider than any entry's k so an expected id the
# reranker pulls up from just outside the top k still shows as a rank.
RERANK_RANK_WINDOW = 12
_RERANK_ENV = "KHIPU_FEATURE_RERANK"


def _rank_of(expect: set[str], payload: dict[str, Any]) -> int | None:
    for rank, row in enumerate(payload.get("results") or [], start=1):
        if str(row.get("id")) in expect:
            return rank
    return None


def _explicit_once(entry: dict[str, Any], *, rerank: bool) -> dict[str, Any]:
    """One explicit search with the rerank switch forced on or off through its
    environment leg (never config.json), restored afterwards."""
    import os

    from khipu.embed import hybrid_search

    prior = os.environ.get(_RERANK_ENV)
    os.environ[_RERANK_ENV] = "1" if rerank else "0"
    try:
        return hybrid_search(
            str(entry["query"]), mode=str(entry.get("mode") or "hybrid"),
            limit=RERANK_RANK_WINDOW, project=entry.get("project"),
            since=entry.get("since"), until=entry.get("until"),
        )
    except Exception as exc:  # noqa: BLE001 — a broken line scores no rank, not a crash
        return {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        if prior is None:
            os.environ.pop(_RERANK_ENV, None)
        else:
            os.environ[_RERANK_ENV] = prior


def run_rerank_eval(entries: list[dict[str, Any]], *, rerank: bool) -> dict[str, Any]:
    """Explicit-path rank of each entry's expected id without the reranker and,
    when ``rerank`` is True, with it too: one run, the same entries both ways.
    ``rank_*`` is the 1-based position of the first expected id within the top
    ``RERANK_RANK_WINDOW`` results; None when absent or the search failed
    (``error_*`` says which)."""
    rows: list[dict[str, Any]] = []
    for entry in entries:
        expect = {str(x) for x in (entry.get("expect") or [])}
        without = _explicit_once(entry, rerank=False)
        row: dict[str, Any] = {
            "query": str(entry["query"]), "expect": sorted(expect),
            "rank_without": _rank_of(expect, without),
            "rank_with": None, "rerank": None,
        }
        if without.get("error"):
            row["error_without"] = without["error"]
        if rerank:
            staged = _explicit_once(entry, rerank=True)
            row["rank_with"] = _rank_of(expect, staged)
            row["rerank"] = staged.get("rerank")
            if staged.get("error"):
                row["error_with"] = staged["error"]
        rows.append(row)
    scored = [r for r in rows if r["expect"]]
    summary: dict[str, Any] = {
        "entries": len(scored),
        "found_without": sum(1 for r in scored if r["rank_without"] is not None),
    }
    if rerank:
        def _pos(rank: int | None) -> float:
            return float("inf") if rank is None else rank

        summary.update({
            "found_with": sum(1 for r in scored if r["rank_with"] is not None),
            "improved": sum(1 for r in scored if _pos(r["rank_with"]) < _pos(r["rank_without"])),
            "worsened": sum(1 for r in scored if _pos(r["rank_with"]) > _pos(r["rank_without"])),
            "applied": sum(1 for r in scored if (r["rerank"] or {}).get("applied")),
        })
    return {"rerank": "on" if rerank else "off", "window": RERANK_RANK_WINDOW,
            "summary": summary, "rows": rows}


_RELEVANCE_ENV = "KHIPU_FEATURE_RELEVANCE_FLOOR"


class _floor_switch:
    """Force the relevance-floor switch on or off through its environment leg
    (never config.json) for one block, restoring the prior value after."""

    def __init__(self, on: bool):
        self._on = on
        self._prior: str | None = None

    def __enter__(self):
        import os

        self._prior = os.environ.get(_RELEVANCE_ENV)
        os.environ[_RELEVANCE_ENV] = "1" if self._on else "0"
        return self

    def __exit__(self, *exc):
        import os

        if self._prior is None:
            os.environ.pop(_RELEVANCE_ENV, None)
        else:
            os.environ[_RELEVANCE_ENV] = self._prior
        return False


def run_relevance_eval(
    entries: list[dict[str, Any]], paths: tuple[str, ...], *, floor: bool,
    budget_ms: int = DEFAULT_STATUS_BUDGET_MS,
) -> dict[str, Any]:
    """Score every entry on every requested path with the relevance floor
    forced off and, when ``floor`` is True, on too: one run, the same entries
    both ways. Per path: abstention correctness under the requested setting
    (and without the floor, for contrast) and ``emptied_positives`` — each
    golden positive whose query the gate emptied although the baseline
    returned rows, by query. That list is the regression; it should be
    empty."""
    rows: list[dict[str, Any]] = []
    by_path: dict[str, list[dict[str, Any]]] = {p: [] for p in paths}
    for entry in entries:
        for path in paths:
            if not _entry_applies(entry, path):
                continue
            with _floor_switch(False):
                base = eval_one_path(entry, path, budget_ms=budget_ms)
            row: dict[str, Any] = {
                "query": base["query"], "path": path, "expect_none": base["expect_none"],
                "got_without": base["got"], "hit_without": base["hit"],
                "abstain_correct_without": base["abstain_correct"],
                "got_with": None, "hit_with": None, "abstain_correct_with": None,
            }
            if floor:
                with _floor_switch(True):
                    staged = eval_one_path(entry, path, budget_ms=budget_ms)
                row.update({
                    "got_with": staged["got"], "hit_with": staged["hit"],
                    "abstain_correct_with": staged["abstain_correct"],
                })
            rows.append(row)
            by_path[path].append(row)

    def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        side = "with" if floor else "without"
        abstain = [r for r in items if r["expect_none"]]
        positive = [r for r in items if not r["expect_none"]]
        out: dict[str, Any] = {
            "entries": len(items),
            "abstain_total": len(abstain),
            "abstain_correct": sum(1 for r in abstain if r[f"abstain_correct_{side}"]),
            "positives": len(positive),
            "positives_found": sum(1 for r in positive if r[f"hit_{side}"]),
        }
        if floor:
            out["abstain_correct_without"] = sum(1 for r in abstain if r["abstain_correct_without"])
            out["emptied_positives"] = [
                r["query"] for r in positive if r["got_without"] and not r["got_with"]
            ]
        return out

    return {
        "relevance_floor": "on" if floor else "off",
        "paths": {p: _summary(by_path[p]) for p in paths},
        "rows": rows,
    }
