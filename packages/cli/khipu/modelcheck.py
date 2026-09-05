"""Prove a model key actually works — one real, cheap call, in plain words.

Scope doc: docs/plans/2026-09-05-setup-that-cannot-strand-you.md, "A model key
is proven on save with one real call". Today `khipu secrets --set` (and the
desktop Settings → Secrets pane) only proves the Keychain write succeeded —
never that the key the user pasted actually works against the provider. A
typo'd or revoked key looks identical to a good one until the next capture or
search silently degrades. ``check_model_keys`` closes that gap: for each
configured provider it makes exactly ONE real API call (never a retry loop —
this is a proof, not a production path) and reports a plain-words verdict the
desktop can show immediately after save, e.g. "Key works · gemini-embedding-2"
instead of just "Saved".

Three fixed checks:
  gemini_embed            embeds a 3-word probe with the ACTIVE embedding
                           profile's model (counts against the daily embed
                           budget like any other embed call — khipu.embed).
  gemini_generate          asks the configured cloud synth model (or the
                           default, if synth isn't cloud-configured) to answer
                           a one-word prompt.
  openai_compat_generate   same, against the configured local OpenAI-compatible
                           endpoint (Settings → Models, synth role "local").

Never logs, prints, or returns the key itself — only presence/absence and the
outcome of the call.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from khipu import embed as embed_mod
from khipu import keychain
from khipu import models as models_mod

CHECK_IDS = ("gemini_embed", "gemini_generate", "openai_compat_generate")
WHICH_VALUES = ("all", "gemini", "openai")

EMBED_PROBE_TEXT = "prove the key"  # exactly three words
GENERATE_PROBE_PROMPT = "Reply with the single word ok"

NO_KEY_DETAIL = "no key stored"

_UNAUTHORIZED_DETAIL = "The key was not accepted — paste it again from the provider's console"
_UNAUTHORIZED_FIX = "Paste it again from the provider's console"
_RATE_LIMIT_DETAIL = (
    "The provider is rate limiting this key right now — wait a minute and try "
    "again; the key itself works"
)
_RATE_LIMIT_FIX = "Wait a minute and try again"
_NETWORK_DETAIL = "Could not reach the provider — check the connection"
_NETWORK_FIX = "Check the connection"

_HTTP_CODE_RE = re.compile(r"HTTP (\d+)")


def _elapsed(start: float) -> float:
    return round(time.monotonic() - start, 3)


def _result(
    check_id: str,
    *,
    ok: bool,
    title: str,
    detail: str,
    model: str | None,
    seconds: float,
    fix: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": check_id,
        "ok": ok,
        "title": title,
        "detail": detail,
        "seconds": seconds,
        "model": model,
    }
    if fix:
        out["fix"] = fix
    return out


def _skip(check_id: str, _label: str) -> dict[str, Any]:
    return _result(
        check_id, ok=True, title="Not checked", detail=NO_KEY_DETAIL,
        model=None, seconds=0.0,
    )


def _pass(check_id: str, *, model: str, start: float) -> dict[str, Any]:
    return _result(
        check_id, ok=True, title=f"Key works · {model}", detail="",
        model=model, seconds=_elapsed(start),
    )


def _fail_http(check_id: str, *, code: int, model: str, start: float) -> dict[str, Any]:
    if code in (401, 403):
        detail, fix = _UNAUTHORIZED_DETAIL, _UNAUTHORIZED_FIX
    elif code == 429:
        detail, fix = _RATE_LIMIT_DETAIL, _RATE_LIMIT_FIX
    elif code == 404:
        detail = f"This key cannot use model {model} — pick another model in Settings"
        fix = "Pick another model in Settings"
    else:
        detail = f"The provider returned an unexpected error (HTTP {code})"
        fix = None
    return _result(
        check_id, ok=False, title="Key check failed", detail=detail,
        model=model, seconds=_elapsed(start), fix=fix,
    )


def _fail_network(check_id: str, *, model: str, start: float) -> dict[str, Any]:
    return _result(
        check_id, ok=False, title="Key check failed", detail=_NETWORK_DETAIL,
        model=model, seconds=_elapsed(start), fix=_NETWORK_FIX,
    )


def _fail_from_embed_exc(check_id: str, exc: Exception, *, model: str, start: float) -> dict[str, Any]:
    msg = str(exc)
    if "budget exhausted" in msg:
        return _result(
            check_id, ok=False, title="Key check failed",
            detail=(
                "Today's embed budget is used up — try again after the daily "
                "reset (UTC midnight); the key itself may still be fine"
            ),
            model=model, seconds=_elapsed(start),
            fix="Wait for the daily reset, or raise KHIPU_EMBED_DAILY_CALLS",
        )
    m = _HTTP_CODE_RE.search(msg)
    if m:
        return _fail_http(check_id, code=int(m.group(1)), model=model, start=start)
    return _fail_network(check_id, model=model, start=start)


def _active_embed_profile() -> str:
    """The profile ``khipu.embed`` actually searches with today.

    Falls back to ``PROFILE_2`` (gemini-embedding-2) when the hub is
    unreachable or unmigrated — this is a key-proving call, not a hub health
    check, so a DB hiccup should not block the answer to "does this Gemini key
    work".
    """
    try:
        from khipu.db import connect

        with connect() as conn:
            with conn.cursor() as cur:
                return embed_mod._active_profile(cur)
    except Exception:  # noqa: BLE001 — best-effort; embed check still runs
        return embed_mod.PROFILE_2


def _check_gemini_embed(*, timeout: float) -> dict[str, Any]:
    try:
        keychain.resolve_gemini_key()
    except RuntimeError:
        return _skip("gemini_embed", "Gemini embeddings")
    profile = _active_embed_profile()
    model = embed_mod.model_for_profile(profile)
    start = time.monotonic()
    try:
        embed_mod.embed_one(EMBED_PROBE_TEXT, profile=profile, retries=0, timeout=timeout)
    except RuntimeError as exc:
        return _fail_from_embed_exc("gemini_embed", exc, model=model, start=start)
    return _pass("gemini_embed", model=model, start=start)


def _post_once(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    """One POST, no retries. HTTP errors come back as (code, body); network
    failures (URLError/TimeoutError/OSError) propagate to the caller."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _check_gemini_generate(*, timeout: float) -> dict[str, Any]:
    try:
        key = keychain.resolve_gemini_key()
    except RuntimeError:
        return _skip("gemini_generate", "Gemini generation")
    settings = models_mod.synth_settings()
    provider = (settings.get("provider") or "cloud").strip().lower()
    model = (
        models_mod.cloud_model_id(settings)
        if provider == "cloud"
        else models_mod.DEFAULT_SYNTH_MODEL
    )
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": GENERATE_PROBE_PROMPT}]}],
        "generationConfig": {
            "temperature": 0, "maxOutputTokens": 16,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }).encode("utf-8")
    headers = {"Content-Type": "application/json", "x-goog-api-key": key}
    start = time.monotonic()
    try:
        code, _text = _post_once(url, body, headers, timeout)
    except (urllib.error.URLError, TimeoutError, OSError):
        return _fail_network("gemini_generate", model=model, start=start)
    if code == 200:
        return _pass("gemini_generate", model=model, start=start)
    return _fail_http("gemini_generate", code=code, model=model, start=start)


def _check_openai_compat_generate(*, timeout: float) -> dict[str, Any]:
    settings = models_mod.synth_settings()
    provider = (settings.get("provider") or "").strip().lower()
    endpoint = (settings.get("endpoint") or "").strip()
    model = (settings.get("model_id") or "").strip()
    if provider != "local" or not endpoint or not model:
        return _skip("openai_compat_generate", "OpenAI-compatible generation")
    url = models_mod.chat_completions_url(endpoint)
    headers = {"Content-Type": "application/json"}
    bearer = keychain.get_openai_compat_key()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": GENERATE_PROBE_PROMPT}],
        "temperature": 0,
        "max_tokens": 16,
    }).encode("utf-8")
    start = time.monotonic()
    try:
        code, _text = _post_once(url, body, headers, timeout)
    except (urllib.error.URLError, TimeoutError, OSError):
        return _fail_network("openai_compat_generate", model=model, start=start)
    if code == 200:
        return _pass("openai_compat_generate", model=model, start=start)
    return _fail_http("openai_compat_generate", code=code, model=model, start=start)


def check_model_keys(*, which: str = "all", timeout: float = 20.0) -> dict[str, Any]:
    """Prove every configured model key with one real, cheap call each.

    ``which``: "all" (default) — every configured provider; "gemini" — the
    Gemini embed + generate checks only; "openai" — the OpenAI-compatible
    local synth check only.

    Returns ``{"ok": bool, "checks": [...]}`` — ``ok`` is True iff no attempted
    check failed (a provider with nothing configured is skipped, not failed,
    so it never blocks ``ok``).
    """
    which = (which or "all").strip().lower()
    if which not in WHICH_VALUES:
        raise ValueError(f"unknown which={which!r}; expected one of {WHICH_VALUES}")
    checks: list[dict[str, Any]] = []
    if which in ("all", "gemini"):
        checks.append(_check_gemini_embed(timeout=timeout))
        checks.append(_check_gemini_generate(timeout=timeout))
    if which in ("all", "openai"):
        checks.append(_check_openai_compat_generate(timeout=timeout))
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}
