"""Focused regressions for audit1010 CLI integration ownership and TOML edits."""
from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from khipu import integrations as integ
from khipu import probe


class _AegisConfigCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.config = self.home / ".grok" / "config.toml"
        self.config.parent.mkdir()
        self.patches = [
            mock.patch.object(integ, "HOME", self.home),
            mock.patch.object(integ, "AEGIS_TOML", self.config),
            mock.patch.object(integ, "CODEX_TOML", self.home / ".codex" / "config.toml"),
            mock.patch.object(integ, "CODEX_HOOKS", self.home / ".codex" / "hooks.json"),
            mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.home / ".config" / "khipu")}),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()


class OwnershipTest(unittest.TestCase):
    def test_lookalike_shell_commands_are_not_khipu_owned(self):
        self.assertFalse(integ._is_ours("echo khipu-stop-hook; /opt/user/log"))
        self.assertFalse(integ._is_our_recall("echo khipu-recall-hook"))
        self.assertFalse(integ._is_our_prompt_recall("/opt/khipu-prompt-recall-copy"))
        self.assertFalse(integ._is_our_capture("/opt/khipu-aegis-capture-copy"))

    def test_install_and_uninstall_keep_lookalike_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            settings = home / ".claude" / "settings.json"
            settings.parent.mkdir()
            settings.write_text('{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo khipu-stop-hook; /opt/user/log"}]}]}}')
            (home / ".claude.json").write_text("{}")
            with mock.patch.object(integ, "HOME", home), \
                    mock.patch.object(integ, "CLAUDE_SETTINGS", settings), \
                    mock.patch.object(integ, "CLAUDE_JSON", home / ".claude.json"):
                integ.install("claude_code")
                integ.uninstall("claude_code")
            commands = [hook["command"] for entry in __import__("json").loads(settings.read_text())["hooks"]["Stop"]
                        for hook in entry["hooks"]]
            self.assertEqual(commands, ["echo khipu-stop-hook; /opt/user/log"])


class AegisTomlTest(_AegisConfigCase):
    def _native_owned(self, event: str) -> str:
        command = integ.aegis_capture_hook()
        return (
            f'[[hooks.{event}]]\n[[hooks.{event}.hooks]]\ntype = "command"\n'
            f'command = "{command}"\ntimeout = 15\n'
            f'[hooks.{event}.hooks.env]\nKHIPU_HARNESS = "aegis"\n'
        )

    def test_managed_plus_normalized_native_collapses_to_one_owned_hook_per_event(self):
        mcp, managed = integ._aegis_blocks()
        self.config.write_text(
            mcp + "\n" + managed + "\n" +
            "\n".join(self._native_owned(event)
                        for event in ("Stop", "PreCompact", "SessionEnd"))
        )

        integ.install("aegis")
        text = self.config.read_text()
        parsed = tomllib.loads(text)
        self.assertEqual(text.count(integ._AEGIS_HOOK_MARK), 1)
        for event in ("Stop", "PreCompact", "SessionEnd"):
            owned = [hook for group in parsed["hooks"][event]
                     for hook in group.get("hooks", [])
                     if hook.get("command") == integ.aegis_capture_hook()]
            self.assertEqual(len(owned), 1, event)
        self.assertEqual(integ.install("aegis")["changes"], [])

        integ.uninstall("aegis")
        parsed = tomllib.loads(self.config.read_text())
        for groups in parsed.get("hooks", {}).values():
            self.assertFalse(any(hook.get("command") == integ.aegis_capture_hook()
                                 for group in groups for hook in group.get("hooks", [])))

    def test_direct_uninstall_removes_native_owned_hooks_and_keeps_foreign(self):
        command = integ.aegis_capture_hook()
        self.config.write_text(
            '[mcp_servers.khipu]\ncommand = "old"\n\n' +
            "\n".join(self._native_owned(event)
                        for event in ("Stop", "PreCompact", "SessionEnd")) +
            '\n[[hooks.UserPromptSubmit]]\n[[hooks.UserPromptSubmit.hooks]]\n'
            'type = "command"\ncommand = "/opt/foreign"\ntimeout = 4\n'
        )

        integ.uninstall("aegis")
        text = self.config.read_text()
        self.assertNotIn(command, text)
        self.assertNotIn("mcp_servers.khipu", text)
        self.assertIn('command = "/opt/foreign"', text)
        tomllib.loads(text)

    def test_fake_mcp_header_inside_multiline_string_is_preserved(self):
        self.config.write_text(
            'banner = """first line\n[mcp_servers.khipu]\ncommand = "/fake"\n"""\n'
            '\n[other]\nvalue = 1\n'
        )

        integ.install("aegis")
        installed = self.config.read_text()
        self.assertIn('banner = """first line\n[mcp_servers.khipu]\ncommand = "/fake"\n"""',
                      installed)
        self.assertEqual(tomllib.loads(installed)["banner"],
                         'first line\n[mcp_servers.khipu]\ncommand = "/fake"\n')
        self.assertEqual(tomllib.loads(installed)["other"]["value"], 1)
        integ.uninstall("aegis")
        self.assertIn('command = "/fake"', self.config.read_text())
        self.assertEqual(tomllib.loads(self.config.read_text())["other"]["value"], 1)

    def test_mixed_native_hooks_preserve_unrelated_events_and_entries(self):
        command = integ.aegis_capture_hook()
        self.config.write_text(
            '[ "mcp_servers" . \'khipu\' ] # normalized by another writer\n'
            'command = "/old/khipu-mcp"\n\n'
            '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
            f'command = "{command}"\ntimeout = 15\n'
            '[hooks.Stop.hooks.env]\nKHIPU_HARNESS = "aegis"\n\n'
            '[[hooks.UserPromptSubmit]]\n[[hooks.UserPromptSubmit.hooks]]\n'
            'type = "command"\ncommand = "/opt/prompt-owner"\ntimeout = 9\n\n'
            '[[hooks.PreCompact]]\n[[hooks.PreCompact.hooks]]\ntype = "command"\n'
            f'command = "{command}"\ntimeout = 15\n'
            '[hooks.PreCompact.hooks.env]\nKHIPU_HARNESS = "aegis"\n\n'
            '[[hooks.SessionEnd]]\n[[hooks.SessionEnd.hooks]]\ntype = "command"\n'
            f'command = "{command}"\ntimeout = 15\n'
            '[hooks.SessionEnd.hooks.env]\nKHIPU_HARNESS = "aegis"\n\n'
            '[[hooks.SessionEnd]]\n[[hooks.SessionEnd.hooks]]\ntype = "command"\n'
            'command = "/opt/session-owner"\ntimeout = 7\n'
        )

        integ.install("aegis")
        installed = self.config.read_text()
        parsed = tomllib.loads(installed)
        self.assertEqual(installed.count("[mcp_servers.khipu]"), 1)
        self.assertEqual(parsed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                         "/opt/prompt-owner")
        self.assertTrue(any(h["command"] == "/opt/session-owner"
                            for group in parsed["hooks"]["SessionEnd"]
                            for h in group.get("hooks", [])))
        self.assertEqual(integ.install("aegis")["changes"], [])

        integ.uninstall("aegis")
        uninstalled = self.config.read_text()
        parsed = tomllib.loads(uninstalled)
        self.assertNotIn("khipu", parsed.get("mcp_servers", {}))
        self.assertEqual(parsed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
                         "/opt/prompt-owner")
        self.assertTrue(any(h["command"] == "/opt/session-owner"
                            for group in parsed["hooks"]["SessionEnd"]
                            for h in group.get("hooks", [])))

    def test_trailing_comment_header_is_replaced_without_duplicate_table(self):
        self.config.write_text(
            'model = "x"\n\n[mcp_servers.khipu] # existing Khipu server\n'
            'command = "/old/khipu-mcp"\nenabled = true\n\n[other]\nvalue = 1\n'
        )
        integ.install("aegis")
        text = self.config.read_text()
        self.assertEqual(text.count("[mcp_servers.khipu]"), 1)
        self.assertEqual(tomllib.loads(text)["other"]["value"], 1)
        integ.uninstall("aegis")
        self.assertNotIn("[mcp_servers.khipu]", self.config.read_text())
        self.assertEqual(tomllib.loads(self.config.read_text())["other"]["value"], 1)

    def test_uninstall_removes_normalized_native_owned_hook(self):
        command = integ.aegis_capture_hook()
        self.config.write_text(
            '[mcp_servers.khipu]\ncommand = "x"\n\n'
            '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
            f'command = "{command}"\ntimeout = 15\n'
            '[hooks.Stop.hooks.env]\nKHIPU_HARNESS = "aegis"\n\n'
            '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\ncommand = "/opt/other"\n'
        )
        integ.install("aegis")
        self.assertEqual(integ.install("aegis")["changes"], [])
        self.assertIn('command = "/opt/other"', self.config.read_text())
        integ.uninstall("aegis")
        text = self.config.read_text()
        self.assertNotIn(command, text)
        self.assertIn('command = "/opt/other"', text)
        tomllib.loads(text)

    def test_codex_trailing_comment_header_round_trips(self):
        config = integ.CODEX_TOML
        config.parent.mkdir()
        config.write_text('[mcp_servers.khipu] # old server\ncommand = "/old/khipu-mcp"\n\n[other]\nvalue = 1\n')
        integ.CODEX_HOOKS.write_text('{"hooks": {}}')
        integ.install("codex")
        text = config.read_text()
        self.assertEqual(text.count("[mcp_servers.khipu]"), 1)
        self.assertEqual(tomllib.loads(text)["other"]["value"], 1)
        integ.uninstall("codex")
        self.assertNotIn("[mcp_servers.khipu]", config.read_text())
        self.assertEqual(tomllib.loads(config.read_text())["other"]["value"], 1)


class ProbePersistenceTest(unittest.TestCase):
    def test_rv4_08_partial_receipt_failure_keeps_aggregate_red(self):
        import json
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": tmp}):
            harness_path = probe.harness_state_path("codex")
            write_text = Path.write_text
            def write(path, *args, **kwargs):
                if path == harness_path:
                    raise OSError("harness receipt denied")
                return write_text(path, *args, **kwargs)
            result = {"ts": probe._now_iso(), "harness": "codex", "ok": True, "error": None}
            with mock.patch.object(Path, "write_text", write):
                current = probe._persist_result(result)
            stored = json.loads(probe.state_path().read_text())
            self.assertFalse(current["ok"])
            self.assertFalse(stored["ok"])
            self.assertEqual(stored["persistence"], current["persistence"])
            self.assertIn("harness receipt denied", stored["error"])
            self.assertFalse(probe.status()["ok"])

    def test_current_probe_result_is_red_when_state_receipt_cannot_persist(self):
        search = {"results": [{"kind": "episode", "id": "7"}]}
        with mock.patch("khipu.config.capture_mode", return_value="hub"), \
                mock.patch("khipu.capture.capture", return_value=0), \
                mock.patch("khipu.probe._find_episode_id", return_value=7), \
                mock.patch("khipu.embed.hybrid_search", return_value=search), \
                mock.patch("khipu.probe._forget_episode", return_value={"ok": True}), \
                mock.patch("khipu.probe._write_state", return_value="OSError: disk full"):
            result = probe.run_probe("codex", timeout_s=0.01)
        self.assertFalse(result["ok"])
        self.assertFalse(result["persistence"]["ok"])
        self.assertIn("disk full", result["error"])

    def test_denied_receipt_write_keeps_current_capture_failure_red_despite_stale_success(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": tmp}), \
                mock.patch("khipu.config.capture_mode", return_value="hub"), \
                mock.patch("khipu.capture.capture", return_value=70):
            receipt = probe.state_path()
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                __import__("json").dumps({
                    "ts": probe._now_iso(), "harness": "codex", "ok": True,
                    "seconds": 0.1, "episode_id": 99, "error": None,
                })
            )
            with mock.patch.object(Path, "write_text", side_effect=OSError("read-only filesystem")):
                current = probe.run_probe("codex", timeout_s=0.01)

            self.assertFalse(current["ok"])
            self.assertFalse(current["persistence"]["ok"])
            self.assertIn("capture exited 70", current["error"])
            self.assertIn("read-only filesystem", current["error"])
            self.assertTrue(__import__("json").loads(receipt.read_text())["ok"])


class GatewayLivenessTest(unittest.TestCase):
    def test_public_ok_body_is_not_an_authenticated_liveness_result(self):
        response = mock.MagicMock()
        response.read.return_value = b'{"ok": true}'
        response.__enter__.return_value = response
        response.__exit__.return_value = False
        with mock.patch("khipu.config.gateway_url", return_value="https://gateway.invalid"), \
                mock.patch.object(integ, "_gateway_token", return_value="expected"), \
                mock.patch("urllib.request.urlopen", return_value=response):
            result = integ.gateway_liveness_check()
        self.assertFalse(result["ok"])
        self.assertIn("authenticated", result["error"])


class QueueOnlyIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(integ, "HOME", self.home),
            mock.patch.object(integ, "CURSOR_MCP", self.home / ".cursor" / "mcp.json"),
            mock.patch.object(integ, "CURSOR_HOOKS", self.home / ".cursor" / "hooks.json"),
            mock.patch.object(integ, "CODEX_TOML", self.home / ".codex" / "config.toml"),
            mock.patch.object(integ, "CODEX_HOOKS", self.home / ".codex" / "hooks.json"),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ordinary_capture_events_install_the_draining_hook_and_only_codex_sessionend_queues(self):
        claude = self.home / ".claude" / "settings.json"
        claude.parent.mkdir()
        claude.write_text("{}")
        integ._claude_hooks_install(claude, False, {"changes": []})

        cursor_dir = self.home / ".cursor"
        cursor_dir.mkdir()
        integ.CURSOR_MCP.write_text("{}")
        integ.CURSOR_HOOKS.write_text("{}")
        integ._cursor_install(False)

        codex_dir = self.home / ".codex"
        codex_dir.mkdir()
        integ.CODEX_TOML.write_text("")
        integ.CODEX_HOOKS.write_text("{}")
        integ._codex_install(False)

        claude_hooks = __import__("json").loads(claude.read_text())["hooks"]
        cursor_hooks = __import__("json").loads(integ.CURSOR_HOOKS.read_text())["hooks"]
        codex_hooks = __import__("json").loads(integ.CODEX_HOOKS.read_text())["hooks"]
        # The installed command is what the harness runs; a queue-only command
        # here would leave every capture queued until something else drained it.
        draining = integ.stop_hook()
        queue_only = integ.queue_only_stop_hook()
        self.assertNotEqual(draining, queue_only)
        for event in ("Stop", "PreCompact", "SessionEnd", "SubagentStop"):
            hook = claude_hooks[event][0]["hooks"][0]
            self.assertEqual(hook["command"], draining, (event, hook))
            self.assertTrue(integ._is_ours(hook["command"]), (event, hook))
            self.assertEqual(hook["timeout"], 20)
        for event in ("stop", "preCompact", "sessionEnd", "subagentStop"):
            hook = cursor_hooks[event][0]
            self.assertEqual(hook["command"], draining, (event, hook))
            self.assertTrue(integ._is_ours(hook["command"]), (event, hook))
            self.assertEqual(hook["timeout"], 20)
        for event in ("Stop", "PreCompact", "SessionEnd", "SubagentStop"):
            hook = codex_hooks[event][0]["hooks"][0]
            want = queue_only if event == "SessionEnd" else draining
            self.assertEqual(hook["command"], want, (event, hook))
            self.assertTrue(integ._is_ours(hook["command"]), (event, hook))
            self.assertEqual(hook["timeout"], 3 if event == "SessionEnd" else 20)


class GatewayTokenPermissionsTest(unittest.TestCase):
    def test_existing_token_is_mode_0600_before_any_secret_bytes_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            token_path = Path(tmp) / "gateway-token"
            token_path.write_text("old-token\n")
            token_path.chmod(0o644)
            real_write = os.write

            def assert_private_then_write(fd, data):
                self.assertEqual(os.fstat(fd).st_mode & 0o777, 0o600)
                return real_write(fd, data)

            with mock.patch.object(integ, "gateway_token_file", return_value=token_path), \
                    mock.patch.object(integ.os, "write", side_effect=assert_private_then_write):
                integ.set_gateway_token("new-token")
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)
