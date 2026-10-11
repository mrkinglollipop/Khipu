"""Passphrase-encrypted join kit export/import for multi-Mac hub pairing."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg.conninfo import conninfo_to_dict, make_conninfo

FORMAT_VERSION = "khipu-join-v1"
_PBKDF2_ITERATIONS = 390_000
_LOCALHOST_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_localhost_dsn(dsn: str) -> bool:
    try:
        host = str(conninfo_to_dict(dsn).get("host") or "").strip().lower()
    except Exception:
        host = (urlsplit(dsn).hostname or "").strip().lower()
    return host in _LOCALHOST_HOSTS


def rewrite_dsn_sslrootcert(dsn: str, cert_path: str) -> str:
    """Point ``sslrootcert`` at ``cert_path`` (percent-encoded for libpq URIs)."""
    cert = str(Path(cert_path).expanduser().resolve())
    if "://" not in dsn:
        params = {k: v for k, v in conninfo_to_dict(dsn).items() if v is not None}
        params["sslrootcert"] = cert
        return make_conninfo(**params)
    parts = urlsplit(dsn)
    query = parse_qs(parts.query, keep_blank_values=True)
    # Absolute path — relative / foreign-Mac paths blow up on the joining machine.
    query["sslrootcert"] = [cert]
    flat = {key: values[-1] for key, values in query.items()}
    # Percent-encode '/' in the *stored* URI for doctor/file health. Connect
    # still must not use this string as-is — see db.conninfo_with_local_root_cert
    # (URI query paths have arrived at TLS as sslrootcert=/Users/matthewsc).
    new_query = urlencode(flat, quote_via=quote)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
    )


def dsn_requires_sslrootcert(dsn: str) -> bool:
    try:
        mode = str(conninfo_to_dict(dsn).get("sslmode") or "").lower()
    except Exception:
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

        params = conninfo_to_dict(resolve_dsn())
        host = params.get("host") or "unknown-host"
        port = params.get("port") or 5432
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
    write_private_file(dest, blob)
    return dest


def write_private_file(path: Path, data: bytes) -> Path:
    """Atomically replace a secret file without a permissive creation window."""
    dest = Path(path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
        return dest
    except Exception:
        try:
            if fd >= 0:
                os.close(fd)
        except OSError:
            pass
        tmp.unlink(missing_ok=True)
        raise


def _write_dsn_file(dsn: str) -> None:
    from khipu.paths import dsn_file, ensure_data_dir

    ensure_data_dir()
    path = dsn_file()
    write_private_file(path, (dsn.strip() + "\n").encode("utf-8"))


def _file_snapshot(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _restore_file(path: Path, snapshot: bytes | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        write_private_file(path, snapshot)


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
    try:
        host = conninfo_to_dict(str(payload.get("database_url") or "")).get("host")
    except Exception:
        host = urlsplit(str(payload.get("database_url") or "")).hostname
    if host:
        out["database_host"] = host
    return out


def import_kit(blob: bytes, passphrase: str) -> dict[str, Any]:
    from khipu import config as config_mod
    from khipu import keychain
    from khipu.config import CAPTURE_MODES, set_capture_mode, set_gateway_url
    from khipu.keychain import set_dsn, set_gemini_key, set_openai_compat_key
    from khipu.models import set_models_replace, validate_models_object
    from khipu.paths import dsn_file, ensure_data_dir, root_cert_file

    payload = decrypt_payload(blob, passphrase)
    dsn = str(payload.get("database_url") or "").strip()
    if not dsn:
        raise ValueError("join kit is missing database_url")
    if "\n" in dsn or "\r" in dsn:
        raise ValueError("database_url must not contain a newline")
    if is_localhost_dsn(dsn):
        raise ValueError(
            "join kit database_url points at localhost — that database lives only on "
            "the exporting Mac; use a reachable hub host (Tailscale, VPN, or shared server)"
        )
    mode = str(payload.get("capture_mode") or "").strip().lower()
    if mode and mode not in CAPTURE_MODES:
        raise ValueError(f"capture_mode must be one of {CAPTURE_MODES}, got {mode!r}")

    # Validate the entire owned payload before changing a file or credential.
    conninfo_to_dict(dsn)
    gateway = payload.get("gateway_url")
    if gateway is not None and not isinstance(gateway, str):
        raise ValueError("gateway_url must be a string")
    gateway_text = gateway.strip().rstrip("/") if isinstance(gateway, str) else ""
    if gateway_text and not gateway_text.startswith("https://"):
        raise ValueError("gateway_url must be an https:// URL (it carries a bearer token)")
    models = payload.get("models")
    if models is not None:
        if not isinstance(models, dict) or not models:
            raise ValueError("models must be a non-empty JSON object")
        validate_models_object(models, for_set=True)
    for field in ("gemini_api_key", "openai_compat_api_key"):
        secret = payload.get(field)
        if secret is not None and not isinstance(secret, str):
            raise ValueError(f"{field} must be a string")
        if isinstance(secret, str) and ("\n" in secret or "\r" in secret):
            raise ValueError(f"{field} must not contain a newline")
    # Every config/model setter shares this document. Refuse existing corrupt
    # bytes before certificate, DSN, or Keychain mutation begins.
    existing_config = config_mod._load_config_for_mutation()
    if isinstance(models, dict) and models and "models" in existing_config:
        validate_models_object(existing_config["models"], for_set=False)

    ensure_data_dir()
    cert_path = root_cert_file()
    dsn_path = dsn_file()
    config_path = config_mod.config_file()
    root_pem = payload.get("root_crt_pem")
    if root_pem is not None and not isinstance(root_pem, str):
        raise ValueError("root_crt_pem must be a string")
    if isinstance(root_pem, str) and not root_pem.strip():
        raise ValueError("root_crt_pem must not be empty")
    if not (isinstance(root_pem, str) and root_pem.strip()) and dsn_requires_sslrootcert(dsn) and not cert_path.is_file():
        raise ValueError(
            "join kit is missing the hub TLS root certificate (root_crt_pem). "
            "On the working Mac confirm Settings data folder has root.crt "
            "(usually ~/.config/khipu/root.crt), export the join kit again, "
            "then re-import on this Mac."
        )

    snapshots = {
        dsn_path: _file_snapshot(dsn_path),
        cert_path: _file_snapshot(cert_path),
        config_path: _file_snapshot(config_path),
    }
    credential_updates = [(keychain.DSN_ACCOUNT, set_dsn, None)]
    for field, account, setter in (
        ("gemini_api_key", keychain.GEMINI_ACCOUNT, set_gemini_key),
        ("openai_compat_api_key", keychain.OPENAI_COMPAT_ACCOUNT, set_openai_compat_key),
    ):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            credential_updates.append((account, setter, value.strip()))
    credentials = {}
    if keychain.keychain_available():
        for account, _, _ in credential_updates:
            receipt = keychain._security("find-generic-password", "-s", keychain.SERVICE,
                                         "-a", account, "-w")
            if receipt.returncode not in (0, 44):
                raise RuntimeError(f"cannot snapshot join credential {account}")
            credentials[account] = receipt.stdout.strip() if receipt.returncode == 0 else None
    modified_credentials = []
    try:
        if isinstance(root_pem, str) and root_pem.strip():
            write_private_file(cert_path, (root_pem.strip() + "\n").encode("utf-8"))
        # Always retarget sslrootcert to THIS Mac — never keep the exporter's path.
        if cert_path.is_file():
            dsn = rewrite_dsn_sslrootcert(dsn, str(cert_path))
        elif conninfo_to_dict(dsn).get("sslrootcert"):
            raise ValueError(f"sslrootcert is set but {cert_path} was not written — cannot join")

        for account, setter, value in credential_updates:
            if account in credentials:
                modified_credentials.append(account)
            setter(dsn if account == keychain.DSN_ACCOUNT else value)
        _write_dsn_file(dsn)
        if mode:
            set_capture_mode(mode)
        if isinstance(models, dict) and models:
            set_models_replace(models)
        if gateway_text:
            set_gateway_url(gateway_text)
    except Exception as exc:
        rollback_errors = []
        for account in reversed(modified_credentials):
            try:
                old = credentials[account]
                if old is not None:
                    keychain.set_password(account, old)
                else:
                    receipt = keychain._security("delete-generic-password", "-s", keychain.SERVICE,
                                                 "-a", account)
                    if receipt.returncode not in (0, 44):
                        raise RuntimeError("credential deletion failed")
            except Exception:
                rollback_errors.append(f"credential {account}")
        for path, snapshot in snapshots.items():
            try:
                _restore_file(path, snapshot)
            except OSError:
                rollback_errors.append(f"file {path.name}")
        if rollback_errors:
            raise RuntimeError("join import failed; rollback failed for " + ", ".join(rollback_errors)) from exc
        raise

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
