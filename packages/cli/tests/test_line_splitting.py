"""JSON Lines and line-based logs split on LF only.

str.splitlines() also breaks at U+2028, U+2029, U+0085, \\v, \\f and
\\x1c-\\x1e. JavaScript's JSON.stringify (and Python's json.dumps with
ensure_ascii=False) leaves U+2028 and U+2029 raw inside strings, so a reader
that used splitlines() cut one record in two and dropped both halves as bad
JSON. Every fixture here is written raw (ensure_ascii=False) on purpose.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from khipu import session_capture as sc

REPO = Path(__file__).resolve().parents[3]
STOP_HOOK = REPO / "packages" / "cli" / "bin" / "khipu-stop-hook"

# Every code point str.splitlines() treats as a line break, beyond LF and CR.
AWKWARD = "a\u2028b\u2029c\u0085d\x0be\x0cf\x1cg\x1dh\x1ei"


def _raw(row) -> str:
    return json.dumps(row, ensure_ascii=False)


def _write_raw(path: Path, rows: list) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("".join(_raw(r) + "\n" for r in rows).encode("utf-8"))
    return path


def _png_b64() -> str:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
           + chunk(b"IDAT", zlib.compress(b"\x00\x01\x02\x03", 9)) + chunk(b"IEND", b""))
    return base64.b64encode(png).decode("ascii")


class TranscriptImageLandTest(unittest.TestCase):
    """land_transcript_images reads the same transcript window as capture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "KHIPU_CAPTURE_HOME": str(self.dir / "kh"),
            "KHIPU_DATA_DIR": str(self.dir / "data"),
        })
        self._env.start()
        from khipu import sources

        sources.save_sources({"sources": [{"id": "conversation_memory"}]})
        sources.set_embed_media("conversation_memory", True)

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_image_record_whose_text_holds_a_line_separator_is_landed(self):
        tp = _write_raw(self.dir / "img.jsonl", [{
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "text", "text": "look at this\u2028please"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": _png_b64()}},
            ]},
        }])
        self.assertEqual(sc.land_transcript_images(tp)["landed"], 1)


class EndToEndCaptureTest(unittest.TestCase):
    """The shipped stop hook queues a raw transcript record intact, and the
    drain's image pass lands the image from that same record and window."""

    def test_every_awkward_code_point_survives_capture(self):
        from khipu import sources

        pieces = ["sep\u2028line", "para\u2029graph", "next\u0085line", "nul\x00byte",
                  "emoji \U0001F600\U0001F44D", "CJK \u8bb0\u5fc6\u4e0d\u4e22", "ctl\x0b\x0c\x1c\x1d\x1e"]
        with tempfile.TemporaryDirectory() as td:
            khome, data = Path(td) / "kh", Path(td) / "data"
            env = {k: v for k, v in os.environ.items() if k != "KHIPU_HARNESS"}
            env.update(KHIPU_CAPTURE_HOME=str(khome), KHIPU_DATA_DIR=str(data),
                       KHIPU_CAPTURE_NO_DRAIN="1", KHIPU_MEMORY_ROOT=str(Path(td) / "mem"))
            with mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(khome), "KHIPU_DATA_DIR": str(data)}):
                sources.save_sources({"sources": [{"id": "conversation_memory"}]})
                sources.set_embed_media("conversation_memory", True)
                tp = _write_raw(Path(td) / ".claude" / "projects" / "p" / "s.jsonl", [
                    {"type": "user", "message": {"role": "user", "content": [
                        {"type": "text", "text": "probe " + " | ".join(pieces) + " " + "x" * 300},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                     "data": _png_b64()}},
                    ]}},
                    {"type": "assistant", "message": {"role": "assistant",
                                                      "content": "ack " + " | ".join(pieces) + " ack" * 40}},
                ])
                raw = tp.read_bytes()
                self.assertIn("\u2028".encode(), raw)
                self.assertIn("\u2029".encode(), raw)
                r = subprocess.run(["sh", str(STOP_HOOK)], input=json.dumps(
                    {"hook_event_name": "PreCompact", "session_id": "lines", "cwd": td,
                     "transcript_path": str(tp)}), capture_output=True, text=True, timeout=120, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
                jobs = list((khome / "queue").glob("*.json"))
                self.assertEqual(len(jobs), 1, r.stderr)
                job = json.loads(jobs[0].read_text(encoding="utf-8"))
                for piece in pieces:
                    self.assertEqual(job["transcript"].count(piece), 2, piece)
                land = sc.land_transcript_images(Path(job["transcript_path"]),
                                                 start_offset=int(job.get("offset_before") or 0),
                                                 end_offset=job.get("offset_after"))
                self.assertEqual(land["landed"], 1)


class LineSplitHelperTest(unittest.TestCase):
    def test_splits_on_lf_only_and_strips_one_trailing_cr(self):
        from khipu.lines import split_lines

        self.assertEqual(split_lines(f"{AWKWARD}\r\nx\n\ny"), [AWKWARD, "x", "", "y"])
        self.assertEqual(split_lines(b"a\xffb\n"), ["a\ufffdb"])
        self.assertEqual(split_lines(""), [])

    def test_keepends_round_trips_the_text(self):
        from khipu.lines import split_lines

        text = f"{AWKWARD}\r\nx\ny"
        self.assertEqual(split_lines(text, keepends=True), [f"{AWKWARD}\r\n", "x\n", "y"])
        self.assertEqual("".join(split_lines(text, keepends=True)), text)


LS = "\u2028"


def _jsonl(path: Path, rows: list) -> Path:
    return _write_raw(path, rows)


class ReaderTest(unittest.TestCase):
    """One case per reader moved off splitlines(): a record holding a raw
    U+2028 between two plain records is read whole."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._env = mock.patch.dict(os.environ, {
            "KHIPU_CAPTURE_HOME": str(self.dir / "kh"),
            "KHIPU_DATA_DIR": str(self.dir / "data"),
        })
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def test_synth_usage_tail(self):
        from khipu import extract

        path = _jsonl(self.dir / "usage.jsonl", [
            {"ts": "2026-10-15T01:00:00Z", "caller": f"a{LS}b"},
            {"ts": "2026-10-15T02:00:00Z", "caller": "c"}])
        lines = extract._usage_lines_since(path, "2026-10-01")
        self.assertEqual([json.loads(ln)["caller"] for ln in lines], [f"a{LS}b", "c"])

    def test_query_log_topic_hits(self):
        from khipu import topic_hits

        text = "".join(_raw(r) + "\n" for r in [
            {"ts": "2026-10-15T01:00:00Z", "query": f"q{LS}r", "top": [{"kind": "topic", "id": "one"}]},
            {"ts": "2026-10-15T02:00:00Z", "query": "s", "top": [{"kind": "topic", "id": "two"}]}])
        self.assertEqual([slug for slug, _ in topic_hits._iter_topic_hits_from_text(text)], ["one", "two"])

    def test_mirror_tail_sync(self):
        from khipu import mirror

        _jsonl(self.dir / "mem" / "episodes.jsonl", [
            {"ts": "2026-10-15T01:00:00Z", "summary": f"x{LS}y"},
            {"ts": "2026-10-15T02:00:00Z", "summary": "z"}])
        conn = mock.MagicMock()
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.drift.episodes_missing_in_pg", return_value=[]):
            stats = mirror.sync_recent_episodes(self.dir / "mem")
        self.assertEqual(stats["tail"], 2)

    def _recall_log(self, *reasons: str) -> Path:
        path = self.dir / "kh" / "logs" / "prompt-recall.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes("".join(
            f"2026-10-15T10:00:0{i}Z [khipu-prompt-recall] session=s{i} reason={r} ms=12 hits=[] "
            f"legs=a routing=ok\n" for i, r in enumerate(reasons)).encode("utf-8"))
        return path

    def test_prompt_recall_log_readers(self):
        from datetime import datetime, timezone

        from khipu import recall_prompt

        self._recall_log(f"ok{LS}tail", "ok")
        now = datetime(2026, 10, 15, 11, tzinfo=timezone.utc)
        self.assertEqual(recall_prompt.prompt_recall_outcomes(now=now)["count"], 2)
        self.assertEqual(recall_prompt.prompt_routing_budget_rate(now=now)["count"], 2)

    def test_routing_replay_counts_log_rows(self):
        from khipu import routing_replay

        self._recall_log(f"ok{LS}tail", "ok")
        self.assertEqual(routing_replay._rows_now(), 2)
        self.assertEqual(sorted(routing_replay._log_rows(2, wait=0)), ["s0", "s1"])

    def test_mcp_probe_frames(self):
        from khipu import integrations

        frames = [{"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": f"k{LS}h"}}},
                  {"jsonrpc": "2.0", "id": 2, "result": {"content": [
                      {"type": "text", "text": json.dumps({"counts": {"episodes": 3}})}]}}]
        done = subprocess.CompletedProcess(
            [], 0, "".join(_raw(f) + "\n" for f in frames).encode("utf-8"), b"")
        with mock.patch.object(integrations.subprocess, "run", return_value=done):
            out = integrations._probe_mcp("khipu-mcp")
        self.assertEqual((out["ok"], out["episodes"]), (True, 3))

    def test_recall_eval_golden_and_replay(self):
        from khipu import recall_eval

        golden = _jsonl(self.dir / "golden.jsonl", [
            {"query": f"what{LS}now", "expect": ["a"]}, {"query": "then", "expect": ["b"]}])
        self.assertEqual([c["query"] for c in recall_eval.load_golden(golden)], [f"what{LS}now", "then"])
        log = _jsonl(self.dir / "query_log.jsonl", [
            {"ts": "2026-10-15T01:00:00Z", "query": f"first{LS}half"},
            {"ts": "2026-10-15T02:00:00Z", "query": "second"}])
        self.assertEqual(sorted(e["query"] for e in recall_eval.load_replay_entries(log)),
                         [f"first{LS}half", "second"])

    def test_embed_timings_read_and_trim(self):
        import time

        from khipu import embed

        path = _jsonl(self.dir / "data" / "embed_timings.jsonl", [
            {"ts": time.time(), "profile": f"p{LS}q", "ms": 7, "ok": True},
            {"ts": time.time(), "profile": "r", "ms": 9, "ok": True}])
        self.assertEqual(embed.median_query_ms(), {f"p{LS}q": 7, "r": 9})
        with mock.patch.object(embed, "_TIMING_COUNT_BYTES", 0), \
                mock.patch.object(embed, "TIMING_TRIM_AT", 2), mock.patch.object(embed, "TIMING_KEEP", 3):
            embed._log_query_timing("s", 5, True)
        rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").split("\n") if ln]
        self.assertEqual([r["profile"] for r in rows], [f"p{LS}q", "r", "s"])

    def test_forget_legacy_file_and_backup(self):
        import hashlib

        from khipu import forget

        rows = [{"ts": "2026-10-15T01:00:00Z", "summary": f"gone{LS}now"},
                {"ts": "2026-10-15T02:00:00Z", "summary": f"kept{LS}too"}]
        mem = self.dir / "mem"
        _jsonl(mem / "episodes.jsonl", rows)
        _jsonl(mem / ".khipu-forget-backups" / "episodes.jsonl.1", rows)
        md5 = hashlib.md5(rows[0]["summary"].encode("utf-8")).hexdigest()
        out = forget.forget_in_legacy_file(mem, rows[0]["ts"], md5)
        self.assertEqual(out["removed"], 1, out)
        self.assertNotIn("reason", out)
        self.assertTrue(out["backups"]["ok"], out)
        for path in (mem / "episodes.jsonl", mem / ".khipu-forget-backups" / "episodes.jsonl.1"):
            left = path.read_text(encoding="utf-8")
            self.assertEqual([json.loads(ln)["summary"] for ln in left.split("\n") if ln], [f"kept{LS}too"])

    def test_drift_legacy_errors_and_query_window(self):
        from datetime import datetime, timezone

        from khipu import drift

        mem = self.dir / "mem"
        (mem / "topics").mkdir(parents=True)
        _jsonl(mem / "episodes.jsonl", [{"ts": "2026-10-15T01:00:00Z", "summary": f"a{LS}b"}])
        self.assertEqual(drift._legacy_drift_errors(mem), [])
        now = datetime.now(timezone.utc).isoformat()
        _jsonl(self.dir / "data" / "query_log.jsonl", [{"ts": now, "query": f"x{LS}y"}, {"ts": now, "query": "z"}])
        self.assertEqual([e["query"] for e in drift._query_log_window(7)], [f"x{LS}y", "z"])

    def test_queue_drain_startup_log(self):
        from khipu import launchd_gen

        log = self.dir / "drain.err"
        log.write_bytes(f"Fatal Python error: x{LS}InterruptedError: [Errno 4] Interrupted system call\n"
                        .encode("utf-8"))
        with mock.patch.object(launchd_gen, "_installed_plist", return_value={"StandardErrorPath": str(log)}):
            self.assertEqual(launchd_gen.queue_drain_startup_failures()["count"], 0)

    def test_query_log_observations(self):
        from khipu import query_log

        path = _jsonl(query_log.log_path(), [{"query": f"a{LS}b"}, {"query": "c"}])
        lines, available = query_log._observations()
        self.assertTrue(available)
        self.assertEqual([json.loads(ln)["query"] for ln in lines], [f"a{LS}b", "c"])
        self.assertEqual(len(query_log._read_lines(path)), 2)

    def test_promote_readers(self):
        import time

        from khipu import promote

        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        log = _jsonl(self.dir / "query_log.jsonl", [
            {"ts": stamp, "query": f"one{LS}two", "harness": "h"}, {"ts": stamp, "query": "three"}])
        self.assertEqual(sorted(q["prompt"] for q in promote.sample_queries(log, n=5)),
                         [f"one{LS}two", "three"])
        state = _jsonl(self.dir / "promotions.jsonl", [{"sha": f"a{LS}b"}, {"sha": "c"}])
        with mock.patch.object(promote, "state_file", return_value=state):
            self.assertEqual([r["sha"] for r in promote.records()], [f"a{LS}b", "c"])
        probe = subprocess.CompletedProcess([], 0, "noise\n" + _raw({"push": True, "note": f"x{LS}y"}) + "\n", "")
        self.assertIsNotNone(promote._probe_answer(probe))

    def test_promote_recall_once_reads_the_hook_log(self):
        from khipu import promote

        def run(argv, *, env, **_kw):
            log = Path(env["KHIPU_CAPTURE_HOME"]) / "logs" / "prompt-recall.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_bytes((f"2026-10-15T10:00:00Z [khipu-prompt-recall] session=s reason=ok{LS}x "
                             f"ms=12 hits=[] legs=a\n").encode("utf-8"))
            return subprocess.CompletedProcess(argv, 0, "{}", "")

        with mock.patch.object(promote, "_run", side_effect=run):
            out = promote._recall_once(self.dir / "hook", {"prompt": "p"}, "s")
        self.assertEqual(out["reason"], f"ok{LS}x")



class DecodingPolicyTest(unittest.TestCase):
    """Replacement decoding only where losing a byte is harmless. Text Khipu
    acts on or compares is decoded strictly, and stdin to a child is always
    encoded strictly, whatever its output decoding is."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_replay_log_with_corrupt_query_bytes_is_refused(self):
        # Replacing both bytes made the two queries one, silently changing
        # the evaluation workload.
        from khipu import recall_eval

        log = self.dir / "query_log.jsonl"
        log.write_bytes(b'{"query": "find caf\xff"}\n{"query": "find caf\xfe"}\n')
        with self.assertRaises(UnicodeDecodeError):
            recall_eval.load_replay_entries(log)

    def test_promote_sample_skips_corrupt_queries_instead_of_merging_them(self):
        # The promote gate replays these queries as hook stdin; replacing the
        # bad byte merged two distinct queries into one corrupted one.
        import time as _time
        from khipu import promote

        stamp = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        log = self.dir / "query_log.jsonl"
        log.write_bytes(b"".join(
            b'{"ts": "%s", "query": "%s"}\n' % (stamp.encode(), q)
            for q in (b"find caf\xe9 notes", b"find caf\xe8 notes", b"plain")))
        self.assertEqual([r["prompt"] for r in promote.sample_queries(log)], ["plain"])

    def test_run_text_encodes_stdin_strictly_and_decodes_output_with_replacement(self):
        from khipu import lines

        with self.assertRaises(UnicodeEncodeError):
            lines.run_text([sys.executable, "-c", "pass"], input="caf\ud800")
        echo = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read() + b'\\xff')"
        r = lines.run_text([sys.executable, "-c", echo], input="café", timeout=30)
        self.assertEqual(r.stdout, "café�")

    def test_codex_cli_prompt_with_a_lone_surrogate_never_reaches_the_child(self):
        from khipu import extract, synth_cli

        seen = self.dir / "stdin.bin"
        cli = self.dir / "codex"
        cli.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(seen)!r}).write_bytes(sys.stdin.buffer.read())\n"
            "pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_text('answer')\n"
            "print(json.dumps({'type': 'turn.completed', 'usage': {}}))\n", encoding="utf-8")
        cli.chmod(0o755)
        with mock.patch.dict(os.environ, {"KHIPU_CAPTURE_HOME": str(self.dir / "kh")}), \
                mock.patch.object(synth_cli, "binary", lambda name: str(cli)), \
                mock.patch.object(extract.time, "sleep", lambda s: None), \
                self.assertRaisesRegex(RuntimeError, "UnicodeEncodeError"):
            extract._generate_codex_cli(json.loads('"caf\\ud800"'), model_id="m", timeout=30,
                                        retries=0, caller="fixture")
        self.assertFalse(seen.exists(), seen.read_bytes() if seen.exists() else b"")


if __name__ == "__main__":
    unittest.main()
