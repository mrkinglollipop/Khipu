"""A wall-clock limit for one blocking stage of a timer run.

A hub that accepts the connection and then goes silent is not noticed by connect timeouts, server statement
timeouts or keepalives (the other end's kernel still answers), so one such call would hold the whole timer run
forever and the runs after it would never start. The limit works only in the main thread (the CLI's); anywhere
else it does nothing. It keeps firing every two seconds until the stage lets go, so a stage that catches the
first TimeoutError and goes on to call the hub again is stopped again.
"""
from __future__ import annotations

import contextlib
import signal
import threading


class StageTimeout(TimeoutError):
    pass


@contextlib.contextmanager
def limit(seconds: float, what: str):
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer"):
        yield
        return

    def fire(_signum, _frame):
        raise StageTimeout(f"{what} took longer than {seconds:g} seconds and was stopped")

    previous = signal.signal(signal.SIGALRM, fire)
    signal.setitimer(signal.ITIMER_REAL, seconds, 2.0)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
