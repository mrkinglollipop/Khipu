"""Per-harness native packs — the engine under the Integrations pane (P3 step 4).

Scope locked 2026-08-17 (agent-integration note § "Integrations pane"): one
NATIVE pack per harness, in that harness's own config format and paths,
verified through that harness's own mechanism; install is not done until a
real probe passes; the Khipu capture hook is installed ALONGSIDE the legacy
capture_v2 hooks (dual-write) and never edits them.

Packs:
  claude_code  ~/.claude.json mcpServers.khipu
               ~/.claude/settings.json hooks.Stop / hooks.PreCompact → khipu-stop-hook
  cursor       ~/.cursor/mcp.json mcpServers.khipu
               ~/.cursor/hooks.json hooks.stop / hooks.preCompact → khipu-stop-hook
               ~/.cursor/hooks.json hooks.sessionStart += khipu-recall-hook
                 (--cursor → additional_context; timeout 30s for PG;
                 does not replace existing harness sessionStart entries)
               optional --project → .cursor/rules/khipu.mdc (pull)
  aegis        ~/.grok/config.toml [mcp_servers.khipu]
               ~/.grok/config.toml [[hooks.Stop]] / [[hooks.PreCompact]] → khipu-stop-hook
               ~/.grok/config.toml [[hooks.Stop]] / [[hooks.PreCompact]] / [[hooks.SessionEnd]]
                 → khipu-aegis-capture (Aegis-native EXTRACTION — Aegis has no legacy
                 capture_v2 hook, so this is what makes Aegis sessions produce episodes)
               (no recall rule: SessionStart/UserPromptSubmit are Observe gates — verified)
  codex        ~/.codex/config.toml [mcp_servers.khipu]            (TOML, like Aegis)
               ~/.codex/hooks.json hooks.Stop / PreCompact / SessionStart (Claude-shaped JSON —
               verified 2026-08-17: same event names + {type,command,timeout} entries)

Every write backs the file up first (``*.bak-khipu-<stamp>``); uninstall
removes only Khipu-owned entries (matched by our command path), so a
hand-written legacy hook is never touched. Detection = the harness's config
root exists; an undetected harness is reported, not errored.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HARNESSES = ("claude_code", "cursor", "aegis", "codex", "grok_bot")
HOME = Path.home()


def _root() -> Path:
    """The repo this code lives in — derived from the module's own path so an
    install on another machine writes correct commands without an env var
    (KHIPU_ROOT still wins). packages/cli/khipu/integrations.py -> repo root."""
    env = os.environ.get("KHIPU_ROOT")
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[3]


def _shim_dir() -> Path:
    # Read HOME at call time so tests that patch it get a per-test shim dir.
    return HOME / ".config" / "khipu" / "bin"


def _shim(name: str) -> str:
    """Return a SPACE-FREE, Khipu-owned path for a bin script, creating or
    re-pointing the symlink as needed.

    The repo may live under a path with spaces; every harness runs a
    hook command through ``sh -c``, so the raw path splits at the space and the
    hook dies with ``/bin/sh: /Volumes/Cloud: No such file or directory`` (found
    2026-08-17 on a real PreCompact — the probes had exec'd the path directly and
    missed it). A symlink under ~/.config/khipu/bin works whether the harness
    uses a shell or a direct exec, so it is what every pack references.
    """
    target = _root() / "packages" / "cli" / "bin" / name
    link = _shim_dir() / name
    try:
        if link.is_symlink() and os.readlink(link) == str(target):
            return str(link)
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)
    except OSError:
        return str(target)  # unwritable HOME: fall back to the raw path
    return str(link)


def mcp_launcher() -> str:
    return _shim("khipu-mcp")


def stop_hook() -> str:
    return _shim("khipu-stop-hook")


def recall_hook() -> str:
    return _shim("khipu-recall-hook")


# Cursor sessionStart must outlive a hub PG slice; harness session_start.sh stays at 5s.
CURSOR_RECALL_TIMEOUT = 30


def recall_hook_cursor() -> str:
    """Cursor sessionStart command: same binary, Cursor inject field via --cursor."""
    return f"{recall_hook()} --cursor"


def aegis_capture_hook() -> str:
    return _shim("khipu-aegis-capture")


def _is_our_capture(cmd: Any) -> bool:
    return isinstance(cmd, str) and "khipu-aegis-capture" in cmd


def _repoint(hooks_list: list[dict], is_ours, want: str) -> bool:
    """Re-point Khipu-owned hook entries whose command drifted from ``want``
    (e.g. an older install wrote the raw, space-containing repo path)."""
    changed = False
    for h in hooks_list:
        if is_ours(h.get("command")) and h.get("command") != want:
            h["command"] = want
            changed = True
    return changed


def _is_our_recall(cmd: Any) -> bool:
    return isinstance(cmd, str) and "khipu-recall-hook" in cmd


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup(path: Path) -> str | None:
    if not path.is_file():
        return None
    dst = path.with_name(path.name + f".bak-khipu-{_stamp()}")
    shutil.copy2(path, dst)
    return str(dst)


class ConfigUnreadable(RuntimeError):
    """An existing harness config could not be parsed, so it must not be rewritten."""


def _load_json(path: Path) -> dict:
    """Read a harness config. ABSENT or EMPTY is {}; PRESENT-BUT-UNPARSEABLE
    raises.

    This used to swallow the parse error and return {}. Every caller then did
    read → modify → write, so a single bad read replaced the whole file with
    Khipu's keys alone. For ``~/.claude.json`` that is 77 KB, 59 top-level keys,
    41 projects and 6 MCP servers destroyed by a config Khipu does not own — and
    the likeliest cause is the most mundane one, a partial read while the
    harness itself is saving the file (audit 2026-08-17).

    A BOM is tolerated (utf-8-sig) rather than treated as corruption: it is a
    real thing editors do, and refusing on it would be a false alarm.
    """
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as e:
        raise ConfigUnreadable(f"{path} could not be read ({e}); refusing to overwrite it") from e
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ConfigUnreadable(
            f"{path} is not valid JSON ({e}); refusing to overwrite it — "
            "check the file, then re-run"
        ) from e
    if not isinstance(data, dict):
        raise ConfigUnreadable(f"{path} is JSON but not an object; refusing to overwrite it")
    return data


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _is_ours(cmd: Any) -> bool:
    return isinstance(cmd, str) and "khipu-stop-hook" in cmd


# ---- Claude Code --------------------------------------------------------------

CLAUDE_JSON = HOME / ".claude.json"
CLAUDE_SETTINGS = HOME / ".claude" / "settings.json"


def _claude_detected() -> bool:
    return (HOME / ".claude").is_dir()


def _claude_install(dry: bool) -> dict:
    out: dict[str, Any] = {"harness": "claude_code", "detected": _claude_detected(), "changes": []}
    if not out["detected"]:
        return out
    # MCP
    d = _load_json(CLAUDE_JSON)
    cur = d.get("mcpServers", {}).get("khipu")
    want = {"command": mcp_launcher()}
    if cur != want:
        out["changes"].append(f"{CLAUDE_JSON}: mcpServers.khipu -> {want['command']}")
        if not dry:
            out.setdefault("backups", []).append(_backup(CLAUDE_JSON))
            d.setdefault("mcpServers", {})["khipu"] = want
            _write_json(CLAUDE_JSON, d)
    # Hooks: append a Khipu-owned entry to Stop + PreCompact + SessionEnd if not
    # present. SessionEnd is the "quit without compacting" net: since 2026-08-17
    # this hook is the harness's actual capture step, not just a tail sync.
    s = _load_json(CLAUDE_SETTINGS)
    hooks = s.setdefault("hooks", {})
    changed = False
    for event in ("Stop", "PreCompact", "SessionEnd"):
        entries = hooks.setdefault(event, [])
        flat = [h for e in entries for h in e.get("hooks", [])]
        if not any(_is_ours(h.get("command")) for h in flat):
            entries.append({"hooks": [{"type": "command", "command": stop_hook(), "timeout": 20}]})
            out["changes"].append(f"{CLAUDE_SETTINGS}: hooks.{event} += khipu-stop-hook")
            changed = True
        elif _repoint(flat, _is_ours, stop_hook()):
            out["changes"].append(f"{CLAUDE_SETTINGS}: hooks.{event} khipu-stop-hook -> {stop_hook()}")
            changed = True
    # Recall rule: SessionStart additionalContext (thin cadence rule, not memory content).
    ss = hooks.setdefault("SessionStart", [])
    flat = [h for e in ss for h in e.get("hooks", [])]
    if not any(_is_our_recall(h.get("command")) for h in flat):
        ss.append({"hooks": [{"type": "command", "command": recall_hook(), "timeout": 10}]})
        out["changes"].append(f"{CLAUDE_SETTINGS}: hooks.SessionStart += khipu-recall-hook")
        changed = True
    elif _repoint(flat, _is_our_recall, recall_hook()):
        out["changes"].append(f"{CLAUDE_SETTINGS}: hooks.SessionStart khipu-recall-hook -> {recall_hook()}")
        changed = True
    if changed and not dry:
        out.setdefault("backups", []).append(_backup(CLAUDE_SETTINGS))
        _write_json(CLAUDE_SETTINGS, s)
    return out


def _claude_uninstall(dry: bool) -> dict:
    out: dict[str, Any] = {"harness": "claude_code", "changes": []}
    d = _load_json(CLAUDE_JSON)
    if "khipu" in d.get("mcpServers", {}):
        out["changes"].append(f"{CLAUDE_JSON}: remove mcpServers.khipu")
        if not dry:
            out.setdefault("backups", []).append(_backup(CLAUDE_JSON))
            d["mcpServers"].pop("khipu")
            _write_json(CLAUDE_JSON, d)
    s = _load_json(CLAUDE_SETTINGS)
    changed = False
    for event in ("Stop", "PreCompact", "SessionEnd", "SessionStart"):
        entries = s.get("hooks", {}).get(event, [])
        kept = []
        for e in entries:
            e_hooks = [h for h in e.get("hooks", [])
                       if not (_is_ours(h.get("command")) or _is_our_recall(h.get("command")))]
            if len(e_hooks) != len(e.get("hooks", [])):
                changed = True
                out["changes"].append(f"{CLAUDE_SETTINGS}: hooks.{event} -= khipu hook")
            if e_hooks:
                e = dict(e)
                e["hooks"] = e_hooks
                kept.append(e)
        if event in s.get("hooks", {}):
            s["hooks"][event] = kept
    if changed and not dry:
        out.setdefault("backups", []).append(_backup(CLAUDE_SETTINGS))
        _write_json(CLAUDE_SETTINGS, s)
    return out


def _claude_status() -> dict:
    d = _load_json(CLAUDE_JSON)
    s = _load_json(CLAUDE_SETTINGS)
    mcp = d.get("mcpServers", {}).get("khipu", {}).get("command") == mcp_launcher()
    def has(ev: str) -> bool:
        return any(_is_ours(h.get("command")) for e in s.get("hooks", {}).get(ev, []) for h in e.get("hooks", []))
    rule = any(_is_our_recall(h.get("command"))
               for e in s.get("hooks", {}).get("SessionStart", []) for h in e.get("hooks", []))
    native = has("Stop") and has("PreCompact")
    return {"harness": "claude_code", "detected": _claude_detected(), "mcp": mcp,
            "hook_stop": has("Stop"), "hook_precompact": has("PreCompact"), "hook_sessionend": has("SessionEnd"),
            "recall_rule": "installed" if rule else "missing",
            # Khipu-native extraction rides on this same hook (session_capture);
            # "legacy" was the model-driven capture_v2 nudge, which is now only
            # a parallel writer until the soak-gated legacy removal.
            "extract": "installed" if native else "missing"}


# ---- Cursor -------------------------------------------------------------------

CURSOR_MCP = HOME / ".cursor" / "mcp.json"
CURSOR_HOOKS = HOME / ".cursor" / "hooks.json"


def _cursor_detected() -> bool:
    return (HOME / ".cursor").is_dir()


CURSOR_RULE_NAME = "khipu.mdc"


def _cursor_rule_path(project: str | None) -> Path | None:
    return Path(project).expanduser() / ".cursor" / "rules" / CURSOR_RULE_NAME if project else None


def _cursor_install(dry: bool, project: str | None = None) -> dict:
    out: dict[str, Any] = {"harness": "cursor", "detected": _cursor_detected(), "changes": []}
    if not out["detected"]:
        return out
    # Recall rule is PROJECT-scoped: Cursor's global User Rules live in app state, not a
    # writable file. Only written when --project is given; never guessed.
    rp = _cursor_rule_path(project)
    if rp is not None:
        from khipu.recall_rule import cursor_mdc

        want = cursor_mdc()
        if not rp.is_file() or rp.read_text(encoding="utf-8") != want:
            out["changes"].append(f"{rp}: write recall rule")
            if not dry:
                if rp.is_file():
                    out.setdefault("backups", []).append(_backup(rp))
                rp.parent.mkdir(parents=True, exist_ok=True)
                rp.write_text(want, encoding="utf-8")
    d = _load_json(CURSOR_MCP)
    want = {"command": mcp_launcher()}
    if d.get("mcpServers", {}).get("khipu") != want:
        out["changes"].append(f"{CURSOR_MCP}: mcpServers.khipu -> {want['command']}")
        if not dry:
            out.setdefault("backups", []).append(_backup(CURSOR_MCP))
            d.setdefault("mcpServers", {})["khipu"] = want
            _write_json(CURSOR_MCP, d)
    h = _load_json(CURSOR_HOOKS)
    hooks = h.setdefault("hooks", {})
    changed = False
    for event in ("stop", "preCompact"):
        entries = hooks.setdefault(event, [])
        if not any(_is_ours(e.get("command")) for e in entries):
            entries.append({"command": stop_hook(), "timeout": 20})
            out["changes"].append(f"{CURSOR_HOOKS}: hooks.{event} += khipu-stop-hook")
            changed = True
        elif _repoint(entries, _is_ours, stop_hook()):
            out["changes"].append(f"{CURSOR_HOOKS}: hooks.{event} khipu-stop-hook -> {stop_hook()}")
            changed = True
    # Push recall: append a second sessionStart entry. Do not replace harness
    # session_start.sh (Cursor runs all matching hooks; inject field is
    # additional_context — verified vs Claude additionalContext).
    ss = hooks.setdefault("sessionStart", [])
    want_recall = recall_hook_cursor()
    if not any(_is_our_recall(e.get("command")) for e in ss):
        ss.append({"command": want_recall, "timeout": CURSOR_RECALL_TIMEOUT})
        out["changes"].append(
            f"{CURSOR_HOOKS}: hooks.sessionStart += khipu-recall-hook "
            f"(additional_context, timeout={CURSOR_RECALL_TIMEOUT})"
        )
        changed = True
    else:
        for e in ss:
            if not _is_our_recall(e.get("command")):
                continue
            if e.get("command") != want_recall or e.get("timeout") != CURSOR_RECALL_TIMEOUT:
                e["command"] = want_recall
                e["timeout"] = CURSOR_RECALL_TIMEOUT
                out["changes"].append(
                    f"{CURSOR_HOOKS}: hooks.sessionStart khipu-recall-hook -> "
                    f"cursor shape timeout={CURSOR_RECALL_TIMEOUT}"
                )
                changed = True
    if changed and not dry:
        out.setdefault("backups", []).append(_backup(CURSOR_HOOKS))
        h.setdefault("version", 1)
        _write_json(CURSOR_HOOKS, h)
    return out


def _cursor_uninstall(dry: bool, project: str | None = None) -> dict:
    out: dict[str, Any] = {"harness": "cursor", "changes": []}
    rp = _cursor_rule_path(project)
    if rp is not None and rp.is_file():
        out["changes"].append(f"{rp}: remove recall rule")
        if not dry:
            out.setdefault("backups", []).append(_backup(rp))
            rp.unlink()
    d = _load_json(CURSOR_MCP)
    if "khipu" in d.get("mcpServers", {}):
        out["changes"].append(f"{CURSOR_MCP}: remove mcpServers.khipu")
        if not dry:
            out.setdefault("backups", []).append(_backup(CURSOR_MCP))
            d["mcpServers"].pop("khipu")
            _write_json(CURSOR_MCP, d)
    h = _load_json(CURSOR_HOOKS)
    changed = False
    for event in ("stop", "preCompact", "sessionStart"):
        entries = h.get("hooks", {}).get(event, [])
        if event == "sessionStart":
            kept = [e for e in entries if not _is_our_recall(e.get("command"))]
            label = "khipu-recall-hook"
        else:
            kept = [e for e in entries if not _is_ours(e.get("command"))]
            label = "khipu-stop-hook"
        if len(kept) != len(entries):
            changed = True
            out["changes"].append(f"{CURSOR_HOOKS}: hooks.{event} -= {label}")
            h["hooks"][event] = kept
    if changed and not dry:
        out.setdefault("backups", []).append(_backup(CURSOR_HOOKS))
        _write_json(CURSOR_HOOKS, h)
    return out


def _cursor_status() -> dict:
    d = _load_json(CURSOR_MCP)
    h = _load_json(CURSOR_HOOKS)
    def has(ev: str) -> bool:
        return any(_is_ours(e.get("command")) for e in h.get("hooks", {}).get(ev, []))
    def has_recall(ev: str) -> bool:
        return any(_is_our_recall(e.get("command")) for e in h.get("hooks", {}).get(ev, []))
    return {"harness": "cursor", "detected": _cursor_detected(),
            "mcp": d.get("mcpServers", {}).get("khipu", {}).get("command") == mcp_launcher(),
            "hook_stop": has("stop"), "hook_precompact": has("preCompact"),
            "hook_sessionstart": has_recall("sessionStart"),
            "recall_rule": "project_scoped",
            "extract": "installed" if has("stop") and has("preCompact") else "missing"}


# ---- Aegis (TOML, textual edit — no toml writer in stdlib) ---------------------

AEGIS_TOML = HOME / ".grok" / "config.toml"
# Stops at the next table header OR at our managed hooks block, so it never
# swallows the "# khipu-pack" marker line that sits between them.
_AEGIS_MCP_RE = re.compile(r"(?ms)^\[mcp_servers\.khipu\]\n.*?(?=^\[|^# khipu-pack:|\Z)")
_AEGIS_HOOK_MARK = "# khipu-pack: managed block — do not edit by hand\n"
_AEGIS_HOOK_RE = re.compile(
    r"(?ms)^# khipu-pack: managed block — do not edit by hand\n.*?# khipu-pack: end\n"
)


def _aegis_detected() -> bool:
    return (HOME / ".grok").is_dir()


def _aegis_blocks() -> tuple[str, str]:
    mcp = (
        "[mcp_servers.khipu]\n"
        f'command = "{mcp_launcher()}"\n'
        "enabled = true\n"
        "startup_timeout_sec = 30\n"
    )
    hooks = _AEGIS_HOOK_MARK
    # ONE Khipu hook per event: the Aegis-native capture trigger. The tail-sync
    # hook the other harnesses run is deliberately NOT installed here — Aegis
    # sandboxes its hooks, and the tail sync needs to read the legacy Memory tree
    # and reach Postgres, both denied in that sandbox (audit 2026-08-17). The
    # capture hook only queues; `khipu aegis drain` does the model + database
    # work outside the sandbox.
    #
    # `env = { KHIPU_HARNESS = "aegis" }` is the pack's signature: Khipu's hook
    # scripts refuse to run under Aegis unless it is present, so a vendor-compat
    # import of another harness's config (e.g. ~/.claude/settings.json) can never
    # run Khipu inside Aegis. Aegis is its own harness; Khipu integrates natively.
    for ev in ("Stop", "PreCompact", "SessionEnd"):
        hooks += (
            f"[[hooks.{ev}]]\n"
            f"  [[hooks.{ev}.hooks]]\n"
            '  type = "command"\n'
            f'  command = "{aegis_capture_hook()}"\n'
            "  timeout = 15\n"
            '  env = { KHIPU_HARNESS = "aegis" }\n'
        )
    hooks += "# khipu-pack: end\n"
    return mcp, hooks


def _aegis_install(dry: bool) -> dict:
    out: dict[str, Any] = {"harness": "aegis", "detected": _aegis_detected(), "changes": []}
    if not out["detected"] or not AEGIS_TOML.is_file():
        out["detected"] = False
        return out
    text = AEGIS_TOML.read_text(encoding="utf-8")
    mcp_block, hook_block = _aegis_blocks()
    new = text
    # Every .sub() below passes a LAMBDA, not the replacement string: re.sub
    # interprets backslash escapes and \g<n> group references in a literal
    # replacement, so a path containing either would be silently rewritten into
    # a corrupt config. The blocks embed filesystem paths (audit 2026-08-17).
    if _AEGIS_MCP_RE.search(new):
        if _AEGIS_MCP_RE.search(new).group(0).strip() != mcp_block.strip():
            new = _AEGIS_MCP_RE.sub(lambda _m: mcp_block, new)
            out["changes"].append(f"{AEGIS_TOML}: replace [mcp_servers.khipu]")
    else:
        new = new.rstrip("\n") + "\n\n" + mcp_block
        out["changes"].append(f"{AEGIS_TOML}: add [mcp_servers.khipu]")
    if _AEGIS_HOOK_RE.search(new):
        if _AEGIS_HOOK_RE.search(new).group(0) != hook_block:
            new = _AEGIS_HOOK_RE.sub(lambda _m: hook_block, new)
            out["changes"].append(f"{AEGIS_TOML}: replace khipu hooks block")
    else:
        new = new.rstrip("\n") + "\n\n" + hook_block
        out["changes"].append(f"{AEGIS_TOML}: add [[hooks.Stop]] + [[hooks.PreCompact]]")
    if new != text and not dry:
        out.setdefault("backups", []).append(_backup(AEGIS_TOML))
        AEGIS_TOML.write_text(new, encoding="utf-8")
    return out


def _aegis_uninstall(dry: bool) -> dict:
    out: dict[str, Any] = {"harness": "aegis", "changes": []}
    if not AEGIS_TOML.is_file():
        return out
    text = AEGIS_TOML.read_text(encoding="utf-8")
    new = _AEGIS_MCP_RE.sub("", text)
    if new != text:
        out["changes"].append(f"{AEGIS_TOML}: remove [mcp_servers.khipu]")
    new2 = _AEGIS_HOOK_RE.sub("", new)
    if new2 != new:
        out["changes"].append(f"{AEGIS_TOML}: remove khipu hooks block")
    if new2 != text and not dry:
        out.setdefault("backups", []).append(_backup(AEGIS_TOML))
        AEGIS_TOML.write_text(new2.rstrip("\n") + "\n", encoding="utf-8")
    return out


def _aegis_status() -> dict:
    text = AEGIS_TOML.read_text(encoding="utf-8") if AEGIS_TOML.is_file() else ""
    m = _AEGIS_MCP_RE.search(text)
    hb = _AEGIS_HOOK_RE.search(text)
    block = hb.group(0) if hb else ""
    ours = bool(_is_our_capture(block) and "[[hooks.SessionEnd]]" in block)
    # Aegis's "capture hook" IS the extraction trigger (no tail sync here — see
    # _aegis_blocks). Both rows report the same installed hook.
    return {"harness": "aegis", "detected": _aegis_detected() and AEGIS_TOML.is_file(),
            "mcp": bool(m and mcp_launcher() in m.group(0)),
            "hook_stop": ours, "hook_precompact": ours,
            "recall_rule": "n/a",
            "extract": "installed" if ours else "missing"}


# ---- Codex (TOML MCP like Aegis + Claude-shaped hooks.json) --------------------

CODEX_TOML = HOME / ".codex" / "config.toml"
CODEX_HOOKS = HOME / ".codex" / "hooks.json"
_CODEX_MCP_RE = re.compile(r"(?ms)^\[mcp_servers\.khipu\]\n.*?(?=^\[|\Z)")


def _codex_detected() -> bool:
    return (HOME / ".codex").is_dir() and CODEX_TOML.is_file()


def _codex_mcp_block() -> str:
    return (
        "[mcp_servers.khipu]\n"
        f'command = "{mcp_launcher()}"\n'
        "enabled = true\n"
    )


def _codex_install(dry: bool) -> dict:
    out: dict[str, Any] = {"harness": "codex", "detected": _codex_detected(), "changes": []}
    if not out["detected"]:
        return out
    text = CODEX_TOML.read_text(encoding="utf-8")
    block = _codex_mcp_block()
    new = text
    m = _CODEX_MCP_RE.search(new)
    if m:
        if m.group(0).strip() != block.strip():
            new = _CODEX_MCP_RE.sub(lambda _m: block, new)
            out["changes"].append(f"{CODEX_TOML}: replace [mcp_servers.khipu]")
    else:
        new = new.rstrip("\n") + "\n\n" + block
        out["changes"].append(f"{CODEX_TOML}: add [mcp_servers.khipu]")
    if new != text and not dry:
        out.setdefault("backups", []).append(_backup(CODEX_TOML))
        CODEX_TOML.write_text(new, encoding="utf-8")
    # hooks.json — identical shape to Claude Code's settings.json hooks block.
    h = _load_json(CODEX_HOOKS)
    hooks = h.setdefault("hooks", {})
    changed = False
    for event in ("Stop", "PreCompact", "SessionEnd"):
        entries = hooks.setdefault(event, [])
        flat = [x for e in entries for x in e.get("hooks", [])]
        if not any(_is_ours(x.get("command")) for x in flat):
            entries.append({"hooks": [{"type": "command", "command": stop_hook(), "timeout": 20}]})
            out["changes"].append(f"{CODEX_HOOKS}: hooks.{event} += khipu-stop-hook")
            changed = True
        elif _repoint(flat, _is_ours, stop_hook()):
            out["changes"].append(f"{CODEX_HOOKS}: hooks.{event} khipu-stop-hook -> {stop_hook()}")
            changed = True
    ss = hooks.setdefault("SessionStart", [])
    flat = [x for e in ss for x in e.get("hooks", [])]
    if not any(_is_our_recall(x.get("command")) for x in flat):
        ss.append({"hooks": [{"type": "command", "command": recall_hook(), "timeout": 10}]})
        out["changes"].append(f"{CODEX_HOOKS}: hooks.SessionStart += khipu-recall-hook")
        changed = True
    elif _repoint(flat, _is_our_recall, recall_hook()):
        out["changes"].append(f"{CODEX_HOOKS}: hooks.SessionStart khipu-recall-hook -> {recall_hook()}")
        changed = True
    if changed and not dry:
        out.setdefault("backups", []).append(_backup(CODEX_HOOKS))
        _write_json(CODEX_HOOKS, h)
    return out


def _codex_uninstall(dry: bool) -> dict:
    out: dict[str, Any] = {"harness": "codex", "changes": []}
    if CODEX_TOML.is_file():
        text = CODEX_TOML.read_text(encoding="utf-8")
        new = _CODEX_MCP_RE.sub("", text)
        if new != text:
            out["changes"].append(f"{CODEX_TOML}: remove [mcp_servers.khipu]")
            if not dry:
                out.setdefault("backups", []).append(_backup(CODEX_TOML))
                CODEX_TOML.write_text(new.rstrip("\n") + "\n", encoding="utf-8")
    h = _load_json(CODEX_HOOKS)
    changed = False
    for event in ("Stop", "PreCompact", "SessionEnd", "SessionStart"):
        entries = h.get("hooks", {}).get(event, [])
        kept = []
        for e in entries:
            e_hooks = [x for x in e.get("hooks", [])
                       if not (_is_ours(x.get("command")) or _is_our_recall(x.get("command")))]
            if len(e_hooks) != len(e.get("hooks", [])):
                changed = True
                out["changes"].append(f"{CODEX_HOOKS}: hooks.{event} -= khipu hook")
            if e_hooks:
                e = dict(e)
                e["hooks"] = e_hooks
                kept.append(e)
        if event in h.get("hooks", {}):
            h["hooks"][event] = kept
    if changed and not dry:
        out.setdefault("backups", []).append(_backup(CODEX_HOOKS))
        _write_json(CODEX_HOOKS, h)
    return out


def _codex_status() -> dict:
    text = CODEX_TOML.read_text(encoding="utf-8") if CODEX_TOML.is_file() else ""
    m = _CODEX_MCP_RE.search(text)
    h = _load_json(CODEX_HOOKS)
    def has(ev: str, pred) -> bool:
        return any(pred(x.get("command")) for e in h.get("hooks", {}).get(ev, []) for x in e.get("hooks", []))
    return {"harness": "codex", "detected": _codex_detected(),
            "mcp": bool(m and mcp_launcher() in m.group(0)),
            "hook_stop": has("Stop", _is_ours), "hook_precompact": has("PreCompact", _is_ours),
            "hook_sessionend": has("SessionEnd", _is_ours),
            "recall_rule": "installed" if has("SessionStart", _is_our_recall) else "missing",
            "extract": "installed" if has("Stop", _is_ours) and has("PreCompact", _is_ours) else "missing"}


# ---- Grok Bot / Cursor cloud (repo-scoped, remote MCP over HTTPS) --------------
#
# Grok Bot (Cursor's cloud agent, xAI) runs on an ephemeral Linux VM with no
# private network, no Keychain, no local files. Its Khipu is the HTTPS gateway
# (khipu.gateway) next to the database — Postgres stays private. Everything the agent needs is
# in the REPO it is working on: `.cursor/mcp.json` (remote server, bearer token
# by env interpolation — the token itself is a Cursor cloud secret, never in
# git) and `.cursor/rules/khipu.mdc` (the recall rule). Nothing global.
#
# The server is named `khipu-cloud` so a repo checked out locally does not
# collide with the local stdio `khipu` server; locally it shows unauthorized
# (no token in env), which is expected and harmless.

GROK_BOT_SERVER = "khipu-cloud"
GROK_BOT_TOKEN_ENV = "KHIPU_GATEWAY_TOKEN"


def _grok_bot_detected() -> bool:
    from khipu.config import gateway_url

    return bool(gateway_url())


def _grok_bot_mcp_path(project: str | None) -> Path | None:
    return Path(project).expanduser() / ".cursor" / "mcp.json" if project else None


def _grok_bot_entry() -> dict:
    from khipu.config import gateway_url

    return {"url": gateway_url() + "/mcp",
            "headers": {"Authorization": "Bearer ${env:" + GROK_BOT_TOKEN_ENV + "}"}}


def grok_bot_account_config() -> dict:
    """What to paste ONCE into Cursor's account-level MCP settings so Khipu is
    present in EVERY repo a cloud agent opens — the scaling answer; the
    per-repo `.cursor/mcp.json` below is only a fallback for a repo that needs
    it pinned. Cursor cloud resolves `${env:VAR}` in `headers` (docs, verified
    2026-08-17), so the token stays a cloud secret and never enters a repo."""
    from khipu.config import gateway_url

    url = gateway_url()
    return {
        "where": "https://cursor.com/agents → MCP dropdown (account/team level; applies to every repo)",
        "secret": {"where": "https://cursor.com/dashboard/cloud-agents → Secrets",
                   "name": GROK_BOT_TOKEN_ENV,
                   "value_from": "security find-generic-password -s Khipu -a gateway_token -w"},
        "config": {"mcpServers": {GROK_BOT_SERVER: _grok_bot_entry()}} if url else None,
        "note": ("The recall rule reaches the agent through the server's MCP `instructions` "
                 "(sent on initialize), so no rule file is needed in each repo."),
    }


def _grok_bot_install(dry: bool, project: str | None = None) -> dict:
    out: dict[str, Any] = {"harness": "grok_bot", "detected": _grok_bot_detected(), "changes": []}
    if not out["detected"]:
        out["error"] = "no gateway_url configured (khipu config --set-gateway-url https://...)"
        return out
    if not project:
        # No --project is the NORMAL case: account-level config covers every repo
        # and is a UI action, so emit exactly what to paste instead of erroring.
        out["account_level"] = grok_bot_account_config()
        return out
    mp = _grok_bot_mcp_path(project)
    d = _load_json(mp)
    want = _grok_bot_entry()
    if d.get("mcpServers", {}).get(GROK_BOT_SERVER) != want:
        out["changes"].append(f"{mp}: mcpServers.{GROK_BOT_SERVER} -> {want['url']}")
        if not dry:
            if mp.is_file():
                out.setdefault("backups", []).append(_backup(mp))
            d.setdefault("mcpServers", {})[GROK_BOT_SERVER] = want
            _write_json(mp, d)
    rp = _cursor_rule_path(project)
    from khipu.recall_rule import cursor_mdc

    rule = cursor_mdc()
    if not rp.is_file() or rp.read_text(encoding="utf-8") != rule:
        out["changes"].append(f"{rp}: write recall rule")
        if not dry:
            if rp.is_file():
                out.setdefault("backups", []).append(_backup(rp))
            rp.parent.mkdir(parents=True, exist_ok=True)
            rp.write_text(rule, encoding="utf-8")
    return out


def _grok_bot_uninstall(dry: bool, project: str | None = None) -> dict:
    out: dict[str, Any] = {"harness": "grok_bot", "changes": []}
    if not project:
        return out
    mp = _grok_bot_mcp_path(project)
    d = _load_json(mp)
    if GROK_BOT_SERVER in d.get("mcpServers", {}):
        out["changes"].append(f"{mp}: remove mcpServers.{GROK_BOT_SERVER}")
        if not dry:
            out.setdefault("backups", []).append(_backup(mp))
            d["mcpServers"].pop(GROK_BOT_SERVER)
            _write_json(mp, d)
    # The rule file is shared with the local Cursor pack; leave it to `uninstall cursor --project`.
    return out


def _grok_bot_status(project: str | None = None) -> dict:
    d = _load_json(_grok_bot_mcp_path(project)) if project else {}
    entry = d.get("mcpServers", {}).get(GROK_BOT_SERVER, {})
    from khipu.config import gateway_url

    return {"harness": "grok_bot", "detected": _grok_bot_detected(),
            "gateway_url": gateway_url() or None,
            # Account-level config lives in Cursor's cloud settings (a UI, not a
            # file), so Khipu cannot read it: `mcp` reports the optional per-repo
            # pin only. The authoritative check is `verify`, which probes the
            # gateway itself, plus a real cloud-agent call in the gateway log.
            "mcp": bool(entry) and entry.get("url", "").startswith(gateway_url() or "\0"),
            "scope": "account_level (every repo) + optional per-repo pin",
            "hook_stop": False, "hook_precompact": False,
            "recall_rule": "mcp_instructions", "extract": "mcp_capture",
            "project": project}


def _gateway_token() -> str:
    tok = (os.environ.get(GROK_BOT_TOKEN_ENV) or "").strip()
    if tok:
        return tok
    try:
        from khipu.keychain import get_password

        return (get_password("gateway_token") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _probe_gateway(url: str, token: str) -> dict:
    """The real thing: HTTPS to the public gateway, initialize + tools/list +
    khipu_status, plus a negative auth check. TLS verified by the system store."""
    import urllib.error
    import urllib.request
    t0 = time.time()
    body = json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "khipu_status", "arguments": {}}},
    ]).encode()
    try:
        req = urllib.request.Request(url + "/mcp", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            frames = json.loads(resp.read().decode())
        tools = [t["name"] for t in frames[1]["result"]["tools"]]
        st = json.loads(frames[2]["result"]["content"][0]["text"])
        # negative: a wrong token must be refused
        req2 = urllib.request.Request(url + "/mcp", data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}', method="POST",
                                      headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req2, timeout=15)
            refused = False
        except urllib.error.HTTPError as e:
            refused = e.code == 401
        ok = "khipu_capture" in tools and "counts" in st and refused
        return {"ok": ok, "episodes": st.get("counts", {}).get("episodes"), "tools": len(tools),
                "auth_refused_wrong_token": refused, "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ---- verify -------------------------------------------------------------------

def _probe_mcp(command: str) -> dict:
    """Spawn the harness's own MCP command and complete initialize + khipu_status."""
    t0 = time.time()
    lines = "\n".join(json.dumps(m) for m in (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "khipu_status", "arguments": {}}},
    )) + "\n"
    try:
        p = subprocess.run([command], input=lines, capture_output=True, text=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    frames = [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]
    if len(frames) < 2 or "result" not in frames[1]:
        return {"ok": False, "error": (p.stderr or p.stdout)[-300:]}
    body = json.loads(frames[1]["result"]["content"][0]["text"])
    return {"ok": not frames[1]["result"].get("isError") and "counts" in body,
            "episodes": body.get("counts", {}).get("episodes"), "ms": int((time.time() - t0) * 1000)}


def _probe_hook(command: str) -> dict:
    """Fire the stop hook with a synthetic event; it must exit 0 quickly.

    Runs through the shell (``sh -c``) exactly as every harness does — a probe
    that exec'd the path directly passed while the real hook was dying on the
    space in the repo path (2026-08-17)."""
    import tempfile
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix="khipu-hook-probe-") as td:
            # Throwaway capture home: a synthetic Stop must never write into the
            # real per-harness heartbeat that liveness reads (verify runs from
            # inside the harnesses themselves).
            env = dict(os.environ, KHIPU_CAPTURE_HOME=str(Path(td) / "kh"), KHIPU_CAPTURE_NO_DRAIN="1")
            p = subprocess.run(command, shell=True, input='{"hook_event_name":"Stop","session_id":"khipu-verify"}',
                               capture_output=True, text=True, timeout=60, env=env)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out = {"ok": p.returncode == 0, "exit": p.returncode, "ms": int((time.time() - t0) * 1000)}
    if p.returncode != 0:
        out["error"] = (p.stderr or p.stdout)[-300:]
    return out


def _probe_recall(command: str) -> dict:
    """SessionStart hook must print Claude or Cursor inject context with khipu_search.
    Shell-run for the same reason as _probe_hook."""
    t0 = time.time()
    try:
        p = subprocess.run(command, shell=True, input="{}", capture_output=True, text=True, timeout=30)
        d = json.loads(p.stdout.strip() or "{}")
        ctx = (
            d.get("hookSpecificOutput", {}).get("additionalContext")
            or d.get("additional_context")
            or ""
        )
        return {"ok": p.returncode == 0 and "khipu_search" in ctx, "chars": len(ctx),
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _probe_extract(command: str) -> dict:
    """Fire the Aegis extraction hook (shell-run, probe mode) with a synthetic
    envelope + transcript: it must parse the ACP stream, count the turn, and
    report the PreCompact window as due — without spawning a worker."""
    import tempfile
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix="khipu-extract-probe-") as td:
            tp = Path(td) / "updates.jsonl"
            rows = [
                {"method": "session/update", "params": {"update": {"sessionUpdate": "user_message_chunk",
                 "content": {"type": "text", "text": "probe: " + "x" * 300}}}},
                {"method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk",
                 "content": {"type": "text", "text": "ack " * 40}}}},
            ]
            tp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            envl = json.dumps({"hookEventName": "pre_compact", "sessionId": "khipu-verify",
                               "cwd": td, "transcriptPath": str(tp)})
            khome = Path(td) / "khipu-home"
            env = dict(os.environ, KHIPU_AEGIS_PROBE="1", KHIPU_AEGIS_HOME=str(khome),
                       KHIPU_HARNESS="aegis")
            p = subprocess.run(command, shell=True, input=envl, capture_output=True, text=True,
                               timeout=30, env=env)
            d = json.loads((p.stdout or "").strip() or "{}")
            # The job on disk is the real assertion: a "due" that queued nothing
            # is the silent failure this probe exists to catch.
            queued = list((khome / "queue").glob("*.json"))
            beat = (khome / "dispatch" / "aegis.json").is_file()
        ok = (p.returncode == 0 and d.get("due") is True and d.get("new_turns") == 1
              and len(queued) == 1 and beat)
        out = {"ok": ok, "exit": p.returncode, "queued": len(queued),
               "ms": int((time.time() - t0) * 1000)}
        if not ok:
            out["error"] = (p.stderr or p.stdout or json.dumps(d))[-300:]
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _probe_native_extract(command: str, harness: str) -> dict:
    """Fire the stop hook (shell-run) with a synthetic Claude-shaped PreCompact
    envelope and a synthetic transcript in that harness's own format: it must
    parse it, count the turn, decide the window is due, and leave exactly ONE
    job on disk plus a heartbeat — under a throwaway capture home, so the probe
    never touches the real queue. Then a dry drain must extract nothing (no
    model call: KHIPU_CAPTURE_DRY_PROBE) — that part is what the unit tests
    cover; here the job on disk is the assertion, exactly as for Aegis."""
    import tempfile
    t0 = time.time()
    try:
        with tempfile.TemporaryDirectory(prefix="khipu-native-probe-") as td:
            tp = Path(td) / ".claude" / "projects" / "probe" / "khipu-verify.jsonl"
            if harness == "cursor":
                tp = Path(td) / ".cursor" / "projects" / "probe" / "agent-transcripts" / "khipu-verify" / "khipu-verify.jsonl"
                rows = [{"role": "user", "message": {"content": [{"type": "text", "text": "probe: " + "x" * 300}]}},
                        {"role": "assistant", "message": {"content": [{"type": "text", "text": "ack " * 40}]}}]
                envl = {"hook_event_name": "preCompact", "conversation_id": "khipu-verify",
                        "workspace_roots": [td], "transcript_path": str(tp)}
            elif harness == "codex":
                tp = Path(td) / ".codex" / "sessions" / "khipu-verify.jsonl"
                rows = [{"type": "event_msg", "payload": {"type": "user_message", "message": "probe: " + "x" * 300}},
                        {"type": "event_msg", "payload": {"type": "agent_message", "message": "ack " * 40}}]
                envl = {"hook_event_name": "PreCompact", "session_id": "khipu-verify", "cwd": td,
                        "transcript_path": str(tp)}
            else:
                rows = [{"type": "user", "message": {"role": "user", "content": "probe: " + "x" * 300}},
                        {"type": "assistant", "message": {"role": "assistant",
                                                          "content": [{"type": "text", "text": "ack " * 40}]}}]
                envl = {"hook_event_name": "PreCompact", "session_id": "khipu-verify", "cwd": td,
                        "transcript_path": str(tp)}
            tp.parent.mkdir(parents=True, exist_ok=True)
            tp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            khome = Path(td) / "khipu-home"
            env = {k: v for k, v in os.environ.items() if k not in ("KHIPU_HARNESS",)}
            # KHIPU_MEMORY_ROOT to a temp dir: the tail-sync leg must not touch
            # the live episodes file from a probe; KHIPU_CAPTURE_NO_DRAIN keeps
            # the queued probe job on disk for the assertion.
            env.update(KHIPU_CAPTURE_HOME=str(khome), KHIPU_CAPTURE_NO_DRAIN="1",
                       KHIPU_MEMORY_ROOT=str(Path(td) / "mem"))
            p = subprocess.run(command, shell=True, input=json.dumps(envl), capture_output=True,
                               text=True, timeout=60, env=env)
            queued = list((khome / "queue").glob("*.json"))
            beat = {}
            try:
                beat = json.loads((khome / "dispatch" / f"{harness}.json").read_text())
            except (OSError, ValueError):
                pass
        ok = (p.returncode == 0 and beat.get("due") is True and beat.get("new_turns") == 1
              and beat.get("harness") == harness and len(queued) == 1)
        out = {"ok": ok, "exit": p.returncode, "queued": len(queued), "harness_seen": beat.get("harness"),
               "ms": int((time.time() - t0) * 1000)}
        if not ok:
            out["error"] = (p.stderr or json.dumps(beat))[-300:]
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _probe_mcp_no_keychain(command: str) -> dict:
    """Run the MCP server the way a SANDBOXED harness runs it: no Keychain.

    Aegis's sandbox denies the Keychain, so its MCP server falls back to the
    file DSN. Every probe here spawns from a shell where the Keychain works, so
    for two days (2026-08-16 to 08-18) `verify aegis` reported mcp=ok while
    Aegis itself could not reach Postgres at all — the file DSN still named a
    root cert deleted in the 2026-08-04 rename. The probe was not wrong about
    what it tested; it tested the wrong environment.

    KHIPU_KEYCHAIN=0 reproduces the one condition that matters, without needing
    to reproduce the sandbox itself.
    """
    prior = os.environ.get("KHIPU_KEYCHAIN")
    os.environ["KHIPU_KEYCHAIN"] = "0"
    try:
        out = _probe_mcp(command)
    finally:
        if prior is None:
            os.environ.pop("KHIPU_KEYCHAIN", None)
        else:
            os.environ["KHIPU_KEYCHAIN"] = prior
    if not out.get("ok"):
        out["error"] = (
            "MCP fails without the Keychain, which is exactly how Aegis runs it — "
            "check the file DSN (khipu doctor -> dsn_file_ok): " + str(out.get("error", ""))[:200]
        )
    return out


def _probe_aegis_isolation(command: str) -> dict:
    """Aegis is its own harness: Khipu's hook scripts must RUN when Aegis
    invokes them through the Aegis pack (KHIPU_HARNESS=aegis) and REFUSE when
    Aegis invokes them through a vendor-compat import of another harness's
    config (Aegis runner env present, no KHIPU_HARNESS). Observed via the
    script's own log under a throwaway HOME: present = ran, absent = refused."""
    import tempfile
    t0 = time.time()
    payload = '{"hookEventName":"stop","sessionId":"khipu-verify"}'
    aegis_env = {"GROK_HOOK_EVENT": "stop", "GROK_HOOK_NAME": "user:stop[0].hooks[0]",
                 "GROK_SESSION_ID": "khipu-verify"}
    try:
        results = {}
        for label, extra in (("native_ran", {"KHIPU_HARNESS": "aegis"}), ("compat_refused", {})):
            with tempfile.TemporaryDirectory(prefix="khipu-iso-") as home:
                env = {k: v for k, v in os.environ.items() if k != "KHIPU_HARNESS"}
                env.update(aegis_env, HOME=home, KHIPU_AEGIS_HOME=str(Path(home) / "kh"), **extra)
                p = subprocess.run(command, shell=True, input=payload, capture_output=True,
                                   text=True, timeout=60, env=env)
                ran = (Path(home) / "kh" / "dispatch" / "aegis.json").is_file()
                results[label] = (ran if label == "native_ran" else not ran) and p.returncode == 0
        out = {"ok": all(results.values()), **results, "ms": int((time.time() - t0) * 1000)}
        if not out["ok"]:
            out["error"] = "isolation: " + ", ".join(f"{k}={v}" for k, v in results.items())
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _probe_aegis_refusal(command: str) -> dict:
    """The mirror of _probe_aegis_isolation, for the scripts Aegis must NEVER
    run: the Stop hook and the recall hook. Those have to do nothing under
    Aegis's runner env by EVERY route, the pack's own KHIPU_HARNESS=aegis mark
    included.

    They carried khipu-aegis-capture's guard, where that exception is correct —
    so with the mark set, the Stop hook ran and wrote
    ~/Library/Logs/khipu/stop-hook.log (a path the sandbox denies) and the
    recall hook emitted the entire rule into a harness whose SessionStart
    discards stdout (audit 2026-08-18). Nothing probed either script, so a
    documented absolute boundary held in only one of the three places it was
    claimed. Evidence: a throwaway HOME that has to stay empty both ways."""
    import tempfile

    t0 = time.time()
    payload = '{"hookEventName":"stop","sessionId":"khipu-verify"}'
    aegis_env = {"GROK_HOOK_EVENT": "stop", "GROK_HOOK_NAME": "user:stop[0].hooks[0]",
                 "GROK_SESSION_ID": "khipu-verify"}
    try:
        results = {}
        for label, extra in (("refused_marked", {"KHIPU_HARNESS": "aegis"}),
                             ("refused_compat", {})):
            with tempfile.TemporaryDirectory(prefix="khipu-ref-") as home:
                env = {k: v for k, v in os.environ.items() if k != "KHIPU_HARNESS"}
                env.update(aegis_env, HOME=home, KHIPU_AEGIS_HOME=str(Path(home) / "kh"), **extra)
                p = subprocess.run(command, shell=True, input=payload, capture_output=True,
                                   text=True, timeout=60, env=env)
                wrote = any(Path(home).rglob("*"))
                # The recall hook refuses by printing a bare {}, so silence on
                # disk is not enough — an emitted rule is also a breach.
                # Claude nested additionalContext OR Cursor flat additional_context.
                leaked = (
                    "additionalContext" in (p.stdout or "")
                    or "additional_context" in (p.stdout or "")
                )
                results[label] = (not wrote) and (not leaked) and p.returncode == 0
        out = {"ok": all(results.values()), **results, "ms": int((time.time() - t0) * 1000)}
        if not out["ok"]:
            out["error"] = "aegis refusal: " + ", ".join(f"{k}={v}" for k, v in results.items())
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _runtime(harness: str) -> dict:
    """What only a real session can prove: that the harness actually invoked the
    hook, that captures are landing, and whether anything is stuck. A probe can
    show the script works; only this shows the harness runs it — and liveness
    is what turns "runs but records nothing" RED instead of quietly green."""
    try:
        from khipu import session_capture

        lv = session_capture.liveness(harness)
        st = session_capture.status(harness)
        return {"ok": lv["ok"], "reasons": lv.get("reasons", []),
                "last_dispatch_age_s": lv.get("last_dispatch_age_s"),
                "last_dispatch": lv.get("last_dispatch_at"),
                "last_captured_age_s": lv.get("last_captured_age_s"),
                "captures": lv.get("captures", 0), "dispatches": lv.get("dispatches", 0),
                "pending_turns": lv.get("pending_turns", 0),
                "queue_depth": lv.get("queue_depth", 0),
                "sessions_tracked": st.get("sessions_tracked"),
                "note": ("no real session has run this hook yet" if not lv.get("seen") else None)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _aegis_runtime() -> dict:
    return _runtime("aegis")


def verify(harness: str, *, project: str | None = None) -> dict:
    st = status(harness, project=project) if harness == "grok_bot" else status(harness)
    out: dict[str, Any] = {"harness": harness, "detected": st["detected"], "components": {}}
    if not st["detected"]:
        return out
    if harness == "grok_bot":
        from khipu.config import gateway_url

        tok = _gateway_token()
        out["components"]["mcp"] = (_probe_gateway(gateway_url(), tok) if tok
                                    else {"ok": False, "error": f"no gateway token ({GROK_BOT_TOKEN_ENV} or Keychain gateway_token)"})
        out["components"]["hook"] = {"ok": True, "na": True,
                                     "note": "cloud agent: capture is the khipu_capture tool over the gateway"}
        out["ok"] = all(c.get("ok") for c in out["components"].values())
        return out
    if st["mcp"]:
        out["components"]["mcp"] = _probe_mcp(mcp_launcher())
    else:
        out["components"]["mcp"] = {"ok": False, "error": "not installed"}
    if harness == "aegis":
        # Aegis runs everything without the Keychain. Re-probe the MCP server
        # that way, or this row keeps reporting a server that only works from
        # here (see _probe_mcp_no_keychain).
        if out["components"]["mcp"].get("ok"):
            sandboxed = _probe_mcp_no_keychain(mcp_launcher())
            out["components"]["mcp"]["sandboxed_ok"] = sandboxed.get("ok")
            if not sandboxed.get("ok"):
                out["components"]["mcp"]["ok"] = False
                out["components"]["mcp"]["error"] = sandboxed.get("error")
        # Aegis's capture hook is its only Khipu hook (the tail sync cannot run in
        # its sandbox), so the hook row and the extraction row are that one hook.
        hook = (_probe_extract(aegis_capture_hook()) if st.get("extract") == "installed"
                else {"ok": False, "error": "not installed"})
        if hook.get("ok"):
            iso = _probe_aegis_isolation(aegis_capture_hook())
            hook.update({k: v for k, v in iso.items() if k != "ms"}, ok=hook["ok"] and iso["ok"])
        out["components"]["hook"] = hook
        out["components"]["extract"] = dict(hook)
        out["aegis"] = _aegis_runtime()
    else:
        if st["hook_stop"]:
            hook = _probe_hook(stop_hook())
            # Aegis must never reach this script. Checked here rather than
            # asserted in a comment: only the aegis capture hook was ever probed.
            if hook.get("ok"):
                ref = _probe_aegis_refusal(stop_hook())
                hook.update({k: v for k, v in ref.items() if k != "ms"},
                            ok=hook["ok"] and ref["ok"])
            out["components"]["hook"] = hook
            # The same hook is the harness's capture step now: prove it parses
            # THIS harness's transcript shape and queues, not just that it exits 0.
            out["components"]["extract"] = _probe_native_extract(stop_hook(), harness)
        else:
            out["components"]["hook"] = {"ok": False, "error": "not installed"}
            out["components"]["extract"] = {"ok": False, "error": "not installed"}
        if harness in ("claude_code", "codex") and st.get("recall_rule") == "installed":
            recall = _probe_recall(recall_hook())
            if recall.get("ok"):
                ref = _probe_aegis_refusal(recall_hook())
                recall.update({k: v for k, v in ref.items() if k != "ms"},
                              ok=recall["ok"] and ref["ok"])
            out["components"]["recall"] = recall
        elif harness == "cursor" and st.get("hook_sessionstart"):
            recall = _probe_recall(recall_hook_cursor())
            if recall.get("ok"):
                ref = _probe_aegis_refusal(recall_hook_cursor())
                recall.update({k: v for k, v in ref.items() if k != "ms"},
                              ok=recall["ok"] and ref["ok"])
            out["components"]["recall"] = recall
        out["runtime"] = _runtime(harness)
    if harness == "aegis":
        out["runtime"] = out["aegis"]
    # A red liveness verdict fails verify: "the hook works" is not "the harness
    # is being recorded", and the second is the question that matters.
    rt = out.get("runtime") or {}
    out["ok"] = all(c.get("ok") for c in out["components"].values()) and rt.get("ok", True)
    return out


# ---- dispatch -----------------------------------------------------------------

_INSTALL = {"claude_code": _claude_install, "cursor": _cursor_install, "aegis": _aegis_install, "codex": _codex_install}
_UNINSTALL = {"claude_code": _claude_uninstall, "cursor": _cursor_uninstall, "aegis": _aegis_uninstall, "codex": _codex_uninstall}
_STATUS = {"claude_code": _claude_status, "cursor": _cursor_status, "aegis": _aegis_status, "codex": _codex_status}


def _guarded(harness: str, fn, *args):
    """An unreadable config is a reported failure for THAT harness, never a
    traceback and never a silent overwrite of the others."""
    try:
        return fn(*args)
    except ConfigUnreadable as e:
        return {"harness": harness, "detected": True, "changes": [], "ok": False,
                "error": str(e), "aborted": True}


def install(harness: str, *, dry_run: bool = False, project: str | None = None) -> dict:
    if harness == "cursor":
        return _guarded(harness, _cursor_install, dry_run, project)
    if harness == "grok_bot":
        return _guarded(harness, _grok_bot_install, dry_run, project)
    return _guarded(harness, _INSTALL[harness], dry_run)


def uninstall(harness: str, *, dry_run: bool = False, project: str | None = None) -> dict:
    if harness == "cursor":
        return _guarded(harness, _cursor_uninstall, dry_run, project)
    if harness == "grok_bot":
        return _guarded(harness, _grok_bot_uninstall, dry_run, project)
    return _guarded(harness, _UNINSTALL[harness], dry_run)


def status(harness: str, *, project: str | None = None) -> dict:
    if harness == "grok_bot":
        return _guarded(harness, _grok_bot_status, project)
    return _guarded(harness, _STATUS[harness])


def status_all() -> list[dict]:
    return [status(h) for h in HARNESSES]
