"""khipu.usage http-quota adapter (slice 9b).

Hermetic: the HTTP call is faked at urllib.request.OpenerDirector.open
(or run against a local fake TLS server below), the key is a fake sentinel
from a faked keychain.get_password, and nothing reaches a real host,
keychain or credential file.
"""
from __future__ import annotations

import http.server
import io
import json
import socket
import ssl
import subprocess
import threading
import time
import urllib.error

import pytest

from khipu import keychain, usage

KEY = "SENTINEL-http-quota-key-do-not-leak"
ACCOUNT = {"label": "Synthetic", "harness": "synthetic-provider", "home": None,
           "probe": "http-quota", "host": "quota.invalid", "provider": "synthetic-provider",
           "quota_key": "synthetic:main"}
REPLY = {"windows": [{"used_pct": 42, "resets_at": 1_800_500_000, "window_mins": 10080},
                     {"percentage": 10, "reset_at": "2027-01-01T00:00:00Z", "window_mins": 300}]}


class _Response:
    def __init__(self, body: bytes, status: int = 200):
        self.status, self._body = status, io.BytesIO(body)

    def read(self, n=-1):
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake(monkeypatch):
    seen = []

    class Fake:
        reply = _Response(json.dumps(REPLY).encode())
        error = None

    def urlopen(self, request, timeout=None):  # noqa: ARG001 - OpenerDirector.open stand-in
        seen.append({"url": request.full_url, "headers": dict(request.header_items()), "timeout": timeout})
        if Fake.error is not None:
            raise Fake.error
        return Fake.reply

    monkeypatch.setattr(keychain, "get_password", lambda *a, **k: KEY)
    monkeypatch.setattr("urllib.request.OpenerDirector.open", urlopen)
    Fake.seen = seen
    return Fake


def _probe(**kw):
    return usage.probe_account(dict(ACCOUNT), **kw)


def test_windows_parse_and_key_goes_only_to_the_confirmed_host(fake):
    row = _probe()
    assert row["ok"] is True and row["source"] == "live"
    assert row["windows"] == [
        {"name": "weekly", "used_pct": 42, "resets_at": usage._iso(1_800_500_000), "window_mins": 10080},
        {"name": "five_hour", "used_pct": 10, "resets_at": "2027-01-01T00:00:00Z", "window_mins": 300}]
    assert len(fake.seen) == 1
    assert fake.seen[0]["url"] == "https://quota.invalid/"
    # Exact header form (P3-4): the bare key, no scheme — see _http_quota_request.
    assert fake.seen[0]["headers"]["Authorization"] == KEY
    assert 0 < fake.seen[0]["timeout"] <= usage.HTTP_QUOTA_TIMEOUT_S
    assert KEY not in json.dumps(row)


def test_glm_shaped_limits_parse(fake):
    fake.reply = _Response(json.dumps({"code": 200, "data": {"limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 15, "nextResetTime": 1_800_018_000_000},
        {"type": "TOKENS_LIMIT", "unit": 6, "percentage": 25, "nextResetTime": 1_800_500_000_000}]}}).encode())
    row = _probe()
    assert [w["used_pct"] for w in row["windows"]] == [15, 25]
    assert [w["name"] for w in row["windows"]] == ["five_hour", "weekly"]


def test_a_returned_redirect_is_refused_and_never_followed(fake):
    fake.reply = _Response(KEY.encode(), status=302)
    row = _probe()
    assert row["error"] == usage.E_REDIRECT == "redirect_refused"
    assert len(fake.seen) == 1 and KEY not in json.dumps(row)


def test_a_raised_redirect_is_refused_without_reading_its_body(fake):
    fake.error = urllib.error.HTTPError("https://quota.invalid/", 302, "found", {},
                                        io.BytesIO(KEY.encode()))
    row = _probe()
    assert row["error"] == usage.E_REDIRECT and KEY not in json.dumps(row)


def test_no_key_makes_no_request(fake, monkeypatch):
    monkeypatch.setattr(keychain, "get_password", lambda *a, **k: None)
    row = _probe()
    assert row["error"] == usage.E_NO_KEY and not fake.seen


def test_a_bad_host_makes_no_request(fake):
    row = usage.probe_account({**ACCOUNT, "host": "https://evil.invalid/path"})
    assert row["error"] == usage.E_INVALID and not fake.seen


def test_statuses_get_closed_codes_and_bodies_never_travel(fake):
    fake.reply = _Response(json.dumps({"msg": KEY}).encode(), status=401)
    assert _probe()["error"] == usage.E_BAD_KEY
    fake.reply = _Response(KEY.encode(), status=500)
    row = _probe()
    assert row["error"] == usage.E_CLI and KEY not in json.dumps(row)
    fake.error = RuntimeError(f"surprise {KEY}")
    row = _probe()
    assert row["error"] == usage.E_NO_ANSWER and KEY not in json.dumps(row)


def test_an_unreadable_or_empty_reply_is_a_closed_code(fake):
    fake.reply = _Response(b"<html>not json</html>")
    assert _probe()["error"] == usage.E_BAD
    fake.reply = _Response(json.dumps({"windows": []}).encode())
    assert _probe()["error"] == usage.E_NO_LIMITS


# ---- real local TLS socket (probe harness copied from the 9b review probes) --
#
# A fake self-signed server for quota.test / evil.test on 127.0.0.1, reached
# through a patched socket.create_connection and a test-only trust anchor.
# The key is still a fake sentinel from a faked keychain; no real host,
# keychain or credential file is touched.


class _QuotaServer:
    def __init__(self, tmp_path, monkeypatch):
        cert, key_file = tmp_path / "c.pem", tmp_path / "k.pem"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key_file), "-out", str(cert), "-days", "1",
                        "-subj", "/CN=quota.test",
                        "-addext", "subjectAltName=DNS:quota.test,DNS:evil.test"],
                       check=True, capture_output=True)
        self.mode = "ok"
        self.hits = []
        hits, server_ref = self.hits, self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                hits.append({"host": self.headers.get("Host"),
                             "auth": self.headers.get("Authorization"), "path": self.path})
                if server_ref.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", "https://evil.test/steal")
                    self.end_headers()
                    return
                if server_ref.mode == "drip":
                    self.send_response(200)
                    self.send_header("Content-Length", "100000")
                    self.end_headers()
                    for _ in range(50):  # one byte every 0.2 s: ~10 s in total
                        try:
                            self.wfile.write(b" ")
                            self.wfile.flush()
                        except OSError:
                            break
                        time.sleep(0.2)
                    return
                body = {
                    "ok": b'{"windows":[{"used_pct":5,"window_mins":300}]}',
                    "neg": b'{"windows":[{"used_pct":-5,"window_mins":300}]}',
                    "over": b'{"windows":[{"used_pct":250,"window_mins":300}]}',
                    "huge_mins": b'{"windows":[{"used_pct":5,"window_mins":99999999999999999999999}]}',
                    "deep": b"[" * 100000,
                }[server_ref.mode]
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key_file))
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        self.port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        real_create = socket.create_connection

        def create_connection(address, *args, **kwargs):
            if address[0] in ("quota.test", "evil.test"):
                address = ("127.0.0.1", self.port)
            return real_create(address, *args, **kwargs)

        monkeypatch.setattr(socket, "create_connection", create_connection)
        monkeypatch.setattr(ssl, "_create_default_https_context",
                            lambda: ssl.create_default_context(cafile=str(cert)))
        monkeypatch.setattr(keychain, "get_password", lambda *a, **k: KEY)
        for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY"):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture
def quota_server(tmp_path, monkeypatch):
    if subprocess.run(["which", "openssl"], capture_output=True).returncode:
        pytest.skip("openssl unavailable for the local TLS fixture")
    return _QuotaServer(tmp_path, monkeypatch)


SOCKET_ACCOUNT = {**ACCOUNT, "host": "quota.test"}


def _probe_socket(**kw):
    return usage.probe_account(dict(SOCKET_ACCOUNT), **kw)


def test_real_socket_ok_and_authorization_is_the_bare_key(quota_server):
    row = _probe_socket()
    assert row["ok"] is True
    assert [w["used_pct"] for w in row["windows"]] == [5]
    # P3-4: the exact form on the wire is ``Authorization: <key>`` — the raw
    # key, no scheme (no ``Bearer``), matching the z.ai-style quota APIs.
    assert quota_server.hits == [{"host": "quota.test", "auth": KEY, "path": "/"}]
    assert KEY not in json.dumps(row)


def test_real_socket_redirect_to_another_host_is_refused(quota_server):
    quota_server.mode = "redirect"
    row = _probe_socket()
    assert row["error"] == usage.E_REDIRECT
    assert [h["host"] for h in quota_server.hits] == ["quota.test"]


def test_proxy_env_is_ignored_only_the_confirmed_host_is_contacted(quota_server, monkeypatch):
    seen = []
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    listener.settimeout(10)

    def accept_once():
        try:
            conn, _ = listener.accept()
            seen.append(conn.recv(4096))
            conn.close()
        except OSError:
            pass

    threading.Thread(target=accept_once, daemon=True).start()
    proxy = f"http://127.0.0.1:{listener.getsockname()[1]}"
    monkeypatch.setenv("https_proxy", proxy)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    row = _probe_socket()
    assert row["ok"] is True
    assert seen == []
    assert [h["host"] for h in quota_server.hits] == ["quota.test"]


def test_slow_drip_hits_the_overall_deadline_and_times_out(quota_server):
    quota_server.mode = "drip"
    started = time.monotonic()
    row = _probe_socket(timeout=2)
    elapsed = time.monotonic() - started
    assert row["error"] == usage.E_TIMEOUT
    assert elapsed < 6


@pytest.mark.parametrize("mode", ["neg", "over", "huge_mins"])
def test_out_of_range_figures_are_a_bad_reply(quota_server, mode):
    quota_server.mode = mode
    row = _probe_socket()
    assert row["ok"] is False and row["error"] == usage.E_BAD


def test_deeply_nested_body_is_a_bad_reply_never_a_recursion_error(quota_server):
    quota_server.mode = "deep"
    row = _probe_socket()
    assert row["ok"] is False and row["error"] == usage.E_BAD
