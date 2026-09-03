"""Golden-query recall evaluation (W6.3) — ``khipu recall eval``.

``<config dir>/recall-golden.jsonl`` (maintainer-local; ``KHIPU_RECALL_GOLDEN`` overrides) holds hand- and evidence-derived queries with
their expected hit ids: ``{query, mode?, expect: [ids], k, note}``. Each line
runs through ``khipu.embed.hybrid_search`` (or the named ``mode``) and scores
hit@k — 1 if ANY id in ``expect`` appears among the top ``k`` results, else 0.
Not a CI gate (GitHub Actions is not used here); it is a manual/soak check
that turns "does default search still find the things it used to" into one
command instead of a memory of a demo that once worked.
"""
from __future__ import annotations

import json
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
    dropping a case."""
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
        if not isinstance(entry, dict) or not entry.get("query") or not entry.get("expect"):
            raise ValueError(f"{path}:{i}: entry needs at least 'query' and 'expect'")
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
        got = [
            str(r.get("id")) for r in out.get("results", [])[:k] if r.get("kind") == "episode"
        ]
        row["got"] = got
        row["hit"] = bool(expect & set(got))
    except Exception as exc:  # noqa: BLE001 — a broken line scores a miss, not a crash
        row["got"] = []
        row["hit"] = False
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
    return {
        "path": str(golden_path),
        "total": total,
        "hits": hits,
        "overall_hit_rate": round(overall, 4),
        "rows": rows,
    }
