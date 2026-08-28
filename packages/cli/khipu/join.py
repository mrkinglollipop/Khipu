"""Passphrase-encrypted join kit export/import for multi-Mac hub pairing."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT_VERSION = "khipu-join-v1"
_PBKDF2_ITERATIONS = 390_000
_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_localhost_dsn(dsn: str) -> bool:
    host = (urlsplit(dsn).hostname or "").strip().lower()
    return host in _LOCALHOST_HOSTS


def rewrite_dsn_sslrootcert(dsn: str, cert_path: str) -> str:
    """Point ``sslrootcert`` at ``cert_path`` (percent-encoded for libpq URIs)."""
    parts = urlsplit(dsn)
    query = parse_qs(parts.query, keep_blank_values=True)
    # Absolute path — relative / foreign-Mac paths blow up on the joining machine.
    query["sslrootcert"] = [str(Path(cert_path).expanduser().resolve())]
    flat = {key: values[-1] for key, values in query.items()}
    # Percent-encode '/' in the *stored* URI for doctor/file health. Connect
    # still must not use this string as-is — see db.conninfo_with_local_root_cert
    # (URI query paths have arrived at TLS as sslrootcert=/Users/matthewsc).
    new_query = urlencode(flat, quote_via=quote)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def dsn_requires_sslrootcert(dsn: str) -> bool:
    mode = (parse_qs(urlsplit(dsn).query).get("sslmode") or [""])[0].lower()
    return mode.startswith("verify")


def classify_hub_connect_error(exc: BaseException, loc: str) -> str:
    """Distinguish TLS/cert misconfig from real network unreachability."""
    text = str(exc)
    low = text.lower()
    # Do not match generic "ssl " — libpq "SSL SYSCALL error: … timed out"
    # is a network failure, not a missing root.crt.
    certish = (
        "root certificate" in low
        or "sslrootcert" in low
        or "certificate file" in low
        or "certificate verify" in low
        or "unknown ca" in low
        or "self signed certificate" in low
        or "certificate has expired" in low
    )
    if certish:
        return (
            f"TLS/certificate problem talking to {loc}: {text}. "
            "The hub network path may be fine — this Mac needs a valid local "
            "root.crt and sslrootcert in the DSN. Re-export the join kit from the "
            "working Mac (Settings → Set up another Mac) so the certificate is "
            "included, then import again. This is not a Tailscale routing failure."
        )
    return (
        f"{loc} unreachable ({type(exc).__name__}: {text}) — "
        "the hub must be reachable (Tailscale, VPN, SSH tunnel, or shared server); "
        "this is not a join-kit passphrase problem"
    )


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        passphrase.encode("utf-8"),
        salt,
        _PBKDF2_ITERATIONS,
        dklen=32,
    )


def encrypt_payload(payload: dict[str, Any], passphrase: str) -> bytes:
    if not passphrase.strip():
        raise ValueError("passphrase is required to encrypt a join kit")
    salt = secrets.token_bytes(16)
    nonce = secrets.token_bytes(12)
    key = _derive_key(passphrase, salt)
    plaintext = json.dumps(payload, sort_keys=True).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    envelope = {
        "v": FORMAT_VERSION,
        "salt_b64": base64.b64encode(salt).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(envelope).encode("utf-8")


def decrypt_payload(blob: bytes, passphrase: str = "") -> dict[str, Any]:
    try:
        envelope = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("join kit is not valid JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("join kit must be a JSON object")
    # AirDrop / file kits may be plaintext (the file is the secret).
    if envelope.get("format") == FORMAT_VERSION and "database_url" in envelope:
        return envelope
    if envelope.get("v") != FORMAT_VERSION:
        raise ValueError(f"unsupported join kit version: {envelope.get('v')!r}")
    if "ciphertext_b64" not in envelope:
        raise ValueError("join kit envelope is malformed")
    if not passphrase.strip():
        raise ValueError(
            "this join kit is passphrase-protected — enter the phrase you typed "
            "when saving it, or save a new kit without a passphrase"
        )
    try:
        salt = base64.b64decode(envelope["salt_b64"])
        nonce = base64.b64decode(envelope["nonce_b64"])
        ciphertext = base64.b64decode(envelope["ciphertext_b64"])
    except (KeyError, ValueError) as exc:
        raise ValueError("join kit envelope is malformed") from exc
    key = _derive_key(passphrase.strip(), salt)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    except Exception as exc:
        raise ValueError("passphrase incorrect or join kit is corrupt") from exc
    payload = json.loads(plaintext.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("join kit payload must be a JSON object")
    return payload


def _dsn_host_port() -> str:
    try:
        from khipu.db import resolve_dsn

        parts = urlsplit(resolve_dsn())
        host = parts.hostname or "unknown-host"
        port = parts.port or 5432
        return f"{host}:{port}"
    except Exception:
        return "hub"


def _fetch_live_counts() -> dict[str, int]:
    from khipu.db import connect

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM episodes")
            episodes = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM topics WHERE deleted_at IS NULL")
            topics = int(cur.fetchone()[0])
            cur.execute("SELECT COUNT(*) FROM nodes")
            nodes = int(cur.fetchone()[0])
    return {"episodes": episodes, "topics": topics, "nodes": nodes}


def _models_export_blob() -> dict[str, Any]:
    from khipu.models import show_models

    shown = show_models()
    return {
        "synth": dict(shown["synth"]),
        "embed": dict(shown["embed"]),
        "vision": dict(shown["vision"]),
    }


def export_kit(passphrase: str) -> bytes:
    from khipu.config import capture_mode, gateway_url
    from khipu.keychain import get_dsn, get_gemini_key, get_openai_compat_key
    from khipu.paths import root_cert_file

    dsn = get_dsn()
    if not dsn:
        raise RuntimeError(
            "No database URL in Keychain. Configure the hub DSN before exporting a join kit."
        )
    cert_path = root_cert_file()
    root_crt_pem: str | None = None
    if cert_path.is_file():
        root_crt_pem = cert_path.read_text(encoding="utf-8")
    payload: dict[str, Any] = {
        "format": FORMAT_VERSION,
        "database_url": dsn,
        "capture_mode": capture_mode(),
        "models": _models_export_blob(),
        "expected": _fetch_live_counts(),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    gateway = gateway_url()
    if gateway:
        payload["gateway_url"] = gateway
    gemini = get_gemini_key()
    if gemini:
        payload["gemini_api_key"] = gemini
    openai_compat = get_openai_compat_key()
    if openai_compat:
        payload["openai_compat_api_key"] = openai_compat
    if root_crt_pem:
        payload["root_crt_pem"] = root_crt_pem
    phrase = (passphrase or "").strip()
    if phrase:
        return encrypt_payload(payload, phrase)
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def write_kit_file(path: Path, blob: bytes) -> Path:
    """Write a join kit and restrict it to owner read/write.

    Plaintext kits hold the DSN and API keys; default umask would leave them
    world-readable on the Desktop.
    """
    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    dest.chmod(0o600)
    return dest


def _write_dsn_file(dsn: str) -> None:
    from khipu.paths import dsn_file, ensure_data_dir

    ensure_data_dir()
    path = dsn_file()
    path.write_text(dsn.strip() + "\n", encoding="utf-8")
    path.chmod(0o600)


def _sanitize_summary(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "format": payload.get("format", FORMAT_VERSION),
        "capture_mode": payload.get("capture_mode"),
        "expected": dict(payload.get("expected") or {}),
        "created_at": payload.get("created_at"),
        "has_gemini_api_key": bool(payload.get("gemini_api_key")),
        "has_openai_compat_api_key": bool(payload.get("openai_compat_api_key")),
        "has_root_crt_pem": bool(payload.get("root_crt_pem")),
    }
    gateway = payload.get("gateway_url")
    if gateway:
        out["gateway_url"] = gateway
    host = urlsplit(str(payload.get("database_url") or "")).hostname
    if host:
        out["database_host"] = host
    return out


def import_kit(blob: bytes, passphrase: str) -> dict[str, Any]:
    from khipu.config import set_capture_mode, set_gateway_url
    from khipu.keychain import set_dsn, set_gemini_key, set_openai_compat_key
    from khipu.models import set_models_replace
    from khipu.paths import ensure_data_dir, root_cert_file

    payload = decrypt_payload(blob, passphrase)
    dsn = str(payload.get("database_url") or "").strip()
    if not dsn:
        raise ValueError("join kit is missing database_url")
    if is_localhost_dsn(dsn):
        raise ValueError(
            "join kit database_url points at localhost — that database lives only on "
            "the exporting Mac; use a reachable hub host (Tailscale, VPN, or shared server)"
        )

    ensure_data_dir()
    cert_path = root_cert_file()
    root_pem = payload.get("root_crt_pem")
    if isinstance(root_pem, str) and root_pem.strip():
        cert_path.write_text(root_pem.strip() + "\n", encoding="utf-8")
        try:
            cert_path.chmod(0o600)
        except OSError:
            pass
    elif dsn_requires_sslrootcert(dsn):
        raise ValueError(
            "join kit is missing the hub TLS root certificate (root_crt_pem). "
            "On the working Mac confirm Settings data folder has root.crt "
            "(usually ~/.config/khipu/root.crt), export the join kit again, "
            "then re-import on this Mac."
        )
    # Always retarget sslrootcert to THIS Mac — never keep the exporter's path.
    if cert_path.is_file():
        dsn = rewrite_dsn_sslrootcert(dsn, str(cert_path))
    elif "sslrootcert" in parse_qs(urlsplit(dsn).query):
        raise ValueError(
            f"sslrootcert is set but {cert_path} was not written — cannot join"
        )

    set_dsn(dsn)
    _write_dsn_file(dsn)

    gemini = payload.get("gemini_api_key")
    if isinstance(gemini, str) and gemini.strip():
        set_gemini_key(gemini.strip())
    openai_compat = payload.get("openai_compat_api_key")
    if isinstance(openai_compat, str) and openai_compat.strip():
        set_openai_compat_key(openai_compat.strip())

    mode = str(payload.get("capture_mode") or "").strip().lower()
    if mode:
        set_capture_mode(mode)

    models = payload.get("models")
    if isinstance(models, dict) and models:
        set_models_replace(models)

    gateway = payload.get("gateway_url")
    if isinstance(gateway, str) and gateway.strip():
        set_gateway_url(gateway.strip())

    return _sanitize_summary(payload)


def verify_live_counts(expected: dict[str, int]) -> dict[str, Any]:
    exp = {
        "episodes": int(expected.get("episodes") or 0),
        "topics": int(expected.get("topics") or 0),
        "nodes": int(expected.get("nodes") or 0),
    }
    try:
        live = _fetch_live_counts()
    except Exception as exc:
        loc = _dsn_host_port()
        empty = {"episodes": 0, "topics": 0, "nodes": 0}
        return {
            "ok": False,
            "expected": exp,
            "live": empty,
            "mismatches": [
                f"{key}: expected {exp[key]}, got 0"
                for key in ("episodes", "topics", "nodes")
            ],
            "error": classify_hub_connect_error(exc, loc),
        }
    mismatches: list[str] = []
    for key in ("episodes", "topics", "nodes"):
        if live[key] != exp[key]:
            mismatches.append(f"{key}: expected {exp[key]}, got {live[key]}")
    # Hard stop is expected N, got 0 — other deltas are reported, not fatal.
    empty_when_expected = [
        f"{key}: expected {exp[key]}, got 0"
        for key in ("episodes", "topics", "nodes")
        if exp[key] > 0 and live[key] == 0
    ]
    ok = not empty_when_expected
    if empty_when_expected:
        mismatches = empty_when_expected + [
            m for m in mismatches if m not in empty_when_expected
        ]
    return {"ok": ok, "expected": exp, "live": live, "mismatches": mismatches}


def resolve_passphrase(arg: str | None) -> str:
    """Optional. Empty means a plaintext join kit (file/AirDrop is the secret)."""
    return (arg or os.environ.get("KHIPU_JOIN_PASSPHRASE") or "").strip()
