"""Install-default path resolution — no maintainer Cloud Storage defaults."""

from __future__ import annotations

import unittest
from pathlib import Path

from khipu import jobs, sources


class InstallDefaultPathsTest(unittest.TestCase):
    def test_jobs_constants_have_no_cloud_storage_defaults(self):
        for name in (
            "CONSOLIDATE_NIGHTLY",
            "CONSOLIDATE_MONTHLY",
            "BUILD_INDEX",
        ):
            value = getattr(jobs, name)
            if value is not None:
                self.assertNotIn(
                    "/Volumes/Cloud Storage",
                    str(value),
                    msg=f"{name} still embeds maintainer path",
                )

    def test_sources_default_document_is_empty(self):
        doc = sources.default_document()
        self.assertEqual(doc["sources"], [])

    def test_no_cloud_storage_in_install_defaults_except_topic_graph(self):
        """Grep gate: Cloud Storage strings are not install defaults.

        Exempt ``topic_graph.py`` ``VOLUME_ROOT`` — wiki-path peeler, not an
        install default. Do not rename it in the portable-DMG slice.
        """
        root = Path(__file__).resolve().parents[1] / "khipu"
        hits: list[str] = []
        for path in root.rglob("*.py"):
            if path.name == "topic_graph.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "/Volumes/Cloud Storage" in text:
                hits.append(str(path.relative_to(root)))
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
