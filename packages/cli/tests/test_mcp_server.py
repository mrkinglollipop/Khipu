"""Tests for khipu.mcp_server — stdio MCP shim (reads plus gated khipu_capture).

Protocol tests (handshake, tools/list, capture rejection, error frames) need
no database. Live tests call the real search/status tools against the Khipu
Postgres read-only and skip cleanly when it is unreachable. One end-to-end
test spawns the actual ``bin/khipu-mcp`` process and speaks newline JSON-RPC
over its stdin/stdout.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
                "khipu_capture",
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
        for row in body["results"]:
            self.assertIn(row["kind"], {"topic", "episode", "node"})

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


if __name__ == "__main__":
    unittest.main()
