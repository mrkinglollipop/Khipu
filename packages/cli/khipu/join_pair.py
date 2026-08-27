"""Nearby-Mac join transfer over Bonjour + TLS (same payload as .khipujoin).

Mac 1 advertises ``_khipu-join._tcp`` via ``dns-sd -R``, runs a short-lived TLS
server, and sends the encrypted join kit after the client proves a 6-digit PIN.

Mac 2 browses, connects (accepts the ephemeral self-signed cert for this
window only), sends the PIN, and receives the kit bytes for ``import_kit``.

Pairing is not a Postgres tunnel. Noise is out of scope — TLS only.
"""

from __future__ import annotations

import json
import secrets
import socket
import ssl
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SERVICE_TYPE = "_khipu-join._tcp"
DEFAULT_TIMEOUT_S = 600
DEFAULT_PORT = 8788
_PROTOCOL = "khipu-join-pair-v1"


def generate_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _self_signed_cert_dir() -> Path:
    """Create a throwaway cert+key with cryptography (already a CLI dep)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "khipu-join-pair")]
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    td = Path(tempfile.mkdtemp(prefix="khipu-join-tls-"))
    (td / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    (td / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return td


def _readline(conn: ssl.SSLSocket, limit: int = 65536) -> bytes:
    buf = b""
    while b"\n" not in buf and len(buf) < limit:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf += chunk
    return buf


def advertise(
    passphrase: str,
    *,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    port: int = DEFAULT_PORT,
    pin: str | None = None,
) -> dict[str, Any]:
    """Export kit, advertise Bonjour, serve once over TLS. Blocks until transfer or timeout."""
    from khipu.join import export_kit

    pin = pin or generate_pin()
    if len(pin) != 6 or not pin.isdigit():
        raise ValueError("PIN must be exactly 6 digits")
    kit = export_kit(passphrase)
    cert_dir = _self_signed_cert_dir()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        certfile=str(cert_dir / "cert.pem"), keyfile=str(cert_dir / "key.pem")
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", port))
    sock.listen(1)
    sock.settimeout(1.0)

    hostname = socket.gethostname().split(".")[0] or "khipu"
    dns = subprocess.Popen(
        [
            "dns-sd",
            "-R",
            f"Khipu Join ({hostname})",
            SERVICE_TYPE,
            "local.",
            str(port),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + max(30, int(timeout_s))
    transferred = False
    error: str | None = None
    try:
        while time.time() < deadline and not transferred:
            try:
                client, addr = sock.accept()
            except socket.timeout:
                continue
            try:
                tls = ctx.wrap_socket(client, server_side=True)
                line = _readline(tls).decode("utf-8", errors="replace").strip()
                req = json.loads(line) if line else {}
                if req.get("protocol") != _PROTOCOL:
                    tls.sendall(b'{"ok":false,"error":"bad protocol"}\n')
                    tls.close()
                    continue
                if str(req.get("pin") or "") != pin:
                    tls.sendall(b'{"ok":false,"error":"bad pin"}\n')
                    tls.close()
                    continue
                header = json.dumps(
                    {"ok": True, "protocol": _PROTOCOL, "bytes": len(kit)}
                ).encode("utf-8")
                tls.sendall(header + b"\n")
                tls.sendall(kit)
                transferred = True
                tls.close()
            except Exception as exc:  # noqa: BLE001
                error = f"{type(exc).__name__}: {exc}"
                try:
                    client.close()
                except OSError:
                    pass
    finally:
        dns.terminate()
        try:
            dns.wait(timeout=2)
        except subprocess.TimeoutExpired:
            dns.kill()
        sock.close()
        for p in cert_dir.iterdir():
            p.unlink(missing_ok=True)
        cert_dir.rmdir()

    return {
        "ok": transferred,
        "pin": pin,
        "port": port,
        "timeout_s": timeout_s,
        "bytes": len(kit) if transferred else 0,
        "error": None
        if transferred
        else (error or "timed out waiting for a nearby Mac"),
    }


def _browse_service(timeout_s: float = 8.0) -> list[dict[str, str]]:
    """Best-effort parse of ``dns-sd -B`` lines for instance names."""
    proc = subprocess.Popen(
        ["dns-sd", "-B", SERVICE_TYPE, "local."],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    found: list[dict[str, str]] = []
    deadline = time.time() + timeout_s
    try:
        assert proc.stdout is not None
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            # Example: TIMESTAMP ... Add  2  4 local.  _khipu-join._tcp.  Khipu Join (mbp)
            if "Add" in line and SERVICE_TYPE.split(".")[0] in line:
                parts = line.split()
                if len(parts) >= 7:
                    name = " ".join(parts[6:]).strip()
                    if name and not any(f["name"] == name for f in found):
                        found.append({"name": name})
            if found:
                break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    return found


def _resolve_host_port(instance: str, timeout_s: float = 6.0) -> tuple[str, int]:
    proc = subprocess.Popen(
        ["dns-sd", "-L", instance, SERVICE_TYPE, "local."],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.time() + timeout_s
    host: str | None = None
    port: int | None = None
    try:
        assert proc.stdout is not None
        while time.time() < deadline and (host is None or port is None):
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            # "... can be reached at mbp.local.:8788 ..."
            if "can be reached at" in line:
                after = line.split("can be reached at", 1)[1].strip()
                target = after.split()[0]
                if ":" in target:
                    h, p = target.rsplit(":", 1)
                    host = h.rstrip(".")
                    port = int(p)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not host or not port:
        raise RuntimeError(
            f"could not resolve Bonjour instance {instance!r}; "
            "try AirDrop / file import instead"
        )
    return host, port


def receive(
    passphrase: str,
    pin: str,
    *,
    host: str | None = None,
    port: int | None = None,
    out_path: Path | None = None,
    apply_import: bool = True,
) -> dict[str, Any]:
    """Connect to an advertising Mac, pull kit, optionally import."""
    from khipu.join import import_kit, verify_live_counts

    pin = str(pin).strip()
    if len(pin) != 6 or not pin.isdigit():
        raise ValueError("PIN must be exactly 6 digits")

    if host is None or port is None:
        services = _browse_service()
        if not services:
            raise RuntimeError(
                "no nearby Khipu join advertiser found — "
                "confirm the other Mac is advertising, or use a .khipujoin file"
            )
        host, port = _resolve_host_port(services[0]["name"])

    raw = socket.create_connection((host, int(port)), timeout=15)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # ephemeral pairing cert only
    tls = ctx.wrap_socket(raw, server_hostname=host)
    req = json.dumps({"protocol": _PROTOCOL, "pin": pin}).encode("utf-8") + b"\n"
    tls.sendall(req)
    header_line = _readline(tls).decode("utf-8", errors="replace").strip()
    header = json.loads(header_line) if header_line else {}
    if not header.get("ok"):
        tls.close()
        raise RuntimeError(header.get("error") or "nearby Mac refused the PIN")
    nbytes = int(header.get("bytes") or 0)
    chunks: list[bytes] = []
    got = 0
    while got < nbytes:
        chunk = tls.recv(min(65536, nbytes - got))
        if not chunk:
            break
        chunks.append(chunk)
        got += len(chunk)
    tls.close()
    blob = b"".join(chunks)
    if len(blob) != nbytes:
        raise RuntimeError(f"incomplete kit transfer ({len(blob)}/{nbytes} bytes)")

    path_written: str | None = None
    if out_path is not None:
        out_path = Path(out_path).expanduser()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(blob)
        path_written = str(out_path)

    result: dict[str, Any] = {
        "ok": True,
        "bytes": len(blob),
        "host": host,
        "port": port,
        "path": path_written,
    }
    if apply_import:
        summary = import_kit(blob, passphrase)
        counts = verify_live_counts(summary.get("expected") or {})
        result["summary"] = summary
        result["counts"] = counts
        result["ok"] = bool(counts.get("ok"))
        if counts.get("error"):
            result["error"] = counts["error"]
        elif not result["ok"] and counts.get("mismatches"):
            result["error"] = "; ".join(counts["mismatches"])
    return result


def advertise_join_kit(
    passphrase: str,
    *,
    timeout: int = DEFAULT_TIMEOUT_S,
    pin: str | None = None,
) -> dict[str, Any]:
    """CLI/Tauri entry: emit PIN on stdout, then block until transfer or timeout."""
    pin = (pin or "").strip() or generate_pin()
    banner = {
        "ok": True,
        "phase": "advertising",
        "pin": pin,
        "port": DEFAULT_PORT,
        "timeout_sec": timeout,
        "service": SERVICE_TYPE,
        "dns_sd": "dns-sd is best-effort on macOS; use file export if browse fails",
    }
    print(json.dumps(banner), flush=True)
    out = advertise(passphrase, timeout_s=timeout, pin=pin)
    out["phase"] = "finished"
    return out


def receive_join_kit(
    passphrase: str,
    pin: str,
    *,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """CLI/Tauri alias for :func:`receive` with import applied."""
    return receive(passphrase, pin, out_path=out_path, apply_import=True)
