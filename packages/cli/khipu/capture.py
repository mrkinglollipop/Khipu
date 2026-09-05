"""``khipu capture`` — the one capture entrypoint every harness hook calls (P3 step 2).

Reads a capture payload (same JSON shape ``capture_v2.py`` accepts) from stdin or
``--payload-file`` and routes it by ``capture_mode``:

  legacy  → shell out to capture_v2 only. Khipu writes nothing here; capture_v2's own
            fail-open mirror still runs if KHIPU_MIRROR permits (unchanged behavior).
  dual    → write PG FIRST and DURABLY (a PG failure is a real exit code, not a
            warning — that is what makes Khipu a second *writer* rather than a mirror),
            then shell out to capture_v2 for the file wiki with KHIPU_MIRROR=0 so it
            does not mirror the same episode a second time.
  hub     → write PG only. The file wiki is maintained by the reverse-mirror
            (``khipu regen-memory`` today; full materialize is P3 end-state).

Identity is minted ONCE here (``ts``, seconds precision, ``Z``) and passed to both
writers, so the PG row and the file line share the (ts, md5(summary)) key the
reconcile upserts on — the P2b dual-mint bug can't come back through this path.

Exit codes mirror capture_v2: 0 ok · 64 no payload · 65 bad payload · 70 PG write
failed (dual/hub) · capture_v2's own code if the file leg fails.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from khipu.config import capture_mode

# ---- W1.4 ingest dedup ------------------------------------------------------
#
# Two shapes of duplicate measured 2026-09-03: (a) the exact same transcript
# window re-queued (a crash between offset-advance and job removal, a retried
# drain) and (b) two independent captures of the same real conversation in
# the same project within a few minutes (a dispatched child session, the
# Stop/compaction overlap the audit found). (a) is an identity match — skip
# outright. (b) is a similarity match — merge into the earlier row rather
# than insert a second episode for one conversation.

DEDUP_WINDOW_MINUTES = 5
DEDUP_CANDIDATE_LIMIT = 20
DEDUP_JACCARD_THRESHOLD = 0.6  # fallback only; cosine uses config dedup_similarity


def _md5_hex(text: str) -> str:
    return hashlib.md5((text or "").encode("utf-8")).hexdigest()


def _dedup_text(payload_like: dict[str, Any]) -> str:
    parts = [payload_like.get("summary") or ""]
    decisions = payload_like.get("decisions") or []
    if isinstance(decisions, list):
        parts.append(" ".join(str(d) for d in decisions))
    return " ".join(parts)


def _jaccard(a: str, b: str) -> float:
    ta = set(re.findall(r"[a-z0-9]+", a.lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _dedup_exact_window(cur, payload: dict[str, Any]) -> int | None:
    harness = payload.get("harness")
    session_id = payload.get("session_id")
    tr = payload.get("transcript_range")
    if not harness or not session_id or not tr:
        return None
    cur.execute(
        "SELECT id FROM episodes WHERE harness = %s AND session_id = %s "
        "AND transcript_range = %s LIMIT 1",
        (harness, session_id, tr),
    )
    row = cur.fetchone()
    return int(row[0]) if row else None


def _inherit_project(cur, payload: dict[str, Any]) -> None:
    """W1.2b: identity.resolve_repo_root cannot resolve a scratchpad/`/tmp`
    cwd — repo_root and project both come back None, which is exactly the
    dispatched-child-session shape the origin audit found (episode 11286).
    When lineage IS known (parent_session_id), inherit project/repo_root from
    the most recent episode in the last 24h that shares it — either as that
    row's OWN parent_session_id (a sibling dispatched from the same host) or
    as that row's session_id (the parent episode itself). Mutates payload in
    place; never raises — a miss here just leaves project/repo_root None and
    the caller falls through to the (harness, session_id, transcript_range)
    exact-window dedup only, same as today."""
    if payload.get("project") or payload.get("repo_root"):
        return
    parent = payload.get("parent_session_id")
    if not parent:
        return
    try:
        cur.execute(
            """
            SELECT project, repo_root
            FROM episodes
            WHERE (parent_session_id = %s OR session_id = %s)
              AND deleted_at IS NULL
              AND project IS NOT NULL
              AND ts >= now() - interval '24 hours'
            ORDER BY ts DESC
            LIMIT 1
            """,
            (parent, parent),
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001 — inheritance is best-effort, never blocks a write
        return
    if not row:
        return
    project, repo_root = row
    if project:
        payload["project"] = project
        _log(f"inherited project={project!r} from parent_session_id={parent!r}")
    if repo_root:
        payload["repo_root"] = repo_root


def _dedup_candidates(cur, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Candidate rows for the similarity leg, grouped the same way a caller
    would filter for "this same conversation": by project when it is known,
    else by parent_session_id (a dispatched child with no resolvable project
    still shares lineage with its siblings). Neither known → no candidates;
    only the exact-window skip in dedup_before_insert still applies."""
    ts = payload.get("ts")
    project = payload.get("project")
    parent = payload.get("parent_session_id")
    if not ts or (not project and not parent):
        return []
    group_col, group_val = ("project", project) if project else ("parent_session_id", parent)
    try:
        cur.execute(
            f"""
            SELECT id, summary, decisions, preferences, topics
            FROM episodes
            WHERE {group_col} = %s
              AND deleted_at IS NULL
              AND ts BETWEEN %s::timestamptz - interval '5 minutes'
                         AND %s::timestamptz + interval '5 minutes'
            ORDER BY ts DESC
            LIMIT %s
            """,
            (group_val, ts, ts, DEDUP_CANDIDATE_LIMIT),
        )
    except Exception:  # noqa: BLE001 — dedup is best-effort, never blocks a write
        return []
    cols = ("id", "summary", "decisions", "preferences", "topics")
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _dedup_cosine_best(
    cur, payload: dict[str, Any], candidates: list[dict[str, Any]], threshold: float
) -> tuple[int, float, str] | None:
    """Best cosine match at/above threshold, or None (no key, no embeddings
    yet for these candidates, or any failure) — caller falls back to Jaccard."""
    if not candidates:
        return None
    try:
        from khipu.embed import _active_profile, _vec_literal, embed_one

        profile = _active_profile(cur)
        vec = embed_one(_dedup_text(payload), profile=profile)
    except Exception:  # noqa: BLE001
        return None
    ids = [str(c["id"]) for c in candidates]
    try:
        cur.execute(
            """
            SELECT ref, 1 - (embedding <=> %s::vector) AS score
            FROM memory_embeddings
            WHERE profile = %s AND kind = 'episode' AND chunk_idx = 0 AND ref = ANY(%s)
            ORDER BY score DESC
            LIMIT 1
            """,
            (_vec_literal(vec), profile, ids),
        )
        row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return None
    if not row or row[1] is None:
        return None
    ref, score = row
    score = float(score)
    if score < threshold:
        return None
    return int(ref), score, "cosine"


def _dedup_jaccard_best(
    payload: dict[str, Any], candidates: list[dict[str, Any]], threshold: float
) -> tuple[int, float, str] | None:
    text = _dedup_text(payload)
    best: tuple[int, float, str] | None = None
    for c in candidates:
        score = _jaccard(text, _dedup_text(c))
        if score >= threshold and (best is None or score > best[1]):
            best = (int(c["id"]), score, "jaccard")
    return best


def _union_json_list(existing: Any, new_items: Any) -> list[Any]:
    out = list(existing or [])

    def _key(v: Any) -> Any:
        return json.dumps(v, sort_keys=True, default=str) if isinstance(v, (dict, list)) else v

    seen = {_key(x) for x in out}
    for item in new_items or []:
        k = _key(item)
        if k not in seen:
            out.append(item)
            seen.add(k)
    return out


def _merge_into_episode(
    cur, target_id: int, payload: dict[str, Any], *, matched_via: str, score: float
) -> bool:
    """Fold a near-duplicate capture into the episode it matched.

    Audit 2026-09-04: the merge used to union topics/decisions/preferences
    only, so ``people`` and ``tags`` named by the second capture were dropped
    on the floor; it opened no commitments for the merged payload (an
    ``open_loops`` entry that arrived on the merged side was lost outright,
    unlike the insert path at ``write_pg``); and it left the target row's
    vectors pointing at the pre-merge text, so the new decisions were
    unsearchable semantically until the nightly backfill. All three are fixed
    here, each fail-open — the row update is the durable part and must never
    be taken down by an additive step.
    """
    from khipu.db import has_columns

    try:
        has_tags = has_columns(cur, "episodes", "tags")
    except Exception:  # noqa: BLE001 — schema probe never blocks the merge
        has_tags = False
    cols = "topics, decisions, preferences, people, raw, ts, summary"
    if has_tags:
        cols += ", tags"
    cur.execute(f"SELECT {cols} FROM episodes WHERE id = %s", (target_id,))
    row = cur.fetchone()
    if not row:
        return False
    topics, decisions, preferences, people, raw, target_ts, target_summary = row[:7]
    tags = row[7] if has_tags else None
    new_topics = _union_json_list(topics, payload.get("topics"))
    new_decisions = _union_json_list(decisions, payload.get("decisions"))
    new_preferences = _union_json_list(preferences, payload.get("preferences"))
    new_people = _union_json_list(people, payload.get("people"))
    new_tags = _union_json_list(tags, payload.get("tags")) if has_tags else None
    raw = dict(raw or {})
    merged_from = list(raw.get("merged_from") or [])
    merged_from.append({
        "session_id": payload.get("session_id"),
        "ts": payload.get("ts"),
        "matched_via": matched_via,
        "score": round(score, 4),
    })
    raw["merged_from"] = merged_from
    sets = ["topics = %s::jsonb", "decisions = %s::jsonb", "preferences = %s::jsonb",
            "people = %s::jsonb", "raw = %s::jsonb"]
    params: list[Any] = [
        json.dumps(new_topics, ensure_ascii=False),
        json.dumps(new_decisions, ensure_ascii=False),
        json.dumps(new_preferences, ensure_ascii=False),
        json.dumps(new_people, ensure_ascii=False),
        json.dumps(raw, ensure_ascii=False),
    ]
    if has_tags:
        sets.append("tags = %s::jsonb")
        params.append(json.dumps(new_tags, ensure_ascii=False))
    params.append(target_id)
    cur.execute(f"UPDATE episodes SET {', '.join(sets)} WHERE id = %s", params)

    # The merged capture's own open/closed loops belong to the target episode
    # — same call the insert path makes in write_pg, same SAVEPOINT fail-open.
    try:
        cur.execute("SAVEPOINT capture_merge_commitments")
    except Exception:  # noqa: BLE001 — no savepoint support (fake cur / autocommit)
        pass
    try:
        from khipu import commitments

        commitments.open_from_episode(cur, payload, target_id)
        commitments.auto_close(cur, payload, target_id)
        commitments.close_session_plan(cur, payload, target_id)
    except Exception as exc:  # noqa: BLE001 — the merged row stays; fail-open
        try:
            cur.execute("ROLLBACK TO SAVEPOINT capture_merge_commitments")
        except Exception:  # noqa: BLE001
            pass
        _log(f"merge commitments step failed ({type(exc).__name__}: {exc})")

    # The target's text changed, so its vectors are stale. embed_on_capture
    # finds the row by (ts, md5(summary)) — both unchanged by a merge — and
    # re-embeds the MERGED text we just wrote.
    _reembed_merged_episode(target_ts, target_summary, {
        "summary": target_summary,
        "topics": new_topics,
        "decisions": new_decisions,
        "preferences": new_preferences,
        "people": new_people,
    })
    return True


def _reembed_merged_episode(ts: Any, summary: Any, merged: dict[str, Any]) -> None:
    """Re-embed a merged target row. Fail-open: the episode is already durable
    and `khipu embed backfill` heals any miss."""
    try:
        from khipu.embed import embed_on_capture

        merged = dict(merged)
        merged["ts"] = ts.isoformat() if hasattr(ts, "isoformat") else ts
        merged["summary"] = summary or ""
        if not merged["ts"] or not merged["summary"]:
            return
        embed_on_capture(merged)
    except Exception as exc:  # noqa: BLE001 — vectors are additive
        _log(f"merge re-embed failed ({type(exc).__name__}: {exc})")


def dedup_before_insert(cur, payload: dict[str, Any]) -> dict[str, Any]:
    """W1.4: {'action': 'skip'|'merge'|'none', ...}. Never raises — any
    failure in the similarity path degrades to 'none' (a normal insert)."""
    from khipu.config import float_setting

    exact_id = _dedup_exact_window(cur, payload)
    if exact_id is not None:
        _log(f"dedup: exact window match episode {exact_id} — skipping insert")
        return {"action": "skip", "matched_episode": exact_id, "matched_via": "exact_window"}

    candidates = _dedup_candidates(cur, payload)
    if not candidates:
        return {"action": "none"}
    threshold = float_setting("dedup_similarity")
    best = _dedup_cosine_best(cur, payload, candidates, threshold)
    if best is None:
        best = _dedup_jaccard_best(payload, candidates, DEDUP_JACCARD_THRESHOLD)
    if best is None:
        return {"action": "none"}
    target_id, score, via = best
    if not _merge_into_episode(cur, target_id, payload, matched_via=via, score=score):
        return {"action": "none"}
    _log(
        f"dedup: merged into episode {target_id} via {via} ({score:.2f}), "
        f"project={payload.get('project')!r}"
    )
    return {"action": "merge", "matched_episode": target_id, "matched_via": via, "score": score}



def _capture_v2() -> Path | None:
    from khipu.config import path_setting

    return path_setting("capture_v2")


EX_USAGE, EX_DATAERR, EX_SOFTWARE = 64, 65, 70


def _log(msg: str) -> None:
    print(f"[khipu-capture] {msg}", file=sys.stderr, flush=True)


def _mint_ts() -> str:
    # capture.py's default format: seconds precision, trailing Z.
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def load_payload(raw: str) -> dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        raise SystemExit(EX_USAGE)
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _log(f"invalid JSON: {exc}")
        raise SystemExit(EX_DATAERR)
    # Coerce before stripping: a payload whose summary is a list or a number
    # (a model extraction can produce either) used to raise AttributeError here
    # and exit with a traceback instead of the documented EX_DATAERR (audit
    # 2026-08-17).
    if not isinstance(payload, dict) or not isinstance(payload.get("summary"), str) \
            or not payload["summary"].strip():
        _log("payload missing a string 'summary'")
        raise SystemExit(EX_DATAERR)
    if not isinstance(payload.get("session_id"), str) or not payload["session_id"].strip():
        sid = os.environ.get("CLAUDE_SESSION_ID", "").strip()
        if sid:
            payload["session_id"] = sid
    if not payload.get("ts"):
        payload["ts"] = _mint_ts()
    return payload


def write_pg(payload: dict[str, Any]) -> dict[str, Any]:
    """Durable PG write: episode row + any topic pages already on disk. Raises on failure.

    Runs the same three steps for every writer that reaches here (the native
    extractor via session_capture.drain, the legacy capture_v2-shaped payload
    path in dual/hub mode, and gateway/MCP captures) — W1.4 ingest dedup, then
    W5.1 topic-vs-tag classification, then the episode insert + W3.3
    commitments open/auto-close.
    """
    from khipu.db import connect
    from khipu.mirror import _upsert_episode, _upsert_topic, parse_topic_file

    # Every writer lands here (hook, MCP tool, gateway), so this is the one
    # place a secret in a summary, decision, loop or topic page is masked
    # before it becomes a row, a page or a vector.
    from khipu.redact import redact_payload

    redacted = redact_payload(payload)
    if redacted:
        _log(f"redacted {redacted} secret(s) from the capture payload")
    topics_written = 0
    episode_id: int | None = None
    with connect() as conn:
        with conn.cursor() as cur:
            _inherit_project(cur, payload)
            dedup = dedup_before_insert(cur, payload)
            if dedup["action"] in ("skip", "merge"):
                conn.commit()
                return {
                    "episode_inserted": False,
                    "topics_written": 0,
                    "episode_id": dedup.get("matched_episode"),
                    "dedup": dedup,
                }

            # W5.1: classify capture topics into topic-slugs vs noise-tags
            # (khipu.hygiene.classify_topics — see its docstring for the
            # actual rule). Gated on topic_aliases + episodes.tags existing
            # (migration 0010) and wrapped in its own SAVEPOINT, same as the
            # topic-graph and commitments steps below: a missing table here
            # used to raise mid-transaction and leave PG in
            # InFailedSqlTransaction, silently failing the episode insert
            # and every step after it, on every pre-migration hub.
            from khipu import hygiene
            from khipu.db import has_columns

            hygiene_ready = has_columns(cur, "episodes", "tags") and has_columns(
                cur, "topic_aliases", "alias", "slug"
            )
            if hygiene_ready:
                cur.execute("SAVEPOINT capture_hygiene")
                try:
                    resolved_topics, tags, topics_unresolved = hygiene.classify_topics(
                        cur, payload.get("topics") or []
                    )
                except Exception as exc:  # noqa: BLE001 — degrade, never abort the write
                    resolved_topics, tags, topics_unresolved = list(payload.get("topics") or []), [], True
                    cur.execute("ROLLBACK TO SAVEPOINT capture_hygiene")
                    _log(f"topic hygiene classify raised ({type(exc).__name__}: {exc})")
                else:
                    if topics_unresolved:
                        # classify_topics caught its own PG error and degraded in
                        # Python, but a caught exception does not reset PG's
                        # SERVER-side aborted-transaction state — roll back to
                        # the savepoint to clear it before anything else runs.
                        cur.execute("ROLLBACK TO SAVEPOINT capture_hygiene")
                        _log("topic hygiene classify degraded; rolled back to savepoint")
                    else:
                        cur.execute("RELEASE SAVEPOINT capture_hygiene")
            else:
                # Pre-migration hub: skip classification outright rather than
                # attempt it and abort the write — every non-blank topic
                # passes through unchanged, same as today's un-migrated
                # behaviour, no tags minted (nothing to classify against).
                seen: set[str] = set()
                resolved_topics = []
                for t in payload.get("topics") or []:
                    s = str(t or "").strip().lower()
                    if s and s not in seen:
                        seen.add(s)
                        resolved_topics.append(s)
                tags, topics_unresolved = [], False
            payload = dict(payload)
            payload["topics"] = resolved_topics
            payload["tags"] = list(dict.fromkeys([*(payload.get("tags") or []), *tags]))
            if topics_unresolved:
                payload["topics_unresolved"] = True

            inserted = _upsert_episode(cur, payload)
            from khipu.config import path_setting
            from khipu.topic_graph import persist_capture_graph

            cur.execute("SAVEPOINT capture_graph")
            try:
                persist_capture_graph(cur, payload)
            except Exception as exc:  # noqa: BLE001 — episode row stays; graph is additive
                cur.execute("ROLLBACK TO SAVEPOINT capture_graph")
                _log(f"topic graph mint failed ({type(exc).__name__}: {exc})")

            if inserted:
                cur.execute(
                    "SELECT id FROM episodes WHERE ts = %s::timestamptz AND md5(summary) = %s",
                    (payload.get("ts"), _md5_hex(payload.get("summary") or "")),
                )
                row = cur.fetchone()
                episode_id = int(row[0]) if row else None
                if episode_id is not None:
                    cur.execute("SAVEPOINT capture_commitments")
                    try:
                        from khipu import commitments

                        commitments.open_from_episode(cur, payload, episode_id)
                        commitments.auto_close(cur, payload, episode_id)
                        commitments.close_session_plan(cur, payload, episode_id)
                    except Exception as exc:  # noqa: BLE001 — episode row stays; fail-open
                        cur.execute("ROLLBACK TO SAVEPOINT capture_commitments")
                        _log(f"commitments step failed ({type(exc).__name__}: {exc})")

            memory_root = path_setting("memory_root")
            # In dual mode capture_v2 has not run yet, so topic pages named in the
            # payload may not exist on disk; upsert whatever is already there and let
            # the nightly reconcile / capture_v2's file write catch the rest.
            # No file wiki configured (hub-only install): the episode is the
            # whole capture; there are no topic files to fold in.
            for tp in (payload.get("topic_pages") or []) if memory_root else []:
                slug = (tp.get("slug") or "").strip() if isinstance(tp, dict) else ""
                if not slug:
                    continue
                path = memory_root / "topics" / f"{slug}.md"
                parsed = parse_topic_file(path)
                if parsed and _upsert_topic(
                    cur, parsed, str(path), source="capture", note="khipu capture"
                ):
                    topics_written += 1
        conn.commit()
    return {
        "episode_inserted": inserted,
        "topics_written": topics_written,
        "episode_id": episode_id,
        "dedup": dedup,
    }


def run_capture_v2(payload: dict[str, Any], *, suppress_mirror: bool) -> int:
    capture_v2 = _capture_v2()
    if capture_v2 is None:
        _log("capture_v2 not configured (khipu config --set capture_v2 PATH); "
             "file wiki not written")
        return EX_SOFTWARE
    if not capture_v2.is_file():
        _log(f"capture_v2 not found at {capture_v2} (khipu config --set capture_v2 PATH)")
        return EX_SOFTWARE
    env = dict(os.environ)
    if suppress_mirror:
        env["KHIPU_MIRROR"] = "0"
    try:
        proc = subprocess.run(
            [sys.executable, str(capture_v2)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            capture_output=True,
            env=env,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        # The PG row is already durable when we get here in dual/hub, and the
        # PG-failure path has already queued to the outbox — so a hung file leg
        # is a bad exit code, never an uncaught traceback out of the hook.
        _log(f"capture_v2 timed out after 300s ({capture_v2})")
        return EX_SOFTWARE
    except OSError as exc:
        _log(f"capture_v2 could not run: {type(exc).__name__}: {exc}")
        return EX_SOFTWARE
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    return proc.returncode


def capture(payload: dict[str, Any], *, mode: str | None = None) -> int:
    mode = (mode or capture_mode()).lower()
    if payload.get("scope") == "trivial":
        _log("scope=trivial — skipping per protocol")
        return 0

    if mode == "legacy":
        _log("mode=legacy → capture_v2 only")
        return run_capture_v2(payload, suppress_mirror=False)

    # dual + hub: PG first, durably. If PG is unreachable the payload goes to the
    # outbox (replayed by the next Stop hook / nightly / `khipu outbox drain`)
    # and the file line is still written, so nothing is ever lost and nothing
    # is ever double-written (the replay is the same identity upsert).
    try:
        stats = write_pg(payload)
    except Exception as exc:  # noqa: BLE001 — surfaced, queued, never swallowed
        from khipu.outbox import enqueue

        _log(f"PG write FAILED ({type(exc).__name__}: {exc}) → outbox")
        try:
            enqueue(payload, reason=f"{type(exc).__name__}")
            queued = True
        except Exception as qexc:  # noqa: BLE001 — outbox itself unwritable
            _log(f"outbox enqueue FAILED ({type(qexc).__name__}: {qexc})")
            queued = False
        # The file leg keeps its own mirror ON here: if PG comes back mid-way it
        # lands the row now, and if not it re-queues the same identity (no dup).
        rc = run_capture_v2(payload, suppress_mirror=False)
        if rc != 0:
            return rc
        return 0 if queued else EX_SOFTWARE
    _log(
        f"mode={mode} pg ok episode_inserted={stats['episode_inserted']} "
        f"topics_written={stats['topics_written']} ts={payload.get('ts')}"
    )
    # Vectors ride the capture (P3 step 3). Fail-open by design — the row is
    # already durable and `khipu embed backfill` heals any miss.
    if stats["episode_inserted"]:
        from khipu.embed import embed_on_capture

        embed_on_capture(payload)

    # dual: legacy file wiki is a peer; hub: PG is the record and the file is
    # its reverse mirror (plan: "Hub owns writes ... and can reverse-mirror to
    # files"). Same call either way — the legacy consumers (nightly consolidate,
    # topic amend, MEMORY.md) keep working in both modes. KHIPU_HUB_FILE_MIRROR=0
    # turns the reverse mirror off in hub for a future all-PG world.
    if mode == "hub" and os.environ.get("KHIPU_HUB_FILE_MIRROR", "1").strip().lower() in {"0", "false", "off"}:
        return 0
    return run_capture_v2(payload, suppress_mirror=True)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="khipu capture")
    ap.add_argument("--payload-file", help="Read JSON payload from a file instead of stdin")
    ap.add_argument(
        "--mode",
        choices=("legacy", "dual", "hub"),
        help="Override capture_mode for this run (default: Hub config)",
    )
    args = ap.parse_args(argv)
    raw = Path(args.payload_file).read_text(encoding="utf-8") if args.payload_file else sys.stdin.read()
    payload = load_payload(raw)
    return capture(payload, mode=args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
