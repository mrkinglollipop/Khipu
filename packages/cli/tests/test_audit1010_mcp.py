"""Hermetic regressions for the MCP and gateway audit boundary."""
from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

from khipu import gateway as gw, mcp_server as srv
import pytest


@pytest.fixture(autouse=True)
def _no_hub_record(monkeypatch):
    # The fake connections here model the write itself; the record of applied
    # owner writes (migration 0031) is tested in test_owner_writes_exactly_once.
    from khipu import owner_writes

    monkeypatch.setenv("KHIPU_DATABASE_URL", "postgresql://synthetic@127.0.0.1:5432/owner_fixture")

    monkeypatch.setattr(owner_writes, "record_on_hub", lambda cur, tool, args: True)
    monkeypatch.setattr(owner_writes, "_refuse_on_hub", lambda conn, cur, tool, args, error: conn.rollback())


def request(name, args, ident=1):
    return {"jsonrpc": "2.0", "id": ident, "method": "tools/call",
            "params": {"name": name, "arguments": args}}


def handler(body=None, bearer="valid", ip="203.0.113.10"):
    h = object.__new__(gw.Handler)
    h.path = "/mcp"
    h.client_address = (ip, 1000)
    h.token = "valid"
    h.tokens = {}
    h.rate = gw._Rate(1000)
    h.token_rate = gw._Rate(1000)
    h.auth_fail_rate = gw._Rate(1000)
    h.headers = {"Authorization": "Bearer " + bearer}
    h.connection = mock.Mock()
    h.rfile = io.BytesIO()
    if body is not None:
        raw = json.dumps(body).encode()
        h.headers["Content-Length"] = str(len(raw))
        h.rfile = io.BytesIO(raw)
    h._send = mock.Mock()
    return h


class EpisodeProjectionTest(unittest.TestCase):
    def test_offline_episode_omits_raw_from_real_replica(self):
        from khipu import hub_snapshot

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "replica.sqlite"
            with sqlite3.connect(path) as con:
                hub_snapshot._create_schema(con)
                con.execute("INSERT INTO episodes(id, summary, raw) VALUES (?, ?, ?)",
                            (7, "public summary", '{"private": "RAW_ONLY_SENTINEL"}'))
            with mock.patch.object(hub_snapshot, "snapshot_path", return_value=path), \
                    mock.patch("khipu.activity.episode_detail", side_effect=ConnectionError()), \
                    mock.patch.object(hub_snapshot, "hub_connection_failed", return_value=True), \
                    mock.patch.object(hub_snapshot, "stale_fields", return_value={"stale": True}):
                out = srv._tool_get({"id": "7", "kind": "episode"})
        self.assertNotIn("raw", out)
        self.assertNotIn("RAW_ONLY_SENTINEL", json.dumps(out))
        self.assertEqual(out["summary"], "public summary")


class IngressTest(unittest.TestCase):
    def test_capture_preserves_preexisting_structured_arguments(self):
        args = {
            "summary": "Recorded decisions and remaining work", "topics": ["khipu"],
            "people": ["Matt"], "decisions": ["Keep local verification"],
            "preferences": ["Concise reports"], "scope": "general",
            "session_id": "cursor_cloud:repo:task", "project": "owner/repo",
            "open_loops": ["Verify the next release", {"text": "Review the report", "owner": "user"}],
            "closed_loops": [{"text": "Completed the audit", "commitment_id": 42}],
            "deliverables": [{"kind": "file", "path": "report.md", "title": "Audit"}],
            "verbatim": {"note": "Preserve this note"},
            "decision_details": [{"text": "Keep local verification", "source_kind": "user"}],
        }
        with mock.patch.object(srv, "_stdio_hook_owns_capture", return_value=False), \
                mock.patch.object(srv, "_capture_mode", return_value="hub"), \
                mock.patch("khipu.capture.capture", return_value=0) as capture, \
                mock.patch.object(srv, "_captured_episode_id", return_value=None):
            out = srv.handle_message(request("khipu_capture", args))
        self.assertFalse(out["result"]["isError"], out)
        payload = capture.call_args.args[0]
        for key, value in args.items():
            self.assertEqual(payload[key], value)
        schema = next(t["inputSchema"] for t in srv.TOOLS if t["name"] == "khipu_capture")
        self.assertTrue(schema.get("additionalProperties", True))

    def test_malformed_mutation_ids_never_open_database(self):
        cases = [
            ("khipu_forget", srv._tool_forget, {"id": 1}, "id"),
            ("khipu_owed_update", srv._tool_owed_update, {"id": 1, "action": "close"}, "id"),
            ("khipu_decisions_update", srv._tool_decisions_update, {"id": 1, "action": "retract"}, "id"),
            ("khipu_decisions_update", srv._tool_decisions_update, {"link_id": 1, "action": "confirm"}, "link_id"),
            ("khipu_decisions_update", srv._tool_decisions_update, {"id": 1, "by": 2, "action": "supersede"}, "by"),
        ]
        for name, func, args, key in cases:
            for bad in (True, False, 42.9, 1.0, 0, -1, "-1", "1.2", "+2", " 2", "2x"):
                with self.subTest(tool=name, key=key, bad=bad), \
                        mock.patch("khipu.db.connect", side_effect=AssertionError("database touched")) as connect:
                    with self.assertRaises(ValueError):
                        func({**args, key: bad})
                    connect.assert_not_called()


class ProtocolTest(unittest.TestCase):
    def test_malformed_envelopes_return_errors_and_ping_survives(self):
        bad = [None, 42, "text", [], {"jsonrpc": "2.0", "method": []},
               {"jsonrpc": "1.0", "id": 1, "method": "ping"},
               {"jsonrpc": "2.0", "id": {}, "method": "ping"},
               {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": [1]},
               {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": []}},
               request([], {}), request("khipu_get", [1])]
        for msg in bad:
            with self.subTest(msg=msg):
                out = srv.handle_message(msg)
                self.assertIn("error", out)
                self.assertIn(out["error"]["code"], (-32600, -32602))
                self.assertEqual(srv.handle_message({"jsonrpc": "2.0", "id": 9, "method": "ping"})["result"], {})

    def test_known_and_failing_notifications_emit_no_response(self):
        for method in ("initialize", "ping", "tools/list", "unknown"):
            with self.subTest(method=method):
                self.assertIsNone(srv.handle_message({"jsonrpc": "2.0", "method": method}))
        msg = request("khipu_get", {})
        del msg["id"]
        self.assertIsNone(srv.handle_message(msg))

    def test_stdio_rejects_scalar_then_processes_ping(self):
        frames = '42\n' + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + '\n'
        output = io.StringIO()
        with mock.patch.object(srv.sys, "stdin", io.StringIO(frames)), \
                mock.patch.object(srv.sys, "stdout", output), \
                mock.patch.object(srv, "_note_loaded_modules"), \
                mock.patch.object(srv, "_RELOAD_ON_CHANGE", False):
            srv.main()
        out = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(out[0]["error"]["code"], -32600)
        self.assertEqual(out[1]["id"], 9)

    def test_gateway_malformed_params_and_mixed_notification_batch(self):
        frames = [request("khipu_get", {}), {"jsonrpc": "2.0", "method": "ping"},
                  {"jsonrpc": "2.0", "id": 2, "method": "ping"}]
        del frames[0]["id"]
        h = handler(frames)
        with mock.patch.object(gw, "_record_liveness"), mock.patch.object(gw, "_log"):
            h.do_POST()
        out = json.loads(h._send.call_args.args[1])
        self.assertEqual([r["id"] for r in out], [2])
        h = handler({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": [1]})
        with mock.patch.object(gw, "_record_liveness"), mock.patch.object(gw, "_log"):
            h.do_POST()
        self.assertEqual(json.loads(h._send.call_args.args[1])["error"]["code"], -32602)

    def test_gateway_scalar_has_structured_error(self):
        h = handler(42)
        with mock.patch.object(gw, "_record_liveness"), mock.patch.object(gw, "_log"):
            h.do_POST()
        self.assertEqual(json.loads(h._send.call_args.args[1])["error"]["code"], -32600)


class GatewayEvidenceTest(unittest.TestCase):
    def test_wrong_bearer_health_cannot_pass_existing_checker(self):
        from khipu import integrations

        h = handler(bearer="wrong")
        h.path = "/healthz"
        h.do_GET()
        status, *body = h._send.call_args.args

        def urlopen(req, timeout):
            if status != 200:
                raise urllib.error.HTTPError(req.full_url, status, "Unauthorized", {}, None)
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = body[0]
            return response

        with mock.patch("khipu.config.gateway_url", return_value="https://gateway.invalid"), \
                mock.patch.object(integrations, "_gateway_token", return_value="wrong"), \
                mock.patch("urllib.request.urlopen", side_effect=urlopen):
            out = integrations.gateway_liveness_check()
        self.assertFalse(out["ok"])
        self.assertEqual(status, 401)

    def test_anonymous_public_health_and_valid_bearer_still_work(self):
        for auth, protected in ((None, False), ("valid", True)):
            h = handler()
            h.path = "/healthz"
            if auth is None:
                h.headers = {}
            with mock.patch.object(gw, "gateway_liveness", return_value={"tokens": {}}):
                h.do_GET()
            self.assertEqual(h._send.call_args.args[0], 200)
            body = json.loads(h._send.call_args.args[1])
            self.assertEqual("gateway_liveness" in body, protected)

    def test_failed_real_tool_records_error_without_raw_detail(self):
        for notification in (False, True):
            msg = request("khipu_search", {"query": "q"})
            if notification:
                del msg["id"]
            h = handler(msg)
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "liveness.json"
                last_ok = "2000-01-01T00:00:00Z"
                path.write_text(json.dumps({"tokens": {"default": {"last_ok_at": last_ok}}}))
                with mock.patch.dict(srv.TOOL_FUNCS, {"khipu_search": mock.Mock(side_effect=RuntimeError("PRIVATE_SENTINEL"))}), \
                        mock.patch.object(gw, "_liveness_path", return_value=path), mock.patch.object(gw, "_log"):
                    h.do_POST()
                entry = json.loads(path.read_text())["tokens"]["default"]
            self.assertEqual(entry["last_ok_at"], last_ok)
            self.assertIn("last_error_at", entry)
            self.assertEqual(entry["last_error"], "tool error")
            self.assertNotIn("PRIVATE_SENTINEL", json.dumps(entry))


class _Connection:
    info = SimpleNamespace(get_parameters=lambda: {"host": "127.0.0.1", "port": "5432", "dbname": "owner_fixture"})

    def __init__(self, row=None):
        self.row = row
        self.committed = 0
        self.rolled_back = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self

    def execute(self, *args):
        pass

    def fetchone(self):
        return self.row

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


class MutationTest(unittest.TestCase):
    def test_gateway_can_forget_accepted_cloud_captures(self):
        for sid in (None, "cursor_cloud:repo:task", "grokbot:repo:task"):
            args = {"summary": "Cloud capture"}
            if sid is not None:
                args["session_id"] = sid
            with self.subTest(sid=sid), \
                    mock.patch.object(srv, "_via_https_gateway", return_value=True), \
                    mock.patch.object(srv, "_stdio_hook_owns_capture", return_value=False), \
                    mock.patch.object(srv, "_capture_mode", return_value="hub"), \
                    mock.patch("khipu.capture.capture", return_value=0) as capture, \
                    mock.patch.object(srv, "_captured_episode_id", return_value=7), \
                    mock.patch("khipu.db.connect", return_value=_Connection((sid,))), \
                    mock.patch("khipu.forget.forget_everywhere", return_value={"ok": True, "id": 7}) as forget:
                captured = srv._tool_capture(args)
                self.assertEqual(capture.call_args.args[0].get("session_id"), sid)
                out = srv.handle_message(request("khipu_forget", {"id": captured["episode_id"]}))
                self.assertFalse(out["result"]["isError"], out)
                forget.assert_called_once_with(7)

    def test_forget_rejects_local_gateway_provenance(self):
        from khipu.forget import LOCAL_HARNESSES

        for sid in (h + ":local" for h in LOCAL_HARNESSES):
            with self.subTest(sid=sid), \
                    mock.patch.object(srv, "_via_https_gateway", return_value=True), \
                    mock.patch("khipu.db.connect", return_value=_Connection((sid,))), \
                    mock.patch("khipu.forget.forget_everywhere") as forget:
                with self.assertRaises(ValueError):
                    srv._tool_forget({"id": 7})
                forget.assert_not_called()

    def test_partial_forget_exposes_diagnostics_as_tool_error(self):
        failed = {"ok": False, "id": 7, "complete": False, "partial": ["outbox"],
                  "error": "forget incomplete; remaining stores: outbox",
                  "partial_errors": {"outbox": [{"error": "PermissionError: denied", "remaining": ["queued.json"]}]}}
        with mock.patch.object(srv, "_via_https_gateway", return_value=False), \
                mock.patch("khipu.forget.forget_everywhere", return_value=failed):
            out = srv.handle_message(request("khipu_forget", {"id": 7}))
        self.assertTrue(out["result"]["isError"])
        body = json.loads(out["result"]["content"][0]["text"])
        self.assertEqual(body.get("partial_errors"), failed["partial_errors"])
        self.assertEqual(body.get("id"), 7)

    def test_replacement_inherits_project_and_no_orphan_on_retry(self):
        conn = _Connection((1, "acme/repo", None))
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.decisions._evidence_ready", return_value=True), \
                mock.patch("khipu.decisions.create_decision", return_value=8) as create, \
                mock.patch("khipu.decisions.supersede", return_value=True):
            out = srv._tool_decisions_update({"id": 1, "action": "supersede", "new_text": "replacement"})
            self.assertTrue(out["ok"])
            self.assertEqual(create.call_args.kwargs["project"], "acme/repo")
            conn.row = (1, "acme/repo", 8)
            create.reset_mock()
            out = srv._tool_decisions_update({"id": 1, "action": "supersede", "new_text": "unused"})
            self.assertFalse(out["ok"])
            create.assert_not_called()

    def test_replacement_rolls_back_if_application_loses_race(self):
        conn = _Connection((1, "acme/repo", None))
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.decisions._evidence_ready", return_value=True), \
                mock.patch("khipu.decisions.create_decision", return_value=8), \
                mock.patch("khipu.decisions.supersede", return_value=False):
            out = srv._tool_decisions_update({"id": 1, "action": "supersede", "new_text": "replacement"})
        self.assertFalse(out["ok"])
        self.assertEqual(conn.committed, 0)
        self.assertEqual(conn.rolled_back, 1)

    def test_decision_text_and_reasons_are_redacted_at_mcp_boundary(self):
        secret = "sk-ant-" + "A" * 30
        conn = _Connection((1, "acme/repo", None))
        with mock.patch("khipu.db.connect", return_value=conn), \
                mock.patch("khipu.decisions._evidence_ready", return_value=True), \
                mock.patch("khipu.decisions.create_decision", return_value=8) as create, \
                mock.patch("khipu.decisions.supersede", return_value=True) as supersede, \
                mock.patch("khipu.decisions.retract", return_value=True) as retract:
            srv._tool_decisions_update({"id": 1, "action": "supersede", "new_text": secret, "reason": secret})
            srv._tool_decisions_update({"id": 1, "action": "retract", "reason": secret})
        self.assertNotIn(secret, str(create.call_args))
        self.assertNotIn(secret, str(supersede.call_args))
        self.assertNotIn(secret, str(retract.call_args))


class RecallBoundaryTest(unittest.TestCase):
    def test_failed_recall_is_not_completed_no_match(self):
        result = {"context": "", "hits": [], "reason": "ok", "degraded": "no legs completed",
                  "prior_work_meta": {"outcome": "no_match", "legs": [], "degraded": "no legs completed"}}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=result):
            out = {}
            srv._attach_prior_work(out, {"prompt": "Investigate the broken recall"})
        self.assertEqual(out["prior_work_meta"]["outcome"], "error")

    def test_final_prior_work_text_obeys_600_character_budget(self):
        result = {"context": "x" * 580 + "\n" + "deliverable" * 20, "hits": [], "reason": "ok"}
        with mock.patch("khipu.recall_prompt.prior_work_for_prompt", return_value=result):
            out = {}
            srv._attach_prior_work(out, {"prompt": "Find the previous deliverable"})
        self.assertLessEqual(len(out["prior_work_text"]), 600)

    def test_search_redacts_query_before_retrieval(self):
        secret = "sk-ant-" + "A" * 30
        with mock.patch("khipu.embed.hybrid_search", return_value={"results": []}) as search, \
                mock.patch("khipu.query_log.log_query"):
            srv._tool_search({"query": "find " + secret})
        self.assertNotIn(secret, search.call_args.args[0])

    def test_reflection_description_matches_identifier_validation_and_cache_writes(self):
        description = next(t["description"] for t in srv.TOOLS if t["name"] == "khipu_reflect")
        self.assertIn("citation identifiers", description)
        self.assertNotIn("a claim the sources do not support is removed", description)
        self.assertIn("retrieval caches", description)
        self.assertNotIn("Writes nothing", description)


class BoundsTest(unittest.TestCase):
    def test_oversized_stdio_frame_is_bounded_and_next_ping_survives(self):
        output = io.StringIO()
        frames = '"' + 'x' * 1500 + '"\n' + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + '\n'
        with mock.patch.object(srv, "MAX_FRAME_BYTES", 1024, create=True), \
                mock.patch.object(srv.sys, "stdin", io.StringIO(frames)), \
                mock.patch.object(srv.sys, "stdout", output), \
                mock.patch.object(srv, "_note_loaded_modules"), \
                mock.patch.object(srv, "_RELOAD_ON_CHANGE", False):
            srv.main()
        out = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertIn("frame too large", out[0]["error"]["message"])
        self.assertEqual(out[1]["id"], 9)

    def test_large_episodes_retain_full_content_and_identity(self):
        # Episodes do not page (their fields are capped at write time). Topic
        # bodies page through the real hub and replica reads, followed to the
        # end, in test_record_paging.py.
        for content in ("x" * 400000 + "THE_END", "漢" * 45000 + "末尾"):
            row = {"id": 7, "summary": content}
            with self.subTest(content_length=len(content)), \
                    mock.patch("khipu.activity.episode_detail", return_value=row), \
                    mock.patch("khipu.decisions.decision_states_for_episode", return_value={}):
                out = srv.handle_message(request("khipu_get", {"id": "7", "kind": "episode"}))
                self.assertFalse(out["result"]["isError"])
                body = json.loads(out["result"]["content"][0]["text"])
                self.assertEqual(body.get("summary"), content)
                self.assertEqual(body.get("id"), 7)

    def test_incomplete_body_timeout_releases_handler(self):
        h = handler()
        h.headers["Content-Length"] = "100"
        h.rfile.read = mock.Mock(side_effect=TimeoutError("body deadline"))
        h.rfile.read1 = h.rfile.read
        with mock.patch.object(gw, "_log"):
            h.do_POST()
        self.assertEqual(h._send.call_args.args[0], 408)

    def test_gateway_bounds_worker_admission_and_releases_slots(self):
        server_class = getattr(gw, "BoundedHTTPServer", None)
        self.assertIsNotNone(server_class)
        server = object.__new__(server_class)
        server._workers = threading.BoundedSemaphore(1)
        sock = mock.Mock()
        with mock.patch.object(gw.ThreadingHTTPServer, "process_request"), \
                mock.patch.object(server, "shutdown_request") as shutdown:
            server.process_request(sock, ("127.0.0.1", 1))
            server.process_request(sock, ("127.0.0.1", 2))
        shutdown.assert_called_once_with(sock)
        self.assertIn(b"503", sock.sendall.call_args.args[0])
        with mock.patch.object(gw.ThreadingHTTPServer, "process_request_thread"):
            server.process_request_thread(sock, ("127.0.0.1", 1))
        self.assertTrue(server._workers.acquire(blocking=False))
