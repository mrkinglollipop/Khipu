"""Forget on the HTTPS gateway, and forget's handling of queued capture jobs.

The gateway is a hub-only server: it sets its own marker at startup and by
design keeps no legacy logs, so with no memory root a gateway forget reports
those logs as not applicable and can be complete. A Mac without a memory root
still reports them as not checked.

A queued capture job that is an exact copy of one of the forgotten episode's
sources (by source key or payload hash) is removed under the outbox lock, in
the queue, the dead folder, quarantine and crash-left temps. A job that cannot
be read, that matches only loosely, or that another live episode also holds
is kept and named with the reason."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import capture_identity as ci
from khipu import forget, outbox
from khipu import mcp_server as srv
from tests import test_forget_ownership as fo
from tests.test_forget_ownership import BASE, run_forget


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a = fo.ForgetOwnershipTest._payload(None, "2026-10-01T09:00:00Z", "forgotten copy",
                                                 "codex:A")
        self.world = {"episodes": {1: {"id": 1, "ts": BASE, "summary": self.a["summary"],
                                       "session_id": "codex:A", "raw": dict(self.a),
                                       "deleted": False}},
                      "sources": [fo.ForgetOwnershipTest._source(None, 1, self.a)],
                      "lines": [self.a], "sources_enabled": True}
        self.replica_dir = self.root / "replica"
        self.replica_dir.mkdir()
        patcher = mock.patch("khipu.hub_snapshot.snapshot_path",
                             return_value=self.replica_dir / "hub_snapshot.sqlite")
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(self.root / "outbox")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(srv.GATEWAY_ACTIVE_ENV, None)

    def located(self, out: dict, path: Path) -> dict:
        found = [loc for loc in out["locations"] if loc["path"] == str(path)]
        self.assertEqual(len(found), 1, out["locations"])
        return found[0]

    def job(self, rel: str, payload: dict | None = None, *, body: str | None = None) -> Path:
        path = self.root / "outbox" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if body is None:
            body = json.dumps({"payload": payload if payload is not None else self.a,
                               "attempts": 0})
        path.write_text(body, encoding="utf-8")
        return path


class GatewayForgetTest(_Base):
    def forget_without_root(self, env: dict[str, str]) -> dict:
        with mock.patch("khipu.db.connect",
                        side_effect=lambda *a, **k: fo._Conn(fo._WorldCur(self.world))), \
                mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot", return_value={"ok": True}), \
                mock.patch("khipu.config.path_setting", return_value=None), \
                mock.patch.dict(os.environ, env):
            return forget.forget_everywhere(1)

    def test_a_gateway_forget_with_no_memory_root_is_complete(self):
        out = self.forget_without_root({srv.GATEWAY_ACTIVE_ENV: "1"})
        self.assertTrue(out["complete"], out.get("partial_errors"))
        legacy = [loc for loc in out["locations"] if loc["place"] == "legacy logs"]
        self.assertEqual(len(legacy), 1, out["locations"])
        self.assertEqual(legacy[0]["state"], "not applicable")
        self.assertIn("gateway", legacy[0]["why"])
        self.assertIn(legacy[0]["why"], out["message"])

    def test_the_marker_the_gateway_sets_at_startup_is_the_signal(self):
        with mock.patch.object(srv, "_GATEWAY_ACTIVE", False), \
                mock.patch.dict(os.environ, {}):
            srv.mark_gateway_active()
            out = self.forget_without_root({})
        self.assertTrue(out["complete"], out.get("partial_errors"))

    def test_a_forget_off_the_gateway_with_no_memory_root_is_still_not_checked(self):
        for env in ({}, {srv.GATEWAY_ACTIVE_ENV: "0"}, {srv.GATEWAY_ACTIVE_ENV: ""}):
            with self.subTest(env=env):
                out = self.forget_without_root(env)
                self.assertFalse(out["complete"])
                self.assertIn("legacy_file", out["partial"])
                legacy = [loc for loc in out["locations"] if loc["place"] == "legacy logs"]
                self.assertEqual([loc["state"] for loc in legacy], ["not checked"])

    def test_a_gateway_with_a_memory_root_still_checks_it(self):
        log = self.root / "episodes.jsonl"
        log.write_bytes(b"\xff\xfe not utf-8\n")
        with mock.patch.dict(os.environ, {srv.GATEWAY_ACTIVE_ENV: "1"}):
            out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertEqual(self.located(out, log)["state"], "unreadable")


class OutboxRemovalTest(_Base):
    def test_exact_copies_are_removed_from_every_outbox_place(self):
        queued = outbox.enqueue(dict(self.a))
        dead = self.job("dead/capture.json")
        quarantined = self.job("quarantine/capture.json")
        error = self.root / "outbox" / "quarantine" / "capture.error"
        error.write_text(json.dumps({"source": "capture.json", "error": "x"}), encoding="utf-8")
        temp = self.job(".capture.json.k2x9.tmp")
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(out["complete"], out.get("partial_errors"))
        for path in (queued, dead, quarantined, temp):
            self.assertFalse(path.exists(), path)
            self.assertEqual(self.located(out, path)["state"], "removed")
        self.assertFalse(error.exists(), "a removed quarantined job takes its error note with it")
        self.assertEqual(out["outbox"]["removed"], 4)
        self.assertIn("removed 4 queued capture jobs", out["message"])

    def test_a_job_unredacted_on_disk_is_still_an_exact_copy(self):
        from khipu.capture import _redact_capture_values
        import copy

        secret = "sk-ant-" + "A" * 40
        a = dict(self.a, summary=f"key {secret}")
        redacted = _redact_capture_values(copy.deepcopy(a))
        self.assertNotEqual(redacted, a)
        self.world["episodes"][1]["summary"] = redacted["summary"]
        self.world["sources"] = [fo.ForgetOwnershipTest._source(None, 1, redacted)]
        queued = self.job("capture.json", a)
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(queued.exists())
        self.assertEqual(self.located(out, queued)["state"], "removed")

    def test_an_unrelated_job_is_left_alone(self):
        other = dict(self.a, summary="another capture", transcript_range="10:20",
                     ts="2026-10-01T10:00:00Z")
        queued = outbox.enqueue(other)
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(queued.exists())
        self.assertEqual(self.located(out, queued)["state"], "clean")
        self.assertTrue(out["complete"], out.get("partial_errors"))


class OutboxKeptTest(_Base):
    def test_an_unreadable_job_is_kept_and_named(self):
        torn = self.job("capture.json", body=json.dumps({"payload": self.a})[:40])
        torn_temp = self.job(".capture.json.k2x9.tmp", body="{\"payload\": ")
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertIn("outbox", out["partial"])
        for path in (torn, torn_temp):
            self.assertTrue(path.exists(), path)
            loc = self.located(out, path)
            self.assertEqual(loc["state"], "unreadable")
            self.assertIn("could not be read", loc["why"])
            self.assertIn(loc["why"], out["message"])
        self.assertEqual(out["outbox"]["removed"], 0)

    def test_a_session_only_match_without_source_rows_is_kept(self):
        self.world["sources"] = []
        self.world["sources_enabled"] = False
        queued = outbox.enqueue(dict(self.a))
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(queued.exists())
        self.assertFalse(out["complete"])
        self.assertEqual(out["remaining"], ["outbox: not cleaned (capture identity needed)"])
        loc = self.located(out, queued)
        self.assertEqual(loc["state"], "copy remains")
        self.assertIn("same session", loc["why"])
        self.assertIn(loc["why"], out["message"])

    def test_a_match_by_time_and_summary_alone_is_kept(self):
        loose = dict(self.a, session_id="codex:Z", transcript_range="40:50")
        queued = self.job("capture.json", loose)
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(queued.exists())
        self.assertFalse(out["complete"])
        self.assertIn("outbox", out["partial"])
        loc = self.located(out, queued)
        self.assertEqual(loc["state"], "copy remains")
        self.assertIn("only by time and summary", loc["why"])
        self.assertIn(loc["why"], out["message"])

    def test_a_job_another_live_episode_holds_is_kept(self):
        receipt = {"source_id": ci.payload_sha256(self.a), "ts": self.a["ts"]}
        self.world["episodes"][2] = {"id": 2, "ts": BASE.replace(hour=11),
                                     "summary": "a later episode", "session_id": "codex:B",
                                     "raw": {"ts": "2026-10-01T11:00:00Z",
                                             "summary": "a later episode",
                                             "merged_from": [receipt]},
                                     "deleted": False}
        queued = outbox.enqueue(dict(self.a))
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(queued.exists())
        self.assertFalse(out["complete"])
        self.assertIn("outbox", out["partial"])
        loc = self.located(out, queued)
        self.assertEqual(loc["state"], "copy remains")
        self.assertIn("another live episode", loc["why"])
        self.assertIn(loc["why"], out["message"])

    def test_without_an_episode_id_nothing_is_removed(self):
        queued = outbox.enqueue(dict(self.a))
        before = queued.read_bytes()
        out = forget._outbox_remainder("codex:A", source_rows=self.world["sources"])
        self.assertEqual(queued.read_bytes(), before)
        self.assertFalse(out["ok"])
        self.assertEqual(out["removed"], 0)

    def test_the_tool_says_jobs_are_removed_and_why_some_are_kept(self):
        tool = next(t for t in srv.TOOLS if t["name"] == "khipu_forget")
        self.assertIn("queued capture job", tool["description"])
        self.assertIn("removed", tool["description"])
        self.assertIn("kept", tool["description"])



class OutboxReportTest(_Base):
    """What the report says when a job, its note or the whole outbox cannot
    be handled: it names what happened, never a copy nobody has seen."""

    def rows(self) -> list[dict]:
        return [{"source_key": ci.source_key(self.a), "payload_sha256": ci.payload_sha256(self.a)}]

    def remainder(self) -> dict:
        with mock.patch("khipu.forget._job_owners", return_value=(set(), set())):
            return forget._outbox_remainder("codex:A", source_rows=self.rows(), episode_id=1)

    def test_a_removed_job_whose_note_stays_is_reported_removed(self):
        quarantined = self.job("quarantine/capture.json")
        note = self.root / "outbox" / "quarantine" / "capture.error"
        note.mkdir()  # unlinking a directory fails, as a locked note would
        out = self.remainder()
        self.assertFalse(quarantined.exists())
        self.assertEqual(out["removed"], 1)
        job = [loc for loc in out["inspected"] if loc["path"] == str(quarantined)]
        self.assertEqual([loc["state"] for loc in job], ["removed"], out["inspected"])
        kept = [loc for loc in out["inspected"] if loc["path"] == str(note)]
        self.assertEqual(len(kept), 1, out["inspected"])
        self.assertEqual(kept[0]["state"], "not checked")
        self.assertIn("note", kept[0]["why"])
        self.assertNotIn("so it was kept", " ".join(loc.get("why", "") for loc in job))

    def test_one_job_that_cannot_be_hashed_does_not_stop_the_others(self):
        other = self.job("a-other.json", body=json.dumps(
            {"payload": {"ts": "2026-10-02T10:00:00Z", "summary": "other \ud800 capture",
                         "session_id": "x"}}))
        mine = self.job("b-mine.json")
        out = self.remainder()
        self.assertNotIn("error", out)
        self.assertFalse(mine.exists())
        self.assertEqual(out["removed"], 1)
        bad = [loc for loc in out["inspected"] if loc["path"] == str(other)]
        self.assertEqual(len(bad), 1, out["inspected"])
        self.assertEqual(bad[0]["state"], "unreadable")
        self.assertIn("UnicodeEncodeError", bad[0]["why"])
        self.assertTrue(other.exists())
        self.assertFalse(out["ok"])

    def test_an_outbox_that_cannot_be_scanned_is_not_checked_not_a_copy(self):
        # A file where the outbox folder should be: nothing can be scanned or locked.
        (self.root / "outbox").write_text("not a directory", encoding="utf-8")
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertIn("outbox", out["partial"])
        self.assertNotIn("still holds a copy", out["message"])
        self.assertNotIn("capture identity needed", " ".join(out["remaining"]))
        loc = [loc for loc in out["locations"] if loc["place"] == "outbox"]
        self.assertEqual(len(loc), 1, out["locations"])
        self.assertEqual(loc[0]["state"], "not checked")
        self.assertIn("could not be checked", loc[0]["why"])
        self.assertIn(loc[0]["why"], out["message"])


class DrainAfterForgetTest(_Base):
    """What a queued exact copy does when the queue drains after a forget."""

    def hub(self):
        from tests.test_capture_identity import _SourcesFakeCursor

        cur = _SourcesFakeCursor({1: {"id": 1, "ts": self.a["ts"], "summary": self.a["summary"],
                                      "session_id": "codex:A", "harness": "codex",
                                      "transcript_range": "0:10", "decisions": [],
                                      "preferences": [], "topics": [], "raw": dict(self.a),
                                      "deleted_at": "2026-10-15T00:00:00Z"}})
        ident = ci.source_identity(dict(self.a))
        cur.sources[ident["source_key"]] = {"episode_id": 1, "role": "origin",
                                            "payload_sha256": ident["payload_sha256"]}
        return cur

    def drain(self, cur) -> tuple[dict, mock.Mock, mock.Mock]:
        from khipu import db
        from tests.test_capture import _FakeConn

        db._TABLE_COLUMNS_CACHE.clear()
        self.addCleanup(db._TABLE_COLUMNS_CACHE.clear)
        with mock.patch("khipu.db.connect", return_value=_FakeConn(cur)), \
                mock.patch("khipu.config.path_setting", return_value=None), \
                mock.patch("khipu.topic_graph.persist_capture_graph", return_value={}), \
                mock.patch("khipu.capture.run_capture_v2") as mirror, \
                mock.patch("khipu.embed.embed_on_capture") as embed:
            return outbox.drain(), mirror, embed

    def test_a_drained_exact_copy_lands_on_the_tombstone_and_brings_nothing_back(self):
        outbox.enqueue(dict(self.a))
        cur = self.hub()
        result, mirror, embed = self.drain(cur)
        self.assertEqual(result["replayed"], 1)
        self.assertEqual(list(cur.episodes), [1], "no new row")
        self.assertIsNone(cur.last_inserted_id)
        self.assertEqual(cur.episodes[1]["deleted_at"], "2026-10-15T00:00:00Z")
        self.assertFalse(any(s.startswith("UPDATE episodes") for s, _p in cur.calls),
                         "nothing un-deletes or rewrites the forgotten row")
        mirror.assert_not_called()
        embed.assert_not_called()

    def test_after_the_forget_there_is_nothing_left_to_drain(self):
        outbox.enqueue(dict(self.a))
        run_forget(self.world, 1, self.root)
        result, _mirror, _embed = self.drain(self.hub())
        self.assertEqual((result["jobs"], result["replayed"]), (0, 0))
        self.assertEqual(outbox.status()["pending"], 0)


if __name__ == "__main__":
    unittest.main()
