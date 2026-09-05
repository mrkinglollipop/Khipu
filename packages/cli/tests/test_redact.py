"""khipu.redact — secrets never reach the summariser or the hub."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from khipu import redact


class RedactSecretsTest(unittest.TestCase):
    def _one(self, text: str) -> str:
        out, n = redact.redact_secrets(text)
        self.assertGreaterEqual(n, 1, f"expected a redaction in {text!r}")
        return out

    def test_vendor_keys(self):
        cases = {
            "export OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789": "sk-proj-abc",
            "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij": "sk-ant-",
            "aws AKIAIOSFODNN7EXAMPLE and more": "AKIAIOSFODNN7",
            "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd pushed": "ghp_ABC",
            "slack xoxb-1234567890-abcdefghij": "xoxb-",
            "google AIzaSyA-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456": "AIzaSy",
            "stripe sk_live_ABCDEFGHIJKLMNOPQRST": "sk_live_",
        }
        for text, must_vanish in cases.items():
            out = self._one(text)
            self.assertNotIn(must_vanish, out, out)
            self.assertIn(redact.MASK, out)

    def test_private_key_block_and_jwt(self):
        pem = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\nlines\n-----END RSA PRIVATE KEY-----\nafter"
        out = self._one(pem)
        self.assertEqual(out, "before\n[REDACTED private key]\nafter")
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        self.assertNotIn("SflKxw", self._one(f"token {jwt}"))

    def test_headers_and_url_credentials(self):
        out = self._one("curl -H 'Authorization: Bearer abcdefghijklmnopqrstuvwxyz' https://x")
        self.assertIn("Bearer [REDACTED]", out)
        self.assertNotIn("abcdefghijklmnop", out)
        out = self._one("postgres://khipu:S3cretPass@db.example.com:5432/khipu?sslmode=require")
        self.assertEqual(out, "postgres://khipu:[REDACTED]@db.example.com:5432/khipu?sslmode=require")

    def test_assignments_keep_the_name_and_drop_the_value(self):
        out = self._one('password = "hunter2hunter2"')
        self.assertEqual(out, 'password = "[REDACTED]"')
        out = self._one("GEMINI_API_KEY: AbCdEfGhIjKlMnOp")
        self.assertIn("GEMINI_API_KEY: [REDACTED]", out)

    def test_ordinary_memory_content_is_untouched(self):
        keep = [
            "merged at cb66df5 and 92f2d4c2f1a0b3c4d5e6f7a8b9c0d1e2f3a4b5c6",
            "the token budget is tight; the password reset flow is broken",
            "see https://github.com/mrkinglollipop/Khipu/pull/66 for the diff",
            "episode 11617 · session claude_code:f916790e-fcaf-46f0-b702-c380d60b5d11",
            "API key lives in the Keychain under khipu-gemini",
        ]
        for text in keep:
            out, n = redact.redact_secrets(text)
            self.assertEqual(n, 0, f"false positive on {text!r}: {out!r}")
            self.assertEqual(out, text)

    def test_idempotent_and_off_switch(self):
        text = "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
        once, n1 = redact.redact_secrets(text)
        twice, n2 = redact.redact_secrets(once)
        self.assertEqual(n1, 1)
        self.assertEqual(n2, 0)
        self.assertEqual(once, twice)
        with mock.patch.dict(os.environ, {"KHIPU_REDACT_SECRETS": "0"}):
            self.assertEqual(redact.redact_secrets(text), (text, 0))

    def test_payload_fields(self):
        payload = {
            "summary": "Set GEMINI_API_KEY=AIzaSyA-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456 in the plist.",
            "scope": "ok",
            "decisions": ["use token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd for pushes"],
            "preferences": [],
            "open_loops": [{"kind": "followup", "text": "rotate xoxb-1234567890-abcdefghij"}],
            "topic_pages": [{"slug": "a", "body": "dsn=postgres://u:pw12345678@h/db"}],
        }
        n = redact.redact_payload(payload)
        self.assertGreaterEqual(n, 4)
        self.assertNotIn("AIzaSy", payload["summary"])
        self.assertNotIn("ghp_", payload["decisions"][0])
        self.assertNotIn("xoxb-", payload["open_loops"][0]["text"])
        self.assertNotIn("pw12345678", payload["topic_pages"][0]["body"])
        self.assertEqual(payload["scope"], "ok")


if __name__ == "__main__":
    unittest.main()
