"""Slice 11a: the replication payload is a closed allow-list (plan section 6).

Every event kind is walked with private sentinels in every field; nothing
private may reach the payload under any key, at any depth. Unknown kinds are
dropped entirely, scrubbing never depends on the caller's bindings, and free
strings travel only in their closed shapes.
"""
from __future__ import annotations

import copy
import inspect
import json
from pathlib import Path

import pytest

from khipu import migrate, routing

HOME = "/private/login-home"
LOGIN = "codex|" + HOME
SENT = "SENTINEL-PRIVATE"
PATH_SENT = "/Users/x/.claude-secret/" + SENT
SECRET_SENT = "sk-ant-" + "A" * 24 + "-" + SENT
OWNER_WORDS = "owner words /Users/x/kept-as-written"
BINDINGS = {LOGIN: "codex:main"}

TYPES = [
    "evidence", "observation", "observation_renew", "source_seen", "hold",
    "hold_end", "rule_add", "rule_edit", "rule_retire", "rule_restore",
    "rule_confirm", "rule_unconfirm", "rule_undo", "rule_dismiss", "note_set",
    "note_end", "priority_set", "priority_confirm", "priority_undo",
    "priority_clear", "priority_drop", "pause", "lift", "clear", "clear_undo",
    "dismiss", "undismiss", "definition_set", "definition_remove",
    "provider_set", "account_migration", "account_binding", "account_rename",
    "account_move", "account_note", "account_txn", "seed_import",
    "reset_check", "owner_change",
]

ALLOWED_KEYS = {
    "type", "id", "entity", "base", "proposal", "op", "txn", "mac",
    "legacy_mac", "via", "kind", "definition", "state", "basis", "source",
    "status", "reason", "window", "harness", "model", "family", "transport",
    "auth", "state_id", "schedule_id", "revision", "date", "written_at",
    "until", "clock_at", "reset_until", "next_at", "reset_moved_to",
    "started_at", "witness_at", "expires_at", "reset_at", "account",
    "target", "to", "n", "count", "checks", "busy_tries", "lock_tries",
    "hub_order", "local_order", "message_time", "owner", "seeded",
    "until_default", "counted", "pending", "prior_confirmed",
    "number_pending", "observed", "observed_turn", "session_logs", "silent",
    "unmatched", "used_pct", "hlc", "written_by", "scope",
    "observation_scope", "declaration", "entries", "holds", "value",
    "label", "login", "probe", "host", "use", "lane_names",
    "models_allowed", "models_never", "accounts", "aliases", "declines",
    "lanes_cut", "text", "owner_request", "prior_text", "role", "level",
    "claim", "as_of", "wall", "counter", "why", "provider", "runner", "lane",
    "budget_account",
}

FORBIDDEN_KEYS = {
    "home", "evidence", "name", "project_name", "session_name", "source_id",
    "op_body", "auth_chain", "file", "sha256", "list_before", "list_after",
    "bindings", "project", "session", "provider_session", "login_account",
    "seq", "_seq",
}

# Fields whose values are the owner's words and must travel byte-identical
# (under the kind and switch conditions the payload contract names).
OWNER_WORD_FIELDS = {"text", "prior_text", "owner_request", "role"}


def leaves(value, field=True):
    if isinstance(value, dict):
        for key, inner in value.items():
            if field:
                yield key, inner
            # Map keys (holds, aliases) are ids, not field names.
            yield from leaves(inner, field=key not in ("holds", "aliases"))
    elif isinstance(value, list):
        for inner in value:
            yield from leaves(inner, field)


def maximal(kind):
    return {
        "type": kind, "id": "e1", "written_at": "2026-10-20T14:00:00Z",
        "written_by": {"kind": "conversation", "harness": "codex", "mac": "m1",
                       "project": SENT, "session": SENT, "name": SENT,
                       "provider_session": SENT, "file": SENT},
        "evidence": SENT, "source_id": SENT, "op_body": SENT,
        "auth_chain": SENT, "file": SENT, "sha256": SENT,
        "list_before": SENT, "list_after": SENT,
        "account": LOGIN, "target": LOGIN, "to": LOGIN,
        "scope": {"kind": "account", "account": LOGIN},
        "entries": [{"account": LOGIN, "label": "Codex", "role": SENT + "-ROLE",
                     "level": {"claim": SENT + "-LEVEL", "as_of": "2026-10-20T14:00:00Z",
                               "until": "2026-10-21T14:00:00Z"}}],
        "declines": [{"account": LOGIN, "why": None}],
        "declaration": {"kind": "declared", "login_account": SENT, "account": LOGIN},
        "bindings": {LOGIN: "codex:main"},
        "aliases": {"acct-0123456789ab": "codex:main"},
        "lanes_cut": [["runner", "lane"]],
        "holds": {"h1": "end"},
        "hlc": {"wall": 1, "counter": 0, "mac": "m1"},
        "text": SENT + "-TEXT", "owner_request": SENT + "-REQUEST",
        "prior_text": SENT + "-PRIOR", "owner": True, "pending": False,
        "definition": "account",
        "value": {"id": "codex:main", "label": "Codex", "provider": "codex"},
        "unknown_field": SENT,
    }


@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize("proposal_text", [False, True])
def test_every_event_kind_walks_clean(kind, proposal_text):
    row = maximal(kind)
    before = copy.deepcopy(row)
    (payload,) = routing.replication_payload([row], proposal_text=proposal_text,
                                             bindings=BINDINGS)
    assert row == before
    assert set(payload) <= ALLOWED_KEYS
    for key, value in leaves(payload):
        assert key in ALLOWED_KEYS
        assert key not in FORBIDDEN_KEYS
        if isinstance(value, str):
            assert HOME not in value
            assert "|" + HOME not in value
    dumped = json.dumps(payload)
    for token in (SENT + "-TEXT", SENT + "-PRIOR", SENT + "-ROLE", SENT + "-LEVEL"):
        if token in dumped:
            # Only owner words may carry a sentinel, and only where the
            # contract allows them for this kind and switch.
            assert kind in ("rule_add", "rule_edit", "note_set", "owner_change", "hold",
                            "priority_set", "rule_confirm", "priority_confirm")
    if not proposal_text:
        assert SENT + "-REQUEST" not in dumped
    assert payload.get("account") in (None, "codex:main")
    assert (payload.get("scope") or {}).get("account") in (None, "codex:main")
    assert payload.get("written_by") == {"kind": "conversation", "harness": "codex", "mac": "m1"}


def test_unmapped_login_key_never_travels():
    row = maximal("evidence")
    (payload,) = routing.replication_payload([row], proposal_text=True, bindings={})
    assert "account" not in payload
    assert "account" not in (payload.get("scope") or {})
    assert HOME not in json.dumps(payload)


def test_conversation_hold_words_drop_and_owner_hold_words_keep():
    base = {"type": "hold", "id": "h1", "text": "words", "scope": {"kind": "all"}}
    (payload,) = routing.replication_payload([dict(base, owner=False)], proposal_text=True,
                                             bindings=BINDINGS)
    assert "text" not in payload
    (payload,) = routing.replication_payload([dict(base, owner=True)], proposal_text=True,
                                             bindings=BINDINGS)
    assert payload["text"] == "words"


def test_pending_words_follow_the_proposal_switch():
    row = {"type": "rule_edit", "id": "e1", "pending": True, "text": "proposal",
           "owner_request": "request", "prior_text": "confirmed before"}
    (off,) = routing.replication_payload([row], proposal_text=False, bindings=BINDINGS)
    assert "text" not in off and "owner_request" not in off and "prior_text" not in off
    (on,) = routing.replication_payload([row], proposal_text=True, bindings=BINDINGS)
    assert on["text"] == "proposal" and on["owner_request"] == "request"


# --- Fix round 1: closed per-kind map, binding-independent scrubbing ---------

def test_bindings_is_a_required_argument():
    assert inspect.signature(routing.replication_payload).parameters["bindings"].default \
        is inspect.Parameter.empty
    with pytest.raises(TypeError):
        routing.replication_payload([{"type": "note_end", "id": "e1"}])


def test_unknown_kind_is_dropped_entirely():
    rows = [{"type": "future_kind", "id": "e1", "reason": SENT, "text": SENT,
             "account": LOGIN},
            {"type": "note_end", "id": "e2"}]
    payload = routing.replication_payload(rows, proposal_text=True, bindings=BINDINGS)
    assert [row["type"] for row in payload] == ["note_end"]
    assert SENT not in json.dumps(payload)


@pytest.mark.parametrize("kind", TYPES)
@pytest.mark.parametrize("bindings", [BINDINGS, {}], ids=["bound", "unbound"])
def test_sentinel_walk_every_kind_every_field(kind, bindings):
    """Sentinels (paths, logins, secrets, plain codes) in every field of
    every kind: only stable ids, closed shapes and owner words may travel."""
    row = maximal(kind)
    # Path and secret sentinels in every free field at every depth.
    row.update({
        "via": PATH_SENT, "status": PATH_SENT, "source": PATH_SENT,
        "basis": PATH_SENT, "window": PATH_SENT, "state": PATH_SENT,
        "reason": PATH_SENT + " " + SECRET_SENT, "kind": PATH_SENT,
        "auth": PATH_SENT, "model": PATH_SENT, "family": PATH_SENT,
        "transport": PATH_SENT, "harness": PATH_SENT, "runner": PATH_SENT,
        "lane": PATH_SENT, "entity": PATH_SENT, "txn": PATH_SENT,
        "base": "e0", "label": PATH_SENT, "login": PATH_SENT,
        "probe": PATH_SENT, "host": PATH_SENT, "use": PATH_SENT,
        "text": OWNER_WORDS, "prior_text": OWNER_WORDS,
        "owner_request": OWNER_WORDS,
        "written_by": {"kind": PATH_SENT, "harness": PATH_SENT, "mac": "m1",
                       "project": PATH_SENT, "session": PATH_SENT},
        "entries": [{"account": LOGIN, "label": PATH_SENT, "role": OWNER_WORDS,
                     "level": {"claim": PATH_SENT, "as_of": "2026-10-20T14:00:00Z",
                               "until": "2026-10-21T14:00:00Z"},
                     "until": "2026-10-21T14:00:00Z"}],
        "holds": {LOGIN: "end", "claude|/other/home": "keep", "h1": "end"},
        "declaration": {"kind": PATH_SENT, "provider": PATH_SENT,
                        "model": PATH_SENT, "identity_invalid": PATH_SENT,
                        "account": "claude|/other/home",
                        "budget_account": LOGIN},
        "declines": [{"account": LOGIN, "why": PATH_SENT}],
        "lanes_cut": [[PATH_SENT, PATH_SENT]],
        "aliases": {"acct-0123456789ab": PATH_SENT, LOGIN: "codex:main"},
        "accounts": [LOGIN, "claude|/other/home", "bare-unmapped"],
        "scope": PATH_SENT,
    })
    if kind == "hold":
        row["scope"] = {"kind": "account", "account": LOGIN}
    before = copy.deepcopy(row)
    (payload,) = routing.replication_payload([row], proposal_text=True, bindings=bindings)
    assert row == before
    dumped = json.dumps(payload)
    assert PATH_SENT not in dumped.replace(OWNER_WORDS, "")
    assert SECRET_SENT not in dumped
    assert LOGIN not in dumped
    assert "claude|/other/home" not in dumped
    assert "bare-unmapped" not in dumped
    for key, value in leaves(payload):
        assert key in ALLOWED_KEYS, (kind, key)
        assert key not in FORBIDDEN_KEYS, (kind, key)
        if isinstance(value, str) and key not in OWNER_WORD_FIELDS:
            assert "/" not in value and "\\" not in value, (kind, key, value)
            assert "|" not in value, (kind, key, value)
    stable = bindings.get(LOGIN)
    for key, value in leaves(payload):
        if key in ("account", "to") and isinstance(value, str):
            assert value == stable or ":" in value and value == "codex:main"


@pytest.mark.parametrize("kind", TYPES)
def test_free_scalars_drop_non_closed_shapes(kind):
    """Plain sentinel words in closed-code and safe-name fields drop: the
    shape check, not the sentinel's path shape, is what stops them."""
    row = maximal(kind)
    row.update({
        "via": SENT, "status": SENT, "source": SENT, "basis": SENT,
        "window": SENT, "state": SENT, "reason": SENT, "kind": SENT,
        "auth": SENT, "model": SENT + " MODEL", "family": SENT + " NAME",
        "transport": SENT + " NAME", "harness": SENT + " NAME",
        "runner": SENT + " NAME", "lane": SENT + " NAME",
        "definition": SENT, "label": SENT + " NAME", "login": SENT,
        "probe": SENT, "host": SENT + " HOST", "use": SENT,
        "written_by": {"kind": SENT, "harness": SENT + " NAME", "mac": "m1"},
        "declaration": {"kind": SENT + " NAME", "provider": SENT + " NAME",
                        "model": SENT + " NAME",
                        "identity_invalid": SENT, "account": LOGIN},
        "declines": [{"account": LOGIN, "why": SENT}],
        "lanes_cut": [[SENT + " NAME", SENT]],
        "entries": [{"account": LOGIN, "label": SENT + " NAME",
                     "level": {"claim": "a claim", "as_of": "2026-10-20T14:00:00Z",
                               "until": "2026-10-21T14:00:00Z"}}],
    })
    (payload,) = routing.replication_payload([row], proposal_text=True, bindings=BINDINGS)
    dumped = json.dumps(payload)
    assert SENT not in dumped.replace(SENT + "-TEXT", "").replace(
        SENT + "-PRIOR", "").replace(SENT + "-ROLE", "").replace(SENT + "-REQUEST", "")


def test_account_like_values_must_map_to_a_stable_id():
    rows = [
        {"type": "hold", "id": "h", "account": "claude-unmapped", "owner": True,
         "scope": {"kind": "account", "account": "acct-0123456789ab"}},
        {"type": "evidence", "id": "e", "account": "claude|",
         "scope": {"kind": "account", "account": LOGIN}},
        {"type": "provider_set", "id": "p", "label": "L", "login": HOME,
         "probe": "/Users/x/probe.sh", "host": "api.x.com", "use": "/Users/x/key",
         "accounts": [LOGIN, "claude|/other/home", "bare"]},
        {"type": "priority_set", "id": "p",
         "entries": [{"account": LOGIN, "label": "/Users/z/.other-home"}]},
        {"type": "account_migration", "id": "m",
         "aliases": {"acct-0123456789ab": "codex:main"},
         "bindings": {LOGIN: "codex:main"}},
    ]
    payload = routing.replication_payload(rows, proposal_text=True, bindings=BINDINGS)
    dumped = json.dumps(payload)
    for token in ("claude-unmapped", "acct-0123456789ab\".*\"account", HOME,
                  "/other/home", "bare", "/Users/z/.other-home", "bindings"):
        assert token not in dumped
    assert payload[0].get("account") is None and "account" not in payload[0]["scope"]
    assert payload[2].get("accounts") == ["codex:main"]
    assert payload[2].get("login") is None and payload[2].get("probe") is None
    assert payload[2].get("use") is None
    assert payload[4].get("aliases") == {"acct-0123456789ab": "codex:main"}
    assert "bindings" not in payload[4]


def test_scrubbing_does_not_depend_on_bindings():
    row = {"type": "evidence", "id": "e1",
           "reason": "read " + HOME + "/x", "account": LOGIN}
    (payload,) = routing.replication_payload([row], bindings={})
    assert "reason" not in payload and "account" not in payload
    (payload,) = routing.replication_payload([row], bindings=BINDINGS)
    assert "reason" not in payload and payload.get("account") == "codex:main"


def test_base_travels_in_the_payload():
    rows = [{"type": "rule_edit", "id": "e2", "base": "e1", "n": 1, "text": "t"},
            {"type": "priority_set", "id": "p", "base": "e1", "pending": True,
             "entries": []}]
    payload = routing.replication_payload(rows, proposal_text=True, bindings=BINDINGS)
    assert payload[0]["base"] == "e1" and payload[1]["base"] == "e1"


def test_owner_words_travel_byte_identical():
    words = "keep /Users/x/.claude and claude|/Users/x as written"
    rows = [{"type": "rule_add", "id": "r", "n": 1, "text": words},
            {"type": "note_set", "id": "n", "text": words},
            {"type": "hold", "id": "h", "owner": True, "text": words,
             "scope": {"kind": "all"}},
            {"type": "priority_set", "id": "p",
             "entries": [{"account": LOGIN, "role": words}]}]
    payload = routing.replication_payload(rows, proposal_text=True, bindings=BINDINGS)
    assert payload[0]["text"] == words and payload[1]["text"] == words
    assert payload[2]["text"] == words
    assert payload[3]["entries"][0]["role"] == words


def test_migration_0042_is_the_next_free_number_and_is_additive():
    versions = [v for v, _ in migrate.available()]
    assert "0041_routing_replication" not in versions
    path = dict(migrate.available())["0042_routing_replication"]
    sql = Path(path).read_text(encoding="utf-8")
    assert "INSERT INTO schema_migrations" in sql
    assert "'0042_routing_replication'" in sql
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("CREATE TABLE"):
            assert "IF NOT EXISTS" in stripped
        if stripped.startswith("CREATE INDEX"):
            assert "IF NOT EXISTS" in stripped
    assert "CREATE TABLE IF NOT EXISTS routing_events" in sql
    assert "CREATE TABLE IF NOT EXISTS routing_sources" in sql
    assert "event_id    TEXT PRIMARY KEY" in sql
    assert migrate.steps(sql)


def test_migration_0042_reverse_is_real_and_hub_order_has_no_redundant_unique():
    sql = Path(dict(migrate.available())["0042_routing_replication"]).read_text(
        encoding="utf-8")
    assert "BIGSERIAL" in sql
    assert "UNIQUE (hub_order)" not in sql and "UNIQUE(hub_order)" not in sql
    header = sql.split("CREATE TABLE", 1)[0]
    reverse = header[header.index("Reverse"):]
    # The reverse names what it removes: both tables and both indexes, so
    # running it (or reading it) leaves nothing unnamed behind.
    for name in ("routing_events", "routing_sources",
                 "idx_routing_events_sender", "idx_routing_events_received"):
        assert name in reverse, name
    assert "schema_migrations" in reverse and "0042" in reverse
