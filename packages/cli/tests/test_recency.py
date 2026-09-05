"""Tests for khipu.recency — retention by decay in search ranking.

Audit 2026-09-04: "Nothing ages; old rows compete for search slots forever."
Rows are never removed for age; a small exponentially-decaying bonus just
keeps a fresher row from being permanently outranked by an equally-relevant
older one.
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from khipu import recency as rec


class AgeDaysTest(unittest.TestCase):
    def test_naive_datetime_treated_as_utc(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        naive = datetime(2026, 9, 4)
        self.assertAlmostEqual(rec.age_days(naive, now=now), 1.0, places=6)

    def test_aware_datetime(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        aware = datetime(2026, 9, 3, tzinfo=timezone.utc)
        self.assertAlmostEqual(rec.age_days(aware, now=now), 2.0, places=6)

    def test_iso_string_with_trailing_z(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            rec.age_days("2026-09-04T00:00:00Z", now=now), 1.0, places=6
        )

    def test_iso_string_with_offset(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        self.assertAlmostEqual(
            rec.age_days("2026-09-04T00:00:00+00:00", now=now), 1.0, places=6
        )

    def test_none_is_none(self):
        self.assertIsNone(rec.age_days(None))

    def test_garbage_string_is_none(self):
        self.assertIsNone(rec.age_days("not-a-date"))

    def test_unsupported_type_is_none(self):
        self.assertIsNone(rec.age_days(12345))


class ApplyRecencyTest(unittest.TestCase):
    def test_fresh_row_outranks_equal_score_old_row(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        half_life = 30.0
        old_ts = (now - timedelta(days=3 * half_life)).isoformat()
        fresh_ts = now.isoformat()
        rows = [
            {"kind": "episode", "id": "old", "score": 0.5, "ts": old_ts},
            {"kind": "episode", "id": "fresh", "score": 0.5, "ts": fresh_ts},
        ]
        out = rec.apply_recency(rows, now=now, half_life_days=half_life)
        self.assertEqual([r["id"] for r in out], ["fresh", "old"])

        fresh = next(r for r in out if r["id"] == "fresh")
        old = next(r for r in out if r["id"] == "old")
        self.assertAlmostEqual(fresh["recency"], rec.RECENCY_WEIGHT, places=6)
        self.assertAlmostEqual(old["recency"], rec.RECENCY_WEIGHT / 8.0, places=6)
        self.assertAlmostEqual(fresh["score"], 0.5 + rec.RECENCY_WEIGHT, places=6)
        self.assertAlmostEqual(old["score"], 0.5 + rec.RECENCY_WEIGHT / 8.0, places=6)

    def test_half_life_zero_disables_and_leaves_rows_unchanged(self):
        rows = [
            {"kind": "episode", "id": "a", "score": 0.5, "ts": "2026-09-05T00:00:00Z"},
            {"kind": "episode", "id": "b", "score": 0.9, "ts": "2020-01-01T00:00:00Z"},
        ]
        out = rec.apply_recency(rows, half_life_days=0)
        self.assertEqual(out, rows)
        for r in out:
            self.assertNotIn("recency", r)

    def test_row_without_ts_keeps_score_and_relative_tie_order(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        rows = [
            {"kind": "episode", "id": "first", "score": 0.5},
            {"kind": "episode", "id": "second", "score": 0.5},
        ]
        out = rec.apply_recency(rows, now=now, half_life_days=30.0)
        self.assertEqual([r["id"] for r in out], ["first", "second"])
        for r in out:
            self.assertNotIn("recency", r)
            self.assertEqual(r["score"], 0.5)

    def test_garbage_ts_gets_no_bonus_and_does_not_raise(self):
        now = datetime(2026, 9, 5, tzinfo=timezone.utc)
        rows = [{"kind": "episode", "id": "x", "score": 0.7, "ts": "not-a-real-date"}]
        out = rec.apply_recency(rows, now=now, half_life_days=30.0)
        self.assertEqual(out[0]["score"], 0.7)
        self.assertNotIn("recency", out[0])


if __name__ == "__main__":
    unittest.main()
