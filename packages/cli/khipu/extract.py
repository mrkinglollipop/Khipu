"""Transcript → durable-memory extraction (the LLM step of capture).

Khipu's role in the harnesses that already have a legacy extractor (Claude Code,
Codex: ``precompact_flush.py``; Cursor: its own hooks) is only to *sync* what
they write. Aegis has no legacy extractor, so Khipu owns the whole step there:
this module turns a transcript window into the same episode shape the legacy
extractor produces (summary / topics / decisions / preferences / scope), so the
resulting rows are indistinguishable downstream (PG, file mirror, embeddings,
topic pages, nightly consolidate).

Synth routing (Settings → Models): cloud = Gemini REST; local = OpenAI-compatible
``/v1/chat/completions``. Embeddings stay on the active Gemini profile.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

# Same contract as the legacy extractor so episodes look the same downstream.
PROMPT = """You are extracting durable memory from an assistant/coding session.
Output ONLY a single JSON object (no prose, no markdown fences) with these keys:
- summary: 1-3 sentence what-happened
- topics: list of short lowercase slug strings
- decisions: list of strings
- preferences: list of strings
- scope: short string
Capture concrete decisions, user preferences/corrections, project state, file paths in play, and ruled-out dead ends.
Return a summary of empty string if nothing durable.

Session project (cwd): {cwd}

Transcript:
{transcript}
"""


def _key() -> str:
    from khipu.keychain import resolve_gemini_key

    key = resolve_gemini_key()
    if not key:
        raise RuntimeError("Gemini API key not found (Keychain / env / file)")
    return key


def _generate_cloud(prompt: str, *, model_id: str, timeout: int, retries: int) -> str:
    # Header auth, not ?key=. Same reasoning as khipu.embed: a live credential
    # in a URL is one logging or exception-formatting change away from the
    # logs. The embed module was fixed in the 2026-08-17 audit and this one was
    # missed in the same pass — the sweep for the pattern was not done.
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model_id}:generateContent"
    )
    headers = {"Content-Type": "application/json", "x-goog-api-key": _key()}
    body = json.dumps({
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        # 2.5-flash "thinks" by default and thinking tokens count against
        # maxOutputTokens — with a small cap the JSON came back truncated
        # (first real Aegis run, 2026-08-17). Extraction needs no thinking.
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 4096,
                             "responseMimeType": "application/json",
                             "thinkingConfig": {"thinkingBudget": 0}},
    }).encode("utf-8")
    last: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")
            if e.code < 500 and e.code != 429:
                break
        except Exception as e:  # noqa: BLE001 — network / timeout; retry
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"extract: model call failed: {last}")


def _response_format_rejected(code: int, body: str) -> bool:
    """Retry without json_object on HTTP 400/415, or if the body names it."""
    if code in (400, 415):
        return True
    lower = (body or "").lower()
    return "response_format" in lower


def _generate_local(
    prompt: str,
    *,
    endpoint: str,
    model_id: str,
    timeout: int,
    retries: int,
) -> str:
    from khipu.keychain import get_openai_compat_key
    from khipu.models import chat_completions_url

    url = chat_completions_url(endpoint)
    headers = {"Content-Type": "application/json"}
    bearer = get_openai_compat_key()
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    def _post(*, with_response_format: bool) -> tuple[int, str]:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        if with_response_format:
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            code, text = _post(with_response_format=True)
            if code == 200:
                data = json.loads(text)
                choices = data.get("choices") or []
                msg = (choices[0] if choices else {}).get("message") or {}
                return str(msg.get("content") or "")
            if _response_format_rejected(code, text):
                code2, text2 = _post(with_response_format=False)
                if code2 == 200:
                    data = json.loads(text2)
                    choices = data.get("choices") or []
                    msg = (choices[0] if choices else {}).get("message") or {}
                    return str(msg.get("content") or "")
                last = RuntimeError(
                    f"HTTP {code2}: {text2[:300]}"
                )
                if code2 in (401, 403) or (code2 < 500 and code2 != 429):
                    break
            else:
                last = RuntimeError(f"HTTP {code}: {text[:300]}")
                # Do not retry response_format on 401/403; transport retries only
                # for 5xx / 429 like cloud.
                if code in (401, 403) or (code < 500 and code != 429):
                    break
        except Exception as e:  # noqa: BLE001 — network / timeout; retry
            last = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"extract: model call failed: {last}")


def _generate(prompt: str, *, timeout: int = 45, retries: int = 2) -> str:
    """Route by per-call ``models.synth`` (Settings). Local never calls ``_key()``."""
    from khipu.models import cloud_model_id, synth_settings

    settings = synth_settings()
    provider = (settings.get("provider") or "cloud").strip().lower()
    if provider == "local":
        endpoint = (settings.get("endpoint") or "").strip()
        model_id = (settings.get("model_id") or "").strip()
        if not endpoint or not model_id:
            raise RuntimeError(
                "local synth requires endpoint and model_id "
                "(Settings → Models, or `khipu models set`)"
            )
        return _generate_local(
            prompt,
            endpoint=endpoint,
            model_id=model_id,
            timeout=timeout,
            retries=retries,
        )
    return _generate_cloud(
        prompt,
        model_id=cloud_model_id(settings),
        timeout=timeout,
        retries=retries,
    )


def _as_str_list(v: Any, *, lower: bool = False) -> list[str]:
    if not isinstance(v, list):
        return []
    out = [str(x).strip() for x in v if str(x).strip()]
    return [x.lower() for x in out] if lower else out


def slugify(s: str) -> str:
    """Topic slugs match the legacy convention (``phase-f-calibration``): the
    model sometimes returns words with spaces even when asked for slugs."""
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)


def parse_model_json(text: str) -> dict[str, Any] | None:
    """Tolerate fences / leading prose: take the first {...} object."""
    text = (text or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def extract_memory(transcript: str, *, cwd: str = "") -> dict[str, Any] | None:
    """Return a capture payload (without ts/session_id) or None when the model
    judged nothing durable. Raises on transport/model failure so the caller can
    decide whether to retry later (it must NOT mark the window consumed)."""
    if not transcript.strip():
        return None
    raw = _generate(PROMPT.format(cwd=cwd or "(unknown)", transcript=transcript))
    parsed = parse_model_json(raw)
    if parsed is None:
        raise RuntimeError(f"extract: model returned non-JSON: {raw[:200]!r}")
    # A model that returns a list here used to become the episode summary via
    # str(), so the memory recorded a literal "['did a thing']" — garbage that
    # is then embedded and searched. Join a list, accept a string, reject the
    # rest rather than storing a repr (audit 2026-08-17, same class as the
    # capture payload fix).
    raw_summary = parsed.get("summary", "")
    if isinstance(raw_summary, list):
        summary = " ".join(str(x).strip() for x in raw_summary if str(x).strip()).strip()
    elif isinstance(raw_summary, str):
        summary = raw_summary.strip()
    else:
        summary = ""
    if not summary:
        return None
    topics: list[str] = []
    for t in _as_str_list(parsed.get("topics"), lower=True):
        t = slugify(t)
        if t and t not in topics:
            topics.append(t)
    if cwd:
        slug = slugify(os.path.basename(cwd.rstrip("/")))
        if slug and slug not in topics:
            topics.append(slug)
    return {
        "summary": summary,
        "topics": topics,
        "decisions": _as_str_list(parsed.get("decisions")),
        "preferences": _as_str_list(parsed.get("preferences")),
        "scope": str(parsed.get("scope", "")).strip(),
        "edges": [],
        "topic_pages": [],
    }
