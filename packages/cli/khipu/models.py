"""Per-role model Settings (synth / embed / vision) in Hub ``config.json``.

Stored under the ``models`` key. Only synth routes this cut (see ``extract.py``);
embed is persist-only display/storage; vision defaults to ``off`` with no ingest.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import urlparse

from khipu.config import load_config, save_config

ROLES = ("synth", "embed", "vision")
PROVIDERS = frozenset({"cloud", "local"})
VISION_PROVIDERS = frozenset({"cloud", "local", "off"})
DEFAULT_SYNTH_MODEL = "gemini-2.5-flash"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def default_role(role: str) -> dict[str, str]:
    if role == "synth":
        return {
            "provider": "cloud",
            "endpoint": "",
            "model_id": DEFAULT_SYNTH_MODEL,
        }
    if role == "embed":
        return {"provider": "cloud", "endpoint": "", "model_id": ""}
    if role == "vision":
        return {"provider": "off", "endpoint": "", "model_id": ""}
    raise ValueError(f"unknown role: {role}")


def default_document() -> dict[str, Any]:
    return {
        "synth": default_role("synth"),
        "embed": default_role("embed"),
        "vision": default_role("vision"),
        "models_error": None,
    }


def normalize_endpoint(endpoint: str) -> str:
    """Strip trailing slash. Empty stays empty."""
    return (endpoint or "").strip().rstrip("/")


def validate_endpoint(endpoint: str) -> str:
    """Allow ``http://`` only for loopback; otherwise require ``https://``."""
    ep = normalize_endpoint(endpoint)
    if not ep:
        raise ValueError("endpoint is empty")
    parsed = urlparse(ep)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme == "http":
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                f"http:// is only allowed for loopback "
                f"(127.0.0.1, localhost, ::1); got host {host!r}"
            )
    elif scheme != "https":
        raise ValueError("endpoint must be http:// (loopback) or https://")
    return ep


def chat_completions_url(endpoint: str) -> str:
    """``{endpoint}/v1/chat/completions`` without doubling a trailing ``/v1``."""
    ep = normalize_endpoint(endpoint)
    if ep.endswith("/v1"):
        return f"{ep}/chat/completions"
    return f"{ep}/v1/chat/completions"


def _normalize_role(role: str, raw: Any, *, for_set: bool) -> dict[str, str]:
    del for_set  # reserved: show vs set share the same role rules today
    if not isinstance(raw, dict):
        raise ValueError(f"{role}: expected an object")
    provider = str(raw.get("provider", "") or "").strip().lower()
    allowed = VISION_PROVIDERS if role == "vision" else PROVIDERS
    if provider not in allowed:
        raise ValueError(
            f"{role}: unknown provider {provider!r}; expected one of {sorted(allowed)}"
        )
    endpoint = normalize_endpoint(str(raw.get("endpoint", "") or ""))
    model_id = str(raw.get("model_id", "") or "").strip()

    if provider == "off":
        # Vision-off: endpoint / model_id optional; do not require them.
        return {"provider": "off", "endpoint": endpoint, "model_id": model_id}

    if provider == "local":
        if not endpoint:
            raise ValueError(f"{role}: local provider requires endpoint")
        if not model_id:
            raise ValueError(f"{role}: local provider requires model_id")
        endpoint = validate_endpoint(endpoint)
        return {"provider": "local", "endpoint": endpoint, "model_id": model_id}

    # cloud
    if role == "synth" and not model_id:
        model_id = DEFAULT_SYNTH_MODEL
    if endpoint:
        # Persist a cleaned endpoint even on cloud (user may flip to local later).
        endpoint = validate_endpoint(endpoint) if endpoint else ""
    return {"provider": "cloud", "endpoint": endpoint, "model_id": model_id}


def validate_models_object(raw: Any, *, for_set: bool) -> dict[str, dict[str, str]]:
    if not isinstance(raw, dict):
        raise ValueError("models must be a JSON object")
    out: dict[str, dict[str, str]] = {}
    for role in ROLES:
        if role not in raw:
            if for_set:
                raise ValueError(f"models payload missing role: {role}")
            out[role] = default_role(role)
            continue
        out[role] = _normalize_role(role, raw[role], for_set=for_set)
    if for_set:
        # Replace requires exactly the three roles (extras refused).
        extra = set(raw) - set(ROLES)
        if extra:
            raise ValueError(f"models payload has unknown keys: {sorted(extra)}")
    return out


def show_models() -> dict[str, Any]:
    """Read path: fail closed to defaults + ``models_error`` on corrupt/invalid."""
    cfg = load_config()
    if "models" not in cfg:
        return default_document()
    raw = cfg.get("models")
    try:
        roles = validate_models_object(raw, for_set=False)
    except ValueError as e:
        out = default_document()
        out["models_error"] = str(e)
        return out
    return {
        "synth": roles["synth"],
        "embed": roles["embed"],
        "vision": roles["vision"],
        "models_error": None,
    }


def _store_roles(roles: dict[str, dict[str, str]]) -> dict[str, Any]:
    data = load_config()
    data["models"] = {
        role: {
            "provider": roles[role]["provider"],
            "endpoint": roles[role]["endpoint"],
            "model_id": roles[role]["model_id"],
        }
        for role in ROLES
    }
    save_config(data)
    return show_models()


def _stored_models_invalid() -> str | None:
    """Error string if on-disk ``models`` fails validation; else None.

    Missing key is first-time OK. Do not use ``show_models()`` alone as a
    write gate: show fail-closes to defaults + ``models_error``, which looks
    like a writable document if the error field is ignored.
    """
    cfg = load_config()
    if "models" not in cfg:
        return None
    try:
        validate_models_object(cfg.get("models"), for_set=False)
    except ValueError as e:
        return str(e)
    return None


def _require_stored_models_writable() -> None:
    err = _stored_models_invalid()
    if err:
        raise ValueError(f"stored models key is invalid: {err}")


def set_models_replace(payload: dict[str, Any]) -> dict[str, Any]:
    """JSON blob replace: all three roles required; invalid → raise, no write.

    Refuse without writing when the on-disk ``models`` key is present and
    fails validation (same gate as merge). Missing key is first-time OK.
    """
    _require_stored_models_writable()
    roles = validate_models_object(payload, for_set=True)
    return _store_roles(roles)


def set_models_merge_role(
    role: str,
    *,
    provider: str,
    endpoint: str | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Flag merge: update one role; leave the others unchanged.

    If the stored ``models`` key is corrupt/invalid (the same conditions that
    set ``models_error`` on show), raise and do not write. Missing key is fine
    and starts from defaults.
    """
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    _require_stored_models_writable()
    current = show_models()
    base = {r: dict(current[r]) for r in ROLES}
    patch: dict[str, Any] = {"provider": provider}
    if endpoint is not None:
        patch["endpoint"] = endpoint
    else:
        patch["endpoint"] = base[role].get("endpoint", "")
    if model_id is not None:
        patch["model_id"] = model_id
    else:
        patch["model_id"] = base[role].get("model_id", "")
    base[role] = _normalize_role(role, patch, for_set=True)
    return _store_roles(base)


def synth_settings() -> dict[str, str]:
    """Per-call synth role for ``extract._generate``.

    ``KHIPU_SYNTH_PROVIDER`` is tests-only: production ignores it unless
    ``KHIPU_TEST=1``. When both are set and the override is cloud/local, it
    replaces the saved provider (including a saved ``local``).
    """
    shown = show_models()
    role = dict(shown["synth"])
    test_gate = (os.environ.get("KHIPU_TEST") or "").strip() == "1"
    env = (os.environ.get("KHIPU_SYNTH_PROVIDER") or "").strip().lower()
    if test_gate and env in ("cloud", "local"):
        role["provider"] = env
    return role


def cloud_model_id(settings: dict[str, str] | None = None) -> str:
    """Cloud synth model id at call time; ``KHIPU_EXTRACT_MODEL`` overrides."""
    role = settings if settings is not None else synth_settings()
    model_id = (role.get("model_id") or "").strip() or DEFAULT_SYNTH_MODEL
    env = (os.environ.get("KHIPU_EXTRACT_MODEL") or "").strip()
    if env:
        return env
    return model_id


def dump_show_json(doc: dict[str, Any] | None = None) -> str:
    return json.dumps(doc if doc is not None else show_models(), indent=2)


def apply_welcome_models(
    *,
    synth_choice: str,
    embed_choice: str,
    synth_endpoint: str = "",
    synth_model_id: str = "",
    embed_endpoint: str = "",
    embed_model_id: str = "",
) -> dict[str, Any]:
    """Persist first-run synth/embed roles and activate embed when applicable."""
    synth_choice = (synth_choice or "skip").strip().lower()
    embed_choice = (embed_choice or "skip").strip().lower()
    if synth_choice not in {"cloud", "local", "skip"}:
        raise ValueError(f"unknown synth_choice: {synth_choice!r}")
    if embed_choice not in {"cloud", "local", "skip"}:
        raise ValueError(f"unknown embed_choice: {embed_choice!r}")

    current = show_models()
    synth = dict(current["synth"])
    embed = dict(current["embed"])

    if synth_choice == "cloud":
        synth = {"provider": "cloud", "endpoint": "", "model_id": DEFAULT_SYNTH_MODEL}
    elif synth_choice == "local":
        synth = {
            "provider": "local",
            "endpoint": synth_endpoint,
            "model_id": synth_model_id,
        }
    else:
        synth = {"provider": "cloud", "endpoint": "", "model_id": ""}

    if embed_choice == "cloud":
        embed = {
            "provider": "cloud",
            "endpoint": "",
            "model_id": "gemini-embedding-2",
        }
    elif embed_choice == "local":
        embed = {
            "provider": "local",
            "endpoint": embed_endpoint,
            "model_id": embed_model_id,
        }
    else:
        embed = {"provider": "cloud", "endpoint": "", "model_id": ""}

    roles = {
        "synth": _normalize_role("synth", synth, for_set=True),
        "embed": _normalize_role("embed", embed, for_set=True),
        "vision": dict(current["vision"]),
    }
    stored = _store_roles(roles)
    embed_result: dict[str, Any] = {"skipped": True}
    if embed_choice == "cloud":
        from khipu.embed import activate_welcome_embed

        embed_result = activate_welcome_embed(provider="cloud")
    return {"ok": True, "models": stored, "embed": embed_result}
