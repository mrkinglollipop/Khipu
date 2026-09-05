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
- people: list of names/handles mentioned as participants or subjects
- decisions: list of strings
- preferences: list of strings
- scope: short string
- open_loops: list of objects {{text, kind, due_after, owner, future_trigger}}. BE STRICT —
  an empty list is the right answer for most sessions. Include an item ONLY if
  it is (a) something the USER must decide, provide, approve, or do, or (b)
  something you explicitly promised to do in a FUTURE session and cannot
  finish in this one.
  NEVER include:
    * in-progress status of any kind — "is running", "still building",
      "pending", "in flight", "waiting on/for", "awaiting";
    * anything about agents, subagents, drives, notifications, reports
      arriving, verdicts, or polling;
    * inter-session or inter-agent coordination — "send/receive ... message",
      "screen free/busy", "ping the session";
    * steps of your OWN plan for this session (you will finish them here);
    * anything this window already reports as completed;
    * vague items with no concrete action or actor.
  GOOD: "Matt to decide whether 0.3.17 ships with the seal fix or waits";
  "Send Matt the Linode restore-drill numbers next session";
  "Matt must supply the second Mac's hostname before soak can start";
  "Approve the AGPL CLA wording".
  BAD: "Drive 46 is still running"; "Generate the UI mocks" (a step of this
  session's own plan); "Send 'screen free' message to the Khipu session";
  "Receive the report from the phase 1-2 agent"; "Visual check agent's
  verdict is pending".
  kind is one of "followup", "blocker", "question", "promise".
  owner is "user" (the USER must decide/provide/approve/do it, or it is a
  question for them) or "assistant" — always answer it.
  future_trigger is true ONLY when the text names an explicit cross-session
  condition ("when Matt says the lane is re-authed, ...", "next session",
  "after the wave merges", "if attempt six dies"); false otherwise.
  NEVER record a within-session reporting duty — "reply with the SHAs",
  "tell the user when it relaunches", "notify Matt", "report back",
  "provide the evidence paths" — unless the USER is the one who owes it.
  due_after is optional; omit or use null when unknown.
- closed_loops: list of objects {{text}} for anything explicitly finished,
  merged, shipped, or no longer needed this turn.
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


def _as_open_loops(v: Any) -> list[dict[str, Any]]:
    if not isinstance(v, list):
        return []
    out: list[dict[str, Any]] = []
    for item in v:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"text": text, "kind": "followup", "due_after": None,
                            "owner": None, "future_trigger": None})
            continue
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        kind = str(item.get("kind") or "followup").strip().lower()
        if kind not in ("followup", "blocker", "question", "promise"):
            kind = "followup"
        out.append({
            "text": text,
            "kind": kind,
            "due_after": item.get("due_after") or None,
            "owner": item.get("owner") or None,
            # Carried through for the record; khipu.commitments decides the
            # stored value deterministically (has_future_trigger wins).
            "future_trigger": item.get("future_trigger"),
        })
    return out


def _as_closed_loops(v: Any) -> list[dict[str, Any]]:
    if not isinstance(v, list):
        return []
    out: list[dict[str, Any]] = []
    for item in v:
        text = item.get("text") if isinstance(item, dict) else item
        text = str(text or "").strip()
        if text:
            out.append({"text": text})
    return out


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
    # The cwd basename used to be force-appended here as a topic slug — the
    # single largest source of dangling/worktree-hash topic nodes (memory
    # reliability audit 2026-09-03: 'aegis' 423, a worktree slug 406, 'tmp'
    # 378). The project identity now belongs in the capture payload's own
    # `project` field (khipu.identity, set by the hook), not in topics.
    return {
        "summary": summary,
        "topics": topics,
        "people": _as_str_list(parsed.get("people")),
        "decisions": _as_str_list(parsed.get("decisions")),
        "preferences": _as_str_list(parsed.get("preferences")),
        "scope": str(parsed.get("scope", "")).strip(),
        "open_loops": _as_open_loops(parsed.get("open_loops")),
        "closed_loops": _as_closed_loops(parsed.get("closed_loops")),
        "edges": [],
        "topic_pages": [],
    }
