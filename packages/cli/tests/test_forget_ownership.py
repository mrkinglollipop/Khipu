"""Forget never erases a legacy line another live episode holds, and never
reports complete while a copy of the forgotten episode's line remains.

Every deletion candidate, the episode's own live identity included, is
checked against every other live episode: its live row, its raw payload and
merge receipts, and its source rows. Times are compared as UTC instants, so a
line minted with an offset (11:00:00+02:00) is the same line as the source
row's 09:00:00+00:00.

The worlds here are built by ``build_world`` and answered by ``_WorldCur``, an
in-memory stand-in for the statements forget_episode runs. The same worlds run
against a real scratch Postgres in test_forget_ownership_scratch.py."""
from __future__ import annotations

import hashlib
import json
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from khipu import capture_identity as ci
from khipu import db, forget

BASE = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
POOL = ("alpha note", "beta note", "gamma note", "delta note")
ZONES = (timezone.utc, timezone(timedelta(hours=2)), timezone(timedelta(hours=-5, minutes=-30)),
         timezone(timedelta(hours=9)), timezone(timedelta(hours=-11)))


def md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def instant(value) -> datetime | None:
    """The test's own reading of a timestamp: UTC, to the second; naive is UTC."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0)


def stamp(rng: random.Random, at: datetime) -> str:
    """One instant written the way some writer might have minted it."""
    zone = rng.choice(ZONES)
    style = rng.randrange(4)
    if zone is timezone.utc and style == 0:
        return at.strftime("%Y-%m-%dT%H:%M:%SZ")
    if zone is timezone.utc and style == 1:
        return at.replace(tzinfo=None).isoformat()
    local = at.astimezone(zone)
    return local.isoformat(timespec="milliseconds") if style == 2 else local.isoformat()


def build_world(rng: random.Random, *, sources_enabled: bool) -> dict:
    """Episodes that share, edit and merge legacy lines across time zones.

    Returns {"episodes": {id: ep}, "sources": [row], "lines": [payload],
    "sources_enabled": bool}. An episode is {"id", "ts" (aware datetime),
    "summary", "session_id", "raw", "deleted"}; a source row is
    {"episode_id", "role", "ts" (aware datetime), "summary_md5", "source_key",
    "payload_sha256"}."""
    episodes: dict[int, dict] = {}
    sources: list[dict] = []
    lines: list[dict] = []

    def payload(session: str) -> dict:
        at = BASE + timedelta(minutes=rng.randrange(3))
        return {"ts": stamp(rng, at), "summary": rng.choice(POOL), "harness": "codex",
                "session_id": session, "transcript_range": "0:10"}

    def source(eid: int, role: str, p: dict) -> None:
        if sources_enabled and rng.random() > 0.15:  # recording fails open
            sources.append({"episode_id": eid, "role": role,
                            "ts": instant(p["ts"]).astimezone(rng.choice(ZONES)),
                            "summary_md5": md5(p["summary"]), "source_key": ci.source_key(p),
                            "payload_sha256": ci.payload_sha256(p)})

    for eid in range(1, rng.randint(2, 5) + 1):
        origin = payload(f"codex:e{eid}")
        lines.append(origin)
        raw = dict(origin)
        source(eid, "origin", origin)
        summary = origin["summary"]
        for k in range(rng.randrange(3)):
            merged = payload(f"codex:e{eid}m{k}")
            lines.append(merged)
            receipt = {"source_id": ci.payload_sha256(merged), "payload": merged, "ts": merged["ts"]}
            if rng.random() < 0.1:
                del receipt["payload"]
            raw.setdefault("merged_from", []).append(receipt)
            source(eid, "merged", merged)
        if rng.random() < 0.5:  # an edit frees the original key for someone else
            summary = rng.choice(POOL)
        live_ts = instant(origin["ts"]).astimezone(rng.choice(ZONES))
        if rng.random() < 0.2:  # a line written under the live identity
            lines.append({"ts": stamp(rng, live_ts), "summary": summary})
        episodes[eid] = {"id": eid, "ts": live_ts, "summary": summary,
                         "session_id": f"codex:e{eid}", "raw": raw,
                         "deleted": rng.random() < 0.2}
    lines.append({"ts": "2026-09-30T08:00:00Z", "summary": "an unrelated line"})
    rng.shuffle(lines)
    return {"episodes": episodes, "sources": sources, "lines": lines,
            "sources_enabled": sources_enabled}


def holdings(world: dict, eid: int) -> set[tuple[str, datetime]]:
    """Every (summary md5, instant) legacy identity one episode holds."""
    ep = world["episodes"][eid]
    held = {(md5(ep["summary"]), instant(ep["ts"]))}
    raw = ep["raw"]
    held.add((md5(raw["summary"]), instant(raw["ts"])))
    for receipt in raw.get("merged_from", []):
        if "payload" in receipt:
            held.add((md5(receipt["payload"]["summary"]), instant(receipt["payload"]["ts"])))
    for row in world["sources"]:
        if row["episode_id"] == eid:
            held.add((row["summary_md5"], instant(row["ts"])))
    return held


def line_identity(line: str) -> tuple[str, datetime]:
    obj = json.loads(line)
    return md5(obj["summary"]), instant(obj["ts"])


class _WorldCur:
    """The statements forget_episode runs, answered from a world."""

    def __init__(self, world: dict):
        self.world = world
        self.rowcount = 0
        self._one = None
        self._all: list = []
        db._TABLE_COLUMNS_CACHE.clear()

    def _others(self, eid: int):
        for other in self.world["episodes"].values():
            if other["id"] != eid and not other["deleted"]:
                yield other

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self.rowcount = 0
        self._all = []
        eps = self.world["episodes"]
        if "information_schema.columns" in s:
            if params[0] == "episode_sources" and self.world["sources_enabled"]:
                self._all = [("source_key",)]
        elif s.startswith("SELECT ts, summary, session_id FROM episodes"):
            ep = eps.get(params[0])
            self._one = (ep["ts"], ep["summary"], ep["session_id"]) if ep else None
        elif s.startswith("SELECT harness, session_id, transcript_range FROM episodes"):
            ep = eps.get(params[0])
            self._one = ((ep.get("harness"), ep["session_id"], ep.get("transcript_range"))
                         if ep else None)
        elif s.startswith("SELECT raw FROM episodes"):
            self._one = (eps[params[0]]["raw"],)
        elif s.startswith("SELECT source_key, role, ts, summary_md5"):
            self._all = [(r["source_key"], r["role"], r["ts"], r["summary_md5"], r["payload_sha256"])
                         for r in self.world["sources"] if r["episode_id"] == params[0]]
        elif "AS held" in s:
            # (md5, timestamptz, text ts) for every identity another live
            # episode holds whose md5 is a candidate's.
            wanted = set(params["md"])
            rows = []
            for other in self._others(params["id"]):
                rows.append((md5(other["summary"]), other["ts"], None))
                raw = other["raw"]
                rows.append((md5(raw["summary"]), None, raw["ts"]))
                for receipt in raw.get("merged_from", []):
                    if "payload" in receipt:
                        rows.append((md5(receipt["payload"]["summary"]), None,
                                     receipt["payload"]["ts"]))
                if "episode_sources" in s:
                    rows += [(r["summary_md5"], r["ts"], None) for r in self.world["sources"]
                             if r["episode_id"] == other["id"]]
            self._all = [r for r in rows if r[0] in wanted]
        elif "AS owned" in s:
            # Payload hashes another live episode holds: a source row's payload
            # hash or source key, or a raw merge receipt's source id.
            wanted = set(params["h"])
            rows = []
            for other in self._others(params["id"]):
                for receipt in other["raw"].get("merged_from", []):
                    rows.append((receipt.get("source_id"),))
                if "episode_sources" in s:
                    for r in self.world["sources"]:
                        if r["episode_id"] == other["id"]:
                            rows += [(r["payload_sha256"],), (r["source_key"],)]
            self._all = [r for r in rows if r[0] in wanted]
        elif "unnest(" in s:
            # The pre-fix query: positions of source-derived candidates another
            # episode's live row or source rows hold.
            ts_list, md_list, eid = params[0], params[1], params[2]
            held = set()
            for other in self._others(eid):
                held.add((md5(other["summary"]), instant(other["ts"])))
                held |= {(r["summary_md5"], instant(r["ts"])) for r in self.world["sources"]
                         if r["episode_id"] == other["id"]}
            self._all = [(n,) for n, (t, m) in enumerate(zip(ts_list, md_list), 1)
                         if (m, instant(t)) in held]
        elif s.startswith("UPDATE episodes SET deleted_at"):
            ep = eps[params[0]]
            self.rowcount = 0 if ep["deleted"] else 1
            ep["deleted"] = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def run_forget(world: dict, eid: int, root: Path, connect=None) -> dict:
    """forget_everywhere on one episode of ``world`` with ``root`` as the
    memory root. ``connect`` defaults to the in-memory world."""
    connect = connect or (lambda *a, **k: _Conn(_WorldCur(world)))
    with mock.patch("khipu.db.connect", side_effect=connect), \
            mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot", return_value={"ok": True}), \
            mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(root / "outbox")}):
        return forget.forget_everywhere(eid, memory_root=root)


def write_lines(root: Path, lines: list[dict]) -> None:
    (root / "episodes.jsonl").write_text("".join(json.dumps(p) + "\n" for p in lines),
                                         encoding="utf-8")


def remaining_lines(root: Path) -> list[str]:
    files = [root / "episodes.jsonl", *(root / ".khipu-forget-backups").glob("episodes.jsonl.*")]
    return [ln for f in files if f.is_file()
            for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]


def check_forget(test: unittest.TestCase, world: dict, eid: int, root: Path, out: dict,
                 before: list[str], context) -> None:
    """The two properties, judged from the world as it stood before the forget."""
    mine = holdings(world, eid)
    others = set().union(*(holdings(world, o) for o, ep in world["episodes"].items()
                           if o != eid and not ep["deleted"]))
    after = (root / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
    removed = list(before)
    for ln in after:
        removed.remove(ln)
    for ln in removed:
        ident = line_identity(ln)
        test.assertIn(ident, mine, (context, "removed a line the episode never held", ln))
        test.assertNotIn(ident, others, (context, "removed a line another live episode holds", ln))
    if out["complete"]:
        left = [ln for ln in remaining_lines(root) if line_identity(ln) in mine]
        test.assertEqual(left, [], (context, "complete while a copy remains", out))


class ForgetOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _payload(self, ts: str, summary: str, session: str) -> dict:
        return {"ts": ts, "summary": summary, "harness": "codex", "session_id": session,
                "transcript_range": "0:10"}

    def _source(self, eid: int, p: dict) -> dict:
        return {"episode_id": eid, "role": "origin", "ts": instant(p["ts"]),
                "summary_md5": md5(p["summary"]), "source_key": ci.source_key(p),
                "payload_sha256": ci.payload_sha256(p)}

    def test_a_live_identity_another_episodes_source_holds_is_kept(self):
        # B's summary was edited, freeing its original key; A's live summary
        # then took that key. B's source row still holds the original line.
        a = self._payload("2026-10-01T09:00:00Z", "A original", "codex:A")
        b = self._payload("2026-10-01T09:00:00Z", "B original", "codex:B")
        world = {"episodes": {
            1: {"id": 1, "ts": BASE, "summary": "B original", "session_id": "codex:A",
                "raw": dict(a), "deleted": False},
            2: {"id": 2, "ts": BASE, "summary": "B edited", "session_id": "codex:B",
                "raw": dict(b), "deleted": False}},
            "sources": [self._source(1, a), self._source(2, b)], "lines": [a, b],
            "sources_enabled": True}
        write_lines(self.root, [a, b])
        out = run_forget(world, 1, self.root)
        left = [json.loads(ln)["summary"] for ln in remaining_lines(self.root)]
        self.assertIn("B original", left)
        self.assertNotIn("A original", left)
        self.assertFalse(out["complete"])
        self.assertIn("legacy_shared", out["partial"])

    def hash_only_world(self, *, via: str) -> tuple[dict, dict]:
        """B holds A's legacy line by payload hash alone: a merge receipt
        without its payload and, with ``via="source"``, a source row whose
        legacy identity was never recorded (summary_md5 NULL)."""
        a = self._payload("2026-10-01T09:00:00Z", "shared capture A", "codex:A")
        b = self._payload("2026-10-01T09:01:00Z", "live episode B", "codex:B")
        digest = ci.payload_sha256(a)
        raw_b = dict(b)
        sources = [self._source(1, a), self._source(2, b)]
        if via == "receipt":
            raw_b["merged_from"] = [{"source_id": digest, "ts": a["ts"],
                                     "session_id": a["session_id"]}]
        else:
            sources.append({"episode_id": 2, "role": "merged", "ts": BASE, "summary_md5": None,
                            "source_key": digest, "payload_sha256": digest})
        world = {"episodes": {
            1: {"id": 1, "ts": BASE, "summary": a["summary"], "session_id": "codex:A",
                "raw": dict(a), "deleted": False},
            2: {"id": 2, "ts": BASE.replace(minute=1), "summary": b["summary"],
                "session_id": "codex:B", "raw": raw_b, "deleted": False}},
            "sources": sources, "lines": [a, b], "sources_enabled": True}
        return world, a

    def test_a_line_another_episode_holds_only_by_payload_hash_is_kept(self):
        for via in ("source", "receipt"):
            with self.subTest(via=via), tempfile.TemporaryDirectory() as td:
                root = Path(td)
                world, a = self.hash_only_world(via=via)
                write_lines(root, world["lines"])
                out = run_forget(world, 1, root)
                left = [json.loads(ln) for ln in remaining_lines(root)]
                self.assertIn(a, left)
                self.assertFalse(out["complete"])
                self.assertIn("legacy_shared", out["partial"])

    def test_a_line_minted_with_an_offset_is_removed(self):
        # The line says 11:00:00+02:00; PG and the source row say 09:00:00 UTC.
        c = self._payload("2026-10-01T11:00:00+02:00", "offset captured evidence", "codex:A")
        world = {"episodes": {1: {"id": 1, "ts": BASE, "summary": c["summary"],
                                  "session_id": "codex:A", "raw": dict(c), "deleted": False}},
                 "sources": [self._source(1, c)], "lines": [c], "sources_enabled": True}
        write_lines(self.root, [c])
        out = run_forget(world, 1, self.root)
        self.assertEqual(remaining_lines(self.root), [])
        self.assertTrue(out["complete"], out.get("partial_errors"))

    def test_a_line_whose_time_cannot_be_read_keeps_the_forget_partial(self):
        c = self._payload("2026-10-01T09:00:00Z", "kept evidence", "codex:A")
        world = {"episodes": {1: {"id": 1, "ts": BASE, "summary": c["summary"],
                                  "session_id": "codex:A", "raw": dict(c), "deleted": False}},
                 "sources": [self._source(1, c)], "lines": [], "sources_enabled": True}
        write_lines(self.root, [c, {"ts": "yesterday-ish", "summary": c["summary"]}])
        out = run_forget(world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertIn("legacy_file", out["partial"])

    def test_same_identity_compares_instants_not_text(self):
        self.assertTrue(forget._same_identity(
            {"ts": "2026-10-01T11:00:00+02:00", "summary_md5": "m"},
            {"ts": "2026-10-01T09:00:00+00:00", "summary_md5": "m"}))
        self.assertFalse(forget._same_identity(
            {"ts": "2026-10-01T09:00:00+02:00", "summary_md5": "m"},
            {"ts": "2026-10-01T09:00:00+00:00", "summary_md5": "m"}))
        self.assertFalse(forget._same_identity({"ts": "garbage", "summary_md5": "m"},
                                               {"ts": "garbage", "summary_md5": "m"}))
        for edge in ("0001-01-01T00:00:00+05:00", "9999-12-31T23:59:59-05:00"):
            self.assertIsNone(forget._instant(edge))

    def test_a_line_at_the_calendar_edge_keeps_the_forget_partial_not_a_crash(self):
        c = self._payload("2026-10-01T09:00:00Z", "edge evidence", "codex:A")
        world = {"episodes": {1: {"id": 1, "ts": BASE, "summary": c["summary"],
                                  "session_id": "codex:A", "raw": dict(c), "deleted": False}},
                 "sources": [self._source(1, c)], "lines": [], "sources_enabled": True}
        write_lines(self.root, [c, {"ts": "0001-01-01T00:00:00+05:00", "summary": c["summary"]}])
        out = run_forget(world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertIn("legacy_file", out["partial"])

    def test_property_forget_never_takes_a_held_line_or_overclaims(self):
        for seed in range(300):
            rng = random.Random(seed)
            world = build_world(rng, sources_enabled=rng.random() < 0.8)
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                write_lines(root, world["lines"])
                order = [e for e, ep in world["episodes"].items() if not ep["deleted"]]
                rng.shuffle(order)
                if not order:  # every episode already forgotten
                    continue
                for eid in order[:rng.randint(1, len(order))]:
                    before = (root / "episodes.jsonl").read_text(encoding="utf-8").splitlines()
                    snapshot = json.loads(json.dumps(world, default=str))
                    frozen = {**world, "episodes": {k: dict(v) for k, v in
                                                    world["episodes"].items()}}
                    out = run_forget(world, eid, root)
                    check_forget(self, frozen, eid, root, out, before, (seed, eid, snapshot))


if __name__ == "__main__":
    unittest.main()
