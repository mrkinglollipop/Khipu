"""Forget finds every copy of a forgotten line wherever Khipu keeps one, and
never reports complete while a copy stays or a place cannot be read.

The places: every active legacy log ``drift.episode_files`` knows (rotated
years included), ``.bak`` copies beside them, the retained forget backups,
temporary files a crashed rewrite left behind, and every outbox job,
crash-left temporary job files included."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests import test_forget_ownership as fo
from khipu import forget
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

    def test_a_crash_left_outbox_temp_keeps_the_forget_partial(self):
        envelope = json.dumps({"payload": self.a, "attempts": 0})
        for rel, body in (("outbox/.capture.json.k2x9.tmp", envelope),
                          ("outbox/dead/.capture.json.k2x9.tmp", envelope),
                          ("outbox/.capture.json.k2x9.tmp", envelope[:40])):
            with self.subTest(rel=rel, torn=len(body) < len(envelope)):
                temp = self.root / rel
                temp.parent.mkdir(parents=True, exist_ok=True)
                temp.write_text(body, encoding="utf-8")
                out = run_forget(self.world, 1, self.root)
                self.assertFalse(out["complete"])
                self.assertIn("outbox", out["partial"])
                self.assertTrue(any(str(temp) in item for item in out["remaining"]), out["remaining"])
                temp.unlink()

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


if __name__ == "__main__":
    unittest.main()
