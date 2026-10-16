"""The one rule for "this error means the hub could not be reached".

Only a connection-level failure is offline: the connect failed, timed out or
was refused, the name did not resolve, or the connection dropped. A statement
timeout, a cancelled query or any other error from a hub that answered is a
real failure, never evidence of an outage (the promotion gate and doctor
excuse offline evidence, so a wrong "offline" would hide a real fault).

Callers classify a recorded error string (an outbox job's ``last_error`` or
``reason``: ``"ClassName: message"``, or the class name alone) with
``unreachable_text``, and a live exception with ``unreachable_error``. A test
(tests/test_hub_errors.py) fails when another module keeps its own match.
"""

from __future__ import annotations

# Class names a failed connect or a dropped connection raises. Plain
# ``OperationalError`` is what libpq raises for a connect that fails or a
# connection that drops; its SQLSTATE subclasses (QueryCanceled, DiskFull,
# AdminShutdown...) carry their own names and come from a hub that answered.
_CONNECTION_CLASSES = frozenset({
    "OperationalError", "InterfaceError", "ConnectionTimeout", "ConnectionError", "ConnectionRefusedError",
    "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError", "TimeoutError", "timeout",
    "gaierror", "herror",
})
# Words a wrapped connection failure carries in its message.
_CONNECTION_PHRASES = (
    "could not connect", "connection refused", "connection timed out", "connection timeout",
    "timeout expired", "connection to server", "server closed the connection", "connection is lost",
    "connection is closed", "could not translate host name", "network is unreachable", "no route to host",
    "name or service not known", "nodename nor servname", "temporary failure in name resolution",
    "no khipu dsn",
)
# A statement the hub ran and stopped: the hub answered.
_ANSWERED_PHRASES = ("canceling statement", "statement timeout", "lock timeout", "querycanceled")


def unreachable_text(text: str | None) -> bool:
    """Whether a recorded error says the hub could not be reached. ``text``
    is ``"ClassName: message"``, or the class name alone, optionally after a
    word of context (``"mirror OperationalError"``)."""
    if not text:
        return False
    words = str(text).partition(":")[0].split()
    name = words[-1] if words else ""
    lowered = str(text).lower()
    if any(p in lowered for p in _ANSWERED_PHRASES):
        return False
    return name in _CONNECTION_CLASSES or any(p in lowered for p in _CONNECTION_PHRASES)


def unreachable_error(exc: BaseException) -> bool:
    """The same rule for a live exception."""
    return unreachable_text(f"{type(exc).__name__}: {exc}")
