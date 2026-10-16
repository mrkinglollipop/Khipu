"""Final output confidentiality, using synthetic data and mocked command I/O."""

import json
import random
import string
from contextlib import ExitStack
from urllib.parse import quote, quote_plus, unquote, unquote_plus
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


def encoded_secret(value, encoding):
    if encoding == "over-encoded":
        return "".join(f"%{byte:02x}" for byte in value.encode())
    if encoding == "double-encoded":
        return quote(quote(value, safe=""), safe="")
    return quote(value, safe="").replace("%2F", "%2f").replace("%A9", "%a9")


def decoding_forms(value, max_passes=5):
    """Every decoder branch must expose no registered secret at any depth."""
    seen = {value}
    frontier = {value}
    for _ in range(max_passes):
        frontier = {
            decoded
            for candidate in frontier
            for decoded in (unquote(candidate), unquote_plus(candidate))
            if decoded not in seen
        }
        seen.update(frontier)
    return seen


@pytest.mark.parametrize("encoding", ["mixed-case", "over-encoded", "double-encoded"])
@pytest.mark.parametrize("argv,module,name", COMMANDS, ids=[" ".join(c[0][:2]) for c in COMMANDS])
def test_encoded_secrets_in_views_and_text_across_commands(argv, module, name, encoding,
                                                         tmp_path, monkeypatch, capsys):
    import sys

    password, api, phrase, token = secrets = [f"{kind}-round6/+é" for kind in
                                            ("db", "api", "phrase", "token")]
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATABASE_URL", "postgresql://u:" + quote(password, safe="") + "@hub.example/db")
    monkeypatch.setenv("KHIPU_JOIN_PASSPHRASE", phrase)
    (tmp_path / "config.json").write_text(json.dumps({"gateway_token": token,
        "providers": {"one": {"api_key": api}}}))
    path = tmp_path / "kit.khipujoin"
    path.write_text(json.dumps({"format": join.FORMAT_VERSION,
                               "database_url": "postgresql://u@hub.example/db"}))
    if argv[:2] == ["join", "import"]:
        argv = [*argv, str(path)]

    def fail(*args, **kwargs):
        config.load_config()
        db.resolve_dsn()
        join.decrypt_payload(path.read_bytes(), phrase)
        query = "&".join(f"view{i}=prefix-{encoded_secret(secret, encoding)}-suffix"
                         for i, secret in enumerate(secrets))
        for stream in (sys.stdout, sys.stderr):
            print(json.dumps({"url": "https://gateway.example/join?ready=yes&" + query}), file=stream)
            for secret in secrets:
                print("detail: prefix-" + encoded_secret(secret, encoding) + "-suffix", file=stream)
        raise join.JoinError("synthetic failure")

    with mock.patch.object(module, name, side_effect=fail):
        assert cli.main(argv) in (1, 2)
    captured = capsys.readouterr()
    for text in (captured.out, captured.err):
        decoded = unquote_plus(text)
        for secret in secrets:
            assert secret not in decoded
        assert "ready=yes" in text
        assert "REDACTED" in text


@pytest.mark.parametrize("encoding", ["mixed-case", "over-encoded", "double-encoded"])
def test_successful_import_redacts_encoded_view(encoding, tmp_path, monkeypatch, capsys):
    secret = "api-round6/+é"
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "kit.khipujoin"
    path.write_text(json.dumps({"format": join.FORMAT_VERSION,
        "database_url": "postgresql://u@hub.example/db", "gemini_api_key": secret,
        "gateway_url": "https://gateway.example/join?ready=yes&view=" + encoded_secret(secret, encoding)}))
    with (mock.patch.object(join, "read_passphrase", return_value="synthetic phrase"),
          mock.patch.object(keychain, "set_dsn"), mock.patch.object(keychain, "set_gemini_key"),
          mock.patch.object(join, "verify_live_counts", return_value={"ok": True})):
        assert cli.main(["join", "import", "--file", str(path), "--skip-migrate-check"]) == 0
    captured = capsys.readouterr()
    assert secret not in unquote_plus(captured.out + captured.err)
    assert json.loads(captured.out)["summary"]["gateway_url"] == "https://gateway.example/join?ready=yes&view=REDACTED"


def test_successful_import_redacts_literal_plus_in_url_path(tmp_path, monkeypatch, capsys):
    secret = "api-literal+path/729"
    encoded_path = quote(secret, safe="+")
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path / "data"))
    path = tmp_path / "kit.khipujoin"
    path.write_text(json.dumps({"format": join.FORMAT_VERSION,
        "database_url": "postgresql://u@hub.example/db", "gemini_api_key": secret,
        "gateway_url": "https://gateway.example/join/" + encoded_path + "?ready=yes"}))
    with (mock.patch.object(join, "read_passphrase", return_value="synthetic phrase"),
          mock.patch.object(keychain, "set_dsn"), mock.patch.object(keychain, "set_gemini_key"),
          mock.patch.object(join, "verify_live_counts", return_value={"ok": True})):
        assert cli.main(["join", "import", "--file", str(path), "--skip-migrate-check"]) == 0
    captured = capsys.readouterr()
    for form in decoding_forms(encoded_path):
        assert form not in captured.out + captured.err
    assert "REDACTED" in json.loads(captured.out)["summary"]["gateway_url"]


@pytest.mark.parametrize("argv,module,name", COMMANDS, ids=[" ".join(c[0][:2]) for c in COMMANDS])
def test_literal_plus_path_and_nested_decoding_stay_private(argv, module, name,
                                                            tmp_path, monkeypatch, capsys):
    """A literal plus needs unquote while nested values need both decoders."""
    import sys

    literal_plus = "literal-plus+canary/729"
    nested = "nested-canary/+729"
    phrase = "phrase-canary/+729"
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATABASE_URL", "postgresql://u@hub.example/db")
    monkeypatch.setenv("KHIPU_JOIN_PASSPHRASE", phrase)
    (tmp_path / "config.json").write_text(json.dumps({
        "gateway_token": nested,
        "providers": {"one": {"api_key": literal_plus}},
    }))
    path = tmp_path / "kit.khipujoin"
    path.write_text(json.dumps({"format": join.FORMAT_VERSION,
                                "database_url": "postgresql://u@hub.example/db"}))
    if argv[:2] == ["join", "import"]:
        argv = [*argv, str(path)]

    def fail(*args, **kwargs):
        config.load_config()
        db.resolve_dsn()
        join.decrypt_payload(path.read_bytes(), phrase)
        literal_path = quote(literal_plus, safe="+")
        double_encoded = encoded_secret(nested, "double-encoded")
        url = f"https://gateway.example/join/{literal_path}?view={double_encoded}&ready=yes"
        for stream in (sys.stdout, sys.stderr):
            print(json.dumps({"url": url}), file=stream)
        raise join.JoinError("synthetic failure")

    with mock.patch.object(module, name, side_effect=fail):
        assert cli.main(argv) in (1, 2)
    captured = capsys.readouterr()
    for candidate in (quote(literal_plus, safe="+"),
                      encoded_secret(nested, "double-encoded")):
        for form in decoding_forms(candidate):
            assert form not in captured.out + captured.err
    assert "REDACTED" in captured.out + captured.err


@pytest.mark.parametrize("argv,module,name", COMMANDS, ids=[" ".join(c[0][:2]) for c in COMMANDS])
def test_flushes_hold_split_secret_until_final_drain(argv, module, name,
                                                    tmp_path, monkeypatch, capsys):
    """Neither stream may expose a secret that spans explicit flush calls."""
    import sys

    secret = "split-flush-canary/+729"
    phrase = "phrase-canary/+729"
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KHIPU_DATABASE_URL", "postgresql://u@hub.example/db")
    monkeypatch.setenv("KHIPU_JOIN_PASSPHRASE", phrase)
    (tmp_path / "config.json").write_text(json.dumps({"gateway_token": secret}))
    path = tmp_path / "kit.khipujoin"
    path.write_text(json.dumps({"format": join.FORMAT_VERSION,
                                "database_url": "postgresql://u@hub.example/db"}))
    if argv[:2] == ["join", "import"]:
        argv = [*argv, str(path)]

    def fail(*args, **kwargs):
        config.load_config()
        db.resolve_dsn()
        join.decrypt_payload(path.read_bytes(), phrase)
        split = len(secret) // 2
        for stream in (sys.stdout, sys.stderr):
            stream.write("partial: " + secret[:split])
            stream.flush()
            stream.write(secret[split:] + "\n")
            stream.flush()
        raise join.JoinError("synthetic failure")

    with mock.patch.object(module, name, side_effect=fail):
        assert cli.main(argv) in (1, 2)
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert "REDACTED" in captured.out
    assert "REDACTED" in captured.err


def test_explicit_flush_does_not_emit_before_final_drain(capsys):
    from khipu.output_guard import guard_output, remember
    import sys

    secret = "flush-timing-canary/+729"
    early = []

    @guard_output
    def emit():
        remember(secret)
        split = len(secret) // 2
        for stream in (sys.stdout, sys.stderr):
            stream.write("partial: " + secret[:split])
            stream.flush()
            early.append(capsys.readouterr())
            stream.write(secret[split:] + "\n")
            stream.flush()
            early.append(capsys.readouterr())

    emit()
    assert all(not captured.out and not captured.err for captured in early)
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "REDACTED" in captured.out
    assert "REDACTED" in captured.err


def test_plain_text_quote_cannot_mask_json_secret_on_next_line(capsys):
    from khipu.output_guard import guard_output, remember

    secret = 'quoted-json-canary "\\雪'

    @guard_output
    def emit():
        remember(secret)
        print("plain detail: " + secret)
        print(json.dumps({"error": secret}))

    emit()
    captured = capsys.readouterr().out
    assert secret not in captured
    assert json.dumps(secret)[1:-1] not in captured
    assert captured.count("REDACTED") == 2


def test_five_pass_decoding_checks_all_intermediate_forms(capsys):
    from khipu.output_guard import guard_output, remember

    secret = "five-pass+canary/729"
    encoded = secret
    for _ in range(5):
        encoded = quote(encoded, safe="+")

    @guard_output
    def emit():
        remember(secret)
        print(json.dumps({"error": encoded}))

    emit()
    captured = capsys.readouterr().out
    for form in decoding_forms(encoded):
        assert form not in captured
    assert "REDACTED" in captured


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
    ordinary = "pending ordinary output"

    def advertise(*args, on_ready, **kwargs):
        remember(secret)
        print(ordinary)
        on_ready()
        captured = capsys.readouterr()
        assert secret not in captured.out
        assert ordinary not in captured.out
        assert json.loads(captured.out)["ipv4"] == "REDACTED"
        return {"ok": True}

    with (mock.patch.object(join_pair, "lan_ipv4", return_value=secret),
          mock.patch.object(join_pair, "advertise", side_effect=advertise)):
        assert join_pair.advertise_join_kit("test phrase", pin="123456")["ok"]
    assert capsys.readouterr().out == ordinary + "\n"
