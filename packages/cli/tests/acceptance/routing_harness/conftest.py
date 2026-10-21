import fcntl
import json
from pathlib import Path

import pytest

from .run import Session, Unavailable


@pytest.fixture(scope="session")
def rig(request):
    evidence = request.config.getoption("--routing-evidence")
    cache = request.config._routing_run_cache
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = Path(evidence).resolve() if evidence else cache / "evidence"
    if root.is_relative_to(Path(__file__).resolve().parents[5]):
        raise pytest.UsageError("received-context evidence must stay outside the checkout")
    with (cache / "evidence.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registered = cache / "evidence.json"
        if registered.exists():
            if json.loads(registered.read_text()) != str(root):
                raise pytest.UsageError("one evidence directory is allowed per pytest invocation chain")
        else:
            if evidence and root.exists():
                raise pytest.UsageError("received-context evidence must be a fresh directory")
            root.mkdir(parents=True, exist_ok=not evidence, mode=0o700)
            registered.write_text(json.dumps(str(root)))
    auth = request.config.getoption("--routing-auth-root")
    replay = request.config.getoption("--routing-replay-evidence")
    adapters = {name: request.config.getoption(f"--routing-{name}-adapter") for name in ("t3", "grok", "aegis")}
    return Session(root, Path(auth).resolve() if auth else None, adapters,
                   Path(replay).resolve() if replay else None,
                   cache_dir=request.config._routing_run_cache)


@pytest.fixture
def received(rig):
    def get(name):
        try:
            return rig.get(name)
        except Unavailable as exc:
            pytest.skip(str(exc))
    return get
