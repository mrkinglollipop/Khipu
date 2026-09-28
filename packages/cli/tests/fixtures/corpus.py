# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Synthetic corpus for the memory-reasoning scenario suite (Phase 1, session B;
docs/plans/2026-09-27-memory-reasoning-scope.md, "Local evaluation and
acceptance": "a versioned synthetic corpus").

Everything named in this module is invented (two projects, ``acme/widget`` and
``acme/rocket``; no real project, person, or path appears anywhere below —
test_repo_hygiene.py enforces that). ``build_corpus`` writes a SQLite replica
using the product's OWN ``hub_snapshot._create_schema`` (never a hand-rolled
copy of the schema, so a real migration is exercised by the same code path a
production dump would use) plus a fresh ``hub_snapshot.sqlite.meta.json``, so
the replica passes ``hub_snapshot.snapshot_is_fresh()`` the moment it is
built. ``installed_corpus`` is the one entry point tests use: a context
manager that patches ``hub_snapshot.snapshot_path``/``meta_path`` at the
replica it just wrote, and ``recall_prompt._cached_query_embed`` at the same
deterministic ``pseudo_embed`` the corpus embedded documents with — so the
local recall path (``khipu.recall_prompt``) runs genuinely end to end with no
database and no network, exactly as docs/plans/2026-09-27-memory-reasoning-
scope.md's "Local evaluation and acceptance" requires.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

# Bump when a change here would shift an existing scenario's scores/ranking
# enough to matter (a new filler row, a reworded fixture, a different pseudo-
# embed bucket count) — nothing reads this at runtime; it exists so a future
# diff can say "the corpus changed" instead of a silent score drift.
CORPUS_VERSION = 1

PROJECTS = ("acme/widget", "acme/rocket")
PROFILE_ID = "pseudo-768"
PROFILE_DIM = 768

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _bucket(token: str, dim: int) -> int:
    digest = hashlib.md5(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % dim


def _sign(token: str) -> float:
    # A second, independent hash decides the sign so two unrelated tokens
    # landing in the same bucket do not always reinforce each other — a
    # cheap stand-in for a real embedding's near-orthogonality of unrelated
    # terms. Deterministic (stdlib hashlib, never Python's randomized
    # str.__hash__), so a rebuilt corpus scores identically every run.
    digest = hashlib.md5((token + "#sign").encode("utf-8")).digest()
    return 1.0 if digest[0] % 2 == 0 else -1.0


def pseudo_embed(text: str, *, dim: int = PROFILE_DIM) -> list[float]:
    """Deterministic, dependency-free stand-in for a real embedding call:
    feature-hashes lowercased tokens into ``dim`` buckets with a per-token
    sign, then L2-normalises. The same function embeds a document at corpus-
    build time and a query through the ``_cached_query_embed`` patch below,
    so cosine similarity tracks literal token overlap — enough signal for a
    scenario to assert "this hit outranks that one", never a claim about a
    real model's semantics.
    """
    vec = [0.0] * dim
    for tok in _TOKEN_RE.findall((text or "").lower()):
        vec[_bucket(tok, dim)] += _sign(tok)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _days_ago(n: float, *, now: datetime) -> str:
    return _iso(now - timedelta(days=n))


@dataclass
class Episode:
    id: int
    ts: str
    summary: str
    project: str
    session_id: str
    harness: str
    topics: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    people: list[str] = field(default_factory=list)
    preferences: list[str] = field(default_factory=list)
    deleted_at: str | None = None


@dataclass
class Topic:
    slug: str
    title: str
    body: str
    project: str | None = None
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""


@dataclass
class CorpusHandle:
    dir: Path
    snapshot_path: Path
    meta_path: Path
    episodes: list[Episode]
    topics: list[Topic]
    now: datetime

    def episode(self, ref: str) -> Episode:
        return next(e for e in self.episodes if e.summary.startswith(ref))

    def by_id(self, episode_id: int) -> Episode:
        return next(e for e in self.episodes if e.id == episode_id)


# ---- named fixtures, one per mandatory scenario that needs one -------------
# Each summary is prefixed with a stable, human-legible ref tag so a test can
# find it (``handle.by_id`` for ids, or by searching the ref substring) without
# hardcoding row order. IDs are deliberately non-contiguous — nothing here
# depends on dense numbering, only on the id being stable within one test.

_NAMED_EPISODE_SPECS: list[dict[str, Any]] = [
    dict(id=1, days_ago=2, project="acme/widget",
         summary="Approved: ship the widget-batching endpoint using cursor-based pagination.",
         decisions=["Use cursor-based pagination for the widget-batching endpoint."],
         topics=["widget-batching"]),
    dict(id=2, days_ago=40, project="acme/widget",
         summary="Decided to use blue-green deployment for the widget-batching endpoint rollout.",
         decisions=["Use blue-green deployment for the widget-batching endpoint rollout."],
         topics=["widget-batching"]),
    dict(id=3, days_ago=5, project="acme/widget",
         summary=("Reversal: blue-green deployment for the widget-batching endpoint "
                   "rollout is retracted; switched to rolling deployment instead."),
         decisions=["Use rolling deployment for the widget-batching endpoint rollout "
                     "(supersedes blue-green deployment)."],
         topics=["widget-batching"]),
    dict(id=4, days_ago=40, project="acme/rocket",
         summary="As of the September pricing review, the rocket launch fee was set to 500 dollars.",
         decisions=["Set the rocket launch fee to 500 dollars (September pricing review)."],
         topics=["rocket-pricing"]),
    dict(id=5, days_ago=10, project="acme/rocket",
         summary="The rocket launch fee was raised to 750 dollars after the October pricing review.",
         decisions=["Raise the rocket launch fee to 750 dollars (October pricing review)."],
         topics=["rocket-pricing"]),
    dict(id=6, days_ago=6, project="acme/widget",
         summary="Use a three second retry backoff for the sync job.",
         decisions=["Use a three second retry backoff for the sync job."],
         topics=["sync-job"]),
    dict(id=7, days_ago=6, project="acme/rocket",
         summary="Use a three second retry backoff for the sync job.",
         decisions=["Use a three second retry backoff for the sync job."],
         topics=["sync-job"]),
    dict(id=9, days_ago=3, project="acme/widget",
         summary=("Ran `khipu snapshot refresh --profile pseudo-768` on the widget deploy "
                   "box; it failed with `FileNotFoundError: hub snapshot missing: "
                   "/var/khipu/hub_snapshot.sqlite`."),
         decisions=[], topics=["ops-runbook"]),
    dict(id=10, days_ago=7, project="acme/rocket",
         summary="Team decided to deprecate the legacy webhook endpoint for acme/rocket.",
         decisions=["Deprecate the legacy webhook endpoint."],
         topics=["webhook-endpoint"]),
    dict(id=11, days_ago=6, project="acme/rocket",
         summary=("Team decided to keep the legacy webhook endpoint for one more quarter "
                   "for acme/rocket."),
         decisions=["Keep the legacy webhook endpoint for one more quarter."],
         topics=["webhook-endpoint"]),
    dict(id=12, days_ago=9, project="acme/widget",
         summary=("Migration plan for the billing database: initially planned a blue-green "
                   "cutover, later switched to a rolling migration."),
         decisions=["Use a blue-green cutover for the billing database migration.",
                     "Use a rolling migration for the billing database migration "
                     "(supersedes blue-green cutover)."],
         topics=["billing-migration"]),
    dict(id=13, days_ago=20, project="acme/widget",
         summary="Set the staging database connection pool size to 20 connections.",
         decisions=["Set the staging database pool size to 20 connections."],
         topics=["staging-db"]),
    dict(id=14, days_ago=1, project="acme/widget",
         summary="Increased the staging database connection pool size to 40 connections (was 20).",
         decisions=["Set the staging database pool size to 40 connections "
                     "(revises pool size 20)."],
         topics=["staging-db"]),
    dict(id=15, days_ago=15, project="acme/widget",
         summary="Recorded a throwaway note about a scratch experiment that was later deleted.",
         decisions=[], topics=["scratch-notes"], deleted_days_ago=1),
]

_NAMED_TOPIC_SPECS: list[dict[str, Any]] = [
    dict(slug="widget-batching", title="Widget batching", project="acme/widget",
         body="The widget-batching endpoint batches writes for the acme/widget API."),
    dict(slug="rocket-pricing", title="Rocket pricing", project="acme/rocket",
         body="Pricing policy for acme/rocket launch fees."),
    dict(slug="sync-job", title="Sync job", project=None,
         body="Retry and backoff configuration for the background sync job."),
    dict(slug="billing-service", title="Billing service", project="acme/widget",
         body="The billing-service module handles invoicing for acme/widget."),
    dict(slug="rate-limits", title="Rate limits", project="acme/widget",
         body="Central throttling configuration for outbound API calls."),
    dict(slug="ops-runbook", title="Ops runbook", project="acme/widget",
         body="Operational runbook entries for the widget deploy box."),
    dict(slug="webhook-endpoint", title="Webhook endpoint", project="acme/rocket",
         body="The legacy webhook endpoint for acme/rocket integrations."),
    dict(slug="billing-migration", title="Billing migration", project="acme/widget",
         body="Migration plan notes for the billing database."),
    dict(slug="staging-db", title="Staging database", project="acme/widget",
         body="Connection pool sizing for the staging database."),
    dict(slug="scratch-notes", title="Scratch notes", project="acme/widget",
         body="Throwaway scratch notes, superseded by nothing in particular."),
    dict(slug="legacy-pagination", title="Legacy pagination", project="acme/widget",
         status="superseded",
         body="Old cursor-based pagination approach for widget-batching — see the current "
              "widget-batching decisions instead."),
    dict(slug="deployment-strategy", title="Deployment strategy", project="acme/widget",
         body="Deployment strategy notes that link back to widget-batching."),
    dict(slug="pricing-policy", title="Pricing policy", project="acme/rocket",
         body="Pricing policy notes that link back to rocket-pricing."),
    dict(slug="retry-policy", title="Retry policy", project="acme/widget",
         body="Retry policy notes that link back to the sync job."),
    dict(slug="onboarding-notes", title="Onboarding notes", project="acme/rocket",
         status="retired",
         body="Retired onboarding notes for acme/rocket, kept for history only."),
]

# topic:<slug> <-> topic:<slug> wiki_link, and one topic:<slug> <-> path:<rel>
# lives_in — the "indirect graph relationship" scenario needs billing-service
# wiki-linked to rate-limits with NO lexical overlap between the two bodies.
_WIKI_LINKS: list[tuple[str, str]] = [
    ("billing-service", "rate-limits"),
    ("deployment-strategy", "widget-batching"),
    ("pricing-policy", "rocket-pricing"),
    ("retry-policy", "sync-job"),
]
_LIVES_IN: list[tuple[str, str]] = [
    ("billing-service", "packages/cli/khipu/billing.py"),
]

_FILLER_PROJECTS = PROJECTS
_FILLER_HARNESSES = ("claude_code", "codex", "cursor", "aegis")
_FILLER_TOPICS = ("release-notes", "onboarding", "infra-notes", "perf-notes", "misc-notes")


def _filler_episodes(now: datetime, *, start_id: int, count: int) -> list[Episode]:
    """Unrelated, deterministic padding rows so the corpus has the ~40-episode
    shape the scope calls for and "no relevant memory" queries have a real
    (non-empty) haystack to come up empty against."""
    out = []
    for i in range(count):
        eid = start_id + i
        project = _FILLER_PROJECTS[i % len(_FILLER_PROJECTS)]
        harness = _FILLER_HARNESSES[i % len(_FILLER_HARNESSES)]
        topic = _FILLER_TOPICS[i % len(_FILLER_TOPICS)]
        out.append(Episode(
            id=eid,
            ts=_days_ago(3 + i * 2.5, now=now),
            summary=f"Routine status update {i}: reviewed {topic} for {project}, nothing durable.",
            project=project,
            session_id=f"{harness}:filler-{i:03d}",
            harness=harness,
            topics=[topic],
        ))
    return out


def _named_episodes(now: datetime) -> list[Episode]:
    out = []
    for i, spec in enumerate(_NAMED_EPISODE_SPECS):
        harness = _FILLER_HARNESSES[i % len(_FILLER_HARNESSES)]
        deleted_at = None
        if "deleted_days_ago" in spec:
            deleted_at = _days_ago(spec["deleted_days_ago"], now=now)
        out.append(Episode(
            id=spec["id"],
            ts=_days_ago(spec["days_ago"], now=now),
            summary=spec["summary"],
            project=spec["project"],
            session_id=f"{harness}:named-{spec['id']:03d}",
            harness=harness,
            topics=list(spec.get("topics") or []),
            decisions=list(spec.get("decisions") or []),
            deleted_at=deleted_at,
        ))
    return out


def _named_topics(now: datetime) -> list[Topic]:
    ts = _iso(now)
    out = []
    for spec in _NAMED_TOPIC_SPECS:
        out.append(Topic(
            slug=spec["slug"], title=spec["title"], body=spec["body"],
            project=spec.get("project"), status=spec.get("status", "active"),
            created_at=ts, updated_at=ts,
        ))
    return out


def _episode_rank_text(ep: Episode) -> str:
    return " ".join([ep.summary, *ep.decisions, *ep.topics])


def build_corpus(
    tmp_dir: Path,
    *,
    now: datetime | None = None,
    extra_episodes: list[Episode] | None = None,
    extra_topics: list[Topic] | None = None,
    filler_count: int = 25,
    profile_dim: int = PROFILE_DIM,
    active_profile: bool = True,
    refreshed_at: datetime | None = None,
) -> CorpusHandle:
    """Write a fresh SQLite replica (via the product's own ``_create_schema``)
    plus a matching meta.json under ``tmp_dir``. Parameterised so a test can
    append or override rows (``extra_episodes``/``extra_topics``) without
    forking the whole builder, and can force the replica stale
    (``refreshed_at`` in the past) for the stale/offline scenario.
    """
    from khipu import hub_snapshot as hs

    now = now or datetime.now(timezone.utc)
    episodes = _named_episodes(now) + _filler_episodes(
        now, start_id=1000, count=filler_count
    ) + list(extra_episodes or [])
    topics = _named_topics(now) + list(extra_topics or [])

    tmp_dir.mkdir(parents=True, exist_ok=True)
    snap = tmp_dir / hs.SNAPSHOT_NAME
    meta_file = tmp_dir / hs.META_NAME

    con = sqlite3.connect(str(snap))
    hs._create_schema(con)

    for ep in episodes:
        con.execute(
            "INSERT INTO episodes (id, ts, session_id, summary, topics, people, "
            "decisions, preferences, scope, harness, project, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ep.id, ep.ts, ep.session_id, ep.summary, json.dumps(ep.topics),
             json.dumps(ep.people), json.dumps(ep.decisions),
             json.dumps(ep.preferences), ep.project, ep.harness, ep.project,
             ep.deleted_at),
        )

    for t in topics:
        frontmatter = json.dumps({"project": t.project}) if t.project else None
        con.execute(
            "INSERT INTO topics (slug, title, body, status, created_at, updated_at, "
            "frontmatter) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (t.slug, t.title, t.body, t.status, t.created_at, t.updated_at, frontmatter),
        )

    topic_slugs = {t.slug for t in topics}
    for a, b in _WIKI_LINKS:
        if a not in topic_slugs or b not in topic_slugs:
            continue
        con.execute(
            "INSERT INTO nodes (id, type, name) VALUES (?, 'topic', ?) "
            "ON CONFLICT(id) DO NOTHING",
            (f"topic:{a}", a),
        )
        con.execute(
            "INSERT INTO nodes (id, type, name) VALUES (?, 'topic', ?) "
            "ON CONFLICT(id) DO NOTHING",
            (f"topic:{b}", b),
        )
        con.execute(
            "INSERT INTO edges (src, dst, type, weight) VALUES (?, ?, 'wiki_link', 1.0)",
            (f"topic:{a}", f"topic:{b}"),
        )
    for slug, relpath in _LIVES_IN:
        if slug not in topic_slugs:
            continue
        con.execute(
            "INSERT INTO nodes (id, type, name) VALUES (?, 'path', ?) "
            "ON CONFLICT(id) DO NOTHING",
            (f"path:{relpath}", relpath),
        )
        con.execute(
            "INSERT INTO edges (src, dst, type, weight) VALUES (?, ?, 'lives_in', 1.0)",
            (f"topic:{slug}", f"path:{relpath}"),
        )

    con.execute(
        "INSERT INTO embedding_profiles (id, provider, model, dim, normalize, "
        "is_active, created_at) VALUES (?, 'pseudo', 'pseudo-hash', ?, 'l2', ?, ?)",
        (PROFILE_ID, profile_dim, 1 if active_profile else 0, _iso(now)),
    )
    for ep in episodes:
        text = _episode_rank_text(ep)
        vec = pseudo_embed(text, dim=profile_dim)
        blob = _pack(vec)
        con.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, "
            "embedding, built_at) VALUES (?, 'episode', ?, 0, ?, ?, ?)",
            (PROFILE_ID, str(ep.id), text, blob, _iso(now)),
        )
    for t in topics:
        text = f"{t.title} {t.body}"
        vec = pseudo_embed(text, dim=profile_dim)
        blob = _pack(vec)
        con.execute(
            "INSERT INTO memory_embeddings (profile, kind, ref, chunk_idx, chunk_text, "
            "embedding, built_at) VALUES (?, 'topic', ?, 0, ?, ?, ?)",
            (PROFILE_ID, t.slug, text, blob, _iso(now)),
        )

    con.commit()
    con.close()

    refreshed = refreshed_at or now
    counts = {
        "episodes": len(episodes), "topics": len(topics), "topic_revisions": 0,
        "nodes": len({n for pair in _WIKI_LINKS for n in pair}) + len(_LIVES_IN),
        "edges": len(_WIKI_LINKS) + len(_LIVES_IN),
        "embedding_profiles": 1, "memory_embeddings": len(episodes) + len(topics),
    }
    meta_file.write_text(
        json.dumps({
            "refreshed_at": _iso(refreshed),
            "size_bytes": snap.stat().st_size,
            "counts": counts,
        }, indent=2),
        encoding="utf-8",
    )

    return CorpusHandle(
        dir=tmp_dir, snapshot_path=snap, meta_path=meta_file,
        episodes=episodes, topics=topics, now=now,
    )


def _pack(vec: list[float]) -> bytes:
    import struct

    return struct.pack(f"{len(vec)}f", *vec)


def rewrite_meta(handle: CorpusHandle, *, refreshed_at: datetime) -> None:
    """Mutate an already-built corpus's meta.json in place (stale / reconnect
    scenario: age it, then freshen it again, without rebuilding the replica)."""
    data = json.loads(handle.meta_path.read_text(encoding="utf-8"))
    data["refreshed_at"] = _iso(refreshed_at)
    handle.meta_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@contextmanager
def installed_corpus(tmp_dir: Path, **build_kwargs: Any):
    """Build a corpus under ``tmp_dir`` and patch the local recall path onto
    it for the duration of the ``with`` block: ``hub_snapshot.snapshot_path``/
    ``meta_path`` point at the replica just written, and
    ``recall_prompt._cached_query_embed`` returns ``pseudo_embed`` instead of
    calling a real embedding API — so ``khipu.recall_prompt`` runs genuinely
    end to end against this fixture with no database and no network. Yields
    the ``CorpusHandle`` so a test can also read/mutate the raw rows.
    """
    from khipu import hub_snapshot as hs
    from khipu import recall_prompt as rp

    profile_dim = build_kwargs.get("profile_dim", PROFILE_DIM)
    handle = build_corpus(tmp_dir, **build_kwargs)

    def _fake_query_embed(prompt: str, profile: str) -> list[float]:  # noqa: ARG001
        return pseudo_embed(prompt, dim=profile_dim)

    with mock.patch.object(hs, "snapshot_path", return_value=handle.snapshot_path), \
         mock.patch.object(hs, "meta_path", return_value=handle.meta_path), \
         mock.patch.object(rp, "_cached_query_embed", side_effect=_fake_query_embed):
        yield handle
