from pathlib import Path

import pytest

from .run import Session, Unavailable


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


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-routing-harness"):
        skip = pytest.mark.skip(reason="received context is opt-in: --run-routing-harness (costs tokens)")
        for item in items:
            if "routing_received" in item.keywords:
                item.add_marker(skip)


@pytest.fixture(scope="session")
def rig(request, tmp_path_factory):
    evidence = request.config.getoption("--routing-evidence")
    root = Path(evidence).resolve() if evidence else tmp_path_factory.mktemp("routing-received")
    if root.is_relative_to(Path(__file__).resolve().parents[5]):
        raise pytest.UsageError("received-context evidence must stay outside the checkout")
    if evidence and root.exists():
        raise pytest.UsageError("received-context evidence must be a fresh directory")
    root.mkdir(parents=True, exist_ok=not evidence, mode=0o700)
    auth = request.config.getoption("--routing-auth-root")
    replay = request.config.getoption("--routing-replay-evidence")
    adapters = {name: request.config.getoption(f"--routing-{name}-adapter") for name in ("t3", "grok", "aegis")}
    return Session(root, Path(auth).resolve() if auth else None, adapters,
                   Path(replay).resolve() if replay else None)


@pytest.fixture
def received(rig):
    def get(name):
        try:
            return rig.get(name)
        except Unavailable as exc:
            pytest.skip(str(exc))
    return get
