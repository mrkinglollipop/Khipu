"""Tests for the Aegis gateway-token gap (found live 2026-09-14): Aegis talks
to Khipu over the HTTPS gateway with a bearer it reads from env
KHIPU_GATEWAY_TOKEN or a `[memory.khipu] token_file` in its own
~/.grok/config.toml — and nothing ever staged that file, so every Aegis
recall 401'd while `khipu integrations verify aegis` and `khipu doctor`
stayed green.

Covers:
  * `khipu gateway token set` / `status` (khipu.integrations + the CLI verb)
  * `khipu integrations install aegis` wiring `[memory]` / `[memory.khipu]`
    only when the token file exists, never overriding an existing backend
  * `khipu integrations verify aegis`'s real gateway round trip: red on
    401/403, red on a missing token, green on 200

Every test runs against a temp HOME + temp KHIPU_DATA_DIR, same convention as
test_integrations.py and test_gateway.py's GrokBotPackTest.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import integrations as integ


class _TempHomeAndDataDirCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="khipu-gwtok-"))
        self.data_dir = self.home / ".config" / "khipu"
        self._patches = [
            mock.patch.object(integ, "HOME", self.home),
            mock.patch.object(integ, "AEGIS_TOML", self.home / ".grok" / "config.toml"),
            mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.data_dir)}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()


class GatewayTokenFileTest(_TempHomeAndDataDirCase):
    def test_set_writes_mode_0600_and_status_never_echoes_it(self):
        secret = "gw-tok-abc123-do-not-echo"
        path = integ.set_gateway_token(secret)
        self.assertEqual(path, self.data_dir / "gateway-token")
        self.assertTrue(path.is_file())
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(path.read_text(encoding="utf-8").strip(), secret)

        status = integ.gateway_token_file_status()
        self.assertEqual(status, {"path": str(path), "exists": True,
                                   "mode": "0o600", "bytes": len(secret) + 1})
        self.assertNotIn(secret, json.dumps(status))

    def test_status_reports_absence_without_creating_anything(self):
        status = integ.gateway_token_file_status()
        self.assertEqual(status, {"path": str(self.data_dir / "gateway-token"), "exists": False})
        self.assertFalse((self.data_dir / "gateway-token").exists())

    def test_set_is_umask_safe(self):
        """A permissive umask must not weaken the file below 0600 — the explicit
        chmod after os.open is what guarantees this, not the open() mode alone."""
        old = os.umask(0o022)
        try:
            path = integ.set_gateway_token("tok")
        finally:
            os.umask(old)
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_set_overwrites_and_is_idempotent_in_shape(self):
        integ.set_gateway_token("first-token")
        integ.set_gateway_token("second-token")
        path = integ.gateway_token_file()
        self.assertEqual(path.read_text(encoding="utf-8").strip(), "second-token")

    def test_set_rejects_empty_or_newline_embedded_tokens(self):
        with self.assertRaises(ValueError):
            integ.set_gateway_token("")
        with self.assertRaises(ValueError):
            integ.set_gateway_token("   ")
        with self.assertRaises(ValueError):
            integ.set_gateway_token("tok\nwith-newline")


class GatewayTokenCliTest(_TempHomeAndDataDirCase):
    def _run(self, args_ns):
        from khipu import cli

        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_gateway(args_ns)
        return rc, json.loads(out.getvalue())

    def test_set_reads_from_stdin_by_default(self):
        from khipu import cli

        args = mock.Mock(gateway_cmd="token", gateway_token_cmd="set", from_env=None)
        with mock.patch("sys.stdin", io.StringIO("piped-secret-token\n")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_gateway(args)
        payload = json.loads(out.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertNotIn("piped-secret-token", json.dumps(payload))
        self.assertEqual(integ.gateway_token_file().read_text(encoding="utf-8").strip(),
                          "piped-secret-token")

    def test_set_from_env_reads_the_named_variable(self):
        args = mock.Mock(gateway_cmd="token", gateway_token_cmd="set", from_env="MY_KHIPU_TOK")
        with mock.patch.dict(os.environ, {"MY_KHIPU_TOK": "env-sourced-token"}):
            rc, payload = self._run(args)
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(integ.gateway_token_file().read_text(encoding="utf-8").strip(),
                          "env-sourced-token")

    def test_set_from_env_missing_is_refused_without_writing(self):
        args = mock.Mock(gateway_cmd="token", gateway_token_cmd="set", from_env="NOT_SET_VAR")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NOT_SET_VAR", None)
            rc, payload = self._run(args)
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])
        self.assertFalse(integ.gateway_token_file().exists())

    def test_status_via_cli_reports_presence_only(self):
        integ.set_gateway_token("some-token")
        args = mock.Mock(gateway_cmd="token", gateway_token_cmd="status")
        rc, payload = self._run(args)
        self.assertEqual(rc, 0)
        self.assertTrue(payload["exists"])
        self.assertNotIn("some-token", json.dumps(payload))


class AegisInstallMemoryBlockTest(_TempHomeAndDataDirCase):
    def _write_toml(self, text: str) -> None:
        (self.home / ".grok").mkdir(parents=True, exist_ok=True)
        (self.home / ".grok" / "config.toml").write_text(text, encoding="utf-8")

    def test_missing_token_file_reports_the_fix_and_touches_no_memory_block(self):
        self._write_toml('model = "grok"\n')
        out = integ.install("aegis")
        self.assertEqual(out.get("note"),
                          "Aegis can search memory only with the gateway token: "
                          "run `khipu gateway token set`")
        text = (self.home / ".grok" / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn("[memory]", text)
        self.assertNotIn("[memory.khipu]", text)

    def test_token_file_present_wires_memory_and_token_file_and_is_idempotent(self):
        import tomllib

        self._write_toml('model = "grok"\n')
        integ.set_gateway_token("staged-token")
        out = integ.install("aegis")
        self.assertIn(f'{integ.AEGIS_TOML}: add [memory] backend = "khipu", enabled = true',
                       out["changes"])
        self.assertIn(f"{integ.AEGIS_TOML}: add [memory.khipu] token_file", out["changes"])
        text = (self.home / ".grok" / "config.toml").read_text(encoding="utf-8")
        t = tomllib.loads(text)
        self.assertEqual(t["memory"]["backend"], "khipu")
        self.assertTrue(t["memory"]["enabled"])
        self.assertEqual(t["memory"]["khipu"]["token_file"], str(integ.gateway_token_file()))

        # idempotent: installing again with the same state changes nothing
        out2 = integ.install("aegis")
        self.assertEqual(out2["changes"], [])
        self.assertNotIn("note", out2)

    def test_existing_memory_backend_is_never_overridden(self):
        self._write_toml('[memory]\nbackend = "someone-elses-tool"\nenabled = true\n')
        integ.set_gateway_token("staged-token")
        out = integ.install("aegis")
        self.assertEqual(out["memory_backend"], "someone-elses-tool")
        self.assertIn("someone-elses-tool", out["note"])
        text = (self.home / ".grok" / "config.toml").read_text(encoding="utf-8")
        self.assertIn('backend = "someone-elses-tool"', text)
        self.assertNotIn('backend = "khipu"', text)
        # token_file is still wired even though backend was left alone
        self.assertIn("[memory.khipu]", text)

    def test_existing_khipu_backend_is_reported_but_not_rewritten(self):
        self._write_toml('[memory]\nbackend = "khipu"\nenabled = true\nextra_key = 1\n')
        integ.set_gateway_token("staged-token")
        out = integ.install("aegis")
        self.assertEqual(out["memory_backend"], "khipu")
        self.assertNotIn("note", out)  # nothing to warn about
        text = (self.home / ".grok" / "config.toml").read_text(encoding="utf-8")
        self.assertIn("extra_key = 1", text)  # the pre-existing block survived untouched

    def test_backs_up_before_writing_memory_blocks(self):
        self._write_toml('model = "grok"\n')
        integ.set_gateway_token("staged-token")
        out = integ.install("aegis")
        self.assertTrue(out.get("backups"))
        self.assertTrue(Path(out["backups"][0]).is_file())


class AegisGatewayVerifyTest(_TempHomeAndDataDirCase):
    def setUp(self):
        super().setUp()
        self._url_patch = mock.patch.dict(os.environ, {"KHIPU_GATEWAY_URL": "https://khipu.example.test"})
        self._url_patch.start()

    def tearDown(self):
        self._url_patch.stop()
        super().tearDown()

    def test_not_applicable_without_a_configured_gateway_url(self):
        with mock.patch.dict(os.environ, {"KHIPU_GATEWAY_URL": ""}):
            out = integ.aegis_gateway_check()
        self.assertTrue(out["ok"])
        self.assertFalse(out["applicable"])

    def test_red_on_missing_token(self):
        out = integ.aegis_gateway_check()
        self.assertFalse(out["ok"])
        self.assertTrue(out["applicable"])
        self.assertEqual(out["fix"], "run `khipu gateway token set`")

    def test_red_on_401(self):
        integ.set_gateway_token("wrong-token")
        with mock.patch.object(integ, "_probe_gateway",
                               return_value={"ok": False, "http_status": 401,
                                             "error": "HTTP 401: Unauthorized"}) as pg:
            out = integ.aegis_gateway_check()
        pg.assert_called_once_with("https://khipu.example.test", "wrong-token")
        self.assertFalse(out["ok"])
        self.assertEqual(out["http_status"], 401)
        self.assertEqual(out["fix"], "run `khipu gateway token set`")

    def test_red_on_403(self):
        integ.set_gateway_token("forbidden-token")
        with mock.patch.object(integ, "_probe_gateway",
                               return_value={"ok": False, "http_status": 403, "error": "HTTP 403: Forbidden"}):
            out = integ.aegis_gateway_check()
        self.assertFalse(out["ok"])
        self.assertEqual(out["fix"], "run `khipu gateway token set`")

    def test_red_gateway_unreachable(self):
        integ.set_gateway_token("some-token")
        with mock.patch.object(integ, "_probe_gateway",
                               return_value={"ok": False, "http_status": None,
                                             "error": "URLError: timed out"}):
            out = integ.aegis_gateway_check()
        self.assertFalse(out["ok"])
        self.assertIsNone(out["http_status"])
        self.assertEqual(out["error"], "gateway unreachable")
        self.assertNotIn("fix", out)

    def test_green_on_200(self):
        integ.set_gateway_token("good-token")
        with mock.patch.object(integ, "_probe_gateway",
                               return_value={"ok": True, "http_status": 200, "episodes": 3,
                                             "tools": 9, "ms": 12}) as pg:
            out = integ.aegis_gateway_check()
        pg.assert_called_once_with("https://khipu.example.test", "good-token")
        self.assertTrue(out["ok"])
        self.assertTrue(out["gateway_ok"])
        self.assertEqual(out["http_status"], 200)

    def test_env_token_takes_priority_over_the_file(self):
        integ.set_gateway_token("file-token")
        with mock.patch.dict(os.environ, {"KHIPU_GATEWAY_TOKEN": "env-token"}), \
             mock.patch.object(integ, "_probe_gateway", return_value={"ok": True, "http_status": 200}) as pg:
            integ.aegis_gateway_check()
        pg.assert_called_once_with("https://khipu.example.test", "env-token")

    def test_verify_aegis_includes_the_gateway_component_and_fails_verify_on_401(self):
        """The end-to-end wiring into `integrations.verify('aegis')`: a red
        gateway check must fail the whole verify, the same way a broken hook
        or a failed recall probe does."""
        (self.home / ".grok").mkdir(parents=True, exist_ok=True)
        (self.home / ".grok" / "config.toml").write_text('model = "grok"\n', encoding="utf-8")
        integ.set_gateway_token("wrong-token")
        integ.install("aegis")
        with mock.patch.object(integ, "_probe_gateway",
                               return_value={"ok": False, "http_status": 401, "error": "HTTP 401"}), \
             mock.patch.object(integ, "_probe_mcp", return_value={"ok": True}), \
             mock.patch.object(integ, "_probe_mcp_no_keychain", return_value={"ok": True}), \
             mock.patch.object(integ, "_probe_extract", return_value={"ok": True}), \
             mock.patch.object(integ, "_probe_aegis_isolation", return_value={"ok": True}), \
             mock.patch.object(integ, "_aegis_runtime", return_value={"ok": True}), \
             mock.patch("khipu.probe.run_probe", return_value={"ok": True, "harness": "aegis"}):
            v = integ.verify("aegis")
        self.assertIn("gateway", v["components"])
        self.assertFalse(v["components"]["gateway"]["ok"])
        self.assertFalse(v["ok"], v)


if __name__ == "__main__":
    unittest.main()
