"""Command-output confidentiality with synthetic kits and mocked I/O only."""

import json
from unittest import mock

import pytest

from khipu import cli, join, join_pair

PASSWORD = "synthetic-db-password-729"
KEY = "synthetic-api-key-729"
PHRASE = "synthetic-passphrase-729"
SECRETS = (PASSWORD, KEY, PHRASE)
RAW = " / ".join(SECRETS)


def output(capsys):
    captured = capsys.readouterr()
    for secret in SECRETS:
        assert secret not in captured.out + captured.err
    assert captured.err == ""
    return json.loads(captured.out)


@pytest.mark.parametrize("field", ["expected", "gateway_url", "created_at"])
def test_import_summary_redacts_kit_values(field, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    kit = {
        "format": join.FORMAT_VERSION,
        "database_url": f"postgresql://user:{PASSWORD}@hub.example/db",
        "expected": {"episodes": 1, "topics": 2, "nodes": 3},
    }
    if field == "expected":
        kit[field][KEY] = PHRASE
    elif field == "gateway_url":
        kit[field] = f"https://{KEY}:{PASSWORD}@gateway.example:443/join"
    else:
        kit[field] = PHRASE
    path = tmp_path / "kit.khipujoin"
    path.write_text(json.dumps(kit))
    with (
        mock.patch.object(join, "read_passphrase", return_value=PHRASE),
        mock.patch.object(join, "verify_live_counts", return_value={"ok": True}),
        mock.patch("khipu.keychain.set_dsn"),
    ):
        assert cli.main(["join", "import", "--file", str(path), "--skip-migrate-check"]) == 0
    summary = output(capsys)["summary"]
    assert summary["expected"] == {"episodes": 1, "topics": 2, "nodes": 3}
    if field == "gateway_url":
        assert summary[field] == "https://gateway.example:443/join"


def test_receive_refusal_uses_fixed_message(capsys):
    tls = mock.Mock()
    context = mock.Mock()
    context.wrap_socket.return_value = tls
    with (
        mock.patch.object(join, "read_passphrase", return_value=PHRASE),
        mock.patch.object(join_pair, "_browse_service", return_value=[{"name": "fixture"}]),
        mock.patch.object(join_pair, "_resolve_service", return_value=("peer.local", 8788, None)),
        mock.patch.object(join_pair, "_tcp_connect", return_value=(mock.Mock(), "peer.local", 8788)),
        mock.patch.object(join_pair.ssl, "SSLContext", return_value=context),
        mock.patch.object(join_pair, "_readline", return_value=json.dumps({"ok": False, "error": RAW}).encode()),
    ):
        assert cli.main(["join", "receive", "--pin", "123456"]) == 1
    assert "nearby" in output(capsys)["error"].lower()


@pytest.mark.parametrize("userinfo", [f"{KEY}:{PASSWORD}", KEY, f"{KEY}:{PASSWORD}@{PHRASE}"])
def test_shared_redactor_strips_all_url_userinfo(userinfo, monkeypatch):
    monkeypatch.setenv("KHIPU_REDACT_SECRETS", "0")
    assert join.redact_connection_text(f"https://{userinfo}@gateway.example/join") == "https://gateway.example/join"
