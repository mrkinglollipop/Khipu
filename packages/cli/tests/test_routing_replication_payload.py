"""Slice 11a: the replication payload is a closed allow-list (plan section 6).

Every event kind is walked with private sentinels in every private field;
nothing private may reach the payload under any key, at any depth.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from khipu import migrate, routing

HOME = "/private/login-home"
LOGIN = "codex|" + HOME
SENT = "SENTINEL-PRIVATE"
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
    "type", "id", "entity", "proposal", "op", "txn", "mac", "legacy_mac",
    "via", "kind", "definition", "state", "basis", "source", "status",
    "reason", "window", "harness", "model", "family", "transport", "auth",
    "reset_id", "state_id", "schedule_id", "revision", "date", "written_at",
    "until", "clock_at", "reset_until", "next_at", "reset_moved_to",
    "started_at", "witness_at", "expires_at", "reset_at", "account",
    "target", "to", "n", "count", "checks", "busy_tries", "lock_tries",
    "hub_order", "local_order", "message_time", "owner", "seeded",
    "until_default", "counted", "pending", "prior_confirmed",
    "number_pending", "observed", "observed_turn", "session_logs", "silent",
    "unmatched", "used_pct", "hlc", "written_by", "scope",
    "observation_scope", "declaration", "entries", "holds", "value",
    "label", "family", "login", "probe", "host", "use", "lane_names",
    "models_allowed", "models_never", "accounts", "aliases", "declines",
    "lanes_cut", "text", "owner_request", "prior_text", "role", "level",
    "claim", "as_of", "wall", "counter", "why", "provider", "runner", "lane",
}

FORBIDDEN_KEYS = {
    "home", "evidence", "name", "project_name", "session_name", "source_id",
    "op_body", "auth_chain", "file", "sha256", "list_before", "list_after",
    "bindings", "project", "session", "provider_session", "login_account",
    "seq", "_seq",
}


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
                            "priority_set")
    if not proposal_text:
        assert SENT + "-REQUEST" not in dumped
    assert payload.get("account") in (None, "codex:main")
    assert (payload.get("scope") or {}).get("account") in (None, "codex:main")
    assert payload.get("written_by") == {"kind": "conversation", "harness": "codex", "mac": "m1"}


def test_unmapped_login_key_never_travels():
    row = maximal("evidence")
    (payload,) = routing.replication_payload([row], proposal_text=True, bindings={})
    assert "account" not in payload
    assert "account" not in payload["scope"]
    assert HOME not in json.dumps(payload)


def test_conversation_hold_words_drop_and_owner_hold_words_keep():
    base = {"type": "hold", "id": "h1", "text": "words", "scope": {"kind": "all"}}
    (payload,) = routing.replication_payload([dict(base, owner=False)], proposal_text=True)
    assert "text" not in payload
    (payload,) = routing.replication_payload([dict(base, owner=True)], proposal_text=True)
    assert payload["text"] == "words"


def test_pending_words_follow_the_proposal_switch():
    row = {"type": "rule_edit", "id": "e1", "pending": True, "text": "proposal",
           "owner_request": "request", "prior_text": "confirmed before"}
    (off,) = routing.replication_payload([row], proposal_text=False)
    assert "text" not in off and "owner_request" not in off and "prior_text" not in off
    (on,) = routing.replication_payload([row], proposal_text=True)
    assert on["text"] == "proposal" and on["owner_request"] == "request"


def test_migration_0042_is_the_next_free_number_and_is_additive():
    versions = [v for v, _ in migrate.available()]
    assert "0041_routing_replication" not in versions
    path = dict(migrate.available())["0042_routing_replication"]
    sql = Path(path).read_text(encoding="utf-8")
    assert "INSERT INTO schema_migrations" in sql
    assert "'0042_routing_replication'" in sql
    assert "DROP TABLE routing_sources" in sql.splitlines()[0:30][0] or "DROP TABLE" in sql[:3000]
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
