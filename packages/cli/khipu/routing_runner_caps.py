"""Runner configs against Khipu's cap facts (slice 19b).

Compares a registered job runner's own ``budget`` declarations with the
owner-confirmed cap facts the folded view holds. Pure: opens nothing, writes
nothing, and never turns a runner number into a fact or a permission.
"""
from __future__ import annotations

_WINDOW_KINDS = frozenset(("share", "per_day", "pace_target"))
_CAP_VALUES = {"share": ("pct",), "per_day": ("pct",), "pace_target": ("pct", "slack"),
               "spend": ("usd", "days"), "concurrency": ("n",)}
_TEXT_MAX = 200


_UNNAMED_ACCOUNT = "an unnamed account"
_WITHHELD_LABEL = "(name withheld)"


def _account_label(account: dict) -> str:
    """The label a flag may show for a known account. A label that is, or
    contains, the account's login key (or any path/key shape) is withheld:
    a login key must never be echoed into an account label or any output."""
    label = account.get("label")
    if not isinstance(label, str) or not label.strip():
        return _UNNAMED_ACCOUNT
    label = label.strip()
    if label == account.get("key") or any(mark in label for mark in ("/", "\\", "|")):
        return _WITHHELD_LABEL
    return label


def compare_runner_caps(declarations: list[dict], accounts: list[dict], account_key: str | None = None) -> list[dict]:
    """Where each runner declaration has no Khipu cap fact, names no Khipu
    account, or disagrees with the fact it has. Declarations that agree are
    not reported. Only ``declared`` and ``fact`` carry numbers; ``text`` is
    Khipu's own words and never a runner's free text. A matched account is
    named by its label only, never by its key (a key holds its login
    folder); an unmatched account is named by a fixed phrase, never by the
    name the runner wrote (a letters-only key cannot be told from a name).
    When ``account_key`` is given, only that account's flags are kept."""
    by_key: dict[str, dict] = {}
    by_label: dict[str, list[dict]] = {}
    for entry in accounts or []:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            continue
        by_key[entry["key"]] = entry
        label = entry.get("label")
        if isinstance(label, str) and label.strip():
            by_label.setdefault(label.strip().casefold(), []).append(entry)
    flags: list[dict] = []
    for declaration in declarations or []:
        if not isinstance(declaration, dict):
            continue
        kind = declaration.get("kind")
        if kind not in _CAP_VALUES:
            continue
        runner = str(declaration.get("runner") or "a job runner")
        raw_account = declaration.get("account")
        matched = _runner_account(raw_account, by_key, by_label)
        if account_key is not None and (matched is None or matched.get("key") != account_key):
            continue
        declared = _declared_values(declaration)
        window = declaration.get("window") if kind in _WINDOW_KINDS else None
        if matched is None:
            flags.append({
                "runner": runner, "account": None, "account_words": _UNNAMED_ACCOUNT,
                "kind": kind, "window": window, "declared": declared, "fact": None,
                "status": "no_account",
                "text": _clip(f"{runner}'s budget names {_UNNAMED_ACCOUNT}, which isn't a "
                              f"Khipu account; its {_cap_words(kind, window)} of "
                              f"{_value_words(kind, declaration)} has no fact to match.")})
            continue
        label = _account_label(matched)
        facts = _facts_for(matched, kind, window, declaration.get("purpose", "job"))
        if not facts:
            flags.append({
                "runner": runner, "account": label, "account_words": label,
                "kind": kind, "window": window, "declared": declared, "fact": None,
                "status": "no_fact",
                "text": _clip(f"{runner}'s budget sets {label} a "
                              f"{_cap_words(kind, window)} of {_value_words(kind, declaration)}; "
                              "Khipu holds no such cap.")})
            continue
        if any(_same_cap(kind, declaration, fact) for fact in facts):
            continue
        fact = facts[0]
        flags.append({
            "runner": runner, "account": label, "account_words": label,
            "kind": kind, "window": window, "declared": declared, "fact": _fact_values(fact, kind),
            "status": "disagrees",
            "text": _clip(f"{runner}'s budget sets {label} a "
                          f"{_cap_words(kind, window)} of {_value_words(kind, declaration)}; "
                          f"Khipu's cap is {_value_words(kind, fact)}.")})
    flags.sort(key=lambda f: (f["runner"], f["account"] or "", f["kind"], f["window"] or ""))
    return flags


def _runner_account(account, by_key: dict[str, dict], by_label: dict[str, list[dict]]) -> dict | None:
    if not isinstance(account, str) or not account:
        return None
    if account in by_key:
        return by_key[account]
    matches = by_label.get(account.strip().casefold()) or []
    return matches[0] if len(matches) == 1 else None


def _facts_for(account: dict, kind: str, window, purpose: str = "job") -> list[dict]:
    out = []
    for fact in account.get("caps") or []:
        if not isinstance(fact, dict) or fact.get("kind") != kind:
            continue
        if fact.get("purpose", "job") != purpose:
            continue
        if kind in _WINDOW_KINDS and fact.get("window") != window:
            continue
        out.append(fact)
    return out


def _declared_values(declaration: dict) -> dict:
    out = {k: declaration[k] for k in ("kind", "window", "purpose", "tier", "sensitive_only") if declaration.get(k) is not None}
    for field in _CAP_VALUES[declaration["kind"]]:
        if declaration.get(field) is not None:
            out[field] = declaration[field]
    return out


def _fact_values(fact: dict, kind: str) -> dict:
    out = {"kind": kind}
    if fact.get("purpose", "job") != "job":
        out["purpose"] = fact["purpose"]
    out.update({k: fact[k] for k in ("tier", "sensitive_only") if k in fact})
    if fact.get("window") is not None:
        out["window"] = fact["window"]
    for field in _CAP_VALUES[kind]:
        if fact.get(field) is not None:
            out[field] = fact[field]
    if fact.get("revision") is not None:
        out["revision"] = fact["revision"]
    return out


def _same_cap(kind: str, declaration: dict, fact: dict) -> bool:
    if declaration.get("tier") != fact.get("tier") or bool(declaration.get("sensitive_only")) != bool(fact.get("sensitive_only")):
        return False
    for field in _CAP_VALUES[kind]:
        if declaration.get(field) != fact.get(field):
            return False
    return True


def _window_word(window) -> str:
    return str(window or "").replace("_", "-") or "account"


def _cap_words(kind: str, window) -> str:
    if kind == "share":
        return f"{_window_word(window)} share"
    if kind == "per_day":
        return "rolling daily cap"
    if kind == "pace_target":
        return f"{_window_word(window)} pace target"
    if kind == "spend":
        return "spend cap"
    return "concurrency cap"


def _value_words(kind: str, declaration: dict) -> str:
    if kind in ("share", "per_day"):
        return f"{_number_words(declaration.get('pct'))}%"
    if kind == "pace_target":
        return f"{_number_words(declaration.get('pct'))}% with {_number_words(declaration.get('slack'))} slack"
    if kind == "spend":
        return f"${_number_words(declaration.get('usd'))} in {_number_words(declaration.get('days'))} days"
    n = declaration.get("n")
    jobs = "job" if n == 1 else "jobs"
    return f"{_number_words(n)} {jobs}"


def _number_words(value) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) and not isinstance(value, bool) else "?"


def _clip(text: str) -> str:
    return text if len(text) <= _TEXT_MAX else text[: _TEXT_MAX - 1].rstrip() + "…"
