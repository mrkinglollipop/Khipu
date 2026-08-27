#!/usr/bin/env python3
"""Fail if kinglollipop.com is not serving this Khipu version's page copy + DMG."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

SEMVER_RE = __import__("re").compile(r"^\d+\.\d+\.\d+$")


def location_ok(location: str, version: str) -> bool:
    loc = (location or "").strip().split("#", 1)[0].split("?", 1)[0]
    tag = f"/v{version}/"
    if tag not in loc:
        return False
    return loc.endswith(f"Khipu_{version}_aarch64.dmg") or loc.endswith(
        "Khipu_aarch64.dmg"
    )


def _curl(url: str, method: str) -> tuple[int, dict[str, str], bytes]:
    # curl --max-redirs 0 still prints the 302; urllib would follow.
    cmd = [
        "curl",
        "-sS",
        "-D",
        "-",
        "-o",
        "-",
        "--max-redirs",
        "0",
        "-X",
        method,
        "-A",
        "khipu-site-verify",
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode not in (0, 47):
        # 47 = CURLE_TOO_MANY_REDIRECTS when a 302 is refused — still have headers.
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"curl {method} {url} failed ({proc.returncode}): {err}")
    raw = proc.stdout
    header_blob, _, body = raw.partition(b"\r\n\r\n")
    if not header_blob:
        header_blob, _, body = raw.partition(b"\n\n")
    lines = header_blob.decode("utf-8", "replace").splitlines()
    status = 0
    headers: dict[str, str] = {}
    for line in lines:
        if line.upper().startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
            continue
        if ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip().lower()] = val.strip()
    return status, headers, body


def _get_text(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "khipu-site-verify"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def check_once(origin: str, version: str) -> list[str]:
    errors: list[str] = []
    page = f"{origin.rstrip('/')}/khipu/"
    news = f"{origin.rstrip('/')}/news.json"
    download = f"{origin.rstrip('/')}/khipu/download"
    needle = f"Mac · Alpha beta · {version}"
    try:
        html = _get_text(page)
        if needle not in html:
            errors.append(f"{page} missing {needle!r}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        errors.append(f"{page} fetch failed: {exc}")
    try:
        payload = json.loads(_get_text(news))
        ids = [
            str(item.get("id") or "")
            for item in payload.get("items", [])
            if isinstance(item, dict)
        ]
        if f"khipu-{version}" not in ids:
            errors.append(f"{news} missing item id khipu-{version} (have {ids[:5]})")
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        errors.append(f"{news} fetch failed: {exc}")
    for method in ("GET", "HEAD"):
        try:
            status, headers, _body = _curl(download, method)
        except RuntimeError as exc:
            errors.append(str(exc))
            continue
        loc = headers.get("location", "")
        if status != 302:
            errors.append(f"{method} {download} HTTP {status} (want 302) location={loc!r}")
        elif not location_ok(loc, version):
            errors.append(f"{method} {download} Location not this release: {loc!r}")
    return errors


def selftest() -> None:
    assert location_ok(
        "https://github.com/mrkinglollipop/Khipu/releases/download/v0.3.5/Khipu_0.3.5_aarch64.dmg",
        "0.3.5",
    )
    assert location_ok(
        "https://github.com/mrkinglollipop/Khipu/releases/download/v0.3.6/Khipu_aarch64.dmg",
        "0.3.6",
    )
    assert location_ok(
        "https://github.com/mrkinglollipop/Khipu/releases/download/v0.3.5/Khipu_0.3.5_aarch64.dmg?fresh=1",
        "0.3.5",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", help="semver without a leading v")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument(
        "--origin",
        default=os.environ.get("KHIPU_SITE_ORIGIN", "https://kinglollipop.com"),
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=int(os.environ.get("KHIPU_SITE_VERIFY_ATTEMPTS", "24")),
    )
    parser.add_argument(
        "--sleep",
        type=int,
        default=int(os.environ.get("KHIPU_SITE_VERIFY_SLEEP", "15")),
    )
    args = parser.parse_args()
    if args.selftest:
        selftest()
        print("verify_khipu_website selftest ok")
        return 0
    if not args.version or not SEMVER_RE.match(args.version):
        print("usage: verify_khipu_website.py <x.y.z>", file=sys.stderr)
        return 2
    attempts = max(1, args.attempts)
    last: list[str] = []
    for i in range(attempts):
        last = check_once(args.origin, args.version)
        if not last:
            print(f"site ok: {args.origin} serves Khipu {args.version}")
            return 0
        print(f"site verify attempt {i + 1}/{attempts} failed:", file=sys.stderr)
        for err in last:
            print(f"  {err}", file=sys.stderr)
        if i + 1 < attempts:
            time.sleep(max(0, args.sleep))
    print("Blocked on Matt: kinglollipop.com is not serving this Khipu version", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
