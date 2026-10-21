"""Routing slice 9b fix round 1: provider-set account refs (P3-2).

Copied from the 9b review probe: an owner-written provider fact for a fake
provider must not be able to claim another provider's account (claude:main,
glm:main), while the case-9 synthetic http-quota provider keeps its own
account (synthetic-provider claiming synthetic:main).
"""

from __future__ import annotations

from khipu import routing, routing_entries


def _event(kind, **fields):
    return {"type": "provider_set", "written_by": {"kind": kind}, **fields}


_EVIL = dict(
    id="evil",
    label="Evil",
    family="x",
    probe="http-quota",
    host="evil.test",
    accounts=["glm:main", "claude:main"],
)


def test_non_owner_provider_fact_binds_no_accounts():
    for kind in ("conversation", "runner"):
        out = routing_entries.definitions([_event(kind, **_EVIL)], None)
        assert out["accounts"] == {}


def test_fake_http_quota_provider_cannot_claim_builtin_accounts():
    out = routing_entries.definitions([_event("owner", **_EVIL)], None)
    assert out["accounts"] == {}


def test_fake_provider_cannot_steal_glm_account_in_either_order():
    glm = _event(
        "owner", id="glm", label="G", family="x", probe="glm", accounts=["glm:main"]
    )
    for events in ([glm, _event("owner", **_EVIL)], [_event("owner", **_EVIL), glm]):
        out = routing_entries.definitions(events, None)
        assert out["accounts"].get("glm:main", {}).get("provider") == "glm"


def test_http_quota_provider_keeps_its_case9_synthetic_account():
    event = _event(
        "owner",
        id="synthetic-provider",
        label="Synthetic",
        family="Synthetic",
        probe="http-quota",
        host="quota.invalid",
        accounts=["synthetic:main"],
    )
    out = routing_entries.definitions([event], None)
    assert (
        out["accounts"].get("synthetic:main", {}).get("provider")
        == "synthetic-provider"
    )


def test_provider_claiming_its_own_prefix_still_binds():
    out = routing_entries.definitions(
        [
            _event(
                "owner",
                id="deepseek",
                label="D",
                family="DeepSeek",
                probe="none",
                accounts=["deepseek:main"],
            )
        ],
        None,
    )
    assert out["accounts"].get("deepseek:main", {}).get("provider") == "deepseek"


def test_checked_event_rejects_a_fake_providers_claim_on_glm():
    data = {
        "type": "provider_set",
        "written_at": "2026-10-20T00:00:00Z",
        "written_by": {"kind": "owner"},
        **_EVIL,
    }
    assert routing._checked(data) is None
    good = {
        "type": "provider_set",
        "written_at": "2026-10-20T00:00:00Z",
        "written_by": {"kind": "owner"},
        "id": "synthetic-provider",
        "label": "Synthetic",
        "family": "Synthetic",
        "probe": "http-quota",
        "host": "quota.invalid",
        "accounts": ["synthetic:main"],
    }
    assert routing._checked(good) is not None
