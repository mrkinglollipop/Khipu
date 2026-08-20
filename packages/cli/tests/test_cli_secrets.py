"""`khipu secrets --set` is the only write path the desktop app has to the
Keychain. It reads the value from stdin because argv is world-readable via `ps`
and a flag would also land in shell history.
"""

from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from khipu import cli


def _run(account, stdin_text, stored=True):
    args = mock.Mock(set=account)
    calls = []

    def fake_set(acct, value):
        calls.append((acct, value))

    status = {"gemini_in_keychain": stored, "dsn_in_keychain": stored}
    with mock.patch("khipu.keychain.set_password", side_effect=fake_set), \
         mock.patch("khipu.keychain.secrets_status", return_value=status), \
         mock.patch("sys.stdin", io.StringIO(stdin_text)), \
         mock.patch("sys.stdout", new_callable=io.StringIO) as out:
        rc = cli.cmd_secrets(args)
    return rc, json.loads(out.getvalue()), calls


class SecretsSetTest(unittest.TestCase):
    def test_the_value_is_read_from_stdin(self):
        rc, payload, calls = _run("gemini_api_key", "AIza-test-key\n")
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(calls, [("gemini_api_key", "AIza-test-key")])

    def test_the_response_never_echoes_the_value(self):
        secret = "AIza-Zq7-do-not-echo"
        _, payload, _ = _run("gemini_api_key", secret + "\n")
        self.assertNotIn(secret, json.dumps(payload))

    def test_an_unknown_account_is_refused_without_writing(self):
        rc, payload, calls = _run("aws_secret_key", "value\n")
        self.assertEqual(rc, 2)
        self.assertFalse(payload["ok"])
        self.assertEqual(calls, [], "refused account must not reach the Keychain")

    def test_openai_compat_api_key_is_allowed(self):
        status = {
            "gemini_in_keychain": False,
            "dsn_in_keychain": False,
            "openai_compat_in_keychain": True,
        }
        args = mock.Mock(set="openai_compat_api_key")
        calls = []

        def fake_set(acct, value):
            calls.append((acct, value))

        with mock.patch("khipu.keychain.set_password", side_effect=fake_set), \
             mock.patch("khipu.keychain.secrets_status", return_value=status), \
             mock.patch("sys.stdin", io.StringIO("sk-local\n")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_secrets(args)
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out.getvalue())["ok"])
        self.assertEqual(calls, [("openai_compat_api_key", "sk-local")])
        self.assertIn("openai_compat_api_key", cli.SETTABLE_SECRETS)
        self.assertEqual(
            cli.SETTABLE_SECRETS["openai_compat_api_key"],
            "openai_compat_in_keychain",
        )

    def test_empty_stdin_is_refused(self):
        for blank in ("", "\n", "   \n"):
            with self.subTest(blank=blank):
                rc, payload, calls = _run("gemini_api_key", blank)
                self.assertEqual(rc, 2)
                self.assertFalse(payload["ok"])
                self.assertEqual(calls, [])

    def test_success_is_reported_from_a_reread_not_from_the_absence_of_an_error(self):
        # set_password succeeding does not prove the item landed; the command
        # re-reads presence and reports that.
        _, payload, _ = _run("gemini_api_key", "k\n", stored=False)
        self.assertFalse(payload["stored"])

    def test_database_url_maps_to_the_dsn_status_field(self):
        # secrets_status() does not name its fields after the Keychain accounts.
        _, payload, _ = _run("database_url", "postgres://u:p@h/db\n")
        self.assertTrue(payload["stored"])


class SecretsStatusTest(unittest.TestCase):
    def test_without_set_it_still_reports_presence_only(self):
        args = mock.Mock(set=None)
        status = {"gemini_in_keychain": True, "dsn_in_keychain": False}
        with mock.patch("khipu.keychain.secrets_status", return_value=status), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rc = cli.cmd_secrets(args)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), status)
