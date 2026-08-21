"""PostgreSQL 19 floor gate."""

from __future__ import annotations

import unittest

from khipu.components_postgres import check_server_version_num


class PgVersionGateTest(unittest.TestCase):
    def test_pg18_refused(self):
        out = check_server_version_num(180_006)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "postgres_version_too_old")

    def test_pg19beta3_accepted(self):
        out = check_server_version_num(190_003)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(out["server_version_num"], 190_000)


if __name__ == "__main__":
    unittest.main()
