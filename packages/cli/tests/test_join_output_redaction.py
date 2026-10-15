"""Command-output confidentiality with synthetic kits and mocked I/O only."""

import json
import sqlite3
import subprocess
from unittest import mock

import pytest

from khipu import cli, graph_backup as gb, join, join_pair

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


@pytest.fixture
def backup(tmp_path):
    snap = tmp_path / "graph-20261015T000000Z.sqlite"
    snap.write_bytes(b"fixture")
    with (
        mock.patch.object(gb, "latest_snapshot", return_value=snap),
        mock.patch.object(gb, "_integrity_check", return_value="ok"),
        mock.patch.object(gb, "_remember_integrity_ok"),
        mock.patch.object(gb, "_count_embeddings", return_value=0),
        mock.patch.object(gb, "_resolve_producer", return_value=False),
        mock.patch.object(gb, "_required_graph_tables", return_value=None),
        mock.patch.object(gb, "resolve_dest_remote", return_value="r2:fixture/graph"),
        mock.patch.object(gb, "resolve_rclone", return_value="mock-rclone"),
        mock.patch.object(gb, "has_r2_remote", return_value=True),
        mock.patch.object(gb, "_live_graph", return_value=tmp_path / "live.sqlite"),
        mock.patch.object(gb, "_prune_remote_snapshots", return_value=[]) as prune,
        mock.patch.object(gb.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="", stderr="")) as run,
        mock.patch("khipu.ops_events.record", return_value={"id": 1}) as record,
    ):
        yield run, record, prune


@pytest.mark.parametrize("step", ["record", "embeddings"])
def test_record_local_uses_fixed_errors(step, backup, capsys):
    _, record, _ = backup
    if step == "record":
        record.side_effect = RuntimeError(RAW)
    with mock.patch.object(gb, "_count_embeddings", side_effect=sqlite3.DatabaseError(RAW) if step == "embeddings" else None, return_value=0):
        assert cli.main(["graph-backup", "record-local"]) == 2
    body = output(capsys)
    assert "failed" in body.get("reason", body["integrity"])


@pytest.mark.parametrize("step", ["stderr", "timeout", "spawn", "prune", "record"])
def test_offsite_uses_fixed_errors(step, backup, capsys):
    run, record, prune = backup
    if step == "stderr":
        run.return_value = mock.Mock(returncode=1, stderr=RAW, stdout=RAW)
        record.side_effect = RuntimeError(RAW)
    elif step == "timeout":
        run.side_effect = subprocess.TimeoutExpired([RAW], 1, output=RAW, stderr=RAW)
        record.side_effect = RuntimeError(RAW)
    elif step == "spawn":
        run.side_effect = OSError(RAW)
    elif step == "prune":
        prune.side_effect = RuntimeError(RAW)
    else:
        record.side_effect = RuntimeError(RAW)
    assert cli.main(["graph-backup", "offsite"]) == (0 if step == "prune" else 2)
    body = output(capsys)
    assert "failed" in str(body)


@pytest.mark.parametrize("returncode", [0, 1])
def test_backup_integrity_output_is_fixed(returncode):
    with mock.patch.object(gb.subprocess, "run", return_value=mock.Mock(returncode=returncode, stdout=RAW, stderr=RAW)):
        result = gb._integrity_check(mock.Mock())
    for secret in SECRETS:
        assert secret not in result
    assert "integrity" in result


@pytest.mark.parametrize("action, function", [
    ("record-local", "record_local"),
    ("offsite", "run_offsite"),
    ("drill", "scratch_drill"),
    ("status", "status_payload"),
])
def test_backup_unexpected_errors_are_fixed(action, function, capsys):
    with mock.patch.object(gb, function, side_effect=RuntimeError(RAW)):
        assert cli.main(["graph-backup", action]) == 2
    assert output(capsys)["error"] == f"graph backup {action} failed"


@pytest.mark.parametrize("userinfo", [f"{KEY}:{PASSWORD}", KEY, f"{KEY}:{PASSWORD}@{PHRASE}"])
def test_shared_redactor_strips_all_url_userinfo(userinfo, monkeypatch):
    monkeypatch.setenv("KHIPU_REDACT_SECRETS", "0")
    assert join.redact_connection_text(f"https://{userinfo}@gateway.example/join") == "https://gateway.example/join"
