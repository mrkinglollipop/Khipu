"""Capture regressions: in-place same-length rewrite on the idle sweep, and a repeated user line after a compaction replay."""
from __future__ import annotations

import json
import os
import unittest

from khipu import session_capture as sc
from tests import test_idle_sweep as _idle


class IdleSweepEqualLengthRewriteTest(unittest.TestCase):
    setUp = _idle.IdleSweepTest.setUp
    session = _idle.IdleSweepTest.session

    def test_idle_sweep_reads_a_same_length_in_place_rewrite(self):
        path, _ = self.session()
        first = sc.sweep_idle(now=self.now)
        self.assertEqual(first["jobs"], 1)
        data = path.read_bytes()
        new = data.replace(b"Question 0", b"Rewrite? 0")
        self.assertEqual(len(new), len(data))
        ino = path.stat().st_ino
        with open(path, "r+b") as fh:
            fh.write(new)
        self.assertEqual(path.stat().st_ino, ino)
        os.utime(path, (self.old - 5, self.old - 5))
        second = sc.sweep_idle(now=self.now)
        self.assertGreaterEqual(second["jobs"] + second["read"], 1,
                                f"same-inode same-length rewrite never re-read: {second}")


class CompactionReplayRealRepeatTest(unittest.TestCase):
    def test_a_user_repeating_an_old_line_after_a_compaction_replay_is_still_a_turn(self):
        import tempfile
        from pathlib import Path
        U = "Please rerun the whole release checklist and tell me what is still open."
        R = "The checklist is done except the notarization step, which needs credentials."
        X = "I looked at the build logs and every target compiled without a warning."

        def user(t, **kw):
            return {"type": "user", "message": {"role": "user", "content": t}, **kw}

        def asst(t, mid):
            return {"type": "assistant", "message": {"id": mid, "role": "assistant",
                                                     "content": [{"type": "text", "text": t}]}}
        rows = [user("Summary of the conversation so far, written by the compactor.", isCompactSummary=True),
                asst(X, "m_x"),
                user(U),
                asst(R, "m_new")]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.jsonl"
            p.write_text("".join(json.dumps(r) + "\n" for r in rows))
            seen = [sc.text_fingerprint("user", U), sc.text_fingerprint("assistant", R),
                    sc.text_fingerprint("assistant", X),
                    sc._message_identity(asst(X, "m_x"), "assistant", sc.text_fingerprint("assistant", X))]
            stats: dict = {}
            msgs, _off, turns = sc.read_window(p, 0, seen=seen, stats=stats)
        self.assertTrue(any(U in t for _r, t in msgs), f"a genuine repeated user line was dropped as a copy: {msgs} {stats.get('deduped_msgs')}")
