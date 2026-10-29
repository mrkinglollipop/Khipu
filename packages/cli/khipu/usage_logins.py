"""Usage limits read with a tool's own login: Grok, Cursor and OpenCode Go.

Owner decision 2026-10-29: Khipu may read these tools' own login tokens, as
T3 Code does, to show their real usage limits. Read-only: a token is never
refreshed and its file or Keychain item is never written; a 401/403 is
"can't read". The token lives in a local only for its provider's own usage
request to the fixed host below (``usage._https_get``: no redirect, no proxy,
no error body), and never reaches a row, the cache, a log, argv, env or an
exception. Each provider has the owner's switch (``LOGIN_SWITCHES``, routing
settings, so only the owner turns one on). Grok falls back to its Grok Build
reading; Cursor and OpenCode Go are rows of their own outside routing
(Cursor's lanes already count as the Grok account there).
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from pathlib import Path

from khipu.usage import (
    E_BAD,
    E_BAD_KEY,
    E_CLI,
    E_NO_KEY,
    E_NO_PLAN,
    E_PENDING,
    E_REDIRECT,
    LABELS,
    LOGIN_FILE_MAX,
    LOGIN_READ_MIN_S,
    LOGIN_TIMEOUT_S,
    ProbeError,
    TIMEOUT_S,
    _TOOL_DIRS,
    _dict,
    _guard,
    _https_get,
    _iso,
    _kimi_month_mins,
    _parse_time,
    _window,
    cache_file,
)

LOGIN_SWITCHES = {"grok": "routing_usage_login_grok", "cursor": "routing_usage_login_cursor",
                  "opencode": "routing_usage_login_opencode"}
# A config file Khipu itself created marks a new install: there a switch
# stays off until the owner answers the app's one-line notice.
LOGIN_ASK_NAME = "login-usage-ask"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
_GROK_AUTH_ENTRIES = ("https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828", "https://accounts.x.ai/sign-in")
# Grok signs in some other way under these: its auth.json is not the login in use.
_GROK_OTHER_AUTH_ENV = ("XAI_API_KEY", "GROK_AUTH")
_GROK_OTHER_AUTH_PREFIXES = ("GROK_OIDC", "GROK_OAUTH2", "GROK_AUTH_PROVIDER", "GROK_LOCAL_AUTH", "GROK_CONFIG")
_GROK_CONFIG_FILES = ("config.toml", "managed_config.toml", "requirements.toml")
_GROK_OTHER_AUTH_SECTION = re.compile(r"^\s*\[\s*(auth|grok_com_config|endpoints)\s*\]", re.M)
CURSOR_USAGE_URL = "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
CURSOR_KEYCHAIN_SERVICE = "cursor-access-token"
CURSOR_KEYCHAIN_ACCOUNT = "cursor-user"
# planUsage field -> (window name, model words); all reset at billingCycleEnd.
_CURSOR_WINDOWS = (("totalPercentUsed", "monthly", None), ("autoPercentUsed", "monthly_cursor_models", "Cursor models"),
                   ("apiPercentUsed", "monthly_other_models", "Other models"))
OPENCODE_USAGE_URL = "https://opencode.ai/zen/go/v1/usage"
OPENCODE_TIMEOUT_S = 5.0
_OPENCODE_WINDOWS = (("rolling", "five_hour", 300), ("weekly", "weekly", 10080), ("monthly", "monthly", None))


def _grok_login_file(home: str | None = None) -> Path:
    base = home or os.environ.get("GROK_HOME") or "~/.grok"
    return Path(os.path.expanduser(base)) / "auth.json"


def _opencode_login_file() -> Path:
    data = os.environ.get("XDG_DATA_HOME") or "~/.local/share"
    return Path(os.path.expanduser(data)) / "opencode" / "auth.json"


def login_present(provider: str) -> bool:
    """Whether this provider's login is on this Mac, judged without reading
    any token: the login file exists, or (Cursor, whose login is a Keychain
    item Khipu never touches unasked) its CLI is installed."""
    if provider == "grok":
        return _grok_login_file().is_file()
    if provider == "opencode":
        return _opencode_login_file().is_file()
    if provider == "cursor":
        return bool(shutil.which("cursor-agent") or shutil.which("cursor-agent", path=":".join(
            os.path.expanduser(d) for d in _TOOL_DIRS)))
    return False


def _login_new_install() -> bool:
    from khipu.config import config_file

    path = config_file()
    return not path.is_file() or path.with_name(LOGIN_ASK_NAME).exists()


def login_switch(provider: str, config: dict | None = None) -> tuple[bool, str]:
    """(on, source) of the owner's switch for reading ``provider``'s login.
    A conversation's change still waiting for the owner never takes effect:
    the value before it holds. Unset: Cursor (its Keychain item) is off; Grok
    and OpenCode Go are on where their login is on this Mac, except on a new
    install, where they stay off until the owner answers once."""
    from khipu import routing

    if config is None:
        from khipu.config import load_config

        config = load_config()
    key = LOGIN_SWITCHES[provider]
    waiting = routing._setting_requests(config).get(key)
    stored_set, stored = (bool(waiting.get("prior_set")), waiting.get("prior")) if waiting \
        else (key in config, config.get(key))
    if stored_set and stored is not None:
        return (stored, "file") if isinstance(stored, bool) else (False, "invalid")
    if provider == "cursor" or _login_new_install():
        return False, "default"
    return login_present(provider), "default"


def login_switches_status(config: dict | None = None) -> dict:
    """What the app shows beside each switch: on, where that comes from, and
    whether the login is on this Mac (never anything read from it)."""
    out = {}
    for provider in LOGIN_SWITCHES:
        on, source = login_switch(provider, config)
        out[provider] = {"on": on, "source": source, "login_found": login_present(provider),
                         "ask": source == "default" and not on and provider != "cursor"}
    return out


def login_read_due(provider: str, every: float = LOGIN_READ_MIN_S) -> bool:
    """At most one read of ``provider``'s login per ``every`` seconds, across
    every caller (the app, a reset check, the periodic refresh)."""
    stamp = cache_file().with_name(f"usage.login-{provider}.stamp")
    try:
        if time.time() - stamp.stat().st_mtime < every:
            return False
    except OSError:
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.touch()
        os.utime(stamp)
    except OSError:
        return False
    return True


def _read_login_json(path: Path) -> dict:
    """A tool's own login file as JSON, read-only: no symlink is followed and
    it must be a regular file this user owns that no one else can write. Any
    failure is ``E_NO_KEY`` with no text from the file in it."""
    import stat
    from khipu.paths import open_trusted_directory

    try:
        parent = open_trusted_directory(path.parent)
    except OSError:
        raise ProbeError(E_NO_KEY) from None
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    except OSError:
        raise ProbeError(E_NO_KEY) from None
    finally:
        os.close(parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ProbeError(E_NO_KEY)
        raw = os.read(fd, LOGIN_FILE_MAX + 1)
    except OSError:
        raise ProbeError(E_NO_KEY) from None
    finally:
        os.close(fd)
    if len(raw) > LOGIN_FILE_MAX:
        raise ProbeError(E_NO_KEY)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise ProbeError(E_NO_KEY) from None
    finally:
        raw = None
    if not isinstance(data, dict):
        raise ProbeError(E_NO_KEY)
    return data


def _login_reply(status: int, body: bytes, *, no_plan: tuple[int, ...] = ()) -> dict:
    """The JSON object a usage endpoint answered with; 401/403 is a login it
    did not accept (never retried, never refreshed)."""
    if 300 <= status < 400:
        raise ProbeError(E_REDIRECT)
    if status in no_plan:
        raise ProbeError(E_NO_PLAN)
    if status in (401, 403):
        raise ProbeError(E_BAD_KEY)
    if status != 200:
        raise ProbeError(E_CLI)
    try:
        reply = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeError, RecursionError):
        reply = None
    if not isinstance(reply, dict):
        raise ProbeError(E_BAD)
    return reply


def _login_pct(value) -> float | None:
    """A percent used as the endpoint states it, clamped to 0-100 (a server
    may report more than 100); a present non-number is a malformed reply."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ProbeError(E_BAD)
    return round(min(100.0, float(value)), 2)


def _grok_signs_in_otherwise(home: str | None) -> bool:
    if any((os.environ.get(name) or "").strip() for name in _GROK_OTHER_AUTH_ENV):
        return True
    if any(name.startswith(_GROK_OTHER_AUTH_PREFIXES) for name in os.environ):
        return True
    folders = [_grok_login_file(home).parent, Path("/etc/grok")]
    for folder in folders:
        for name in _GROK_CONFIG_FILES:
            try:
                text = (folder / name).read_text(encoding="utf-8", errors="replace")[:LOGIN_FILE_MAX]
            except OSError:
                continue
            if _GROK_OTHER_AUTH_SECTION.search(text):
                return True
    return False


def _grok_login_token(home: str | None) -> str:
    if _grok_signs_in_otherwise(home):
        raise ProbeError(E_NO_KEY)
    data = _read_login_json(_grok_login_file(home))
    entry = next((data[k] for k in _GROK_AUTH_ENTRIES if isinstance(data.get(k), dict)), None)
    token = entry.get("key") if entry else None
    if not isinstance(token, str) or not token.strip() or entry.get("auth_mode") == "api_key":
        raise ProbeError(E_NO_KEY)
    return token.strip()


def parse_grok_billing(status: int, body: bytes) -> dict:
    """``GET /v1/billing?format=credits``: the credits period as one window,
    sized from its own start and end so it can be paced."""
    reply = _login_reply(status, body)
    config = _dict(reply, "config", required=True)
    period = _dict(config, "currentPeriod")
    kind = period.get("type")
    kind = re.sub(r"[^a-z0-9_]", "", kind.lower().removeprefix("usage_period_type_"))[:32] \
        if isinstance(kind, str) else ""
    start, end = _parse_time(_iso(period.get("start"))), _parse_time(_iso(period.get("end")))
    mins = int((end - start).total_seconds() // 60) if start and end and end > start else None
    used = _login_pct(config.get("creditUsagePercent"))
    if used is None and mins:
        used = 0.0
    window = _window(kind or "period", used, period.get("end"), mins)
    if not window:
        raise ProbeError(E_NO_PLAN)
    return {"ok": True, "plan": None, "windows": [window], "credits": None, "version": None, "source": "live"}


def grok_login_read(home: str | None, timeout: float) -> dict:
    token = _grok_login_token(home)
    try:
        status, body = _https_get(GROK_BILLING_URL, f"Bearer {token}", timeout)
    finally:
        token = None  # the token lives only for the request above
    return parse_grok_billing(status, body)


def _cursor_token() -> str | None:
    """Cursor's own login from its Keychain item, read-only (``security
    find-generic-password -w``: the value comes back on stdout, never argv).
    macOS may ask the owner to allow it."""
    from khipu import keychain

    try:
        return keychain.get_password(CURSOR_KEYCHAIN_ACCOUNT, service=CURSOR_KEYCHAIN_SERVICE) or None
    except Exception:  # noqa: BLE001 - no item, or access refused; the message is never used
        return None


def parse_cursor(status: int, body: bytes) -> dict:
    """``GetCurrentPeriodUsage``: the billing cycle's overall use and its two
    pools (Cursor's own models; other models), all resetting at
    ``billingCycleEnd`` (epoch ms)."""
    reply = _login_reply(status, body)
    plan = _dict(reply, "planUsage")
    end = reply.get("billingCycleEnd")
    if isinstance(end, str) and end.strip().isdigit():
        end = int(end.strip())
    resets = _iso(end) if isinstance(end, (int, float)) and not isinstance(end, bool) else None
    mins = _kimi_month_mins(resets)
    windows = [w for field, name, model in _CURSOR_WINDOWS
               if (w := _window(name, _login_pct(plan.get(field)), resets, mins, model=model))]
    if not windows:
        raise ProbeError(E_NO_PLAN)
    return {"ok": True, "plan": None, "windows": windows, "credits": None, "version": None, "source": "live"}


@_guard
def probe_cursor(*, timeout: float = TIMEOUT_S) -> dict:
    if not login_switch("cursor")[0]:
        raise ProbeError(E_NO_KEY)
    if not login_read_due("cursor"):
        raise ProbeError(E_PENDING)
    token = _cursor_token()
    if not token:
        raise ProbeError(E_NO_KEY)
    try:
        status, body = _https_get(CURSOR_USAGE_URL, f"Bearer {token}",
                                  LOGIN_TIMEOUT_S if timeout == TIMEOUT_S else timeout, body=b"{}",
                                  headers={"Content-Type": "application/json", "connect-protocol-version": "1",
                                           "x-cursor-client-type": "cli"})
    finally:
        token = None
    return parse_cursor(status, body)


def _opencode_token() -> str:
    data = _read_login_json(_opencode_login_file())
    entry = data.get("opencode-go")
    token = entry.get("key") if isinstance(entry, dict) and entry.get("type") == "api" else None
    if not isinstance(token, str) or not token.strip():
        raise ProbeError(E_NO_KEY)
    return token.strip()


def parse_opencode(status: int, body: bytes) -> dict:
    """``GET /zen/go/v1/usage``: the rolling (five-hour), weekly and monthly
    windows, each ``{percent, resetsAt}``. A 403 is a login with no Go plan."""
    reply = _login_reply(status, body, no_plan=(403,))
    usage_ = _dict(reply, "usage")
    windows = []
    for field, name, mins in _OPENCODE_WINDOWS:
        item = _dict(usage_, field)
        resets = item.get("resetsAt")
        window = _window(name, _login_pct(item.get("percent")), resets, mins or _kimi_month_mins(_iso(resets)))
        if window:
            windows.append(window)
    if not windows:
        raise ProbeError(E_NO_PLAN)
    return {"ok": True, "plan": "Go", "windows": windows, "credits": None, "version": None, "source": "live"}


@_guard
def probe_opencode(*, timeout: float = TIMEOUT_S) -> dict:
    if not login_switch("opencode")[0]:
        raise ProbeError(E_NO_KEY)
    if not login_read_due("opencode"):
        raise ProbeError(E_PENDING)
    token = _opencode_token()
    try:
        status, body = _https_get(OPENCODE_USAGE_URL, f"Bearer {token}",
                                  OPENCODE_TIMEOUT_S if timeout == TIMEOUT_S else timeout)
    finally:
        token = None
    return parse_opencode(status, body)


def login_accounts(config: dict | None = None) -> list[dict]:
    """Cursor and OpenCode Go, each once its switch is on (OpenCode Go only
    with its login on this Mac)."""
    out = []
    if login_switch("cursor", config)[0]:
        out.append({"label": LABELS["cursor"], "harness": "cursor", "home": None})
    if login_switch("opencode", config)[0] and login_present("opencode"):
        out.append({"label": LABELS["opencode"], "harness": "opencode", "home": None})
    return out


def probe(harness: str, *, timeout: float = TIMEOUT_S) -> dict:
    return probe_cursor(timeout=timeout) if harness == "cursor" else probe_opencode(timeout=timeout)
