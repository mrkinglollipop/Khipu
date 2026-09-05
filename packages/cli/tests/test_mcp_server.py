"""Tests for khipu.mcp_server — stdio MCP shim (reads plus gated khipu_capture).

Protocol tests (handshake, tools/list, capture rejection, error frames) need
no database. Live tests call the real search/status tools against the Khipu
Postgres read-only and skip cleanly when it is unreachable. One end-to-end
test spawns the actual ``bin/khipu-mcp`` process and speaks newline JSON-RPC
over its stdin/stdout.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import mcp_server as ms
from khipu.mcp_server import LATEST_PROTOCOL, TOOLS, _tool_get, handle_message


def _pg_available() -> bool:
    try:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    except Exception:
        return False
    return True


PG_AVAILABLE = _pg_available()


def _req(req_id, method, params=None) -> dict:
    msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


class InitializeInstructionsTest(unittest.TestCase):
    """The recall rule must ride the protocol: `instructions` on initialize is
    the only channel that reaches every client in every repo with no files
    (added 2026-08-17 for cloud agents; MCP lifecycle spec, optional field)."""

    def test_initialize_carries_the_recall_rule(self):
        from khipu.recall_rule import RULE_MD

        out = handle_message(_req(1, "initialize", {"protocolVersion": "2025-06-18"}))
        ins = out["result"]["instructions"]
        self.assertEqual(ins, RULE_MD.strip())          # single source, cannot drift
        for token in ("khipu_search", "khipu_get", "khipu_capture", "khipu_graph"):
            self.assertIn(token, ins)

    def test_capture_tool_description_tells_the_agent_when_to_call_it(self):
        from khipu.mcp_server import TOOLS

        desc = next(t for t in TOOLS if t["name"] == "khipu_capture")["description"]
        self.assertIn("session_id", desc)
        self.assertIn("hub", desc)


class ProtocolTest(unittest.TestCase):
    def test_initialize_echoes_supported_version(self):
        out = handle_message(
            _req(1, "initialize", {"protocolVersion": "2024-11-05"})
        )
        self.assertEqual(out["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(out["result"]["serverInfo"]["name"], "khipu")
        self.assertIn("tools", out["result"]["capabilities"])

    def test_initialize_unknown_version_falls_back(self):
        out = handle_message(_req(1, "initialize", {"protocolVersion": "1999-01-01"}))
        self.assertEqual(out["result"]["protocolVersion"], LATEST_PROTOCOL)

    def test_tools_list_names(self):
        out = handle_message(_req(2, "tools/list"))
        names = {t["name"] for t in out["result"]["tools"]}
        self.assertEqual(
            names,
            {t["name"] for t in TOOLS},
        )
        self.assertEqual(
            names,
            {
                "khipu_search",
                "khipu_get",
                "khipu_graph",
                "khipu_status",
                "khipu_owed_update",
                "khipu_forget",
                "khipu_capture",
                "khipu_owed",
            },
        )
        for tool in out["result"]["tools"]:
            self.assertIn("inputSchema", tool)

    def test_notification_returns_none(self):
        self.assertIsNone(
            handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )

    def test_unknown_method_is_jsonrpc_error(self):
        out = handle_message(_req(3, "resources/list"))
        self.assertEqual(out["error"]["code"], -32601)

    def test_unknown_tool_is_invalid_params(self):
        out = handle_message(_req(4, "tools/call", {"name": "nope", "arguments": {}}))
        self.assertEqual(out["error"]["code"], -32602)

    def test_get_without_id_is_a_tool_error(self):
        out = handle_message(
            _req(6, "tools/call", {"name": "khipu_get", "arguments": {}})
        )
        self.assertTrue(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertIn("id is required", body["error"])

    def test_ping(self):
        self.assertEqual(handle_message(_req(5, "ping"))["result"], {})


class CaptureRejectionTest(unittest.TestCase):
    """Capture gating: dual/legacy reject; hub writes on gateway / no-hook;
    local stdio + khipu-stop-hook declines. Isolated from live Hub config and
    live PG (``capture()`` is mocked — this class must not insert episodes)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": self.tmp.name})
        self._env.start()
        os.environ.pop("KHIPU_CAPTURE_MODE", None)
        self._hook = mock.patch(
            "khipu.mcp_server._local_capture_hook_is_writer", return_value=False
        )
        self._hook.start()
        self._cap = mock.patch("khipu.capture.capture", return_value=0)
        self.cap_mock = self._cap.start()

    def tearDown(self):
        self._cap.stop()
        self._hook.stop()
        self._env.stop()
        self.tmp.cleanup()

    def _call_capture(self) -> dict:
        out = handle_message(
            _req(9, "tools/call", {"name": "khipu_capture", "arguments": {"summary": "x"}})
        )
        self.assertTrue(out["result"]["isError"])
        return json.loads(out["result"]["content"][0]["text"])

    def test_rejected_in_dual_default(self):
        body = self._call_capture()
        self.cap_mock.assert_not_called()
        self.assertIn("capture_mode=dual", body["error"])
        self.assertIn("capture_v2", body["error"])

    def test_hub_routes_to_capture_writer(self):
        """P3 step 2: in hub with no local hook the MCP tool IS a writer."""
        os.environ["KHIPU_CAPTURE_MODE"] = "hub"
        out = handle_message(
            _req(9, "tools/call", {"name": "khipu_capture", "arguments": {"summary": "x"}})
        )
        self.assertFalse(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "hub")
        self.assertRegex(body["ts"], r"^\d{4}-\d{2}-\d{2}T")
        self.cap_mock.assert_called_once()
        self.assertEqual(self.cap_mock.call_args.kwargs.get("mode"), "hub")

    def test_hub_stdio_with_local_hook_rejected(self):
        os.environ["KHIPU_CAPTURE_MODE"] = "hub"
        with mock.patch(
            "khipu.mcp_server._local_capture_hook_is_writer", return_value=True
        ), mock.patch(
            "khipu.mcp_server._via_https_gateway", return_value=False
        ):
            body = self._call_capture()
        self.cap_mock.assert_not_called()
        self.assertIn("khipu-stop-hook", body["error"])
        self.assertIn("khipu-aegis-capture", body["error"])

    def test_hub_gateway_writes_even_with_local_hook(self):
        os.environ["KHIPU_CAPTURE_MODE"] = "hub"
        with mock.patch(
            "khipu.mcp_server._local_capture_hook_is_writer", return_value=True
        ), mock.patch(
            "khipu.mcp_server._via_https_gateway", return_value=True
        ):
            out = handle_message(
                _req(9, "tools/call", {"name": "khipu_capture", "arguments": {"summary": "x"}})
            )
        self.assertFalse(out["result"]["isError"])
        self.cap_mock.assert_called_once()

    def test_search_requires_query(self):
        out = handle_message(
            _req(10, "tools/call", {"name": "khipu_search", "arguments": {}})
        )
        self.assertTrue(out["result"]["isError"])

    def test_bad_mode_is_a_tool_error(self):
        out = handle_message(
            _req(
                11,
                "tools/call",
                {"name": "khipu_search", "arguments": {"query": "x", "mode": "bogus"}},
            )
        )
        self.assertTrue(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertIn("mode", body["error"])

    def test_kind_node_rejected_for_semantic_mode(self):
        out = handle_message(
            _req(
                12,
                "tools/call",
                {
                    "name": "khipu_search",
                    "arguments": {"query": "x", "mode": "semantic", "kind": "node"},
                },
            )
        )
        self.assertTrue(out["result"]["isError"])

    def test_kind_media_rejected_for_hybrid_mode(self):
        out = handle_message(
            _req(
                13,
                "tools/call",
                {
                    "name": "khipu_search",
                    "arguments": {"query": "x", "mode": "hybrid", "kind": "media"},
                },
            )
        )
        self.assertTrue(out["result"]["isError"])


class LocalCaptureHookWriterTest(unittest.TestCase):
    """``_local_capture_hook_is_writer`` itself — not routed through capture()."""

    def test_unreadable_existing_hooks_json_is_treated_as_writer(self):
        from khipu.mcp_server import _local_capture_hook_is_writer

        hooks = mock.Mock()
        hooks.read_text.side_effect = PermissionError("mocked unreadable")
        with mock.patch(
            "khipu.mcp_server._local_hook_config_paths", return_value=(hooks,)
        ):
            self.assertTrue(_local_capture_hook_is_writer())
        hooks.read_text.assert_called_once()


class GetKindOmittedFallbackTest(unittest.TestCase):
    """Numeric ids with kind omitted try episode, then topic, then media.
    Explicit kind=episode stays a hard miss. Lookups are mocked; no live PG."""

    def test_omitted_kind_numeric_id_falls_through_to_topic(self):
        topic = {"slug": "42", "title": "Answer", "body": "x"}
        with mock.patch(
            "khipu.activity.episode_detail", return_value=None
        ) as ep, mock.patch(
            "khipu.activity.topic_detail", return_value=topic
        ) as tp, mock.patch("khipu.activity.media_detail") as md:
            out = _tool_get({"id": "42"})
        self.assertEqual(out["kind"], "topic")
        self.assertEqual(out["slug"], "42")
        ep.assert_called_once_with(42)
        tp.assert_called_once_with("42")
        md.assert_not_called()

    def test_explicit_kind_episode_does_not_fall_through(self):
        with mock.patch(
            "khipu.activity.episode_detail", return_value=None
        ) as ep, mock.patch("khipu.activity.topic_detail") as tp, mock.patch(
            "khipu.activity.media_detail"
        ) as md:
            with self.assertRaises(ValueError) as ctx:
                _tool_get({"id": "42", "kind": "episode"})
        self.assertIn("episode not found", str(ctx.exception))
        ep.assert_called_once_with(42)
        tp.assert_not_called()
        md.assert_not_called()

    def test_omitted_kind_numeric_id_falls_through_to_media_when_topic_missing(self):
        media = {
            "id": "42",
            "path": "/a.png",
            "sha256": "abc",
            "mime": "image/png",
        }
        with mock.patch(
            "khipu.activity.episode_detail", return_value=None
        ), mock.patch(
            "khipu.activity.topic_detail", return_value=None
        ), mock.patch(
            "khipu.activity.media_detail", return_value=media
        ) as md:
            out = _tool_get({"id": "42"})
        self.assertEqual(out["kind"], "media")
        self.assertEqual(out["path"], "/a.png")
        self.assertEqual(out["sha256"], "abc")
        self.assertEqual(out["mime"], "image/png")
        md.assert_called_once_with("42")


@unittest.skipUnless(PG_AVAILABLE, "Postgres unreachable; skipping live MCP tool calls")
class LiveToolTest(unittest.TestCase):
    def test_search_returns_rows(self):
        out = handle_message(
            _req(
                20,
                "tools/call",
                {"name": "khipu_search", "arguments": {"query": "khipu", "limit": 6}},
            )
        )
        self.assertFalse(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(body["query"], "khipu")
        self.assertEqual(body["mode"], "hybrid")
        for row in body["results"]:
            self.assertIn(row["kind"], {"topic", "episode", "node"})

    def test_kind_topic_returns_only_topics(self):
        out = handle_message(
            _req(
                22,
                "tools/call",
                {"name": "khipu_search", "arguments": {"query": "khipu", "kind": "topic", "limit": 6}},
            )
        )
        self.assertFalse(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertTrue(all(r["kind"] == "topic" for r in body["results"]))

    def test_since_filter_excludes_old_rows(self):
        out = handle_message(
            _req(
                23,
                "tools/call",
                {
                    "name": "khipu_search",
                    "arguments": {"query": "khipu", "since": "7d", "kind": "episode", "limit": 8},
                },
            )
        )
        self.assertFalse(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        # every episode returned must be a live PG row; verify none predate 7d.
        from khipu.db import connect

        ids = [int(r["id"]) for r in body["results"]]
        if not ids:
            self.skipTest("no episode hits for 'khipu' to check dates against")
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM episodes WHERE id = ANY(%s) AND ts < now() - interval '7 days'",
                    (ids,),
                )
                stale = cur.fetchall()
        self.assertEqual(stale, [])

    def test_search_logs_to_query_log(self):
        from khipu import query_log

        with mock.patch.object(query_log, "log_query") as logged:
            out = handle_message(
                _req(24, "tools/call", {"name": "khipu_search", "arguments": {"query": "khipu"}})
            )
        self.assertFalse(out["result"]["isError"])
        logged.assert_called_once()
        self.assertEqual(logged.call_args.kwargs.get("mode"), "hybrid")

    def test_status_counts(self):
        out = handle_message(
            _req(21, "tools/call", {"name": "khipu_status", "arguments": {}})
        )
        self.assertFalse(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertGreater(body["counts"]["episodes"], 0)
        self.assertNotIn("drift", body)

    def test_get_returns_full_episode_without_raw(self):
        from khipu.activity import episode_detail, recent_episodes

        recents = recent_episodes(limit=1)
        if not recents:
            self.skipTest("no episodes")
        eid = recents[0]["id"]
        out = handle_message(
            _req(
                22,
                "tools/call",
                {"name": "khipu_get", "arguments": {"id": str(eid), "kind": "episode"}},
            )
        )
        self.assertFalse(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(body["kind"], "episode")
        self.assertEqual(body["id"], eid)
        self.assertIn("summary", body)
        self.assertNotIn("raw", body)
        full = episode_detail(eid)
        self.assertIsNotNone(full)
        self.assertEqual(body["summary"], full["summary"])

    def test_get_missing_episode_is_a_tool_error(self):
        out = handle_message(
            _req(23, "tools/call", {"name": "khipu_get", "arguments": {"id": "0"}})
        )
        self.assertTrue(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertIn("not found", body["error"])


class StdioEndToEndTest(unittest.TestCase):
    """Spawn the real launcher and complete a handshake + tools/list."""

    def test_handshake_over_stdio(self):
        launcher = Path(__file__).resolve().parents[1] / "bin" / "khipu-mcp"
        lines = "\n".join(
            json.dumps(m)
            for m in (
                _req(1, "initialize", {"protocolVersion": LATEST_PROTOCOL}),
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                _req(2, "tools/list"),
            )
        )
        proc = subprocess.run(
            [str(launcher)],
            input=lines + "\n",
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        responses = [
            json.loads(line) for line in proc.stdout.splitlines() if line.strip()
        ]
        self.assertEqual(len(responses), 2)  # notification produces no frame
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "khipu")
        names = {t["name"] for t in responses[1]["result"]["tools"]}
        self.assertIn("khipu_search", names)
        self.assertIn("khipu_get", names)


class SearchStaleFallbackForwardingTest(unittest.TestCase):
    """fix 7: khipu_search's hub-unreachable fallback must forward project/
    session_id/harness to search_stale_payload — before this fix they were
    silently dropped the moment the hub went down."""

    def test_forwards_project_session_id_harness_to_the_snapshot_fallback(self):
        from khipu import mcp_server as srv

        captured = {}

        def fake_stale(query, limit, *, semantic, kind, since, until,
                        project, session_id, harness):
            captured.update(project=project, session_id=session_id, harness=harness)
            return {"query": query, "mode": "literal", "results": [], "filters_dropped": []}

        with mock.patch("khipu.embed.hybrid_search",
                         side_effect=RuntimeError("connection refused")), \
                mock.patch("khipu.hub_snapshot.hub_connection_failed", return_value=True), \
                mock.patch("khipu.hub_snapshot.search_stale_payload", fake_stale):
            out = srv._tool_search({
                "query": "khipu", "project": "acme/widget",
                "session_id": "claude_code:host-1", "harness": "claude_code",
            })
        self.assertEqual(captured["project"], "acme/widget")
        self.assertEqual(captured["session_id"], "claude_code:host-1")
        self.assertEqual(captured["harness"], "claude_code")
        self.assertEqual(out["results"], [])


if __name__ == "__main__":
    unittest.main()


class GatewayFlagTest(unittest.TestCase):
    """Audit 2026-09-04: `_via_https_gateway` walked the call stack for a frame
    named "khipu.gateway". The shipped container runs `python -m khipu.gateway`,
    so that module's __name__ is "__main__" and the walk NEVER matched — every
    gateway capture was rejected as a local stdio double-write."""

    def setUp(self):
        self._prev = os.environ.pop(ms.GATEWAY_ACTIVE_ENV, None)
        ms._GATEWAY_ACTIVE = False

    def tearDown(self):
        ms._GATEWAY_ACTIVE = False
        os.environ.pop(ms.GATEWAY_ACTIVE_ENV, None)
        if self._prev is not None:
            os.environ[ms.GATEWAY_ACTIVE_ENV] = self._prev

    def test_off_by_default(self):
        self.assertFalse(ms._via_https_gateway())

    def test_mark_gateway_active_sets_module_flag_and_env(self):
        ms.mark_gateway_active()
        self.assertTrue(ms._via_https_gateway())
        self.assertEqual(os.environ[ms.GATEWAY_ACTIVE_ENV], "1")

    def test_env_var_alone_is_enough_for_a_re_execed_child(self):
        os.environ[ms.GATEWAY_ACTIVE_ENV] = "1"
        self.assertTrue(ms._via_https_gateway())

    def test_a_gateway_process_never_defers_to_the_local_stdio_hook(self):
        ms.mark_gateway_active()
        with mock.patch.object(ms, "_local_capture_hook_is_writer", return_value=True):
            self.assertFalse(ms._stdio_hook_owns_capture())

    def test_gateway_serve_marks_the_flag(self):
        from khipu import gateway

        with mock.patch.object(gateway, "ThreadingHTTPServer", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                gateway.serve("127.0.0.1:8787", token="x" * 32)
        self.assertTrue(ms._via_https_gateway())


class ToolDispatchNeverKillsTheServerTest(unittest.TestCase):
    """capture.load_payload raises SystemExit(EX_DATAERR) on a bad payload. In a
    long-lived MCP server that ended the process and took every other client's
    session with it (audit 2026-09-04)."""

    def _call(self, name="khipu_status", args=None):
        return ms.handle_message({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        })

    def test_systemexit_from_a_tool_becomes_a_json_rpc_tool_error(self):
        with mock.patch.dict(ms.TOOL_FUNCS,
                             {"khipu_status": lambda a: (_ for _ in ()).throw(SystemExit(65))}):
            out = self._call()
        self.assertEqual(out["id"], 7)
        self.assertTrue(out["result"]["isError"])
        self.assertIn("SystemExit", out["result"]["content"][0]["text"])
        self.assertIn("65", out["result"]["content"][0]["text"])

    def test_keyboard_interrupt_still_propagates(self):
        with mock.patch.dict(ms.TOOL_FUNCS,
                             {"khipu_status": lambda a: (_ for _ in ()).throw(KeyboardInterrupt())}):
            with self.assertRaises(KeyboardInterrupt):
                self._call()

    def test_capture_rejects_a_non_string_summary_before_load_payload(self):
        with mock.patch.object(ms, "_capture_mode", return_value="hub"), \
                mock.patch.object(ms, "_stdio_hook_owns_capture", return_value=False), \
                mock.patch("khipu.capture.load_payload") as m_load:
            out = self._call("khipu_capture", {"summary": ["not", "a", "string"]})
        m_load.assert_not_called()
        self.assertTrue(out["result"]["isError"])
        self.assertIn("non-empty string 'summary'", out["result"]["content"][0]["text"])

    def test_capture_silences_stdout_and_reports_the_episode_id(self):
        import sys as _sys

        def _noisy_capture(payload, mode=None):
            print("OK · episode appended")   # would corrupt the JSON-RPC stream
            self.assertEqual(os.environ["KHIPU_HUB_FILE_MIRROR"], "0")
            return 0

        prev = os.environ.get("KHIPU_HUB_FILE_MIRROR")
        os.environ.pop("KHIPU_HUB_FILE_MIRROR", None)
        buf = io.StringIO()
        try:
            with mock.patch.object(ms, "_capture_mode", return_value="hub"), \
                    mock.patch.object(ms, "_stdio_hook_owns_capture", return_value=False), \
                    mock.patch.object(ms, "_captured_episode_id", return_value=4242), \
                    mock.patch("khipu.capture.capture", side_effect=_noisy_capture), \
                    mock.patch.object(_sys, "stdout", buf):
                out = ms._tool_capture({"summary": "a real capture", "session_id": "x:1"})
        finally:
            if prev is not None:
                os.environ["KHIPU_HUB_FILE_MIRROR"] = prev
            else:
                os.environ.pop("KHIPU_HUB_FILE_MIRROR", None)
        self.assertEqual(out["episode_id"], 4242)
        self.assertEqual(buf.getvalue(), "", "capture output must never reach stdout")
        # The env override is restored, not leaked into the rest of the process.
        self.assertIsNone(os.environ.get("KHIPU_HUB_FILE_MIRROR"))
