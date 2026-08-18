"""macOS Keychain helpers for Khipu secrets (DSN + Gemini)."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

SERVICE = os.environ.get("KHIPU_KEYCHAIN_SERVICE") or os.environ.get(
    "ALZY_KEYCHAIN_SERVICE", "Khipu"
)
LEGACY_SERVICE = "Alzy"
DSN_ACCOUNT = "database_url"
GEMINI_ACCOUNT = "gemini_api_key"
CONFIG_DIR = Path.home() / ".config" / "khipu"
LEGACY_CONFIG_DIR = Path.home() / ".config" / "alzy"


def _security(
    *args: str, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["security", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def keychain_available() -> bool:
    raw = os.environ.get("KHIPU_KEYCHAIN") or os.environ.get("ALZY_KEYCHAIN", "1")
    if raw.strip().lower() in {"0", "false", "off"}:
        return False
    return Path("/usr/bin/security").is_file()


def get_password(account: str, *, service: str = SERVICE) -> str | None:
    if not keychain_available():
        return None
    r = _security("find-generic-password", "-s", service, "-a", account, "-w")
    if r.returncode != 0:
        return None
    val = (r.stdout or "").strip()
    return val or None


def set_password(account: str, password: str, *, service: str = SERVICE) -> None:
    if not keychain_available():
        raise RuntimeError("macOS security(1) unavailable or KHIPU_KEYCHAIN=0")
    if "\n" in password or "\r" in password:
        # The stdin protocol below is newline-delimited, so an embedded newline
        # would silently truncate the stored secret.
        raise ValueError("secret must not contain a newline")
    _security("delete-generic-password", "-s", service, "-a", account)
    # `-w` with no value makes security(1) read the secret from stdin, prompting
    # twice. Passing it as an argument instead would expose it in `ps` output to
    # every other process on this machine for the lifetime of the call.
    r = _security(
        "add-generic-password",
        "-s",
        service,
        "-a",
        account,
        "-U",
        "-w",
        stdin=f"{password}\n{password}\n",
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"keychain add failed: {(r.stderr or r.stdout or '').strip()}"
        )


def migrate_legacy_secrets() -> dict:
    """Copy Alzy Keychain + ~/.config/alzy → Khipu when missing. Never prints secrets."""
    report = {
        "dsn_migrated": False,
        "gemini_migrated": False,
        "config_dir_migrated": False,
    }
    if keychain_available():
        for account, flag in (
            (DSN_ACCOUNT, "dsn_migrated"),
            (GEMINI_ACCOUNT, "gemini_migrated"),
        ):
            if get_password(account, service=SERVICE):
                continue
            legacy = get_password(account, service=LEGACY_SERVICE)
            if legacy:
                set_password(account, legacy, service=SERVICE)
                report[flag] = True
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if LEGACY_CONFIG_DIR.is_dir():
        for name in ("dsn", "root.crt"):
            src = LEGACY_CONFIG_DIR / name
            dst = CONFIG_DIR / name
            if src.is_file() and not dst.is_file():
                dst.write_bytes(src.read_bytes())
                if name == "dsn":
                    dst.chmod(0o600)
                report["config_dir_migrated"] = True
    return report


def get_dsn() -> str | None:
    val = get_password(DSN_ACCOUNT)
    if val:
        return val
    legacy = get_password(DSN_ACCOUNT, service=LEGACY_SERVICE)
    if legacy:
        try:
            set_password(DSN_ACCOUNT, legacy)
        except Exception:
            pass
        return legacy
    return None


def set_dsn(dsn: str) -> None:
    set_password(DSN_ACCOUNT, dsn.strip())


def get_gemini_key() -> str | None:
    val = get_password(GEMINI_ACCOUNT)
    if val:
        return val
    legacy = get_password(GEMINI_ACCOUNT, service=LEGACY_SERVICE)
    if legacy:
        try:
            set_password(GEMINI_ACCOUNT, legacy)
        except Exception:
            pass
        return legacy
    return None


def set_gemini_key(key: str) -> None:
    set_password(GEMINI_ACCOUNT, key.strip())


def resolve_gemini_key(*, key_file: Path | None = None) -> str:
    """Order: GEMINI_API_KEY → Keychain → optional key file (never print)."""
    env = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if env:
        return env
    kc = get_gemini_key()
    if kc:
        return kc
    path = key_file or _gemini_key_file()
    if path is not None and path.is_file():
        key = path.read_text(encoding="utf-8").strip()
        if key:
            return key
    raise RuntimeError(
        "No Gemini key. Set it in the app (Settings → Secrets), pipe it to "
        "`khipu secrets --set gemini_api_key`, or export GEMINI_API_KEY."
    )


def _gemini_key_file() -> Path | None:
    """Optional last-resort key file: env KHIPU_GEMINI_KEY_FILE → config.json → None."""
    from khipu.config import path_setting

    legacy = (os.environ.get("ALZY_GEMINI_KEY_FILE") or "").strip()
    return path_setting("gemini_key_file") or (Path(legacy) if legacy else None)


def dsn_file_health(path: Path | None = None) -> dict:
    """Is the on-disk DSN actually usable, without revealing it?

    The Keychain DSN and the file DSN are written at different times and can
    disagree. On 2026-08-18 they did: the Keychain named
    ``~/.config/khipu/root.crt`` and the file still named
    ``~/.config/khipu/../alzy/root.crt``, a directory removed in the 2026-08-04
    rename. Every check passed because every check ran somewhere the Keychain
    was reachable — while Aegis, whose sandbox denies the Keychain, fell back to
    the file and had been unable to reach Postgres for two days. Nothing
    compared the two, so nothing noticed.

    Returns presence and validity only; never the DSN or the password.
    """
    from urllib.parse import parse_qs, unquote, urlsplit

    if path is None:
        from khipu.paths import data_dir

        path = data_dir() / "dsn"
    out: dict[str, object] = {"path": str(path), "present": path.is_file(), "ok": True,
                              "reasons": []}
    if not path.is_file():
        return out
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        out["ok"] = False
        out["reasons"].append(f"unreadable: {type(exc).__name__}")
        return out
    q = parse_qs(urlsplit(raw).query)
    cert = unquote((q.get("sslrootcert") or [""])[0])
    mode = (q.get("sslmode") or [""])[0]
    out["sslmode"] = mode
    out["sslrootcert"] = cert or None
    if cert:
        out["sslrootcert_exists"] = Path(cert).is_file()
        if not Path(cert).is_file():
            out["ok"] = False
            out["reasons"].append(
                f"sslrootcert names a file that does not exist: {cert} — anything that "
                "cannot reach the Keychain (Aegis's sandbox) will fail to connect")
    elif mode.startswith("verify"):
        out["ok"] = False
        out["reasons"].append(f"sslmode={mode} but no sslrootcert")
    return out


def secrets_status() -> dict:
    """Presence-only status (never returns secret material).

    Two things this must get right, both found by the 2026-08-17 audit:
    ``get_dsn()`` is a `security(1)` subprocess (two, when it falls back to the
    legacy service), so calling it twice here doubled the cost of every
    ``khipu doctor`` — resolve once. And the on-disk DSN lives wherever
    ``paths.data_dir()`` says, not at the hardcoded default: with a relocated
    data dir this reported "no dsn" while the file was sitting right there.
    """
    from khipu.paths import data_dir

    gemini_file = _gemini_key_file()
    active_dir = data_dir()
    dsn_in_keychain = bool(get_dsn())
    dsn_on_disk = (active_dir / "dsn").is_file() or (CONFIG_DIR / "dsn").is_file() \
        or (LEGACY_CONFIG_DIR / "dsn").is_file()
    dsn_in_env = bool(
        (os.environ.get("KHIPU_DATABASE_URL") or os.environ.get("ALZY_DATABASE_URL") or "").strip()
    )
    return {
        "keychain_enabled": keychain_available(),
        "dsn": dsn_in_keychain or dsn_in_env or dsn_on_disk,
        "dsn_in_keychain": dsn_in_keychain,
        "gemini_in_keychain": bool(get_gemini_key()),
        "gemini_env": bool((os.environ.get("GEMINI_API_KEY") or "").strip()),
        "gemini_file_present": bool(gemini_file and gemini_file.is_file()),
        "config_dir": str(active_dir),
        "default_config_dir": str(CONFIG_DIR),
        # The file DSN is the fallback every sandboxed harness lands on, so its
        # health is not implied by the Keychain working here.
        "dsn_file": dsn_file_health(active_dir / "dsn"),
    }
