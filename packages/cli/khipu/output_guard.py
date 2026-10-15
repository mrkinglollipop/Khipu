"""Last-mile output guard for commands which handle join/backup credentials.

Collectors observe values already loaded by the command; they never open a
credential store. The registry lives only for the guarded command invocation.
"""
from __future__ import annotations

import inspect
import io
import json
import os
import re
import sys
from contextlib import redirect_stderr, redirect_stdout
from contextvars import ContextVar
from functools import wraps
from urllib.parse import parse_qsl, quote, quote_plus, unquote, unquote_plus, urlsplit

REPLACEMENT = "REDACTED"
_SECRET_NAME = re.compile(r"key|token|passphrase|password|secret|auth", re.I)
_USERINFO = re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)[^/?#\r\n"<>]*@')
_URL = re.compile(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s"<>\\]+')
_JSON_STRING = re.compile(r'"(?:[^"\\]|\\.)*"')
_ACTIVE: ContextVar[set[str] | None] = ContextVar("command_output_secrets", default=None)


def remember(value, name: str = "secret"):
    """Register a loaded secret, config, kit, or DSN and return it unchanged."""
    secrets = _ACTIVE.get()
    if secrets is None:
        return value
    if isinstance(value, dict):
        for key, item in value.items():
            remember(item, "secret" if _SECRET_NAME.search(name) else str(key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            remember(item, name)
    elif isinstance(value, str) and value:
        if _SECRET_NAME.search(name):
            secrets.add(value)
            if value.strip():
                secrets.add(value.strip())
        if name.lower() in ("dsn", "database_url"):
            try:
                from psycopg.conninfo import conninfo_to_dict

                remember(conninfo_to_dict(value).get("password"))
            except Exception:
                pass
        if "://" in value:
            try:
                parts = urlsplit(value)
                if parts.password:
                    remember(unquote(parts.password))
                for key, item in parse_qsl(parts.query, keep_blank_values=True):
                    if _SECRET_NAME.search(key):
                        remember(item)
            except ValueError:
                pass
    return value


def secret_result(name: str):
    """Observe a loader without changing its behavior outside a command."""
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            return remember(fn(*args, **kwargs), name)
        return wrapped
    return decorate


def _strip_urls(text: str) -> str:
    # Accept spaces in malformed userinfo: url tokenizers stop too early there.
    text = _USERINFO.sub(r"\1", text)

    def clean(match):
        url = match.group()
        base, sep, query = url.partition("?")
        if not sep:
            return url
        query, fragment_sep, fragment = query.partition("#")
        kept = [part for part in query.split("&")
                if not _SECRET_NAME.search(unquote_plus(part.partition("=")[0]))]
        return base + ("?" + "&".join(kept) if kept else "") + fragment_sep + fragment

    return _URL.sub(clean, text)


def sanitize(text: str) -> str:
    """Protect JSON string contents and plain text, including encoded values."""
    variants = set()
    for secret in _ACTIVE.get() or ():
        for form in (secret, quote(secret, safe=""), quote_plus(secret), quote(secret)):
            variants.add(form)
            variants.add(re.sub(r"%[0-9A-F]{2}", lambda m: m.group().lower(), form))

    def clean(value):
        for variant in sorted(variants, key=len, reverse=True):
            value = value.replace(variant, REPLACEMENT)
        return _strip_urls(value)

    def clean_json(match):
        try:
            value = json.loads(match.group())
        except ValueError:
            return match.group()
        return json.dumps(clean(value), ensure_ascii=True)

    # Decode escapes before redaction; replacing inside serialized JSON can
    # otherwise miss Unicode secrets or leave a dangling backslash.
    text = _JSON_STRING.sub(clean_json, text)
    return clean(text)


class _GuardedStream(io.TextIOBase):
    def __init__(self, target):
        self.target = target
        self.pending = io.StringIO()

    @property
    def encoding(self):
        return self.target.encoding

    def isatty(self):
        return self.target.isatty()

    def fileno(self):
        return self.target.fileno()

    def write(self, text):
        self.pending.write(text)
        return len(text)

    def flush(self):
        # Garbage collection closes this wrapper after the command, when the
        # stream it wrapped may already be closed; nothing is pending then.
        if getattr(self.target, "closed", False):
            return
        text = self.pending.getvalue()
        self.pending.seek(0)
        self.pending.truncate()
        if text:
            self.target.write(sanitize(text))
        self.target.flush()


def guard_output(fn):
    """Protect both printers, retaining explicit flush for the pairing banner."""
    signature = inspect.signature(fn)

    @wraps(fn)
    def wrapped(*args, **kwargs):
        def collect_inputs():
            for name, value in signature.bind_partial(*args, **kwargs).arguments.items():
                remember(vars(value) if hasattr(value, "__dict__") else value, name)
            for name, value in os.environ.items():
                if name.endswith(("API_KEY", "PASSPHRASE", "PASSWORD", "TOKEN", "SECRET", "DATABASE_URL")):
                    remember(value, name)

        if _ACTIVE.get() is not None:
            collect_inputs()
            return fn(*args, **kwargs)
        token = _ACTIVE.set(set())
        stdout, stderr = _GuardedStream(sys.stdout), _GuardedStream(sys.stderr)
        try:
            collect_inputs()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                try:
                    return fn(*args, **kwargs)
                finally:
                    stdout.flush()
                    stderr.flush()
        finally:
            _ACTIVE.reset(token)
    return wrapped
