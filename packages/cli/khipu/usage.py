"""Plan usage: how much of each subscription CLI's plan is left.

Every CLI already reports its own plan headroom through a protocol it speaks,
using the login it already holds, so Khipu runs the CLI as the user, asks, and
reads the answer. Khipu never reads, holds or stores a credential, and no model
call is made (no tokens are spent). Scope: docs/plans/2026-10-14-plan-usage.md.

One adapter per CLI, each a short child process:

  claude  ``claude -p`` in stream-json mode, SDK control requests ``initialize``
          then ``get_usage`` (``skip_behaviors``)
  codex   ``codex app-server`` (stdio JSON-RPC): ``initialize``, ``initialized``,
          ``account/rateLimits/read``; falls back to the newest ``rate_limits``
          in the rollout files when the live call fails
  grok    ``grok agent stdio`` (ACP) with ``GROK_HOME``: ``initialize``, then
          ``_x.ai/billing``
  muse    ``muse serve --no-session-log`` (MSP): ``initialize``, ``initialized``,
          ``usage/read`` (last observed figures, never refreshed by a turn)

Each child gets ``synth_cli.environment()`` minus every variable that would
redirect or override the login, its stdin closed as soon as the CLI allows,
a timeout, and a hard kill of its whole process group. Results are cached in
``state/usage.json``; a window a CLI does not report is absent, never zero.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import select
import signal
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HARNESSES = ("claude", "codex", "grok", "muse")
LABELS = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "muse": "Muse"}
TIMEOUT_S = 5.0
DEFAULT_MAX_AGE_S = 120
CACHE_NAME = "usage.json"

# Where each CLI keeps the login a probe should read. Every one is removed from
# every probe's environment; the adapter sets only its own, and only when the
# account names a home.
_HOME_VARS = {"claude": "CLAUDE_CONFIG_DIR", "codex": "CODEX_HOME", "grok": "GROK_HOME"}
# Variables that make a CLI use something other than its stored login (Muse:
# an API key in the environment overrides the signed-in subscription). They
# are removed from the child's copy of the environment and never read.
_LOGIN_OVERRIDES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "CODEX_API_KEY", "XAI_API_KEY", "META_API_KEY",
)

_CLAUDE_ARGS = ("-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--setting-sources", "project", "--no-session-persistence",
                # No MCP servers: the probe never uses a tool, and the user's
                # servers (Khipu's own among them) cost startup time.
                "--strict-mcp-config")
_CLIENT_INFO = {"name": "khipu_usage", "version": "1"}


class ProbeError(Exception):
    """The CLI did not answer, or answered with an error (its own words)."""


# ---- child process ---------------------------------------------------------


def _command(harness: str) -> str | None:
    from khipu import synth_cli

    return synth_cli.binary(harness)


def probe_environment(harness: str, home: str | None = None) -> dict[str, str]:
    from khipu import synth_cli

    env = synth_cli.environment()
    for name in (*_HOME_VARS.values(), *_LOGIN_OVERRIDES):
        env.pop(name, None)
    if home and harness in _HOME_VARS:
        env[_HOME_VARS[harness]] = str(Path(home).expanduser())
    return env


_SECRETISH = re.compile(r"token|key|secret|bearer|password|auth|cookie", re.I)


def _stderr_words(errfile, env: dict[str, str]) -> str:
    from khipu import synth_cli

    try:
        errfile.seek(0)
        tail = errfile.read()[-4000:].decode("utf-8", "replace")
    except (OSError, ValueError):
        return ""
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip() and not _SECRETISH.search(ln)]
    return synth_cli.error_text(" ".join(lines[-3:]), "", env)[:200]


def _kill(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=2)
    for stream in (proc.stdin, proc.stdout):
        with contextlib.suppress(Exception):
            if stream:
                stream.close()


def _converse(argv: list[str], messages: list[dict], is_answer, *, env: dict[str, str],
              timeout: float, close_stdin: bool) -> list[dict]:
    """Write ``messages``, read JSON lines until ``is_answer(frame)``, then kill.

    ``close_stdin``: close stdin right after the requests. Codex and Grok exit
    on end of input before answering, so for them stdin stays open until the
    answer arrives (the kill closes it). Returns the response frames read
    (notifications are dropped: Grok's carry the user's MCP configuration)."""
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="khipu-usage-") as cwd, tempfile.TemporaryFile() as errfile:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errfile,
                                    cwd=cwd, env=env, start_new_session=True)
        except OSError as exc:
            raise ProbeError(f"could not start {Path(argv[0]).name}: {exc.strerror or type(exc).__name__}") from None
        try:
            try:
                proc.stdin.write("".join(json.dumps(m) + "\n" for m in messages).encode())
                proc.stdin.flush()
                if close_stdin:
                    proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            fd = proc.stdout.fileno()
            buf = b""
            frames: list[dict] = []
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ProbeError(f"no answer within {timeout:g} s")
                ready, _, _ = select.select([fd], [], [], left)
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=0.5)
                    words = _stderr_words(errfile, env)
                    code = proc.returncode
                    raise ProbeError(words or f"exited{'' if code is None else f' ({code})'} before answering")
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        frame = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(frame, dict):
                        continue
                    if "id" in frame or frame.get("type") == "control_response":
                        frames.append(frame)
                    if is_answer(frame):
                        return frames
        finally:
            _kill(proc)


# ---- small helpers -----------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value) -> str | None:
    """Epoch seconds, epoch milliseconds or an ISO string -> ``...Z`` (seconds)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 100_000_000_000 else value
            dt = datetime.fromtimestamp(seconds, timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _window_name(mins) -> str:
    return {300: "five_hour", 1440: "daily", 10080: "weekly"}.get(mins, f"{mins}_min" if mins else "window")


def _window(name: str, used, resets_at, mins=None, **extra) -> dict | None:
    if used is None or isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    w = {"name": name, "used_pct": used, "resets_at": _iso(resets_at)}
    if mins:
        w["window_mins"] = mins
    w.update({k: v for k, v in extra.items() if v is not None})
    return w


def _answer(frames: list[dict], rid) -> dict:
    for frame in frames:
        if frame.get("id") == rid:
            if "error" in frame:
                err = frame.get("error") or {}
                raise ProbeError(str(err.get("message") or err)[:200])
            result = frame.get("result")
            if not isinstance(result, dict):
                raise ProbeError("answer had no result")
            return result
    raise ProbeError("no answer")


def _number(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def _guard(fn):
    def run(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ProbeError as exc:
            return {"ok": False, "error": str(exc) or "probe failed"}
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def _not_installed(harness: str) -> dict:
    return {"ok": False, "error": f"{harness} CLI not found"}


# ---- Claude ------------------------------------------------------------------


def parse_claude(frames: list[dict]) -> dict:
    responses = {(f.get("response") or {}).get("request_id"): f.get("response") or {} for f in frames}
    init = (responses.get("init") or {}).get("response") or {}
    usage = responses.get("usage")
    if usage is None:
        raise ProbeError("no answer to get_usage")
    if usage.get("subtype") == "error":
        raise ProbeError(str(usage.get("error") or "get_usage failed")[:200])
    body = usage.get("response") or {}
    limits = body.get("rate_limits")
    if not isinstance(limits, dict):
        raise ProbeError("Claude reported no plan limits for this login (a login made with a setup "
                         "token answers none; run /login in this home)")
    windows = [
        _window("five_hour", (limits.get("five_hour") or {}).get("utilization"),
                (limits.get("five_hour") or {}).get("resets_at"), 300),
        _window("weekly", (limits.get("seven_day") or {}).get("utilization"),
                (limits.get("seven_day") or {}).get("resets_at"), 10080),
    ]
    seen = set()
    for scoped in limits.get("model_scoped") or []:
        model = str(scoped.get("display_name") or "").strip()
        if model:
            seen.add(model.lower())
            windows.append(_window(f"weekly_{re.sub(r'[^a-z0-9]+', '_', model.lower())}",
                                   scoped.get("utilization"), scoped.get("resets_at"), 10080, model=model))
    for key, model in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
        block = limits.get(key)
        if isinstance(block, dict) and model.lower() not in seen:
            windows.append(_window(f"weekly_{model.lower()}", block.get("utilization"),
                                   block.get("resets_at"), 10080, model=model))
    extra = limits.get("extra_usage") or {}
    credits = {k: v for k, v in {
        "extra_usage_enabled": extra.get("is_enabled"),
        "extra_usage_used": extra.get("used_credits"),
        "extra_usage_limit": extra.get("monthly_limit"),
        "currency": extra.get("currency"),
    }.items() if v is not None} or None
    return {"ok": True, "plan": body.get("subscription_type"), "windows": [w for w in windows if w],
            "credits": credits, "version": init.get("claude_code_version"), "source": "live"}


@_guard
def probe_claude(config_dir: str | None = None, *, timeout: float = TIMEOUT_S) -> dict:
    """Claude Code's own ``/usage`` figures for the login in ``config_dir``
    (``CLAUDE_CONFIG_DIR``; None = the default ``~/.claude``). Never ``--bare``:
    bare mode skips the keychain, so there is no login to read."""
    binary = _command("claude")
    if not binary:
        return _not_installed("claude")
    messages = [
        {"type": "control_request", "request_id": "init", "request": {"subtype": "initialize"}},
        {"type": "control_request", "request_id": "usage",
         "request": {"subtype": "get_usage", "skip_behaviors": True}},
    ]
    frames = _converse([binary, *_CLAUDE_ARGS], messages,
                       lambda f: f.get("type") == "control_response"
                       and (f.get("response") or {}).get("request_id") == "usage",
                       env=probe_environment("claude", config_dir), timeout=timeout, close_stdin=True)
    return parse_claude(frames)


# ---- Codex -------------------------------------------------------------------


def _codex_windows(snapshot: dict, *, camel: bool) -> list[dict]:
    windows = []
    for slot in ("primary", "secondary"):
        block = snapshot.get(slot)
        if not isinstance(block, dict):
            continue  # either may be null: the pro plan reports one weekly window
        if camel:
            used, mins, resets = block.get("usedPercent"), block.get("windowDurationMins"), block.get("resetsAt")
        else:
            used, mins, resets = block.get("used_percent"), block.get("window_minutes"), block.get("resets_at")
        w = _window(_window_name(mins), used, resets, mins, slot=slot)
        if w:
            windows.append(w)
    return windows


def _codex_credits(credits, *, camel: bool) -> dict | None:
    if not isinstance(credits, dict):
        return None
    out = {
        "balance": _number(credits.get("balance")),
        "has_credits": credits.get("hasCredits" if camel else "has_credits"),
        "unlimited": credits.get("unlimited"),
    }
    return {k: v for k, v in out.items() if v is not None} or None


def parse_codex(frames: list[dict]) -> dict:
    version = None
    for frame in frames:
        if frame.get("id") == 1:
            agent = str((frame.get("result") or {}).get("userAgent") or "")
            m = re.search(r"/(\d[\w.\-]*)", agent)
            version = m.group(1) if m else None
    result = _answer(frames, 2)
    snapshot = result.get("rateLimits")
    if not isinstance(snapshot, dict):
        raise ProbeError("Codex reported no rate limits")
    return {"ok": True, "plan": snapshot.get("planType"), "windows": _codex_windows(snapshot, camel=True),
            "credits": _codex_credits(snapshot.get("credits"), camel=True), "version": version,
            "source": "live"}


def codex_rollout(home: str | None = None) -> dict | None:
    """The newest ``rate_limits`` in a ``token_count`` event of the newest
    rollout file: current as of the last Codex turn, not live."""
    root = Path(home or "~/.codex").expanduser() / "sessions"
    try:
        files = sorted(root.glob("**/rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files[:5]:
        last = None
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"token_count"' not in line or '"rate_limits"' not in line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    payload = event.get("payload") or {}
                    if payload.get("type") == "token_count" and isinstance(payload.get("rate_limits"), dict):
                        last = event
        except OSError:
            continue
        if last:
            limits = last["payload"]["rate_limits"]
            return {"ok": True, "plan": limits.get("plan_type"),
                    "windows": _codex_windows(limits, camel=False),
                    "credits": _codex_credits(limits.get("credits"), camel=False),
                    "source": "rollout", "observed_at": _iso(last.get("timestamp"))}
    return None


@_guard
def probe_codex(home: str | None = None, *, timeout: float = TIMEOUT_S) -> dict:
    """Codex's ``account/rateLimits/read``; on failure, the last turn's figures
    from the rollout files (``source: "rollout"``)."""
    binary = _command("codex")
    try:
        if not binary:
            raise ProbeError("codex CLI not found")
        messages = [
            {"id": 1, "method": "initialize", "params": {"clientInfo": _CLIENT_INFO}},
            {"method": "initialized"},
            {"id": 2, "method": "account/rateLimits/read"},
        ]
        frames = _converse([binary, "app-server"], messages, lambda f: f.get("id") == 2,
                           env=probe_environment("codex", home), timeout=timeout, close_stdin=False)
        return parse_codex(frames)
    except ProbeError as exc:
        fallback = codex_rollout(home)
        if fallback is None:
            raise
        fallback["note"] = f"live read failed ({exc}); figures from the last Codex turn"
        return fallback


# ---- Grok --------------------------------------------------------------------


def parse_grok(frames: list[dict]) -> dict:
    version = None
    for frame in frames:
        if frame.get("id") == 1:
            version = ((frame.get("result") or {}).get("_meta") or {}).get("agentVersion")
    result = _answer(frames, 2)
    config = result.get("config") or {}
    period = config.get("currentPeriod") or {}
    kind = str(period.get("type") or "").lower().removeprefix("usage_period_type_") or "period"
    window = _window({"weekly": "weekly", "monthly": "monthly", "daily": "daily"}.get(kind, kind),
                     config.get("creditUsagePercent"), period.get("end") or config.get("billingPeriodEnd"))
    balance = (config.get("prepaidBalance") or {}).get("val")
    credits = {"prepaid_balance": balance} if isinstance(balance, (int, float)) else None
    return {"ok": True, "plan": result.get("subscription_tier"), "windows": [window] if window else [],
            "credits": credits, "version": version, "source": "live"}


@_guard
def probe_grok(grok_home: str | None = None, *, timeout: float = TIMEOUT_S) -> dict:
    """Grok Build's ``_x.ai/billing`` (ACP, undocumented) for the login in
    ``grok_home`` (``GROK_HOME``; None = the default ``~/.grok``)."""
    binary = _command("grok")
    if not binary:
        return _not_installed("grok")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": 1, "clientCapabilities": {}, "clientInfo": _CLIENT_INFO}},
        {"jsonrpc": "2.0", "id": 2, "method": "_x.ai/billing", "params": {}},
    ]
    frames = _converse([binary, "agent", "--no-leader", "stdio"], messages, lambda f: f.get("id") == 2,
                       env=probe_environment("grok", grok_home), timeout=timeout, close_stdin=False)
    return parse_grok(frames)


# ---- Muse --------------------------------------------------------------------


def parse_muse(frames: list[dict]) -> dict:
    version = None
    for frame in frames:
        if frame.get("id") == 1:
            version = ((frame.get("result") or {}).get("serverInfo") or {}).get("version")
    usage = _answer(frames, 2).get("usage")
    if not isinstance(usage, dict):
        # A fresh host answers {} until a Muse turn has run: truthful absence.
        return {"ok": True, "plan": None, "windows": [], "version": version, "source": "last_observed",
                "note": "no turn observed yet"}
    window = usage.get("window") or {}
    weekly = usage.get("weekly") or {}
    mins = window.get("windowDurationMins")
    windows = [
        _window(_window_name(mins), window.get("usedPercent"), window.get("resetsAtMs"), mins),
        _window("weekly", weekly.get("usedPercent"), weekly.get("resetsAtMs"), 10080),
    ]
    return {"ok": True, "plan": usage.get("tier"), "windows": [w for w in windows if w], "version": version,
            "source": "last_observed", "observed_at": _iso(usage.get("observedAtMs"))}


@_guard
def probe_muse(*, timeout: float = TIMEOUT_S) -> dict:
    """Muse's ``usage/read``: the figures as of the last Muse turn (marked
    ``observed_at``); never refreshed by spending a turn."""
    binary = _command("muse")
    if not binary:
        return _not_installed("muse")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"clientInfo": _CLIENT_INFO, "capabilities": {"experimentalApi": True}}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "usage/read", "params": {}},
    ]
    frames = _converse([binary, "serve", "--no-session-log"], messages, lambda f: f.get("id") == 2,
                       env=probe_environment("muse"), timeout=timeout, close_stdin=True)
    return parse_muse(frames)


# ---- accounts ----------------------------------------------------------------


def configured_accounts() -> list[dict]:
    """Config ``usage_accounts`` (``[{label, harness, home?}]``), or by default
    every installed CLI with its default home. Invalid entries come back with
    an ``invalid`` reason so the report shows them instead of hiding them."""
    from khipu.config import load_config

    raw = load_config().get("usage_accounts")
    if not isinstance(raw, list) or not raw:
        return [{"label": LABELS[h], "harness": h, "home": None} for h in HARNESSES if _command(h)]
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            out.append({"label": "?", "harness": "?", "home": None, "invalid": "entry is not an object"})
            continue
        harness = str(entry.get("harness") or "").strip().lower()
        label = str(entry.get("label") or "").strip() or LABELS.get(harness, harness or "?")
        home = entry.get("home")
        home = str(home).strip() if isinstance(home, str) and home.strip() else None
        account = {"label": label, "harness": harness or "?", "home": home}
        if harness not in HARNESSES:
            account["invalid"] = f"unknown harness {harness or '(none)'!r}; one of {', '.join(HARNESSES)}"
        elif harness == "muse" and home:
            account["invalid"] = "Muse has one login per user; a home cannot be chosen"
        out.append(account)
    return out


def _key(account: dict) -> str:
    return f"{account['harness']}:{account['label']}"


def probe_account(account: dict, *, timeout: float = TIMEOUT_S) -> dict:
    harness, home = account["harness"], account.get("home")
    started = time.monotonic()
    if account.get("invalid"):
        data = {"ok": False, "error": f"invalid usage_accounts entry: {account['invalid']}"}
    elif harness == "claude":
        data = probe_claude(home, timeout=timeout)
    elif harness == "codex":
        data = probe_codex(home, timeout=timeout)
    elif harness == "grok":
        data = probe_grok(home, timeout=timeout)
    elif harness == "muse":
        data = probe_muse(timeout=timeout)
    else:
        data = {"ok": False, "error": f"unknown harness {harness!r}"}
    row = {"harness": harness, "account": account["label"], "home": home, "plan": None, "windows": [],
           "credits": None, "version": None, "source": None, "observed_at": None}
    row.update(data)
    row.update({"probed_at": _iso(_now().timestamp()), "ms": int((time.monotonic() - started) * 1000)})
    row.setdefault("error", None)
    row.setdefault("note", None)
    return row


def probe_all(accounts: list[dict], *, timeout: float = TIMEOUT_S) -> list[dict]:
    """Every account in parallel; one hung CLI never delays the others past
    its own timeout, and a failed probe never hides the rest."""
    if not accounts:
        return []
    with ThreadPoolExecutor(max_workers=len(accounts)) as pool:
        futures = [pool.submit(probe_account, a, timeout=timeout) for a in accounts]
        rows = []
        for account, future in zip(accounts, futures):
            try:
                rows.append(future.result(timeout=timeout + 10))
            except Exception as exc:  # noqa: BLE001 — one account never breaks the report
                rows.append({"harness": account["harness"], "account": account["label"],
                             "home": account.get("home"), "plan": None, "windows": [], "credits": None,
                             "ok": False, "error": type(exc).__name__, "probed_at": _iso(_now().timestamp()),
                             "ms": None})
        return rows


# ---- cache + report ------------------------------------------------------------


def cache_file() -> Path:
    from khipu.paths import data_dir

    return data_dir() / "state" / CACHE_NAME


def _load_cache() -> dict[str, dict]:
    try:
        data = json.loads(cache_file().read_text(encoding="utf-8"))
        rows = data.get("rows") if isinstance(data, dict) else None
        return {k: v for k, v in rows.items() if isinstance(v, dict)} if isinstance(rows, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(rows: dict[str, dict]) -> None:
    path = cache_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, suffix=".tmp",
                                         encoding="utf-8") as fh:
            json.dump({"version": 1, "rows": rows}, fh, indent=2, sort_keys=True)
            tmp = Path(fh.name)
        os.replace(tmp, path)
    except OSError:
        pass  # a read-only data dir costs the cache, never the answer


def _age_s(row: dict, now: datetime) -> float | None:
    when = _parse_time(row.get("observed_at")) or _parse_time(row.get("probed_at"))
    return None if when is None else (now - when).total_seconds()


def report(*, fresh: bool = False, max_age: float = DEFAULT_MAX_AGE_S, timeout: float = TIMEOUT_S,
           accounts: list[dict] | None = None) -> dict:
    """One row per account, from the cache when its probe is younger than
    ``max_age`` seconds (``fresh`` bypasses it). A row whose figures are older
    than ``max_age`` (a cached row, Muse's last observed turn, Codex's rollout
    fallback) is marked ``stale: true``, never dropped."""
    accounts = configured_accounts() if accounts is None else accounts
    cache = _load_cache()
    now = _now()
    rows: list[dict | None] = [None] * len(accounts)
    pending = []
    for i, account in enumerate(accounts):
        cached = cache.get(_key(account))
        probed = _parse_time((cached or {}).get("probed_at"))
        if (not fresh and cached and not account.get("invalid") and probed
                and (now - probed).total_seconds() <= max_age):
            rows[i] = dict(cached, cached=True)
        else:
            pending.append(i)
    if pending:
        for i, row in zip(pending, probe_all([accounts[i] for i in pending], timeout=timeout)):
            previous = cache.get(_key(accounts[i]))
            if not row.get("ok") and previous and previous.get("ok") and previous.get("windows"):
                # Keep the last good figures, honestly labelled: this probe failed.
                row = dict(previous, ok=False, error=row.get("error"), last_attempt_at=row.get("probed_at"))
            cache[_key(accounts[i])] = {k: v for k, v in row.items() if k not in ("cached", "stale")}
            rows[i] = dict(row, cached=False)
        _save_cache(cache)
    now = _now()
    for row in rows:
        age = _age_s(row, now)
        row["stale"] = bool(age is not None and age > max_age)
    return {"generated_at": _iso(now.timestamp()), "max_age_s": max_age, "accounts": rows,
            "summary_text": [summary_line(r) for r in rows]}


# ---- text ----------------------------------------------------------------------


def _window_label(window: dict) -> str:
    name = window.get("name") or "window"
    if name == "five_hour":
        return "5 h"
    if name == "weekly":
        return "week"
    if name == "daily":
        return "day"
    if name.startswith("weekly_"):
        return f"week {window.get('model') or name.removeprefix('weekly_')}"
    return name.replace("_", " ")


def _pct(value) -> str:
    return f"{value:g}%" if isinstance(value, (int, float)) else "?"


def _when(iso: str | None) -> str:
    dt = _parse_time(iso)
    return dt.strftime("%a %H:%MZ") if dt else "?"


def summary_line(row: dict) -> str:
    """'Claude (max): 5 h 13%, week 34%; tightest week 34% resets Sun 15:00Z'."""
    head = row.get("account") or row.get("harness") or "?"
    if row.get("plan"):
        head += f" ({row['plan']})"
    windows = row.get("windows") or []
    if not row.get("ok") and not windows:
        return f"{head}: unavailable: {row.get('error') or 'probe failed'}"
    if not windows:
        text = f"{head}: {row.get('note') or 'no windows reported'}"
    else:
        text = f"{head}: " + ", ".join(f"{_window_label(w)} {_pct(w.get('used_pct'))}" for w in windows)
        tight = max(windows, key=lambda w: w.get("used_pct") if isinstance(w.get("used_pct"), (int, float)) else -1)
        if tight.get("resets_at"):
            text += f"; tightest {_window_label(tight)} {_pct(tight.get('used_pct'))} resets {_when(tight['resets_at'])}"
    credits = row.get("credits") or {}
    for key, label in (("balance", "credits"), ("prepaid_balance", "prepaid")):
        if isinstance(credits.get(key), (int, float)):
            text += f", {label} {credits[key]:g}"
    if row.get("observed_at"):
        text += f", as of {_when(row['observed_at'])}"
    if row.get("source") == "rollout":
        text += " (from the last Codex turn)"
    if not row.get("ok"):
        text += f" [probe failed: {row.get('error')}]"
    if row.get("stale"):
        text += " [stale]"
    return text


def table(rep: dict) -> str:
    header = ("ACCOUNT", "PLAN", "WINDOW", "USED", "RESETS", "NOTE")
    lines: list[tuple[str, ...]] = []
    for row in rep.get("accounts") or []:
        notes = []
        if not row.get("ok"):
            notes.append(f"failed: {row.get('error')}")
        if row.get("note"):
            notes.append(row["note"])
        if row.get("observed_at"):
            notes.append(f"as of {_when(row['observed_at'])}")
        credits = row.get("credits") or {}
        if isinstance(credits.get("balance"), (int, float)):
            notes.append(f"credits {credits['balance']:g}")
        if isinstance(credits.get("prepaid_balance"), (int, float)):
            notes.append(f"prepaid {credits['prepaid_balance']:g}")
        if row.get("stale"):
            notes.append("stale")
        windows = row.get("windows") or [None]
        for n, w in enumerate(windows):
            lines.append((
                (row.get("account") or "?") if n == 0 else "",
                (row.get("plan") or "-") if n == 0 else "",
                _window_label(w) if w else "-",
                _pct(w.get("used_pct")) if w else "-",
                _when(w.get("resets_at")) if w and w.get("resets_at") else "-",
                "; ".join(notes) if n == 0 else "",
            ))
    widths = [max(len(r[i]) for r in [header, *lines]) for i in range(len(header) - 1)]
    out = []
    for r in [header, *lines]:
        out.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r[:-1])) + "  " + r[-1])
    return "\n".join(line.rstrip() for line in out)
