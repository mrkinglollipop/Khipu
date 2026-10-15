"""Final output confidentiality, using synthetic data and mocked command I/O."""

import json
import random
import string
from contextlib import ExitStack
from urllib.parse import quote, quote_plus
from unittest import mock

import pytest

from khipu import cli, config, db, graph_backup as gb, join, join_pair, keychain


@pytest.mark.parametrize("gateway", [
    "https://gateway.example/join?key={key}&passphrase={phrase}&view=ready",
    "https://user:{password} {key} {phrase}@gateway.example/join",
])
def test_import_gateway_query_and_spaced_userinfo(gateway, tmp_path, monkeypatch, capsys):
    values = dict(password="db canary-729", key="api canary-729", phrase="phrase canary-729")
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("KHIPU_JOIN_PASSPHRASE", values["phrase"])
    kit = {"format": join.FORMAT_VERSION,
           "database_url": "postgresql://user:" + quote(values["password"], safe="") + "@hub.example/db",
           "gemini_api_key": values["key"],
           "gateway_url": gateway.format(**values)}
    path = tmp_path / "kit.khipujoin"
    path.write_text(json.dumps(kit))
    with (mock.patch.object(keychain, "set_dsn"), mock.patch.object(keychain, "set_gemini_key"),
          mock.patch.object(join, "verify_live_counts", return_value={"ok": True})):
        assert cli.main(["join", "import", "--file", str(path), "--skip-migrate-check"]) == 0
    captured = capsys.readouterr()
    for value in values.values():
        assert value not in captured.out + captured.err
    url = json.loads(captured.out)["summary"]["gateway_url"]
    assert "@" not in url
    assert "key=" not in url and "passphrase=" not in url
    assert "gateway.example/join" in url


def test_backup_status_omits_historical_stderr_and_exceptions(capsys):
    raw = "unknown historical password and api-key and passphrase"
    event = {"status": "fail", "detail": {"stderr": raw, "error": raw,
             "nested": {"exception": raw}}, "created_at": None, "age_seconds": 0}
    with (mock.patch.object(gb, "_resolve_producer", return_value=True),
          mock.patch.object(gb, "local_health", return_value={"ok": True}),
          mock.patch.object(gb, "latest_snapshot", return_value=None),
          mock.patch.object(gb, "_live_graph", return_value=None),
          mock.patch.object(gb, "has_r2_remote", return_value=True),
          mock.patch.object(gb, "resolve_dest_remote", return_value="r2:fixture/graph"),
          mock.patch.object(gb, "_latest_ops_event", return_value=event)):
        assert cli.main(["graph-backup", "status"]) == 0
    captured = capsys.readouterr()
    assert raw not in captured.out + captured.err
    body = json.loads(captured.out)
    for item in (body["last_graph_snapshot"], body["last_graph_snapshot_offsite"],
                 body["last_graph_restore_drill"], body["offsite"]["latest"]):
        assert item["summary"] == "Backup operation failed; full details are stored in the log."
        assert "ops_events.detail" in item["log_location"]
    assert event["detail"]["stderr"] == raw


COMMANDS = [
    (["join", "export", "--out", "unused"], join, "export_kit"),
    (["join", "import", "--file"], join, "import_kit"),
    (["join", "advertise"], join_pair, "advertise_join_kit"),
    (["join", "receive", "--pin", "123456"], join_pair, "receive_join_kit"),
    (["graph-backup", "record-local"], gb, "record_local"),
    (["graph-backup", "offsite"], gb, "run_offsite"),
    (["graph-backup", "drill"], gb, "scratch_drill"),
    (["graph-backup", "status"], gb, "status_payload"),
]


@pytest.mark.parametrize("argv,module,name", COMMANDS, ids=[" ".join(c[0][:2]) for c in COMMANDS])
@pytest.mark.parametrize("seed", range(4))
def test_all_command_error_paths_keep_synthetic_secrets_private(argv, module, name, seed,
                                                               tmp_path, monkeypatch, capsys):
    """Vary secret contents, encodings, JSON/text, streams and split writes."""
    import sys

    rng = random.Random(seed)
    secrets = ["".join(rng.choices(string.ascii_letters + string.digits, k=19)) + suffix
               for suffix in (" /+@", " ?&=", ' "\\雪', " /+", " @/", " ?+")]
    password, api, phrase, token, extra_api, loaded_key = secrets
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATABASE_URL", "postgresql://u:" + quote(password, safe="") + "@hub.example/db")
    monkeypatch.setenv("KHIPU_JOIN_PASSPHRASE", phrase)
    (tmp_path / "config.json").write_text(json.dumps({"gateway_token": token,
        "providers": {"one": {"api_key": api}, "two": {"api_key": extra_api}}}))
    kit = {"format": join.FORMAT_VERSION, "database_url": "postgresql://u@hub.example/db",
           "other_api_key": loaded_key}
    path = tmp_path / "kit.khipujoin"
    path.write_text(json.dumps(kit))
    if argv[:2] == ["join", "import"]:
        argv = [*argv, str(path)]

    def fail(*args, **kwargs):
        config.load_config()
        db.resolve_dsn()
        join.decrypt_payload(path.read_bytes(), phrase)
        keychain.get_password("gateway_token")
        forms = [form for secret in secrets for form in
                 (secret, quote(secret, safe=""), quote_plus(secret), quote(secret))]
        for stream in (sys.stdout, sys.stderr):
            for form in forms:
                text = json.dumps({"error": form}) if seed % 2 else "error: " + form
                mid = len(text) // 2
                stream.write(text[:mid])
                stream.write(text[mid:] + "\n")
            stream.flush()
        raise join.JoinError(" / ".join(forms))

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(keychain, "keychain_available", return_value=True))
        stack.enter_context(mock.patch.object(keychain, "_security", return_value=
                                             mock.Mock(returncode=0, stdout=loaded_key)))
        stack.enter_context(mock.patch.object(module, name, side_effect=fail))
        assert cli.main(argv) in (1, 2)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    for secret in secrets:
        for form in (secret, quote(secret, safe=""), quote_plus(secret), quote(secret),
                     json.dumps(secret)[1:-1]):
            assert form not in text
    assert "REDACTED" in text


def test_unhandled_join_error_uses_guarded_stderr(tmp_path, monkeypatch, capsys):
    phrase = "stdin phrase with /+ and Unicode 雪"
    path = tmp_path / "kit.khipujoin"
    path.write_text("{}")
    with (mock.patch.object(join, "read_passphrase", return_value=phrase),
          mock.patch.object(join, "import_kit", return_value={}),
          mock.patch.object(join, "verify_live_counts", side_effect=RuntimeError(phrase))):
        assert cli.main(["join", "import", "--file", str(path)]) == 1
    captured = capsys.readouterr()
    assert phrase not in captured.out + captured.err
    assert captured.err == "error: REDACTED\n"


@pytest.mark.parametrize("name", ["key", "api_key", "token", "passphrase", "password", "secret", "auth", "%74oken"])
def test_final_guard_strips_unknown_url_secrets(name, capsys):
    from khipu.output_guard import guard_output

    @guard_output
    def emit():
        print(json.dumps({"url": f"https://unknown user:unknown value@gateway.example/path?view=ok&{name}=unknown&other=ok"}))

    emit()
    url = json.loads(capsys.readouterr().out)["url"]
    assert url == "https://gateway.example/path?view=ok&other=ok"


def test_pairing_banner_is_guarded_and_flushed_before_transfer(capsys):
    from khipu.output_guard import remember

    secret = "banner-secret/+"

    def advertise(*args, on_ready, **kwargs):
        remember(secret)
        on_ready()
        captured = capsys.readouterr()
        assert secret not in captured.out
        assert json.loads(captured.out)["ipv4"] == "REDACTED"
        return {"ok": True}

    with (mock.patch.object(join_pair, "lan_ipv4", return_value=secret),
          mock.patch.object(join_pair, "advertise", side_effect=advertise)):
        assert join_pair.advertise_join_kit("test phrase", pin="123456")["ok"]
