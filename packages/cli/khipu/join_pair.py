"""Nearby-Mac join transfer over Bonjour + TLS (same payload as .khipujoin).

Mac 1 advertises ``_khipu-join._tcp`` via ``dns-sd -R``, runs a short-lived TLS
server, and sends the encrypted join kit after the client proves a 6-digit PIN.

Mac 2 browses, connects (accepts the ephemeral self-signed cert for this
window only), sends the PIN, and receives the kit bytes for ``import_kit``.

Pairing is not a Postgres tunnel. Noise is out of scope — TLS only.

The advertiser publishes its LAN IPv4 in a Bonjour TXT record (``ipv4=…``).
The receiver prefers that address, then other IPv4 results from
``getaddrinfo``, before falling back to the ``.local`` hostname — so we avoid
the common ``EHOSTUNREACH`` / errno 65 failure when ``.local`` resolves to a
dead or link-local face.
"""

from __future__ import annotations

import errno
import json
import re
import secrets
import select
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
_IPV4_TXT_RE = re.compile(r"\bipv4=(\d{1,3}(?:\.\d{1,3}){3})\b")
_TXT_IPV4_GRACE_S = 0.5
_CONNECT_TRY_S = 3.0
_CONNECT_LAST_S = 15.0


def generate_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def usable_lan_ipv4(ip: str | None) -> str | None:
    """Unicast LAN IPv4, or None (loopback / link-local / Tailscale CGNAT)."""
    if not ip:
        return None
    text = ip.strip().rstrip(".")
    parts = text.split(".")
    if len(parts) != 4:
        return None
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 or n > 255 for n in nums):
        return None
    a, b = nums[0], nums[1]
    if a == 0 or a == 127 or a >= 224:
        return None
    if a == 169 and b == 254:
        return None
    # 100.64.0.0/10 — Tailscale (and CGNAT). Nearby join needs Wi‑Fi LAN.
    if a == 100 and 64 <= b <= 127:
        return None
    return text


def lan_ipv4() -> str | None:
    """Best-effort primary LAN IPv4 (outbound route), excluding loopback."""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No packets are sent; this only selects a route.
            probe.connect(("8.8.8.8", 80))
            ip = probe.getsockname()[0]
        finally:
            probe.close()
    except OSError:
        ip = None
    got = usable_lan_ipv4(ip)
    if got:
        return got
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            cand = usable_lan_ipv4(info[4][0])
            if cand:
                return cand
    except OSError:
        pass
    return None


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
    ipv4 = lan_ipv4()
    register_cmd = [
        "dns-sd",
        "-R",
        f"Khipu Join ({hostname})",
        SERVICE_TYPE,
        "local.",
        str(port),
    ]
    if ipv4:
        register_cmd.append(f"ipv4={ipv4}")
    dns = subprocess.Popen(
        register_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + max(30, int(timeout_s))
    transferred = False
    error: str | None = None
    try:
        while time.time() < deadline and not transferred:
            try:
                client, _addr = sock.accept()
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
        "ipv4": ipv4,
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


def _parse_dns_sd_resolve_line(
    line: str,
    host: str | None,
    port: int | None,
    ipv4: str | None,
) -> tuple[str | None, int | None, str | None]:
    # "... can be reached at mbp.local.:8788 ..."
    if "can be reached at" in line:
        after = line.split("can be reached at", 1)[1].strip()
        target = after.split()[0] if after else ""
        if ":" in target:
            h, p = target.rsplit(":", 1)
            try:
                port = int(p)
            except ValueError:
                pass
            else:
                host = h.rstrip(".")
    match = _IPV4_TXT_RE.search(line)
    if match:
        parsed = usable_lan_ipv4(match.group(1))
        if parsed:
            ipv4 = parsed
    return host, port, ipv4


def _read_txt_ipv4_grace(stdout: Any, grace_s: float) -> str | None:
    """Read extra dns-sd lines briefly for TXT ipv4 without blocking forever."""
    grace_end = time.time() + max(0.0, grace_s)
    while time.time() < grace_end:
        wait = grace_end - time.time()
        try:
            ready, _, _ = select.select([stdout], [], [], wait)
        except (ValueError, OSError, TypeError):
            return None
        if not ready:
            return None
        extra = stdout.readline()
        if not extra:
            return None
        match = _IPV4_TXT_RE.search(extra)
        if match:
            parsed = usable_lan_ipv4(match.group(1))
            if parsed:
                return parsed
    return None


def _resolve_service(
    instance: str, timeout_s: float = 6.0
) -> tuple[str, int, str | None]:
    """Return (hostname, port, optional ipv4 from TXT)."""
    proc = subprocess.Popen(
        ["dns-sd", "-L", instance, SERVICE_TYPE, "local."],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    deadline = time.time() + timeout_s
    host: str | None = None
    port: int | None = None
    ipv4: str | None = None
    try:
        assert proc.stdout is not None
        # Wait for SRV (host:port). TXT ipv4 is optional — do not block the
        # full timeout (or hang in readline) when the advertiser omitted it.
        while time.time() < deadline and (host is None or port is None):
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            host, port, ipv4 = _parse_dns_sd_resolve_line(line, host, port, ipv4)
            if host is not None and port is not None:
                break
        if host is not None and port is not None and ipv4 is None:
            ipv4 = _read_txt_ipv4_grace(proc.stdout, _TXT_IPV4_GRACE_S)
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
    return host, port, ipv4


def connect_candidates(
    host: str, port: int, preferred_ipv4: str | None = None
) -> list[tuple[str, int]]:
    """Ordered TCP targets: TXT IPv4, then AF_INET getaddrinfo, then hostname."""
    ordered: list[tuple[str, int]] = []
    seen: set[str] = set()

    def add(target: str) -> None:
        key = target.strip().rstrip(".")
        if not key or key in seen:
            return
        seen.add(key)
        ordered.append((key, int(port)))

    preferred = usable_lan_ipv4(preferred_ipv4)
    if preferred:
        add(preferred)
    try:
        for info in socket.getaddrinfo(
            host, int(port), socket.AF_INET, socket.SOCK_STREAM
        ):
            cand = usable_lan_ipv4(info[4][0])
            if cand:
                add(cand)
    except OSError:
        pass
    add(host)
    return ordered


def friendly_connect_error(exc: BaseException, tried: list[tuple[str, int]]) -> str:
    """Human message for LAN join connect failures (esp. errno 65)."""
    targets = ", ".join(f"{h}:{p}" for h, p in tried) or "unknown"
    en = getattr(exc, "errno", None)
    if en is None and isinstance(exc, OSError) and exc.args:
        maybe = exc.args[0]
        if isinstance(maybe, int):
            en = maybe
    # Darwin: EHOSTUNREACH=65; also map string form from nested wrappers.
    msg = str(exc).lower()
    no_route = en in (
        errno.EHOSTUNREACH,
        getattr(errno, "EHOSTDOWN", -1),
    ) or "no route to host" in msg
    refused = en == errno.ECONNREFUSED or "connection refused" in msg
    timed_out = en in (errno.ETIMEDOUT, errno.EAGAIN) or "timed out" in msg

    if no_route:
        return (
            "Found the other Mac over Bonjour, but this Mac could not open a "
            f"network path to it ({targets}). Put both on the same Wi‑Fi "
            "(not a guest/client-isolated network), allow incoming connections "
            "for Khipu on the advertising Mac if the firewall asks, and approve "
            "Local Network access for Khipu if macOS prompts. "
            "Or use AirDrop: Save join kit on the working Mac, then Import join "
            "kit file on this one."
        )
    if refused:
        return (
            f"Reached {targets}, but nothing accepted the join connection. "
            "Confirm the other Mac still shows Advertise nearby (PIN) and try "
            "again — or AirDrop the .khipujoin file."
        )
    if timed_out:
        return (
            f"Timed out connecting to {targets}. Stay on the same network, "
            "keep Advertise nearby running on the other Mac, or AirDrop the "
            ".khipujoin file."
        )
    return f"Could not connect to nearby Mac ({targets}): {exc}"


def _tcp_connect(host: str, port: int, preferred_ipv4: str | None = None) -> tuple[socket.socket, str, int]:
    candidates = connect_candidates(host, port, preferred_ipv4)
    errors: list[BaseException] = []
    n = len(candidates)
    for i, (cand_host, cand_port) in enumerate(candidates):
        timeout = _CONNECT_LAST_S if i == n - 1 else _CONNECT_TRY_S
        try:
            sock = socket.create_connection((cand_host, cand_port), timeout=timeout)
            return sock, cand_host, cand_port
        except OSError as exc:
            errors.append(exc)
    last = errors[-1] if errors else RuntimeError("no connect candidates")
    raise RuntimeError(friendly_connect_error(last, candidates)) from last


def receive(
    passphrase: str,
    pin: str,
    *,
    host: str | None = None,
    port: int | None = None,
    preferred_ipv4: str | None = None,
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
                "No nearby Khipu found. On the Mac that already works: Settings → "
                "Set up another Mac → Advertise nearby (PIN). Stay on the same "
                "Wi‑Fi, or AirDrop a .khipujoin file and use Import join kit file."
            )
        host, port, txt_ipv4 = _resolve_service(services[0]["name"])
        if preferred_ipv4 is None:
            preferred_ipv4 = txt_ipv4

    raw, connected_host, connected_port = _tcp_connect(
        host, int(port), preferred_ipv4=preferred_ipv4
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # ephemeral pairing cert only
    tls = ctx.wrap_socket(raw, server_hostname=connected_host)
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
        from khipu.join import write_kit_file

        path_written = str(write_kit_file(out_path, blob))

    result: dict[str, Any] = {
        "ok": True,
        "bytes": len(blob),
        "host": connected_host,
        "port": connected_port,
        "resolved_host": host,
        "path": path_written,
    }
    if apply_import:
        summary = import_kit(blob, passphrase)
        counts = verify_live_counts(summary.get("expected") or {})
        result["summary"] = summary
        result["counts"] = counts
        result["kit_imported"] = True
        result["hub_ok"] = bool(counts.get("ok"))
        result["ok"] = True
        if counts.get("error"):
            result["warning"] = counts["error"]
        elif not result["hub_ok"] and counts.get("mismatches"):
            result["warning"] = "; ".join(counts["mismatches"])
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
        "ipv4": lan_ipv4(),
        "hint": (
            "On the new Mac: Join existing Khipu → enter this PIN → Find nearby Mac. "
            "Same Wi‑Fi required. File/AirDrop works if nearby fails. Passphrase only "
            "if you locked the kit when saving."
        ),
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
