"""Shared pytest fixtures for the khipu test suite.

Hermetic by default (Phase 0 session A; B1 in
docs/research/hindsight-plan-review-2026-09-28.md). On a machine that has
ever installed a Khipu pack, the real HOME carries live state: a resolvable
Postgres DSN (env, Keychain or ~/.config/khipu/dsn), a Keychain-stored Gemini
key, and the ~/.config/khipu/bin launcher symlinks every real harness session
currently runs its hooks through. `khipu.integrations._shim()` used to
re-point those symlinks on every call, including from read-only paths
(status/verify/doctor), and `khipu.db.resolve_dsn()` / `khipu.keychain` walk
straight to the configured hub and Keychain whenever nothing has cleared the
environment first. A suite that resolves any of that by accident is not
testing khipu, it is operating on it: running this suite un-hermetically once
re-pointed three live hook launchers at the checkout under test and let ~45
tests reach the production hub and embedding API.

So, before anything in THIS PROCESS imports `khipu` — the block below runs as
module-level code, which pytest executes while collecting this conftest,
ahead of any test module's own `from khipu import ...` — HOME is redirected
at a throwaway temporary directory (removed at process exit) and every
credential/identity environment variable is stripped. `Path.home()` and the
several `HOME`-derived module-level constants (`khipu.integrations.HOME`,
`khipu.paths.DEFAULT_DIR`, `khipu.db.DEFAULT_DSN_FILE`, ...) are only ever
evaluated at import time, so patching the environment first is what makes
this take effect for every one of them without patching each module
individually. `KHIPU_KEYCHAIN=0` additionally forces `khipu.keychain` to
report the Keychain unavailable rather than shelling out to `security(1)`.

Set `KHIPU_LIVE_TESTS=1` to opt out of all of this and run against your real,
configured environment exactly as found — the deliberate way to run the
tests that write and delete their own probe rows on the configured hub and
call the real embedding API. Individual tests that need to simulate a
DIFFERENT home (their own temp dirs, per test) still patch things locally on
top of this default, same as before — nothing here forecloses that.

Unrelated to the above: ``khipu.db.table_columns`` (fix 13 consolidation)
keeps a per-process cache keyed by table name so repeated schema checks
(embed/drift/hub_snapshot) share one information_schema round trip. That is
correct in a real process but poisons test isolation: two test methods that
fake different schema shapes for the same table (e.g. "pre-migration
episodes" vs "post-migration episodes") would otherwise see whichever one
ran first. Clear it before every test so each test's fake cursor is the sole
source of truth.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest

if os.environ.get("KHIPU_LIVE_TESTS") != "1":
    _HERMETIC_HOME = tempfile.mkdtemp(prefix="khipu-hermetic-home-")
    atexit.register(shutil.rmtree, _HERMETIC_HOME, ignore_errors=True)
    os.environ["HOME"] = _HERMETIC_HOME
    os.environ["KHIPU_KEYCHAIN"] = "0"
    _KEEP = {"KHIPU_KEYCHAIN", "KHIPU_LIVE_TESTS", "KHIPU_SCRATCH_DSN"}
    for _name in [n for n in os.environ if n.startswith(("KHIPU_", "ALZY_")) and n not in _KEEP]:
        del os.environ[_name]
    # CLAUDE_CONFIG_DIR: Khipu now installs into that home too (khipu.claude_homes),
    # and a suite launched from a Claude session on a second account inherits the
    # real one. Left set, any install test would write into a live Claude home.
    for _name in ("GEMINI_API_KEY", "VOYAGE_API_KEY", "GROK_HOOK_NAME", "GROK_HOOK_EVENT",
                  "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CONFIG_DIR"):
        os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def _reset_table_columns_cache():
    from khipu import db

    db._TABLE_COLUMNS_CACHE.clear()
    yield
    db._TABLE_COLUMNS_CACHE.clear()


@pytest.fixture(autouse=True)
def _reset_project_for_cwd_cache():
    # Same reason as above: a per-process cache that is right in a one-shot
    # hook and wrong between tests that fake different projects for one path.
    from khipu import recall_prompt

    recall_prompt._PROJECT_FOR_CWD.clear()
    yield
    recall_prompt._PROJECT_FOR_CWD.clear()


@pytest.fixture(autouse=True)
def _reset_process_level_caches():
    # The recall service keeps the replica's matrix, the API key and the embed
    # transport for the life of its process. A test process is one long process
    # running many fake worlds, so none of it may carry across tests.
    from khipu import embed, hub_snapshot, profiles

    def _clear():
        hub_snapshot._MATRIX_CACHE.clear()
        embed._KEY_CACHE.clear()
        embed._VOYAGE_KEY_CACHE.clear()
        embed._OPENAI_KEY_CACHE.clear()
        profiles.clear_learned()
        embed.set_transport(None)

    _clear()
    yield
    _clear()
