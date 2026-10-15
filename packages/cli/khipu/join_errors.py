"""Error text for the join commands: Khipu's own messages, redacted, or a
fixed message. Kept free of heavy imports so join_pair can raise it."""

from __future__ import annotations

import re


class JoinError(ValueError, RuntimeError):
    """A message Khipu wrote itself: shown to the user after redaction. Any
    other exception's text can quote a database address (libpq parse errors
    quote the faulty token, which may be the password) and is never shown."""


_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]*@")
_PASSWORD_PARAM = re.compile(
    r"(?i)\b(password|passwd|pwd)(\s*=\s*)('(?:[^'\\]|\\.)*'?|[^\s&]+)"
)


def redact_connection_text(text: str) -> str:
    """Strip URL userinfo and password parameters, then the shared secret
    patterns. For text that must carry detail."""
    from khipu.redact import redact_secrets

    out = _URL_USERINFO.sub(r"\1[REDACTED]@", text or "")
    out = _PASSWORD_PARAM.sub(r"\1\2[REDACTED]", out)
    return redact_secrets(out, force=True)[0]


def public_error(exc: BaseException, action: str) -> str:
    """What a join command may print about ``exc``."""
    if isinstance(exc, JoinError):
        return redact_connection_text(str(exc))
    return (
        f"{action} failed ({type(exc).__name__}). The details are not shown because "
        "they can contain the database password."
    )
