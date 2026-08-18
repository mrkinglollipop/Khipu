"""khipu.keychain fronts macOS `security(1)`. Untested until the 2026-08-17
audit, which found secrets_status() resolving the DSN twice per doctor run and
reporting a hardcoded config dir that ignored a relocated data dir.

`security` is always faked here — no test touches the real Keychain.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import keychain as kc


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=["security"], returncode=rc, stdout=out, stderr=err)


class KeychainAvailabilityTest(unittest.TestCase):
    def test_env_can_disable_the_keychain(self):
        for off in ("0", "false", "OFF", " off "):
            with self.subTest(off=off), mock.patch.dict(os.environ, {"KHIPU_KEYCHAIN": off}):
                self.assertFalse(kc.keychain_available())

    def test_disabled_keychain_reads_none_and_writes_raise(self):
        with mock.patch.dict(os.environ, {"KHIPU_KEYCHAIN": "0"}):
            self.assertIsNone(kc.get_password("database_url"))
            with self.assertRaises(RuntimeError):
                kc.set_password("database_url", "postgres://x")


class GetSetTest(unittest.TestCase):
    def setUp(self):
        self._avail = mock.patch.object(kc, "keychain_available", return_value=True)
        self._avail.start()

    def tearDown(self):
        self._avail.stop()

    def test_get_password_returns_the_trimmed_value(self):
        with mock.patch.object(kc, "_security", return_value=_cp(0, "secret-value\n")) as m:
            self.assertEqual(kc.get_password("acct"), "secret-value")
        m.assert_called_once_with("find-generic-password", "-s", kc.SERVICE, "-a", "acct", "-w")

    def test_missing_entry_is_none_not_an_error(self):
        with mock.patch.object(kc, "_security", return_value=_cp(44, "", "not found")):
            self.assertIsNone(kc.get_password("acct"))

    def test_empty_stored_value_is_none(self):
        with mock.patch.object(kc, "_security", return_value=_cp(0, "   \n")):
            self.assertIsNone(kc.get_password("acct"))

    def test_set_password_deletes_then_adds(self):
        with mock.patch.object(kc, "_security", return_value=_cp(0)) as m:
            kc.set_password("acct", "v")
        self.assertEqual(m.call_args_list[0].args[0], "delete-generic-password")
        self.assertEqual(m.call_args_list[1].args[0], "add-generic-password")

    def test_failed_add_raises_with_the_tool_message(self):
        with mock.patch.object(kc, "_security", side_effect=[_cp(0), _cp(1, "", "boom")]):
            with self.assertRaises(RuntimeError) as e:
                kc.set_password("acct", "v")
        self.assertIn("boom", str(e.exception))

    def test_legacy_alzy_value_is_promoted_to_the_khipu_service(self):
        calls = []

        def fake(*args, **_kw):
            calls.append(args)
            if args[0] == "find-generic-password":
                service = args[args.index("-s") + 1]
                return _cp(0, "legacy-dsn") if service == kc.LEGACY_SERVICE else _cp(1)
            return _cp(0)

        with mock.patch.object(kc, "_security", side_effect=fake):
            self.assertEqual(kc.get_dsn(), "legacy-dsn")
        self.assertTrue(any(a[0] == "add-generic-password" for a in calls))

    def test_a_failed_promotion_still_returns_the_legacy_value(self):
        def fake(*args, **_kw):
            if args[0] == "find-generic-password":
                return _cp(0, "legacy") if args[args.index("-s") + 1] == kc.LEGACY_SERVICE else _cp(1)
            return _cp(1, "", "denied")          # every write fails

        with mock.patch.object(kc, "_security", side_effect=fake):
            self.assertEqual(kc.get_dsn(), "legacy")


class ResolveGeminiKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_env_wins_over_everything(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "from-env"}):
            self.assertEqual(kc.resolve_gemini_key(), "from-env")

    def test_keychain_is_next(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), \
             mock.patch.object(kc, "get_gemini_key", return_value="from-kc"):
            self.assertEqual(kc.resolve_gemini_key(), "from-kc")

    def test_file_is_last(self):
        f = self.root / "key.txt"
        f.write_text("from-file\n")
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), \
             mock.patch.object(kc, "get_gemini_key", return_value=None):
            self.assertEqual(kc.resolve_gemini_key(key_file=f), "from-file")

    def test_nothing_anywhere_raises_without_leaking_a_value(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}), \
             mock.patch.object(kc, "get_gemini_key", return_value=None):
            with self.assertRaises(RuntimeError) as e:
                kc.resolve_gemini_key(key_file=self.root / "absent.txt")
        self.assertIn("No Gemini key", str(e.exception))


class SecretsStatusTest(unittest.TestCase):
    """Regressions from the 2026-08-17 audit."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "relocated"
        self.data.mkdir()
        self._env = mock.patch.dict(
            os.environ,
            {"KHIPU_DATA_DIR": str(self.data), "GEMINI_API_KEY": "",
             "KHIPU_DATABASE_URL": "", "ALZY_DATABASE_URL": "",
             "KHIPU_GEMINI_KEY_FILE": str(self.root / "no-key.txt")},
        )
        self._env.start()
        self._cfg = mock.patch.object(kc, "CONFIG_DIR", self.root / "default-config")
        self._cfg.start()
        self._legacy = mock.patch.object(kc, "LEGACY_CONFIG_DIR", self.root / "legacy-config")
        self._legacy.start()

    def tearDown(self):
        self._legacy.stop()
        self._cfg.stop()
        self._env.stop()
        self.tmp.cleanup()

    def test_dsn_resolved_once_not_twice(self):
        """It ran get_dsn() twice — two `security` subprocesses — every doctor."""
        with mock.patch.object(kc, "get_dsn", return_value="postgres://x") as g, \
             mock.patch.object(kc, "get_gemini_key", return_value=None), \
             mock.patch.object(kc, "keychain_available", return_value=True):
            st = kc.secrets_status()
        self.assertEqual(g.call_count, 1)
        self.assertTrue(st["dsn"] and st["dsn_in_keychain"])

    def test_a_dsn_in_a_relocated_data_dir_is_found(self):
        """It only ever looked in the hardcoded ~/.config/khipu, so a relocated
        data dir reported 'no dsn' with the file sitting right there."""
        (self.data / "dsn").write_text("postgres://x")
        with mock.patch.object(kc, "get_dsn", return_value=None), \
             mock.patch.object(kc, "get_gemini_key", return_value=None), \
             mock.patch.object(kc, "keychain_available", return_value=True):
            st = kc.secrets_status()
        self.assertTrue(st["dsn"])
        self.assertFalse(st["dsn_in_keychain"])
        self.assertEqual(st["config_dir"], str(self.data))

    def test_status_never_returns_secret_material(self):
        (self.data / "dsn").write_text("postgres://user:hunter2@host/db")
        with mock.patch.object(kc, "get_dsn", return_value="postgres://user:hunter2@host/db"), \
             mock.patch.object(kc, "get_gemini_key", return_value="AIzaSyREALKEY"), \
             mock.patch.object(kc, "keychain_available", return_value=True):
            blob = repr(kc.secrets_status())
        self.assertNotIn("hunter2", blob)
        self.assertNotIn("AIzaSy", blob)


SECRET_PW = "Zq7-never-echo-this-password-Zq7"


class DsnFileHealthTest(unittest.TestCase):
    """The file DSN is the fallback every sandboxed harness lands on. On
    2026-08-18 it named ~/.config/alzy/root.crt — removed in the 2026-08-04
    rename — while the Keychain named the live path, so Aegis could not reach
    Postgres for two days and every check stayed green, because every check ran
    where the Keychain was reachable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cert = self.root / "root.crt"
        self.cert.write_text("-----BEGIN CERTIFICATE-----\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _dsn(self, cert: Path, mode: str = "verify-full") -> Path:
        import urllib.parse
        f = self.root / "dsn"
        f.write_text(
            f"postgresql://u:{SECRET_PW}@h:5433/db?sslmode={mode}"
            f"&sslrootcert={urllib.parse.quote(str(cert), safe='')}\n"
        )
        return f

    def test_a_live_cert_is_ok(self):
        out = kc.dsn_file_health(self._dsn(self.cert))
        self.assertTrue(out["ok"])
        self.assertTrue(out["sslrootcert_exists"])

    def test_a_cert_that_does_not_exist_is_red_and_names_the_path(self):
        missing = self.root / "gone" / "root.crt"
        out = kc.dsn_file_health(self._dsn(missing))
        self.assertFalse(out["ok"])
        self.assertIn(str(missing), " ".join(out["reasons"]))

    def test_verify_full_without_a_cert_is_red(self):
        f = self.root / "dsn"
        f.write_text("postgresql://u:pw@h:5433/db?sslmode=verify-full\n")
        out = kc.dsn_file_health(f)
        self.assertFalse(out["ok"])

    def test_a_missing_dsn_file_is_not_a_failure(self):
        out = kc.dsn_file_health(self.root / "absent")
        self.assertTrue(out["ok"])
        self.assertFalse(out["present"])

    def test_it_never_returns_the_password(self):
        # The sentinel has to be long and distinctive: a two-character "pw"
        # matched a random tempdir name (tmpetrg2pwr) and failed at random.
        blob = repr(kc.dsn_file_health(self._dsn(self.cert)))
        self.assertNotIn(SECRET_PW, blob)
        self.assertNotIn("postgresql://", blob)


if __name__ == "__main__":
    unittest.main()


class SecretTransportTest(unittest.TestCase):
    """A secret passed as an argument is readable by any local process through
    `ps` for the lifetime of the call. These pin the stdin transport so a future
    refactor cannot quietly put it back on argv.
    """

    SECRET = "Zq7-never-echo-this-key-Zq7"

    def setUp(self):
        self._avail = mock.patch.object(kc, "keychain_available", return_value=True)
        self._avail.start()
        self.addCleanup(self._avail.stop)

    def test_the_secret_never_appears_in_argv(self):
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw.get("input")))
            return _cp()

        with mock.patch.object(kc.subprocess, "run", side_effect=fake_run):
            kc.set_password("gemini_api_key", self.SECRET)

        self.assertTrue(calls, "security was never invoked")
        for argv, _ in calls:
            for arg in argv:
                self.assertNotIn(
                    self.SECRET, arg, f"secret leaked into argv: {argv!r}"
                )

    def test_the_secret_is_delivered_on_stdin_twice(self):
        # security(1) prompts for the password and then a confirmation; one copy
        # would leave the process waiting on a second read.
        seen = []

        def fake_run(argv, **kw):
            seen.append((argv, kw.get("input")))
            return _cp()

        with mock.patch.object(kc.subprocess, "run", side_effect=fake_run):
            kc.set_password("gemini_api_key", self.SECRET)

        add = [(a, i) for a, i in seen if "add-generic-password" in a]
        self.assertEqual(len(add), 1)
        argv, payload = add[0]
        self.assertEqual(payload, f"{self.SECRET}\n{self.SECRET}\n")
        self.assertIn("-w", argv)
        self.assertEqual(argv[-1], "-w", "-w must carry no value")

    def test_a_newline_in_the_secret_is_refused(self):
        # The stdin protocol is newline-delimited, so an embedded newline would
        # store a silently truncated secret.
        with mock.patch.object(kc.subprocess, "run", side_effect=AssertionError):
            for bad in ("abc\ndef", "abc\rdef"):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    kc.set_password("gemini_api_key", bad)
