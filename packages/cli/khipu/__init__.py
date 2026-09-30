"""Khipu CLI — memory hub read path + fail-open mirror helpers."""

# The single source for every version string Khipu itself reports
# (mcp_server.SERVER_VERSION derives from it) — kept equal to the released
# desktop app version in apps/desktop/src-tauri/tauri.conf.json and
# apps/desktop/package.json; tests/test_features.py fails the three apart.
__version__ = "0.4.7"
