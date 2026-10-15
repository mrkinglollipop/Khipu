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

CHECK_IDS = (
    "gemini_embed", "gemini_generate", "openai_compat_generate",
    "anthropic_models", "openai_models", "openrouter_models",
)
WHICH_VALUES = ("all", "gemini", "openai", "anthropic", "openai_key", "openrouter")

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

_BAD_KEY_DETAIL = "This key has characters a key can't have. Replace it."
_BAD_KEY_FIX = "Replace it"
_UNEXPECTED_DETAIL = "The check could not be completed — try again"
_MAX_KEY_LEN = 1024

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


def _fail_empty_generation(check_id: str, *, model: str, start: float) -> dict[str, Any]:
    return _result(
        check_id, ok=False, title="Key check failed",
        detail="The provider returned no usable generated text", model=model,
        seconds=_elapsed(start),
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


def _key_is_sane(key: object) -> bool:
    """True only for a non-empty run of visible ASCII, short enough to be a key.

    Anything else (CR/LF, NUL, spaces, non-Latin-1, a huge paste) would make the
    HTTP layer raise an error whose text can echo the key, so such a key is
    refused here, before any header is built or request made."""
    return (
        isinstance(key, str)
        and 0 < len(key) <= _MAX_KEY_LEN
        and all(0x21 <= ord(ch) <= 0x7E for ch in key)
    )


def _fail_bad_key(check_id: str, *, model: str | None, start: float) -> dict[str, Any]:
    return _result(
        check_id, ok=False, title="Key check failed", detail=_BAD_KEY_DETAIL,
        model=model, seconds=_elapsed(start), fix=_BAD_KEY_FIX,
    )


def _fail_unexpected(check_id: str, *, model: str | None, start: float) -> dict[str, Any]:
    """Fixed text for any error we did not foresee; never the exception's own."""
    return _result(
        check_id, ok=False, title="Key check failed", detail=_UNEXPECTED_DETAIL,
        model=model, seconds=_elapsed(start),
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
        embed_key = keychain.resolve_gemini_key()
    except RuntimeError:
        return _skip("gemini_embed", "Gemini embeddings")
    if not _key_is_sane(embed_key):
        return _fail_bad_key("gemini_embed", model=None, start=time.monotonic())
    try:
        active = embed_mod._profiles.resolve_spec(_active_embed_profile())
    except Exception:  # noqa: BLE001 - key verification must survive hub lookup failure
        active = None
    if active is not None and active.provider == "gemini":
        profile, model = active.id, active.model
    else:
        fallback = embed_mod._profiles.SEED_PROFILES[embed_mod.PROFILE_2]
        profile, model = fallback.id, fallback.model
    start = time.monotonic()
    try:
        embed_mod.embed_one(EMBED_PROBE_TEXT, profile=profile, retries=0, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - the check must not abort its siblings
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


def _has_generated_text(provider: str, response: str) -> bool:
    try:
        payload = json.loads(response)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    if provider == "gemini":
        candidates = payload.get("candidates", [])
        if not isinstance(candidates, list):
            return False
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            if not isinstance(content, dict):
                continue
            parts = content.get("parts", [])
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str) and part["text"].strip():
                    return True
        return False
    choices = payload.get("choices", [])
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message") or {}
        if not isinstance(message, dict):
            continue
        text = message.get("content", choice.get("text"))
        if isinstance(text, str) and text.strip():
            return True
    return False


def _check_gemini_generate(*, timeout: float) -> dict[str, Any]:
    try:
        key = keychain.resolve_gemini_key()
    except RuntimeError:
        return _skip("gemini_generate", "Gemini generation")
    if not _key_is_sane(key):
        return _fail_bad_key("gemini_generate", model=None, start=time.monotonic())
    settings = models_mod.synth_settings()
    provider = (settings.get("provider") or "cloud").strip().lower()
    model = (
        models_mod.cloud_model_id(settings)
        if provider in {"cloud", "gemini"}
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
        code, text = _post_once(url, body, headers, timeout)
    except (urllib.error.URLError, TimeoutError, OSError):
        return _fail_network("gemini_generate", model=model, start=start)
    except Exception:  # noqa: BLE001 - never surface text that may hold the key
        return _fail_unexpected("gemini_generate", model=model, start=start)
    if code == 200 and _has_generated_text("gemini", text):
        return _pass("gemini_generate", model=model, start=start)
    if code == 200:
        return _fail_empty_generation("gemini_generate", model=model, start=start)
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
        if not _key_is_sane(bearer):
            return _fail_bad_key("openai_compat_generate", model=model, start=time.monotonic())
        headers["Authorization"] = f"Bearer {bearer}"
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": GENERATE_PROBE_PROMPT}],
        "temperature": 0,
        "max_tokens": 16,
    }).encode("utf-8")
    start = time.monotonic()
    try:
        code, text = _post_once(url, body, headers, timeout)
    except (urllib.error.URLError, TimeoutError, OSError):
        return _fail_network("openai_compat_generate", model=model, start=start)
    except Exception:  # noqa: BLE001 - never surface text that may hold the key
        return _fail_unexpected("openai_compat_generate", model=model, start=start)
    if code == 200 and _has_generated_text("openai", text):
        return _pass("openai_compat_generate", model=model, start=start)
    if code == 200:
        return _fail_empty_generation("openai_compat_generate", model=model, start=start)
    return _fail_http("openai_compat_generate", code=code, model=model, start=start)


def _get_once(url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    """One GET, no retries. Same contract as ``_post_once``."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _check_key_listing(
    check_id: str, *, resolve: Any, url: str, headers: dict[str, str], label: str,
    timeout: float,
) -> dict[str, Any]:
    """Free, no-generation key proof: one GET that the provider only answers
    200 to for a valid key. The key lives in a local variable only."""
    try:
        key = resolve()
    except RuntimeError:
        return _skip(check_id, label)
    start = time.monotonic()
    if not _key_is_sane(key):
        return _fail_bad_key(check_id, model=label, start=start)
    try:
        code, _text = _get_once(
            url, {k: v.replace("{key}", key) for k, v in headers.items()}, timeout,
        )
    except (urllib.error.URLError, TimeoutError, OSError):
        return _fail_network(check_id, model=label, start=start)
    except Exception:  # noqa: BLE001 - never surface text that may hold the key
        return _fail_unexpected(check_id, model=label, start=start)
    if code == 200:
        return _pass(check_id, model=label, start=start)
    return _fail_http(check_id, code=code, model=label, start=start)


def _check_anthropic_models(*, timeout: float) -> dict[str, Any]:
    return _check_key_listing(
        "anthropic_models", resolve=keychain.resolve_anthropic_key,
        url="https://api.anthropic.com/v1/models?limit=1",
        headers={"x-api-key": "{key}", "anthropic-version": "2023-06-01"},
        label="Claude API", timeout=timeout,
    )


def _check_openai_models(*, timeout: float) -> dict[str, Any]:
    return _check_key_listing(
        "openai_models", resolve=keychain.resolve_openai_key,
        url="https://api.openai.com/v1/models",
        headers={"Authorization": "Bearer {key}"},
        label="OpenAI", timeout=timeout,
    )


def _check_openrouter_models(*, timeout: float) -> dict[str, Any]:
    return _check_key_listing(
        "openrouter_models", resolve=keychain.resolve_openrouter_key,
        url="https://openrouter.ai/api/v1/key",
        headers={"Authorization": "Bearer {key}"},
        label="OpenRouter", timeout=timeout,
    )


def check_model_keys(*, which: str = "all", timeout: float = 20.0) -> dict[str, Any]:
    """Prove every configured model key with one real, cheap call each.

    ``which``: "all" (default) — every configured provider; "gemini" — the
    Gemini embed + generate checks only; "openai" — the OpenAI-compatible
    local synth check only; "anthropic", "openai_key", "openrouter" — that
    provider's free key check only.

    Returns ``{"ok": bool, "checks": [...]}`` — ``ok`` is True iff no attempted
    check failed (a provider with nothing configured is skipped, not failed,
    so it never blocks ``ok``).
    """
    which = (which or "all").strip().lower()
    if which not in WHICH_VALUES:
        raise ValueError(f"unknown which={which!r}; expected one of {WHICH_VALUES}")
    checks: list[dict[str, Any]] = []

    def run(check_id: str, fn: Any) -> None:
        start = time.monotonic()
        try:
            checks.append(fn(timeout=timeout))
        except Exception:  # noqa: BLE001 - one check must never sink the rest or echo a key
            checks.append(_fail_unexpected(check_id, model=None, start=start))

    if which in ("all", "gemini"):
        run("gemini_embed", _check_gemini_embed)
        run("gemini_generate", _check_gemini_generate)
    if which in ("all", "openai"):
        run("openai_compat_generate", _check_openai_compat_generate)
    if which in ("all", "anthropic"):
        run("anthropic_models", _check_anthropic_models)
    if which in ("all", "openai_key"):
        run("openai_models", _check_openai_models)
    if which in ("all", "openrouter"):
        run("openrouter_models", _check_openrouter_models)
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}
