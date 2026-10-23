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
from pathlib import Path

import pytest

# Every temporary folder the run makes (tempfile.mkdtemp in a test, pytest's
# tmp_path, a subprocess's own) lands under one root that is removed at exit;
# without it each run left its folders in the system temp folder for good
# (about 112,000 were found). The root sits directly in the system temp folder
# and its name is short: macOS caps a unix socket path at 104 bytes and
# several tests bind sockets under temporary folders.
TEMP_ROOT = tempfile.mkdtemp(prefix="kt-")
atexit.register(shutil.rmtree, TEMP_ROOT, ignore_errors=True)
os.environ["TMPDIR"] = TEMP_ROOT
tempfile.tempdir = TEMP_ROOT

if os.environ.get("KHIPU_LIVE_TESTS") != "1":
    _HERMETIC_HOME = tempfile.mkdtemp(prefix="khipu-hermetic-home-")
    atexit.register(shutil.rmtree, _HERMETIC_HOME, ignore_errors=True)
    os.environ["HOME"] = _HERMETIC_HOME
    os.environ["KHIPU_KEYCHAIN"] = "0"
    _KEEP = {"KHIPU_KEYCHAIN", "KHIPU_LIVE_TESTS"}
    for _name in [n for n in os.environ if n.startswith(("KHIPU_", "ALZY_")) and n not in _KEEP]:
        del os.environ[_name]
    # CLAUDE_CONFIG_DIR: Khipu now installs into that home too (khipu.claude_homes),
    # and a suite launched from a Claude session on a second account inherits the
    # real one. Left set, any install test would write into a live Claude home.
    for _name in ("GEMINI_API_KEY", "VOYAGE_API_KEY", "GROK_HOOK_NAME", "GROK_HOOK_EVENT",
                  "CLAUDE_CODE_HOST_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_CONFIG_DIR",
                  "CLAUDE_CODE_SESSION_ID", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID"):
        os.environ.pop(_name, None)
    for _name in ("CODEX_HOME", "CODEX_SANDBOX", "CLAUDE_PROJECT_DIR"):
        os.environ.pop(_name, None)
    for _name in [n for n in os.environ if n.startswith("GIT_")]:
        os.environ.pop(_name, None)
    os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                      GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_COUNT="1",
                      GIT_CONFIG_KEY_0="core.hooksPath", GIT_CONFIG_VALUE_0=os.devnull,
                      GIT_TEMPLATE_DIR=os.devnull)
    import psycopg

    def _forbid_pg_connect(*args, **kwargs):
        raise RuntimeError("real PostgreSQL connections require KHIPU_LIVE_TESTS=1")

    psycopg.connect = _forbid_pg_connect
    # launchctl acts on this Mac's live jobs (bootstrap/bootout reload them),
    # and a test that forgets to stub it reaches them. Shadow it on PATH for
    # the whole run: it answers like an unknown service. Tests that need a
    # result patch the call as before.
    _SHIM_DIR = os.path.join(_HERMETIC_HOME, ".khipu-test-bin")
    os.makedirs(_SHIM_DIR)
    _shim = os.path.join(_SHIM_DIR, "launchctl")
    with open(_shim, "w") as _fh:
        _fh.write("#!/bin/sh\necho 'Could not find service (khipu test shim)' >&2\nexit 113\n")
    os.chmod(_shim, 0o755)
    # The owed close rules ask GitHub through `gh pr view` (on by default).
    # No test may reach the real GitHub: this gh fails every call, and a test
    # that needs an answer puts its own fake gh first on PATH.
    _gh = os.path.join(_SHIM_DIR, "gh")
    with open(_gh, "w") as _fh:
        _fh.write("#!/bin/sh\necho 'gh is a khipu test shim' >&2\nexit 1\n")
    os.chmod(_gh, 0o755)
    # Named explicitly too: gh is also looked for in the install folders,
    # where this Mac's real one may be.
    os.environ["KHIPU_GH_CLI"] = _gh
    os.environ["PATH"] = _SHIM_DIR + os.pathsep + os.environ.get("PATH", "")


@pytest.fixture(autouse=True)
def _isolate_routing_inventory(monkeypatch):
    """Each hermetic runtime needs its own process-local helper inventory."""
    from khipu import agent_view

    monkeypatch.setattr(agent_view, "_TABLE", {"sig": None, "at": 0.0, "table": None})


@pytest.fixture(autouse=True)
def _reset_table_columns_cache():
    from khipu import db, provenance

    # provenance.ready caches a yes for the life of the process; each test
    # brings its own database, at its own migration.
    db._TABLE_COLUMNS_CACHE.clear()
    provenance._reset_ready_cache()
    yield
    db._TABLE_COLUMNS_CACHE.clear()
    provenance._reset_ready_cache()


@pytest.fixture(autouse=True)
def _isolate_tombstone_registry(tmp_path, monkeypatch):
    """A forgotten source is intentionally process-durable in production.

    Tests reuse fixture source identities, so each test receives its own
    state-root registry instead of allowing one test's completed forget to
    change a later test's setup.
    """
    from khipu import tombstones
    from khipu.paths import isolated_dir

    def test_registry_path():
        isolated = isolated_dir()
        if isolated is not None:
            return isolated / "forgotten_sources.json"
        # A test that points the data dir at another of its own folders (a
        # second Mac's state) gets that folder's registry, as production does.
        override = os.environ.get("KHIPU_DATA_DIR")
        if override and Path(override).is_relative_to(tmp_path):
            return Path(override) / "forgotten_sources.json"
        return tmp_path / "state" / "forgotten_sources.json"

    monkeypatch.setattr(tombstones, "registry_path", test_registry_path)


@pytest.fixture(autouse=True)
def _forget_hub_unreachable_memo():
    # A test that lets a hub connect fail writes the reachability memo into the
    # shared test data dir; the next test's hub leg would then be skipped.
    from khipu import hub_reach

    hub_reach.memo_path().unlink(missing_ok=True)
    yield
    hub_reach.memo_path().unlink(missing_ok=True)


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
    from khipu import embed, graph_candidates, hub_snapshot, profiles

    def _clear():
        hub_snapshot._MATRIX_CACHE.clear()
        hub_snapshot._EDGE_INDEXES_TRIED.clear()
        graph_candidates._SLUG_CACHE.clear()
        embed._KEY_CACHE.clear()
        embed._VOYAGE_KEY_CACHE.clear()
        embed._OPENAI_KEY_CACHE.clear()
        profiles.clear_learned()
        embed.set_transport(None)

    _clear()
    yield
    _clear()


# The harness that "ran the prompt hook" for turns tests record in this
# process. The real issuer here would be pytest's own Python, which the
# owner-direction check refuses; tests about the issuer pass their own.
FAKE_HOOK_ISSUER = {"pid": 4_000_000, "start": 1.0, "exe": "claude"}


@pytest.fixture(autouse=True)
def _owner_turns_from_a_fake_harness(monkeypatch):
    """In-process ``owner_turns.record`` calls get ``FAKE_HOOK_ISSUER``, and
    the check's caller chain has it as the nearest harness above this
    process. A shim run in a
    subprocess still computes its real issuer."""
    from khipu import owner_direction, owner_turns

    real_record, real_chain = owner_turns.record, owner_direction._caller_chain

    def record(payload, env=None, now=None, issuer=None):
        return real_record(payload, env, now, dict(FAKE_HOOK_ISSUER) if issuer is None else issuer)

    monkeypatch.setattr(owner_turns, "record", record)
    monkeypatch.setattr(owner_direction, "_caller_chain",
                        lambda: [(FAKE_HOOK_ISSUER["pid"], FAKE_HOOK_ISSUER["start"], FAKE_HOOK_ISSUER["exe"])]
                        + real_chain())


class FakeCallers:
    """The process tree the routing owner check walks (``routing._caller``),
    faked through its seams: this process's parent is an installed Khipu.app
    by default, or a shell after ``as_shell()``."""

    def __init__(self, root, monkeypatch):
        import plistlib

        from khipu import routing

        self.bundle = root / "Applications" / "Khipu.app"
        (self.bundle / "Contents" / "MacOS").mkdir(parents=True)
        self.app = self.bundle / "Contents" / "MacOS" / "khipu-desktop"
        self.app.write_text("")
        with open(self.bundle / "Contents" / "Info.plist", "wb") as fh:
            plistlib.dump({"CFBundleIdentifier": routing.APP_BUNDLE_ID, "CFBundleExecutable": "khipu-desktop"}, fh)
        self.tree = {}
        self.as_app()
        monkeypatch.setattr(routing, "_app_bundles", lambda: [self.bundle])
        monkeypatch.setattr(routing, "_proc_info", lambda pid: self.tree.get(pid))

    def as_app(self):
        """Started by the app itself, as the app's Rust code does."""
        self.tree = {os.getpid(): (900, "/usr/bin/python3"), 900: (1, str(self.app))}

    def as_shell(self, *between):
        """Started from a shell (a conversation's command), with ``between``
        executables (each one a parent of the last) below the shell."""
        tree = {os.getpid(): (900, "/usr/bin/python3")}
        pid = 900
        for exe in between:
            tree[pid] = (pid + 1, exe)
            pid += 1
        tree[pid] = (1, "/bin/zsh")
        self.tree = tree


@pytest.fixture
def khipu_app(tmp_path, monkeypatch):
    return FakeCallers(tmp_path / "apps", monkeypatch)


def pytest_addoption(parser):
    group = parser.getgroup("routing received context")
    group.addoption("--run-routing-harness", action="store_true", help="Opt in to bounded subscription model runs")
    group.addoption("--routing-auth-root", help="Credential-only input: codex/auth.json, claude/.credentials.json")
    group.addoption("--routing-evidence", help="Private evidence directory (outside the checkout)")
    group.addoption("--routing-replay-evidence", help="Read a saved native run without spending model tokens")
    for harness in ("t3", "grok", "aegis"):
        group.addoption(f"--routing-{harness}-adapter", help="Reviewed local native-client headless driver")


def pytest_configure(config):
    config.addinivalue_line("markers", "routing_received: opt-in headless received-context acceptance (costs tokens)")
    # The controller creates this once; xdist workers and nested pytest
    # invocations inherit it even though their own TMPDIR changes above.
    shared = os.environ.get("ROUTING_RIG_SESSION_CACHE")
    if not shared:
        shared = str(Path(TEMP_ROOT) / "routing-run-cache")
        os.environ["ROUTING_RIG_SESSION_CACHE"] = shared
    config._routing_run_cache = Path(shared)


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-routing-harness"):
        skip = pytest.mark.skip(reason="received context is opt-in: --run-routing-harness (costs tokens)")
        for item in items:
            if "routing_received" in item.keywords:
                item.add_marker(skip)
