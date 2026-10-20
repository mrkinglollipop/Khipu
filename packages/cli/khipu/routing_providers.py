"""Owner provider facts and advisory model checks. No dispatch is blocked here."""
from __future__ import annotations

import re


# Adapter names describe existing local probes; http-quota is reserved for 9b.
ADAPTERS = frozenset({"claude", "codex", "grok", "muse", "glm", "http-quota", "none"})
FIELDS = frozenset({"id", "label", "family", "login", "probe", "host", "lane_names",
                    "models_allowed", "models_never", "use"})
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def checked(value):
    if not isinstance(value, dict) or set(value) - FIELDS or not {"id", "label"} <= value.keys():
        return None
    for field, item in value.items():
        if field in {"lane_names", "models_allowed", "models_never"}:
            if not isinstance(item, list) or len(item) > 128 or any(
                    not isinstance(v, str) or not NAME.fullmatch(v) for v in item):
                return None
        elif not isinstance(item, str) or not item.strip():
            return None
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", value["id"]):
        return None
    if value.get("login", "none") not in {"folder", "none"} or value.get("probe", "none") not in ADAPTERS:
        return None
    if value.get("use", "normal") not in {"normal", "on_request"}:
        return None
    host = value.get("host")
    if host is not None and (len(host) > 253 or not re.fullmatch(
            r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?", host) or ".." in host):
        return None
    if "family" in value and any(c in value["family"] for c in "/\\|"):
        return None
    return {k: list(v) if isinstance(v, list) else v for k, v in value.items()}


def advisory(provider: dict, model: str | None, *, requested: bool = False) -> list[str]:
    """Exact declared model ids, never inferred aliases or a routing decision."""
    warnings = []
    label = provider.get("label") or provider["id"]
    if model in provider.get("models_never", []):
        warnings.append(f"{label}: model {model} is listed in models_never.")
    allowed = provider.get("models_allowed")
    if allowed is not None and model not in allowed:
        warnings.append(f"{label}: model {model or 'unknown'} is outside models_allowed ({', '.join(allowed)}).")
    if provider.get("use") == "on_request" and not requested:
        warnings.append(f"{label}: use is on_request; an owner request is needed for this dispatch.")
    return warnings


def line(provider: dict) -> str:
    parts = []
    for field in ("models_allowed", "models_never"):
        if field in provider:
            parts.append(f"{field}: [{', '.join(provider[field])}]")
    if provider.get("use") == "on_request":
        parts.append("use: on_request")
    return f"Provider {provider.get('label') or provider['id']}: " + "; ".join(parts) if parts else ""
