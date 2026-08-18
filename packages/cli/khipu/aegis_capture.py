"""Compatibility name. The engine moved to :mod:`khipu.session_capture` on
2026-08-17 when it stopped being Aegis-only and became the native capture step
for every local harness. Aegis's own hook, the nightly and older callers still
import this name; keep it importing until they are all repointed.

``KHIPU_AEGIS_*`` env names are still honored by session_capture as fallbacks
for ``KHIPU_CAPTURE_*``.

Delegation is by lookup, not by copy: an earlier version did
``globals().update(vars(_sc))``, which snapshotted module constants at import
time, so a test that patched ``session_capture.MIN_TURNS`` saw the stale value
through this name (audit 2026-08-17). ``__getattr__`` (PEP 562) resolves every
access against the live module instead, and still satisfies
``from khipu.aegis_capture import _claim``-style imports that the verify probes
and older tests use.
"""
import khipu.session_capture as _sc

__all__ = [n for n in dir(_sc) if not n.startswith("__")]


def __getattr__(name: str):
    try:
        return getattr(_sc, name)
    except AttributeError as e:  # keep the module-not-attribute wording callers expect
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from e


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))
