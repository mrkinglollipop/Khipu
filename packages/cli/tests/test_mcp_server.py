"""Tests for khipu.mcp_server — the read-only stdio MCP shim.

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
import unittest
from pathlib import Path

from khipu.mcp_server import LATEST_PROTOCOL, handle_message


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
        for token in ("khipu_search", "khipu_capture", "khipu_graph"):
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
            names, {"khipu_search", "khipu_graph", "khipu_status", "khipu_capture"}
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

    def test_ping(self):
        self.assertEqual(handle_message(_req(5, "ping"))["result"], {})


class CaptureRejectionTest(unittest.TestCase):
    """Locked P3 semantics: writes rejected in every current mode."""

    def _call_capture(self) -> dict:
        out = handle_message(
            _req(9, "tools/call", {"name": "khipu_capture", "arguments": {"summary": "x"}})
        )
        self.assertTrue(out["result"]["isError"])
        return json.loads(out["result"]["content"][0]["text"])

    def test_rejected_in_dual_default(self):
        os.environ.pop("KHIPU_CAPTURE_MODE", None)
        body = self._call_capture()
        self.assertIn("capture_mode=dual", body["error"])
        self.assertIn("capture_v2", body["error"])

    def test_hub_routes_to_capture_writer(self):
        """P3 step 2: in hub the MCP tool IS a writer. Stub the capture leg so this
        stays a protocol test (the live write is covered in test_capture.py)."""
        from unittest import mock

        import khipu.capture as capmod

        os.environ["KHIPU_CAPTURE_MODE"] = "hub"
        try:
            with mock.patch.object(capmod, "capture", return_value=0) as m:
                out = handle_message(
                    _req(9, "tools/call", {"name": "khipu_capture", "arguments": {"summary": "x"}})
                )
            self.assertFalse(out["result"]["isError"])
            body = json.loads(out["result"]["content"][0]["text"])
            self.assertTrue(body["ok"])
            self.assertEqual(body["mode"], "hub")
            self.assertRegex(body["ts"], r"^\d{4}-\d{2}-\d{2}T")
            m.assert_called_once()
            self.assertEqual(m.call_args.kwargs.get("mode"), "hub")
        finally:
            os.environ.pop("KHIPU_CAPTURE_MODE", None)

    def test_search_requires_query(self):
        out = handle_message(
            _req(10, "tools/call", {"name": "khipu_search", "arguments": {}})
        )
        self.assertTrue(out["result"]["isError"])


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


if __name__ == "__main__":
    unittest.main()
