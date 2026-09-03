"""Tests for khipu.gateway (MCP over HTTP) and the grok_bot pack.

The gateway is exercised in-process on a random loopback port with a fixed
token; `handle_message` is stubbed for the transport tests so they never touch
Postgres. The grok_bot pack is exercised against a temp project dir with a temp
Hub config; its verify probe is mocked (the real HTTPS probe is what
`khipu integrations verify grok_bot` runs — recorded in the state-of-play note).
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from khipu import gateway as gw

TOKEN = "unit-test-token-0123456789abcdef"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url: str, body, token: str | None = TOKEN, headers: dict | None = None) -> tuple[int, object]:
    data = body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()
    headers = dict(headers or {})
    headers["Content-Type"] = "application/json"
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, (json.loads(raw) if raw else None)


class GatewayTransportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        gw.Handler.token = TOKEN
        gw.Handler.rate = gw._Rate(1000)
        gw.Handler.auth_fail_rate = gw._Rate(1000)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), gw.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def test_health_is_open_and_mcp_get_is_405(self):
        with urllib.request.urlopen(self.url + "/healthz", timeout=5) as r:
            self.assertEqual(json.loads(r.read())["server"], "khipu")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.url + "/mcp", timeout=5)
        self.assertEqual(cm.exception.code, 405)

    def test_health_reports_the_build_so_deployed_code_is_identifiable(self):
        """The gateway ran the commit before its own batch-cap fix for hours and
        the one open endpoint could not say so — it reported a constant that had
        read 0.1.0 since the module was written (audit 2026-08-18)."""
        with urllib.request.urlopen(self.url + "/healthz", timeout=5) as r:
            body = json.loads(r.read())
        self.assertIn("build", body)
        self.assertEqual(body["build"], gw.BUILD)

    def test_health_never_exposes_the_bearer_token(self):
        with urllib.request.urlopen(self.url + "/healthz", timeout=5) as r:
            raw = r.read().decode()
        self.assertNotIn(TOKEN, raw)

    def test_auth_required_and_constant_time_compare(self):
        self.assertEqual(_post(self.url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)[0], 401)
        self.assertEqual(_post(self.url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="wrong")[0], 401)
        self.assertEqual(_post(self.url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=TOKEN + "x")[0], 401)
        status, body = _post(self.url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual((status, body["result"]), (200, {}))

    def test_initialize_tools_list_notification_and_batch(self):
        status, body = _post(self.url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                  "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["serverInfo"]["name"], "khipu")
        status, body = _post(self.url + "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, body), (202, None))
        status, body = _post(self.url + "/mcp", [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in body], [1, 2])
        self.assertIn("khipu_capture", [t["name"] for t in body[1]["result"]["tools"]])

    def test_tool_call_is_dispatched_and_tool_errors_are_json_rpc_results(self):
        with mock.patch.object(gw, "handle_message",
                               side_effect=lambda m: {"jsonrpc": "2.0", "id": m.get("id"),
                                                      "result": {"content": [{"type": "text", "text": "{}"}]}}) as hm:
            status, body = _post(self.url + "/mcp", {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                                      "params": {"name": "khipu_status", "arguments": {}}})
        self.assertEqual((status, body["id"]), (200, 7))
        hm.assert_called_once()
        with mock.patch.object(gw, "handle_message", side_effect=RuntimeError("boom")):
            status, body = _post(self.url + "/mcp", {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                                      "params": {"name": "khipu_status", "arguments": {}}})
        self.assertEqual(status, 200)                       # server stays up; error is JSON-RPC
        self.assertEqual(body["error"]["code"], -32603)

    def test_bad_json_body_cap_and_rate_limit(self):
        self.assertEqual(_post(self.url + "/mcp", b"not json")[0], 400)
        # Over the cap the server answers 413 WITHOUT reading the body and closes;
        # a client still streaming sees EPIPE — either outcome means "rejected".
        try:
            status, _ = _post(self.url + "/mcp", b"x" * (gw.MAX_BODY + 1))
            self.assertEqual(status, 413)
        except urllib.error.URLError:
            pass
        old = gw.Handler.rate
        gw.Handler.rate = gw._Rate(2)
        try:
            codes = [_post(self.url + "/mcp", {"jsonrpc": "2.0", "id": i, "method": "ping"})[0] for i in range(3)]
        finally:
            gw.Handler.rate = old
        self.assertEqual(codes, [200, 200, 429])

    def test_rate_limit_keys_on_the_proxys_hop_not_the_clients_claim(self):
        """Audit 2026-08-17: keying on the LEFTMOST X-Forwarded-For let one
        caller bypass the limit entirely by varying the header. Our proxy
        APPENDS, so only the rightmost hop is ours to trust."""
        old = gw.Handler.rate
        gw.Handler.rate = gw._Rate(3)
        try:
            codes = [_post(self.url + "/mcp", {"jsonrpc": "2.0", "id": i, "method": "ping"},
                           headers={"X-Forwarded-For": f"1.2.3.{i}, 203.0.113.7"})[0] for i in range(5)]
        finally:
            gw.Handler.rate = old
        self.assertEqual(codes, [200, 200, 200, 429, 429])   # same real client, throttled

    def test_failed_auth_is_throttled_before_the_token_check(self):
        """Audit 2026-08-17: the rate check ran AFTER auth, so the attempts most
        worth limiting — wrong tokens — were not limited at all."""
        old_r, old_a = gw.Handler.rate, gw.Handler.auth_fail_rate
        gw.Handler.rate, gw.Handler.auth_fail_rate = gw._Rate(100), gw._Rate(3)
        try:
            codes = [_post(self.url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                           token="wrong")[0] for _ in range(6)]
        finally:
            gw.Handler.rate, gw.Handler.auth_fail_rate = old_r, old_a
        self.assertEqual(codes, [401, 401, 401, 429, 429, 429])

    def test_serve_refuses_short_or_missing_token(self):
        with self.assertRaises(SystemExit):
            gw.serve("127.0.0.1:0", token="short")


class BatchAmplificationTest(unittest.TestCase):
    """A batch is one request but N pieces of work. Unbounded, a single 1 MiB
    body held ~9,900 tool calls — each a Postgres query — for one rate-limit
    token (audit 2026-08-17). The gateway is the only public surface, so the
    limiter has to count work, not requests."""

    @classmethod
    def setUpClass(cls):
        cls.port = _free_port()
        gw.Handler.token = TOKEN
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.port), gw.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.port}/mcp"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        gw.Handler.rate = gw._Rate(1000)
        gw.Handler.auth_fail_rate = gw._Rate(1000)

    def _call(self, n):
        return [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
                 "params": {"name": "khipu_status", "arguments": {}}} for i in range(n)]

    def test_an_oversized_batch_is_refused_before_any_work(self):
        calls = []

        def spy(m):
            calls.append(m)
            return {"jsonrpc": "2.0", "id": m.get("id"), "result": {}}

        with mock.patch.object(gw, "handle_message", spy):
            status, body = _post(self.url, self._call(gw.MAX_BATCH + 1))
        self.assertEqual(status, 413)
        self.assertIn("batch too large", json.dumps(body))
        self.assertEqual(calls, [], "no message may be dispatched from a rejected batch")

    def test_a_batch_at_the_cap_still_works(self):
        with mock.patch.object(gw, "handle_message",
                               lambda m: {"jsonrpc": "2.0", "id": m.get("id"), "result": {}}):
            status, body = _post(self.url, self._call(gw.MAX_BATCH))
        self.assertEqual(status, 200)
        self.assertEqual(len(body), gw.MAX_BATCH)

    def test_a_batch_spends_one_token_per_message(self):
        """Before the fix a batch cost exactly one token however large it was."""
        gw.Handler.rate = gw._Rate(10)
        with mock.patch.object(gw, "handle_message",
                               lambda m: {"jsonrpc": "2.0", "id": m.get("id"), "result": {}}):
            self.assertEqual(_post(self.url, self._call(8))[0], 200)
            # 8 spent of 10; a second batch of 8 must not fit.
            self.assertEqual(_post(self.url, self._call(8))[0], 429)

    def test_an_over_budget_batch_is_rejected_without_eating_the_rest(self):
        """A batch bigger than the remaining budget takes no tokens for its
        messages, so the caller's next single request still goes through."""
        gw.Handler.rate = gw._Rate(10)
        with mock.patch.object(gw, "handle_message",
                               lambda m: {"jsonrpc": "2.0", "id": m.get("id"), "result": {}}):
            self.assertEqual(_post(self.url, self._call(12))[0], 429)  # 1 + 11 > 10
            self.assertEqual(_post(self.url, self._call(1))[0], 200)
            self.assertEqual(_post(self.url, self._call(7))[0], 200)   # budget was preserved


class RateMapPruningTest(unittest.TestCase):
    """The limiter keeps a deque per client key on a long-lived public daemon;
    unpruned, the map grew for every address ever seen (audit 2026-08-17)."""

    def test_idle_keys_are_dropped(self):
        r = gw._Rate(10)
        for i in range(50):
            r.allow(f"10.0.0.{i}")
        self.assertLessEqual(len(r._hits), 50)
        for q in r._hits.values():          # age every hit out of the window
            for _ in range(len(q)):
                q[0] = 0.0
                q.rotate(-1)
        r.allow("10.0.0.1")
        self.assertLessEqual(len(r._hits), 2, "idle keys should have been pruned")

    def test_the_map_is_capped_under_key_flooding(self):
        with mock.patch.object(gw, "RATE_KEYS_MAX", 16):
            r = gw._Rate(1000)
            for i in range(400):
                r.allow(f"2001:db8::{i:x}")
            self.assertLessEqual(len(r._hits), 16)

    def test_a_live_key_keeps_its_budget_across_pruning(self):
        r = gw._Rate(3)
        self.assertTrue(r.allow("live"))
        self.assertTrue(r.allow("live"))
        for i in range(20):
            r.allow(f"other-{i}")
        self.assertTrue(r.allow("live"))
        self.assertFalse(r.allow("live"), "budget must survive pruning of other keys")


class GrokBotPackTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp(prefix="khipu-grokbot-")
        self.env = mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(Path(self.td) / "data"),
                                                "KHIPU_GATEWAY_URL": "https://khipu.example.test"})
        self.env.start()
        self.proj = Path(self.td) / "repo"
        self.proj.mkdir()

    def tearDown(self):
        self.env.stop()

    def test_install_merges_mcp_json_writes_rule_and_never_embeds_the_token(self):
        from khipu import integrations as integ

        (self.proj / ".cursor").mkdir()
        (self.proj / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
        out = integ.install("grok_bot", project=str(self.proj))
        self.assertTrue(out["detected"], out)
        self.assertEqual(len(out["changes"]), 2, out)
        d = json.loads((self.proj / ".cursor" / "mcp.json").read_text())
        self.assertIn("other", d["mcpServers"])                       # merged, not replaced
        e = d["mcpServers"]["khipu-cloud"]
        self.assertEqual(e["url"], "https://khipu.example.test/mcp")
        self.assertEqual(e["headers"]["Authorization"], "Bearer ${env:KHIPU_GATEWAY_TOKEN}")
        self.assertNotIn("Bearer eyJ", json.dumps(d))                 # no literal token, ever
        self.assertTrue((self.proj / ".cursor" / "rules" / "khipu.mdc").is_file())
        self.assertIn("khipu_capture", (self.proj / ".cursor" / "rules" / "khipu.mdc").read_text())
        self.assertEqual(integ.install("grok_bot", project=str(self.proj))["changes"], [])   # idempotent
        st = integ.status("grok_bot", project=str(self.proj))
        self.assertTrue(st["mcp"])
        self.assertEqual(st["extract"], "mcp_capture")
        integ.uninstall("grok_bot", project=str(self.proj))
        d = json.loads((self.proj / ".cursor" / "mcp.json").read_text())
        self.assertEqual(list(d["mcpServers"]), ["other"])           # only ours removed

    def test_install_without_project_emits_account_level_config(self):
        """No --project is the normal case: account-level MCP config covers every
        repo a cloud agent opens, and it is a UI action, so the CLI must hand
        over exactly what to paste rather than fail."""
        from khipu import integrations as integ

        out = integ.install("grok_bot")
        self.assertNotIn("error", out)
        acct = out["account_level"]
        self.assertIn("cursor.com/agents", acct["where"])
        self.assertEqual(acct["secret"]["name"], "KHIPU_GATEWAY_TOKEN")
        e = acct["config"]["mcpServers"]["khipu-cloud"]
        self.assertEqual(e["url"], "https://khipu.example.test/mcp")
        self.assertEqual(e["headers"]["Authorization"], "Bearer ${env:KHIPU_GATEWAY_TOKEN}")
        self.assertNotIn("gateway_token", json.dumps(acct["config"]))   # never the value

    def test_install_requires_gateway_url(self):
        from khipu import integrations as integ

        with mock.patch.dict(os.environ, {"KHIPU_GATEWAY_URL": ""}):
            self.assertFalse(integ.status("grok_bot", project=str(self.proj))["detected"])

    def test_verify_uses_the_gateway_probe(self):
        from khipu import integrations as integ

        integ.install("grok_bot", project=str(self.proj))
        with mock.patch.object(integ, "_gateway_token", return_value="t" * 30), \
                mock.patch.object(integ, "_probe_gateway", return_value={"ok": True, "episodes": 1, "tools": 4,
                                                                          "auth_refused_wrong_token": True}) as pg, \
                mock.patch("khipu.probe.run_probe", return_value={"ok": True, "harness": "grok_bot"}):
            v = integ.verify("grok_bot", project=str(self.proj))
        self.assertTrue(v["ok"], v)
        pg.assert_called_once_with("https://khipu.example.test", "t" * 30)
        self.assertIn("recall_probe", v["components"])
        with mock.patch.object(integ, "_gateway_token", return_value=""), \
                mock.patch("khipu.probe.run_probe", return_value={"ok": True, "harness": "grok_bot"}):
            v = integ.verify("grok_bot", project=str(self.proj))
        self.assertFalse(v["ok"])


if __name__ == "__main__":
    unittest.main()
