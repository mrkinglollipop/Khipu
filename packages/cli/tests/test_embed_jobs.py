# --bypass-harness (sonnet lane) — authored directly by the dispatched on-sub
# agent (brief: do not delegate); no further agent to route to.
"""Embedding jobs with receipts (khipu.embed_jobs, the --job paths of
`embed backfill` and `library backfill`). Progress files live in a temp data
dir; the hub is the in-memory fake the library tests use; no network."""
from __future__ import annotations

import argparse
import io
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import cli, embed, embed_jobs, library
from tests import test_embed as te
from tests import test_library as tl


class StubJob:
    """The two things a backfill asks of a job: update() and cancelled."""

    def __init__(self, cancel_when_done_at_least: int | None = None):
        self.calls: list[tuple[int, int, int]] = []
        self._limit = cancel_when_done_at_least

    @property
    def cancelled(self) -> bool:
        return self._limit is not None and bool(self.calls) and self.calls[-1][0] >= self._limit

    def update(self, done, total, failed):
        self.calls.append((done, total, failed))


class _DataDir(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": self.tmp.name})
        patch.start()
        self.addCleanup(patch.stop)
        self.jobs = Path(self.tmp.name) / "jobs"

    def read(self, job_id):
        return json.loads((self.jobs / f"{job_id}.json").read_text())

    def run_job(self, work, **kw):
        out = io.StringIO()
        rc = embed_jobs.run_job(kind="embed-backfill", profile="p@3", space="memory",
                                work=work, stream=out, **kw)
        return rc, out.getvalue()


class JobFileTest(_DataDir):
    def test_update_writes_the_documented_fields_atomically(self):
        job = embed_jobs.Job("j1", kind="embed-backfill", profile="p@3", space="memory")
        job.update(5, 20, 1)
        row = self.read("j1")
        self.assertEqual(
            set(row),
            {"job", "kind", "profile", "space", "state", "done", "total", "failed",
             "started_at", "updated_at", "error", "pid"},
        )
        self.assertEqual((row["state"], row["done"], row["total"], row["failed"], row["error"]),
                         ("running", 5, 20, 1, None))
        self.assertEqual(row["pid"], os.getpid())
        self.assertEqual(list(self.jobs.glob("*.tmp")), [])

    def test_a_job_id_is_validated(self):
        for bad in ("", "../x", "a b", "-x", "x" * 81):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                embed_jobs.validate_job_id(bad)
        self.assertEqual(embed_jobs.validate_job_id("memory-1.a_b"), "memory-1.a_b")

    def test_generated_ids_are_valid_and_distinct(self):
        # --bypass-harness (sonnet lane): dispatched agent, brief says do not delegate.
        ids = [embed_jobs.new_job_id("library:biblical") for _ in range(20)]
        for job_id in ids:
            self.assertRegex(job_id, embed_jobs.JOB_ID_RE.pattern)
            self.assertTrue(job_id.startswith("library-biblical-"))
        self.assertGreater(len(set(ids)), 1)


class RunJobTest(_DataDir):
    def test_a_clean_run_is_done_and_prints_one_json_line(self):
        def work(job):
            job.update(2, 4, 0)
            return {"embedded": 4, "failed_chunks": 0}

        rc, printed = self.run_job(work, job_id="ok1")
        self.assertEqual(rc, 0)
        lines = printed.strip().splitlines()
        self.assertEqual(len(lines), 1)
        receipt = json.loads(lines[0])
        self.assertEqual((receipt["ok"], receipt["state"], receipt["job"]), (True, "done", "ok1"))
        row = self.read("ok1")
        self.assertEqual((row["state"], row["done"], row["total"], row["failed"]), ("done", 4, 4, 0))

    def test_partial_is_done_with_the_failed_count_not_hidden(self):
        rc, _ = self.run_job(lambda job: (job.update(3, 10, 0), {"embedded": 3, "failed_chunks": 7})[1],
                             job_id="part")
        row = self.read("part")
        self.assertEqual(rc, 0)
        self.assertEqual((row["state"], row["done"], row["total"], row["failed"]), ("done", 3, 10, 7))

    def test_everything_failed_is_failed_with_a_reason(self):
        rc, printed = self.run_job(lambda job: {"embedded": 0, "failed_chunks": 5}, job_id="allbad")
        self.assertEqual(rc, 1)
        row = self.read("allbad")
        self.assertEqual(row["state"], "failed")
        self.assertIn("5 chunks", row["error"])
        self.assertFalse(json.loads(printed)["ok"])

    def test_a_missing_key_says_so(self):
        _, _ = self.run_job(lambda job: {"embedded": 0, "failed": 3, "embed_provider": "missing key"},
                            job_id="nokey")
        self.assertEqual(self.read("nokey")["error"], "no API key for this provider")

    def test_budget_exhaustion_is_failed_and_resumable(self):
        rc, _ = self.run_job(lambda job: {"embedded": 64, "budget_exhausted": True}, job_id="bud")
        row = self.read("bud")
        self.assertEqual((rc, row["state"], row["done"]), (1, "failed", 64))
        self.assertIn("budget", row["error"])

    def test_an_exception_is_a_failed_receipt_not_a_traceback(self):
        def work(job):
            raise RuntimeError("hub unreachable")

        rc, printed = self.run_job(work, job_id="boom")
        self.assertEqual(rc, 1)
        self.assertEqual(self.read("boom")["error"], "RuntimeError: hub unreachable")
        self.assertEqual(json.loads(printed)["state"], "failed")

    def test_sigterm_finishes_the_batch_then_writes_cancelled_and_exits_0(self):
        before = signal.getsignal(signal.SIGTERM)

        def work(job):
            job.update(2, 10, 0)
            self.assertFalse(job.cancelled)
            os.kill(os.getpid(), signal.SIGTERM)  # the app stopping the job
            self.assertTrue(job.cancelled)
            job.update(4, 10, 0)  # the batch in flight completes and is recorded
            return {"embedded": 4, "failed_chunks": 0}

        rc, printed = self.run_job(work, job_id="term")
        self.assertEqual(rc, 0)
        row = self.read("term")
        self.assertEqual((row["state"], row["done"], row["total"]), ("cancelled", 4, 10))
        self.assertEqual(json.loads(printed)["state"], "cancelled")
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_a_cancelled_flag_from_the_backfill_is_cancelled_too(self):
        rc, _ = self.run_job(lambda job: {"embedded": 1, "cancelled": True}, job_id="c2")
        self.assertEqual((rc, self.read("c2")["state"]), (0, "cancelled"))


class ListAndClearTest(_DataDir):
    def write(self, job_id, **over):
        row = {"job": job_id, "kind": "embed-backfill", "profile": "p@3", "space": "memory",
               "state": "done", "done": 1, "total": 1, "failed": 0,
               "started_at": "2026-10-07T10:00:00+00:00", "updated_at": "2026-10-07T10:00:01+00:00",
               "error": None, "pid": os.getpid()}
        row.update(over)
        self.jobs.mkdir(parents=True, exist_ok=True)
        (self.jobs / f"{job_id}.json").write_text(json.dumps(row))

    def test_list_is_newest_first_and_skips_garbage(self):
        self.write("old", started_at="2026-10-06T10:00:00+00:00")
        self.write("new", started_at="2026-10-07T10:00:00+00:00")
        (self.jobs / "junk.json").write_text("{not json")
        (self.jobs / "list.json").write_text("[1]")
        self.assertEqual([r["job"] for r in embed_jobs.list_jobs()], ["new", "old"])

    def test_a_running_job_whose_process_died_is_reported_failed(self):
        self.write("ghost", state="running", pid=2_000_000_000)
        rows = embed_jobs.list_jobs()
        self.assertEqual(rows[0]["state"], "failed")
        self.assertIn("exited without finishing", rows[0]["error"])
        self.assertEqual(self.read("ghost")["state"], "failed")  # and the file now says so

    def test_a_live_running_job_stays_running(self):
        self.write("live", state="running", pid=os.getpid())
        self.assertEqual(embed_jobs.list_jobs()[0]["state"], "running")

    def test_clear_removes_only_finished_jobs(self):
        for jid, state in (("a", "done"), ("b", "failed"), ("c", "cancelled"), ("d", "running")):
            self.write(jid, state=state)
        out = embed_jobs.clear_jobs()
        self.assertEqual(out, {"ok": True, "removed": 3})
        self.assertEqual([p.stem for p in self.jobs.glob("*.json")], ["d"])


class MemoryBackfillJobHookTest(unittest.TestCase):
    """embed.backfill(job=...): progress after each batch, cancel between them."""

    def _run(self, job, n_rows):
        rows = [(f"note:n{i}", f"Title {i}", "short body") for i in range(n_rows)]
        cur = te._BackfillCursor(rows)
        conn = te._BackfillConn(cur)
        calls = {"n": 0}

        def _embed(api, profile, retries=None, delay=None):
            calls["n"] += 1
            return [[0.0] * embed.DIM for _ in api]

        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch.object(embed, "embed_batch", side_effect=_embed), \
                mock.patch.object(embed.time, "sleep", lambda s: None):
            stats = embed.backfill(kind="topic", job=job)
        return stats, calls["n"]

    def test_progress_is_reported_after_every_batch(self):
        job = StubJob()
        stats, batches = self._run(job, embed.BATCH + 1)
        self.assertEqual(batches, 2)
        self.assertEqual(job.calls, [(0, embed.BATCH + 1, 0), (embed.BATCH, embed.BATCH + 1, 0),
                                     (embed.BATCH + 1, embed.BATCH + 1, 0)])
        self.assertNotIn("cancelled", stats)

    def test_cancel_stops_after_the_batch_in_flight(self):
        job = StubJob(cancel_when_done_at_least=embed.BATCH)
        stats, batches = self._run(job, embed.BATCH * 2 + 1)
        self.assertEqual(batches, 1)
        self.assertTrue(stats["cancelled"])
        self.assertEqual(stats["embedded"], embed.BATCH)

    def test_a_failed_batch_is_reported_in_the_failed_count(self):
        rows = [(f"note:n{i}", f"Title {i}", "short body") for i in range(3)]
        cur = te._BackfillCursor(rows)
        job = StubJob()

        def _embed(api, profile, retries=None, delay=None):
            raise RuntimeError("embed HTTP 500: nope")

        with mock.patch("khipu.db.connect", return_value=te._BackfillConn(cur)), \
                mock.patch.object(embed, "embed_batch", side_effect=_embed), \
                mock.patch.object(embed.time, "sleep", lambda s: None):
            embed.backfill(kind="topic", job=job)
        self.assertEqual(job.calls[-1], (0, 3, 3))


class LibraryBackfillJobHookTest(tl._Base):
    def setUp(self):
        super().setUp()
        self.add()
        for i in range(7):
            self.write(f"A/doc{i}.txt", f"document number {i}")
        library.scan(self.hub, "lib1")
        patch = mock.patch.object(embed, "BATCH", 3)
        patch.start()
        self.addCleanup(patch.stop)

    def run_backfill(self, job, **kw):
        embed.set_transport(tl._voyage_transport())
        self.addCleanup(embed.set_transport, None)
        return library.backfill(self.hub, "lib1", job=job, **kw)

    def test_total_is_what_is_left_and_progress_follows_each_batch(self):
        job = StubJob()
        out = self.run_backfill(job)
        self.assertEqual(job.calls, [(0, 7, 0), (3, 7, 0), (6, 7, 0), (7, 7, 0)])
        self.assertEqual(out["embedded"], 7)

    def test_limit_caps_the_total(self):
        job = StubJob()
        self.run_backfill(job, limit=4)
        self.assertEqual(job.calls[0], (0, 4, 0))
        self.assertEqual(job.calls[-1], (4, 4, 0))

    def test_cancel_stops_before_the_next_batch_and_skips_the_index_build(self):
        job = StubJob(cancel_when_done_at_least=3)
        out = self.run_backfill(job)
        self.assertTrue(out["cancelled"])
        self.assertEqual((out["embedded"], out["remaining"]), (3, 4))
        self.assertEqual([s for s, _ in self.hub.executed if s.startswith("CREATE INDEX")], [])

    def test_a_profile_override_embeds_under_that_profile_and_leaves_the_pointer(self):
        other = tl.ProfileSpec("voyage-o@4", "voyage", "voyage-o", 4)
        self.hub.profile_rows[other.id] = ("voyage", "voyage-o", 4, "l2", None)
        out = self.run_backfill(None, profile=other.id)
        self.assertEqual((out["profile"], out["embedded"]), (other.id, 7))
        self.assertEqual({k[0] for k in self.hub.embs}, {other.id})
        self.assertEqual(self.hub.sources["lib1"]["profile"], tl.TINY.id)


class CliJobTest(_DataDir):
    def _run(self, fn, **kw):
        args = argparse.Namespace(**kw)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = fn(args)
        return rc, out.getvalue()

    def test_a_memory_job_needs_a_profile_and_a_valid_id(self):
        rc, out = self._run(cli.cmd_embed_backfill_job, profile=None, job_id=None, limit=None)
        self.assertEqual((rc, json.loads(out)["ok"]), (2, False))
        rc, out = self._run(cli.cmd_embed_backfill_job, profile="p@3", job_id="../x", limit=None)
        self.assertEqual((rc, json.loads(out)["ok"]), (2, False))

    def test_a_memory_job_runs_the_backfill_with_its_job_and_prints_one_line(self):
        seen = {}

        def fake_backfill(**kw):
            seen.update(kw)
            kw["job"].update(2, 2, 0)
            return {"embedded": 2, "failed_chunks": 0}

        with mock.patch("khipu.embed.backfill", side_effect=fake_backfill):
            rc, out = self._run(cli.cmd_embed_backfill_job, profile="p@3", job_id="m1", limit=7)
        self.assertEqual(rc, 0)
        self.assertEqual((seen["profile"], seen["limit"]), ("p@3", 7))
        self.assertEqual(len(out.strip().splitlines()), 1)
        row = self.read("m1")
        self.assertEqual((row["space"], row["profile"], row["state"], row["done"]),
                         ("memory", "p@3", "done", 2))

    def test_embed_backfill_with_job_flag_routes_to_the_job_runner(self):
        ns = cli.build_parser().parse_args(["embed", "backfill", "--profile", "p@3", "--job", "--job-id", "x1"])
        self.assertTrue(ns.job)
        with mock.patch("khipu.embed.backfill", return_value={"embedded": 0}):
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(cli.cmd_embed(ns), 0)
        self.assertEqual(self.read("x1")["state"], "done")

    def test_a_library_job_records_the_libraries_profile_and_space(self):
        hub = tl.FakeHub()
        hub.sources["lib1"] = {"root": "/x", "profile": tl.TINY.id, "enabled": True}
        with mock.patch("khipu.db.connect", return_value=hub), \
                mock.patch.object(library, "backfill",
                                  return_value={"embedded": 5, "failed": 0}) as bf:
            rc, out = self._run(cli.cmd_library_backfill_job, name="lib1", job_id="l1",
                                profile=None, stale=True, limit=None)
        self.assertEqual(rc, 0)
        self.assertTrue(bf.call_args.kwargs["stale"])
        row = self.read("l1")
        self.assertEqual((row["space"], row["profile"], row["state"]),
                         ("library:lib1", tl.TINY.id, "done"))

    def test_an_unknown_library_is_a_failed_job_file_not_a_missing_one(self):
        with mock.patch("khipu.db.connect", return_value=tl.FakeHub()):
            rc, out = self._run(cli.cmd_library_backfill_job, name="nope", job_id="l2",
                                profile=None, stale=False, limit=None)
        self.assertEqual(rc, 1)
        self.assertEqual(self.read("l2")["state"], "failed")
        self.assertIn("no library named", self.read("l2")["error"])

    def test_the_hyphenated_spawn_wrappers_exist(self):
        p = cli.build_parser()
        a = p.parse_args(["embed-backfill", "--profile", "p@3", "--job-id", "j"])
        b = p.parse_args(["library-backfill", "biblical", "--stale", "--job-id", "j"])
        self.assertEqual((a.func, b.func), (cli.cmd_embed_backfill_job, cli.cmd_library_backfill_job))
        self.assertEqual((b.name, b.stale), ("biblical", True))
        c = p.parse_args(["library", "backfill", "biblical", "--job", "--profile", "q@4"])
        self.assertEqual((c.job, c.profile), (True, "q@4"))


if __name__ == "__main__":
    unittest.main()
