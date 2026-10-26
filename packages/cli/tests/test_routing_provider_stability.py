"""Provider identities stay the same across local upkeep entry points."""

import copy

from khipu import routing, routing_replication, usage
from tests.test_routing_acceptance import replay  # noqa: F401


def test_provider_definitions_do_not_depend_on_the_upkeep_callers_available_clis(replay, monkeypatch):
    # Fictional single-login providers model both a readable provider and
    # owner-defined accounts which have no reader on this machine.
    providers = ("quartz", "copper", "slate")
    monkeypatch.setattr(usage, "HARNESSES", providers)
    monkeypatch.setattr(usage, "NO_FOLDER", providers)
    monkeypatch.setattr(usage, "LABELS", {p: p.title() for p in providers})
    available = set()
    monkeypatch.setattr(usage, "available", lambda provider: provider in available)
    monkeypatch.setattr(routing_replication, "sync", lambda: {})
    replay.config.pop("usage_accounts")
    replay.configure()
    for provider in providers:
        routing.set_definition("provider", {"id": provider, "label": provider.title(),
                               "family": "Synthetic", "probe": "none"}, via="app")
        routing.set_definition("account", {"id": provider + ":main", "label": provider.title(),
                               "provider": provider}, via="app")

    first = replay.view()
    expected = {p + ":main" for p in providers}
    baseline = copy.deepcopy(first["effective"])
    history = copy.deepcopy(first["changes"])
    log = copy.deepcopy(routing.read_events()[0])
    version = first["version"]
    for installed in (providers, (), ("quartz",), ("copper", "slate")):
        available.clear()
        available.update(installed)
        for writer in (lambda: routing.read_view(replay.now),
                       lambda: routing._poll_refold(replay.now)):
            # Exercise a real refold rather than a cache hit.
            replay.advance(2)
            replay.configure()
            writer()
            view = routing._load_view()
            assert {a["key"] for a in routing.accounts()} == expected
            assert {a["key"] for a in routing._inputs()[2]} == expected
            assert {a["key"] for a in view["accounts"]} == expected
            assert view["effective"] == baseline
            assert view["version"] == version
            assert view["changes"] == history
            assert routing.read_events()[0] == log


def test_explicit_default_login_definition_keeps_its_configured_binding(replay):
    replay.configure(usage_accounts=[{"harness": "glm", "label": "Quartz"}])
    routing.set_definition("account", {"id": "glm:main", "label": "Quartz", "provider": "glm"}, via="app")
    assert [a["key"] for a in routing.accounts()] == ["glm|"]
    assert [a["key"] for a in routing._inputs()[2]] == ["glm|"]
