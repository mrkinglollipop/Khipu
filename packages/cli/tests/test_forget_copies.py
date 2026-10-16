"""Forget finds every copy of a forgotten line wherever Khipu keeps one, and
never reports complete while a copy stays or a place cannot be read.

The places: every active legacy log ``drift.episode_files`` knows (rotated
years included), ``.bak`` copies beside them, the retained forget backups,
temporary files a crashed rewrite left behind, and every outbox job,
crash-left temporary job files included, and temporary replica files a
crashed refresh left. A memory root that is not configured, or a place that
cannot be checked, keeps the forget incomplete, and the result says in plain
words what it keeps by design."""
from __future__ import annotations

import argparse
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests import test_forget_ownership as fo
from khipu import cli, forget, recall_rule
from khipu import hub_snapshot as hs
from khipu import mcp_server as srv
from tests.test_forget_ownership import BASE, md5, run_forget


class ForgetCopiesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a = fo.ForgetOwnershipTest._payload(None, "2026-10-01T09:00:00Z", "forgotten copy", "codex:A")
        self.other = {"ts": "2026-09-30T08:00:00Z", "summary": "an unrelated line"}
        self.world = {"episodes": {1: {"id": 1, "ts": BASE, "summary": self.a["summary"],
                                       "session_id": "codex:A", "raw": dict(self.a),
                                       "deleted": False}},
                      "sources": [fo.ForgetOwnershipTest._source(None, 1, self.a)],
                      "lines": [self.a], "sources_enabled": True}
        (self.root / "episodes.jsonl").write_text(json.dumps(self.other) + "\n", encoding="utf-8")
        # The replica lives in its own folder here, so no test sees another's temps.
        self.replica_dir = self.root / "replica"
        self.replica_dir.mkdir()
        patcher = mock.patch("khipu.hub_snapshot.snapshot_path",
                             return_value=self.replica_dir / "hub_snapshot.sqlite")
        patcher.start()
        self.addCleanup(patcher.stop)

    def put(self, rel: str, lines: list[dict]) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(p) + "\n" for p in lines), encoding="utf-8")
        return path

    def holds_copy(self, path: Path) -> bool:
        return self.a["summary"] in path.read_text(encoding="utf-8")

    def located(self, out: dict, path: Path) -> dict:
        found = [loc for loc in out["locations"] if loc["path"] == str(path)]
        self.assertEqual(len(found), 1, out["locations"])
        return found[0]

    def test_a_copy_in_a_rotated_log_is_removed(self):
        rotated = self.put("episodes_2026.jsonl", [self.a, self.other])
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(self.holds_copy(rotated))
        self.assertTrue(out["complete"], out.get("partial_errors"))
        self.assertEqual(self.located(out, rotated)["place"], "legacy log")

    def test_copies_in_bak_files_and_backups_of_a_rotated_log_are_removed(self):
        bak = self.put("episodes.jsonl.bak-20260611", [self.a, self.other])
        backup = self.put(".khipu-forget-backups/episodes_2026.jsonl.20261001T000000Z",
                          [self.a, self.other])
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(self.holds_copy(bak))
        self.assertFalse(self.holds_copy(backup))
        self.assertTrue(out["complete"], out.get("partial_errors"))
        self.assertEqual(self.located(out, bak)["place"], "legacy copy")
        self.assertEqual(self.located(out, backup)["place"], "forget backup")

    def test_a_crash_left_legacy_temp_holding_the_copy_keeps_the_forget_partial(self):
        for rel in ("episodes_2026.jsonl.tmp", ".khipu-forget-backups/.episodes.jsonl.20261001T000000Z.tmp"):
            with self.subTest(rel=rel):
                temp = self.put(rel, [self.a])
                out = run_forget(self.world, 1, self.root)
                self.assertFalse(out["complete"])
                self.assertIn("legacy_file", out["partial"])
                self.assertEqual(self.located(out, temp)["state"], "copy remains")
                temp.unlink()

    def test_an_unreadable_rotated_log_keeps_the_forget_partial(self):
        rotated = self.root / "episodes_2026.jsonl"
        rotated.write_bytes(b"\xff\xfe not utf-8\n")
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertIn("legacy_file", out["partial"])
        self.assertEqual(self.located(out, rotated)["state"], "unreadable")

    def test_a_crash_left_outbox_temp_is_removed_when_whole_and_kept_when_torn(self):
        envelope = json.dumps({"payload": self.a, "attempts": 0})
        for rel, body in (("outbox/.capture.json.k2x9.tmp", envelope),
                          ("outbox/dead/.capture.json.k2x9.tmp", envelope),
                          ("outbox/.capture.json.k2x9.tmp", envelope[:40])):
            torn = len(body) < len(envelope)
            with self.subTest(rel=rel, torn=torn):
                temp = self.root / rel
                temp.parent.mkdir(parents=True, exist_ok=True)
                temp.write_text(body, encoding="utf-8")
                out = run_forget(self.world, 1, self.root)
                if torn:
                    self.assertFalse(out["complete"])
                    self.assertIn("outbox", out["partial"])
                    self.assertTrue(any(str(temp) in item for item in out["remaining"]),
                                    out["remaining"])
                    temp.unlink()
                else:
                    self.assertTrue(out["complete"], out.get("partial_errors"))
                    self.assertFalse(temp.exists())
                    self.assertEqual(self.located(out, temp)["state"], "removed")

    def test_a_readable_temp_without_the_copy_does_not_block_completion(self):
        self.put("episodes_2026.jsonl.tmp", [self.other])
        temp = self.root / "outbox" / ".other.json.k2x9.tmp"
        temp.parent.mkdir(parents=True)
        temp.write_text(json.dumps({"payload": {**self.other, "session_id": "codex:Z"}}),
                        encoding="utf-8")
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(out["complete"], out.get("partial_errors"))


    def locked(self, directory: Path) -> None:
        """Names listable, entries not stat-able; restored before cleanup."""
        directory.chmod(0o444)
        self.addCleanup(directory.chmod, 0o755)

    def test_a_symlinked_log_is_erased_at_its_target_and_the_link_survives(self):
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        target = Path(elsewhere.name) / "episodes_2026.jsonl"
        target.write_text(json.dumps(self.a) + "\n" + json.dumps(self.other) + "\n",
                          encoding="utf-8")
        link = self.root / "episodes_2026.jsonl"
        link.symlink_to(target)
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(link.is_symlink())
        self.assertFalse(self.holds_copy(target))
        self.assertIn(self.other["summary"], target.read_text(encoding="utf-8"))
        self.assertTrue(out["complete"], out.get("partial_errors"))
        self.assertEqual(self.located(out, link)["state"], "removed")

    def test_a_backup_in_a_directory_that_denies_stat_keeps_the_forget_partial(self):
        backup = self.put(".khipu-forget-backups/episodes.jsonl.20261001T000000Z", [self.a])
        self.locked(backup.parent)
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertEqual(self.located(out, backup)["state"], "unreadable")

    def test_an_unlistable_root_reports_legacy_logs_not_checked(self):
        """A searchable root can hide a rotated copy when names cannot list."""
        self.put("episodes_2026.jsonl", [self.a])
        self.root.chmod(0o311)
        self.addCleanup(self.root.chmod, 0o755)
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        location = self.located(out, self.root)
        self.assertEqual(location["place"], "legacy logs")
        self.assertEqual(location["state"], "not checked")
        self.assertIn("directory could not be listed", location["why"])

    def test_an_outbox_job_in_a_directory_that_denies_stat_keeps_the_forget_partial(self):
        job = self.root / "outbox" / "dead" / "capture.json"
        job.parent.mkdir(parents=True)
        job.write_text(json.dumps({"payload": self.a, "attempts": 0}), encoding="utf-8")
        self.locked(job.parent)
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertIn("outbox", out["partial"])
        self.assertEqual(self.located(out, job)["state"], "unreadable")

    def test_a_line_with_a_lone_surrogate_is_unidentified_not_a_crash(self):
        log = self.root / "episodes.jsonl"
        log.write_text('{"ts":"2026-10-01T09:00:00Z","summary":"\\ud800"}\n'
                       + json.dumps(self.a) + "\n", encoding="utf-8")
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(self.holds_copy(log))
        self.assertFalse(out["complete"])
        self.assertIn("legacy_file", out["partial"])
        self.assertEqual(self.located(out, log)["state"], "unidentified records")

    def test_copies_under_any_episodes_name_are_removed(self):
        copies = [self.put(name, [self.a, self.other])
                  for name in ("episodes.jsonl.orig", "episodes.jsonl~", "episodes.jsonl.backup")]
        out = run_forget(self.world, 1, self.root)
        for path in copies:
            self.assertFalse(self.holds_copy(path), path)
            self.assertEqual(self.located(out, path)["place"], "legacy copy")
        self.assertTrue(out["complete"], out.get("partial_errors"))

    def test_a_fraction_past_microseconds_rounds_like_postgres(self):
        later = BASE + timedelta(seconds=1)
        self.assertEqual(forget._instant("2026-10-01T09:00:00.9999999Z"), later)
        line = {"ts": "2026-10-01T09:00:00.9999999Z", "summary": "rounded line"}
        world = {"episodes": {1: {"id": 1, "ts": later, "summary": line["summary"],
                                  "session_id": "codex:A", "raw": {}, "deleted": False}},
                 "sources": [], "lines": [line], "sources_enabled": False}
        log = self.put("episodes.jsonl", [line, self.other])
        out = run_forget(world, 1, self.root)
        self.assertNotIn("rounded line", log.read_text(encoding="utf-8"))
        self.assertEqual(out["identity"]["summary_md5"], md5("rounded line"))


    def forget_without_root(self) -> dict:
        with mock.patch("khipu.db.connect", side_effect=lambda *a, **k: fo._Conn(fo._WorldCur(self.world))), \
                mock.patch("khipu.hub_snapshot.forget_episode_in_snapshot", return_value={"ok": True}), \
                mock.patch("khipu.config.path_setting", return_value=None), \
                mock.patch.dict(os.environ, {"KHIPU_OUTBOX": str(self.root / "outbox")}):
            return forget.forget_everywhere(1)

    def test_no_memory_root_means_legacy_logs_were_not_checked(self):
        out = self.forget_without_root()
        self.assertFalse(out["complete"])
        self.assertIn("legacy_file", out["partial"])
        self.assertEqual([loc for loc in out["locations"] if loc["place"] == "legacy logs"],
                         [{"place": "legacy logs", "path": None, "state": "not checked",
                           "why": "no memory root is configured, so legacy logs could not be checked"}])
        self.assertIn("no memory root is configured", out["message"])

    def test_the_cli_says_why_in_plain_words(self):
        result = {"ok": False, "id": 1, "complete": False, "partial": ["legacy_file"],
                  "locations": [{"place": "legacy logs", "path": None, "state": "not checked",
                                 "why": forget.NO_ROOT_WHY}]}
        err = io.StringIO()
        with mock.patch("khipu.forget.forget_everywhere", return_value=result), \
                redirect_stdout(io.StringIO()), redirect_stderr(err):
            code = cli.cmd_episode(argparse.Namespace(episode_cmd="forget", id=1))
        self.assertEqual(code, 1)
        self.assertIn("not completely forgotten", err.getvalue())
        self.assertIn("no memory root is configured, so legacy logs could not be checked",
                      err.getvalue())
        self.assertIn("No command purges it yet", err.getvalue())

    def replica_temp(self, name: str, *, rows=(), raw: bytes | None = None) -> Path:
        path = self.replica_dir / name
        if raw is not None:
            path.write_bytes(raw)
            return path
        con = sqlite3.connect(str(path))
        hs._create_schema(con)
        for eid, summary in rows:
            con.execute("INSERT INTO episodes (id, ts, summary) VALUES (?, ?, ?)",
                        (eid, self.a["ts"], summary))
        con.commit()
        con.close()
        return path

    def test_a_crash_left_replica_temp_holding_the_row_keeps_the_forget_partial(self):
        temp = self.replica_temp(".hub_snapshot.k2x9ab.sqlite", rows=[(1, self.a["summary"])])
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertIn("replica_temp", out["partial"])
        self.assertEqual(self.located(out, temp), {"place": "replica temp", "path": str(temp),
                                                   "state": "copy remains"})
        self.assertTrue(temp.is_file(), "a replica temp is reported, never edited")

    def test_a_leftover_replica_temp_holding_the_row_is_removed(self):
        temp = self.replica_temp(".hub_snapshot.k2x9ab.sqlite", rows=[(1, self.a["summary"])])
        journal = Path(str(temp) + "-journal")
        journal.write_bytes(b"pages")
        old = time.time() - hs.STALE_TEMP_AGE_S - 60
        for path in (temp, journal):
            os.utime(path, (old, old))
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(out["complete"], out.get("partial_errors"))
        self.assertEqual(self.located(out, temp), {"place": "replica temp", "path": str(temp),
                                                   "state": "removed"})
        self.assertFalse(temp.exists())
        self.assertFalse(journal.exists())

    def test_a_torn_replica_temp_keeps_the_forget_partial(self):
        temp = self.replica_temp(".hub_snapshot.k2x9ab.sqlite", raw=b"torn replica page " * 300)
        out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertIn("replica_temp", out["partial"])
        self.assertEqual(self.located(out, temp)["state"], "unreadable")

    def test_a_replica_temp_without_the_row_does_not_block_completion(self):
        temp = self.replica_temp(".hub_snapshot.k2x9ab.sqlite", rows=[(9, "another episode")])
        self.replica_temp(".hub_snapshot.meta.k2x9ab.json", raw=b"{}")
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(out["complete"], out.get("partial_errors"))
        self.assertEqual(self.located(out, temp)["state"], "clean")
        self.assertFalse(any(".hub_snapshot.meta." in str(loc["path"]) for loc in out["locations"]))

    def test_a_running_refresh_leaves_replica_temps_not_checked(self):
        with mock.patch("khipu.hub_snapshot._acquire_refresh_lock", return_value=None):
            out = run_forget(self.world, 1, self.root)
        self.assertFalse(out["complete"])
        self.assertIn("replica_temp", out["partial"])
        found = [loc for loc in out["locations"] if loc["place"] == "replica temp"]
        self.assertEqual([loc["state"] for loc in found], ["not checked"])

    def test_the_result_says_what_it_keeps(self):
        out = run_forget(self.world, 1, self.root)
        self.assertTrue(out["complete"], out.get("partial_errors"))
        self.assertIn("keep this episode's row and text, marked forgotten", out["kept"])
        self.assertIn("No command purges it yet, and no command restores it.", out["kept"])
        self.assertTrue(out["message"].endswith(out["kept"]))
        tool = next(t for t in srv.TOOLS if t["name"] == "khipu_forget")
        self.assertIn("no command purges it yet, and no command restores it",
                      tool["description"])
        self.assertIn("no command purges or restores it yet", recall_rule.RULE_MD)
        self.assertNotIn("best-effort", recall_rule.RULE_MD.split("khipu_forget", 1)[1][:300])
        with mock.patch.object(srv, "_via_https_gateway", return_value=False), \
                mock.patch("khipu.forget.forget_everywhere",
                           return_value={"ok": True, "id": 1, "complete": True, "partial": []}):
            result = srv._tool_forget({"id": 1})
        self.assertIn("No command purges it yet", result["kept"])
        self.assertIn("No command purges it yet", result["message"])


if __name__ == "__main__":
    unittest.main()
