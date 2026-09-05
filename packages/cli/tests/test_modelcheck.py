"""khipu.modelcheck.check_model_keys — prove a model key works with one real,
cheap call, in plain words (scope doc:
docs/plans/2026-09-05-setup-that-cannot-strand-you.md).

Pure tests mock ``urlopen``/``embed_one`` for every failure mapping, the skip
path (no key configured), and the CLI wiring (``khipu secrets verify``, exit
codes). One Live test makes a real call on this Mac and is skipped without a
reachable Gemini key.
"""
from __future__ import annotations

import io
import json
import subprocess
import unittest
import urllib.error
from unittest import mock

from khipu import cli, modelcheck as mc


def _http_error(code: int, body: bytes = b"{}") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid", code, "err", {}, io.BytesIO(body)
    )


def _key_available() -> bool:
    try:
        from khipu.keychain import resolve_gemini_key

        return bool(resolve_gemini_key())
    except Exception:
        return False


KEY_AVAILABLE = _key_available()


class GeminiEmbedCheckTest(unittest.TestCase):
    """gemini_embed embeds a 3-word probe with the active profile's model,
    through the normal khipu.embed budget-and-retry path."""

    def _run(self, *, resolve_key="k", embed_side_effect=None, embed_return=None):
        with mock.patch.object(mc.keychain, "resolve_gemini_key",
                                side_effect=None if resolve_key else RuntimeError("no key"),
                                return_value=resolve_key), \
             mock.patch.object(mc, "_active_embed_profile", return_value=mc.embed_mod.PROFILE_2), \
             mock.patch.object(mc.embed_mod, "embed_one",
                                side_effect=embed_side_effect,
                                return_value=embed_return):
            return mc._check_gemini_embed(timeout=5.0)

    def test_probe_text_is_exactly_three_words(self):
        self.assertEqual(len(mc.EMBED_PROBE_TEXT.split()), 3)

    def test_no_key_is_skipped_not_failed(self):
        with mock.patch.object(mc.keychain, "resolve_gemini_key",
                                side_effect=RuntimeError("no key")):
            result = mc._check_gemini_embed(timeout=5.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "no key stored")
        self.assertIsNone(result["model"])

    def test_success_reports_key_works_and_the_model(self):
        result = self._run(embed_return=[0.0] * 768)
        self.assertTrue(result["ok"])
        self.assertEqual(result["title"], "Key works · gemini-embedding-2")
        self.assertEqual(result["model"], "gemini-embedding-2")
        self.assertNotIn("fix", result)

    def test_401_maps_to_key_not_accepted(self):
        result = self._run(embed_side_effect=RuntimeError("embed HTTP 401: bad key"))
        self.assertFalse(result["ok"])
        self.assertIn("was not accepted", result["detail"])
        self.assertIn("console", result["fix"])

    def test_403_maps_to_key_not_accepted(self):
        result = self._run(embed_side_effect=RuntimeError("embed HTTP 403: forbidden"))
        self.assertFalse(result["ok"])
        self.assertIn("was not accepted", result["detail"])

    def test_429_is_not_ok_but_notes_the_key_works(self):
        result = self._run(embed_side_effect=RuntimeError("embed HTTP 429: slow down"))
        self.assertFalse(result["ok"])
        self.assertIn("rate limiting", result["detail"])
        self.assertIn("key itself works", result["detail"])

    def test_404_names_the_model_and_points_at_settings(self):
        result = self._run(embed_side_effect=RuntimeError("embed HTTP 404: not found"))
        self.assertFalse(result["ok"])
        self.assertIn("gemini-embedding-2", result["detail"])
        self.assertIn("Settings", result["detail"])

    def test_network_error_is_a_plain_connection_message(self):
        result = self._run(
            embed_side_effect=RuntimeError(
                "embed network error after 0 retries: URLError: timed out"
            )
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], mc._NETWORK_DETAIL)

    def test_budget_exhausted_is_reported_distinctly(self):
        result = self._run(
            embed_side_effect=RuntimeError("embed budget exhausted: 10000 calls today (cap 10000)")
        )
        self.assertFalse(result["ok"])
        self.assertIn("budget", result["detail"])

    def test_never_returns_the_key(self):
        result = self._run(resolve_key="AIza-super-secret", embed_return=[0.0] * 768)
        self.assertNotIn("AIza-super-secret", json.dumps(result))


class GenerateCheckHttpMappingTest(unittest.TestCase):
    """gemini_generate and openai_compat_generate both go through _post_once;
    exercise the same failure ladder against mocked urlopen."""

    def _run_gemini(self, *, urlopen_side_effect=None, provider="cloud", model_id=""):
        settings = {"provider": provider, "endpoint": "", "model_id": model_id}
        with mock.patch.object(mc.keychain, "resolve_gemini_key", return_value="k"), \
             mock.patch.object(mc.models_mod, "synth_settings", return_value=settings), \
             mock.patch.object(mc.urllib.request, "urlopen", side_effect=urlopen_side_effect):
            return mc._check_gemini_generate(timeout=5.0)

    def _run_openai(self, *, urlopen_side_effect=None, bearer=None):
        settings = {"provider": "local", "endpoint": "http://127.0.0.1:8080", "model_id": "llama"}
        with mock.patch.object(mc.models_mod, "synth_settings", return_value=settings), \
             mock.patch.object(mc.keychain, "get_openai_compat_key", return_value=bearer), \
             mock.patch.object(mc.urllib.request, "urlopen", side_effect=urlopen_side_effect):
            return mc._check_openai_compat_generate(timeout=5.0)

    def test_gemini_no_key_is_skipped(self):
        with mock.patch.object(mc.keychain, "resolve_gemini_key", side_effect=RuntimeError("no key")):
            result = mc._check_gemini_generate(timeout=5.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "no key stored")

    def test_gemini_success(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.read.return_value = b'{"candidates": []}'
        resp.__enter__.return_value = resp
        result = self._run_gemini(urlopen_side_effect=lambda *a, **k: resp)
        self.assertTrue(result["ok"])
        self.assertTrue(result["title"].startswith("Key works ·"))

    def test_gemini_uses_default_model_when_synth_is_local(self):
        resp = mock.MagicMock()
        resp.status = 200
        resp.read.return_value = b"{}"
        resp.__enter__.return_value = resp
        result = self._run_gemini(
            urlopen_side_effect=lambda *a, **k: resp, provider="local", model_id="a-local-model",
        )
        self.assertEqual(result["model"], mc.models_mod.DEFAULT_SYNTH_MODEL)

    def test_gemini_401(self):
        result = self._run_gemini(urlopen_side_effect=_http_error(401))
        self.assertFalse(result["ok"])
        self.assertIn("was not accepted", result["detail"])

    def test_gemini_429_ok_false_with_note(self):
        result = self._run_gemini(urlopen_side_effect=_http_error(429))
        self.assertFalse(result["ok"])
        self.assertIn("key itself works", result["detail"])

    def test_gemini_404_names_the_model(self):
        result = self._run_gemini(urlopen_side_effect=_http_error(404))
        self.assertFalse(result["ok"])
        self.assertIn(result["model"], result["detail"])
        self.assertIn("Settings", result["detail"])

    def test_gemini_network_error(self):
        result = self._run_gemini(urlopen_side_effect=urllib.error.URLError("no route"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], mc._NETWORK_DETAIL)

    def test_openai_not_configured_is_skipped(self):
        with mock.patch.object(mc.models_mod, "synth_settings",
                                return_value={"provider": "cloud", "endpoint": "", "model_id": ""}):
            result = mc._check_openai_compat_generate(timeout=5.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["detail"], "no key stored")

    def test_openai_success_without_a_bearer_key(self):
        # Many local OpenAI-compatible servers need no auth at all — presence
        # of a bearer must not gate whether the check runs.
        resp = mock.MagicMock()
        resp.status = 200
        resp.read.return_value = b'{"choices": []}'
        resp.__enter__.return_value = resp
        result = self._run_openai(urlopen_side_effect=lambda *a, **k: resp, bearer=None)
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "llama")

    def test_openai_403(self):
        result = self._run_openai(urlopen_side_effect=_http_error(403), bearer="tok")
        self.assertFalse(result["ok"])
        self.assertIn("was not accepted", result["detail"])

    def test_openai_network_error(self):
        result = self._run_openai(urlopen_side_effect=TimeoutError("timed out"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["detail"], mc._NETWORK_DETAIL)


class CheckModelKeysScopeTest(unittest.TestCase):
    """`which` selects which checks run; overall ok ignores skipped ones."""

    def test_all_runs_three_checks(self):
        with mock.patch.object(mc, "_check_gemini_embed",
                                return_value={"id": "gemini_embed", "ok": True}), \
             mock.patch.object(mc, "_check_gemini_generate",
                                return_value={"id": "gemini_generate", "ok": True}), \
             mock.patch.object(mc, "_check_openai_compat_generate",
                                return_value={"id": "openai_compat_generate", "ok": True}):
            out = mc.check_model_keys(which="all")
        self.assertEqual([c["id"] for c in out["checks"]],
                          ["gemini_embed", "gemini_generate", "openai_compat_generate"])
        self.assertTrue(out["ok"])

    def test_gemini_scope_skips_openai_check_entirely(self):
        with mock.patch.object(mc, "_check_gemini_embed",
                                return_value={"id": "gemini_embed", "ok": True}), \
             mock.patch.object(mc, "_check_gemini_generate",
                                return_value={"id": "gemini_generate", "ok": True}), \
             mock.patch.object(mc, "_check_openai_compat_generate") as openai_check:
            out = mc.check_model_keys(which="gemini")
        openai_check.assert_not_called()
        self.assertEqual([c["id"] for c in out["checks"]], ["gemini_embed", "gemini_generate"])

    def test_openai_scope_skips_gemini_checks_entirely(self):
        with mock.patch.object(mc, "_check_gemini_embed") as gembed, \
             mock.patch.object(mc, "_check_gemini_generate") as ggen, \
             mock.patch.object(mc, "_check_openai_compat_generate",
                                return_value={"id": "openai_compat_generate", "ok": True}):
            out = mc.check_model_keys(which="openai")
        gembed.assert_not_called()
        ggen.assert_not_called()
        self.assertEqual([c["id"] for c in out["checks"]], ["openai_compat_generate"])

    def test_one_failure_makes_ok_false_even_if_others_skipped(self):
        with mock.patch.object(mc, "_check_gemini_embed",
                                return_value={"id": "gemini_embed", "ok": False}), \
             mock.patch.object(mc, "_check_gemini_generate",
                                return_value={"id": "gemini_generate", "ok": True}), \
             mock.patch.object(mc, "_check_openai_compat_generate",
                                return_value={"id": "openai_compat_generate", "ok": True}):
            out = mc.check_model_keys(which="all")
        self.assertFalse(out["ok"])

    def test_all_skipped_is_still_ok(self):
        with mock.patch.object(mc, "_check_gemini_embed",
                                return_value={"id": "gemini_embed", "ok": True, "detail": "no key stored"}), \
             mock.patch.object(mc, "_check_gemini_generate",
                                return_value={"id": "gemini_generate", "ok": True, "detail": "no key stored"}), \
             mock.patch.object(mc, "_check_openai_compat_generate",
                                return_value={"id": "openai_compat_generate", "ok": True, "detail": "no key stored"}):
            out = mc.check_model_keys(which="all")
        self.assertTrue(out["ok"])

    def test_unknown_which_raises(self):
        with self.assertRaises(ValueError):
            mc.check_model_keys(which="bogus")


class CliSecretsVerifyTest(unittest.TestCase):
    """`khipu secrets verify` — JSON output, exit 0/1/2 per the scope doc."""

    def _run_cmd(self, args_ns):
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_secrets_verify(args_ns)
        return rc, json.loads(out.getvalue())

    def test_exit_0_when_every_configured_provider_passed(self):
        with mock.patch("khipu.modelcheck.check_model_keys",
                         return_value={"ok": True, "checks": []}):
            rc, payload = self._run_cmd(mock.Mock(gemini=False, openai=False, all=False))
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])

    def test_exit_1_when_a_configured_provider_failed(self):
        with mock.patch("khipu.modelcheck.check_model_keys",
                         return_value={"ok": False, "checks": [{"id": "gemini_embed", "ok": False}]}):
            rc, payload = self._run_cmd(mock.Mock(gemini=False, openai=False, all=False))
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])

    def test_gemini_flag_is_routed_as_which(self):
        with mock.patch("khipu.modelcheck.check_model_keys",
                         return_value={"ok": True, "checks": []}) as fn:
            self._run_cmd(mock.Mock(gemini=True, openai=False, all=False))
        fn.assert_called_once_with(which="gemini")

    def test_openai_flag_is_routed_as_which(self):
        with mock.patch("khipu.modelcheck.check_model_keys",
                         return_value={"ok": True, "checks": []}) as fn:
            self._run_cmd(mock.Mock(gemini=False, openai=True, all=False))
        fn.assert_called_once_with(which="openai")

    def test_no_flags_defaults_to_all(self):
        with mock.patch("khipu.modelcheck.check_model_keys",
                         return_value={"ok": True, "checks": []}) as fn:
            self._run_cmd(mock.Mock(gemini=False, openai=False, all=True))
        fn.assert_called_once_with(which="all")

    def test_argparse_refuses_mixing_gemini_and_openai_with_exit_2(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit) as ctx, \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            parser.parse_args(["secrets", "verify", "--gemini", "--openai"])
        self.assertEqual(ctx.exception.code, 2)

    def test_parsed_args_wire_to_cmd_secrets_verify(self):
        parser = cli.build_parser()
        args = parser.parse_args(["secrets", "verify", "--gemini", "--json"])
        self.assertIs(args.func, cli.cmd_secrets_verify)
        self.assertTrue(args.gemini)

    def test_secrets_with_no_subcommand_still_shows_status(self):
        parser = cli.build_parser()
        args = parser.parse_args(["secrets"])
        self.assertIs(args.func, cli.cmd_secrets)


class LiveGeminiVerifyTest(unittest.TestCase):
    """One real call against the actual Gemini API via the CLI, on this Mac."""

    @unittest.skipUnless(KEY_AVAILABLE, "no Gemini key available on this Mac")
    def test_khipu_secrets_verify_gemini_via_subprocess(self):
        proc = subprocess.run(
            ["python3", "-m", "khipu.cli", "secrets", "verify", "--gemini"],
            capture_output=True, text=True, timeout=60,
        )
        payload = json.loads(proc.stdout)
        self.assertIn(proc.returncode, (0, 1))
        ids = [c["id"] for c in payload["checks"]]
        self.assertEqual(ids, ["gemini_embed", "gemini_generate"])
        for check in payload["checks"]:
            self.assertNotIn("AIza", json.dumps(check))


if __name__ == "__main__":
    unittest.main()
