"""Khipu for Antigravity inside T3 Code: per-project files, added from the app.

T3 runs Antigravity through a managed runtime whose hooks and MCP servers live
in a private profile (``~/.t3/userdata/providers/antigravity/<sha256 of the
instance id>``). Khipu never writes that profile, T3's settings or its
database (docs/plans/2026-10-27-cursor-antigravity-hooks.md, decision 6.1).
What the runtime does read is the workspace's own ``.agents/`` folder, so Khipu
adds three files there, only for a project the owner picks in the app:

  <project>/.agents/hooks.json        one ``"khipu"`` key; every other key stays
  <project>/.agents/mcp_config.json   ``mcpServers.khipu``
  <project>/.agents/rules/khipu.md    the always-on rule, in a file of its own

Hooks written: SessionStart (standalone Antigravity only; T3's runtime has no
SessionStart), PreInvocation, PostInvocation and Stop. Never PreToolUse or
PostToolUse: registering PreToolUse makes ``agy`` skip its permission prompt.
T3's runtime also ignores injected context, so recall and the routing start
block reach a T3 session through the MCP tools and the rule, not through hooks.

The folders the owner has added are listed in Khipu's own
``~/.config/khipu/t3-antigravity-projects.json``, so doctor can say how many
projects carry Khipu without reading anything of T3's but its provider ids.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOOK_NAME = "khipu"
AGENTS_DIR = ".agents"
RULE_MARK = "<!-- khipu: managed rule, rewritten by Khipu; remove it from the Khipu app -->"
REGISTRY_NAME = "t3-antigravity-projects.json"

# The one fact about T3's managed runtime that Khipu's states rest on (read from
# agy_acp_server 1.3.0, 2026-10-27): it logs "Trajectory injection (injectSteps)
# is not supported by the SDK yet" and wires no SessionStart. A runtime that
# differs from the one Khipu last recorded is reported as needing a re-check;
# the states stay "unsupported" until slice H proves otherwise on a real run.
KNOWN_NO_INJECTION_RUNTIMES = frozenset({"1.3.0"})

RESTART_NOTE = "If Antigravity asks whether to trust this workspace's hooks, choose Trust."
LOCAL_FILES_NOTE = ("These files point at this Mac's Khipu install: keep them out of version control "
                    "and out of anything you share.")

_ROUTING_RULE = """
## Routing in this harness

Khipu cannot push its routing start block into this session. Before you start
or delegate work, call the `khipu_route` tool once and follow its account and
lane guidance; it is guidance, not a gate. At the start of a session, call
`khipu_status` with the user's first prompt as `prompt` to get the prior work
Khipu would otherwise have pushed.
"""


def _home(home: Path | None) -> Path:
    return home if home is not None else Path.home()


def registry_path(home: Path | None = None) -> Path:
    return _home(home) / ".config" / "khipu" / REGISTRY_NAME


def profile_dir(instance_id: str, home: Path | None = None) -> Path:
    """T3's private profile for this instance. Khipu only ever READS here."""
    digest = hashlib.sha256(str(instance_id).encode("utf-8")).hexdigest()
    return _home(home) / ".t3" / "userdata" / "providers" / "antigravity" / digest


def _version_key(name: str) -> tuple:
    return tuple(int(part) if part.isdigit() else 0 for part in re.split(r"[.\-+]", name))


def runtime_version(home: Path | None = None) -> str | None:
    """The newest managed Antigravity runtime T3 has installed, from the name
    of its ``versions/<version>`` folder; None when there is none. Only that
    folder name is read."""
    root = _home(home) / ".t3" / "tools" / "antigravity-acp"
    try:
        names = [p.name for p in root.glob("*/versions/*") if p.is_dir() and not p.name.startswith(".")]
    except OSError:
        return None
    return max(names, key=_version_key) if names else None


# ---- reading (never raises) -------------------------------------------------


def _read(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _commands(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, dict):
        command = value.get("command")
        if isinstance(command, str):
            out.append(command)
        for inner in value.values():
            out += _commands(inner)
    elif isinstance(value, list):
        for inner in value:
            out += _commands(inner)
    return out


def hook_events(hooks_file: Path) -> dict[str, list[str]]:
    """``{event: [Khipu hook launcher names]}`` from an Antigravity
    ``hooks.json``: every named hook set that is not switched off. Only
    command names that are Khipu launchers leave this function."""
    from khipu import t3

    out: dict[str, list[str]] = {}
    for spec in _read(hooks_file).values():
        if not isinstance(spec, dict) or spec.get("enabled") is False:
            continue
        for event, handlers in spec.items():
            if event == "enabled":
                continue
            for command in _commands(handlers):
                name = t3._khipu_hook_name(command)
                if name:
                    out.setdefault(str(event), []).append(name)
    return out


def has_mcp(config_file: Path) -> bool:
    server = (_read(config_file).get("mcpServers") or {}).get("khipu")
    command = server.get("command") if isinstance(server, dict) else None
    if not isinstance(command, str):
        return False
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return bool(tokens) and Path(tokens[0]).name == "khipu-mcp"


def folder_functions(config_dir: Path) -> dict[str, bool]:
    """What a folder that holds ``hooks.json`` and ``mcp_config.json`` (a
    project's ``.agents`` or T3's profile ``config``) gives Khipu."""
    events = hook_events(config_dir / "hooks.json")
    return {
        "capture": "khipu-stop-hook" in events.get("Stop", []),
        "mcp": has_mcp(config_dir / "mcp_config.json"),
    }


def project_status(project: str | Path) -> dict:
    root = Path(project).expanduser()
    agents = root / AGENTS_DIR
    funcs = folder_functions(agents)
    rule = agents / "rules" / "khipu.md"
    try:
        rule_ok = rule.is_file() and RULE_MARK in rule.read_text(encoding="utf-8")
    except OSError:
        rule_ok = False
    events = hook_events(agents / "hooks.json")
    return {
        "project": str(root),
        "exists": root.is_dir(),
        "capture": funcs["capture"],
        "mcp": funcs["mcp"],
        "rule": rule_ok,
        "prompt_hook": "khipu-prompt-recall" in events.get("PreInvocation", []),
        "installed": funcs["capture"] and funcs["mcp"] and rule_ok,
    }


def registered_projects(home: Path | None = None) -> list[str]:
    projects = _read(registry_path(home)).get("projects")
    return sorted(str(p) for p in projects) if isinstance(projects, dict) else []


def functions_for(instance_id: str, home: Path | None = None) -> dict:
    """The per-function picture for one T3 Antigravity instance: the private
    profile (read only) and every project the owner added. A function is
    ``installed`` when any of them carries it; the three that need injected
    context are ``unsupported`` while T3's runtime ignores it."""
    capture = mcp = False
    installed_projects = 0
    projects = registered_projects(home)
    for project in projects:
        status = project_status(project)
        capture |= status["capture"]
        mcp |= status["mcp"]
        installed_projects += 1 if status["installed"] else 0
    profile = folder_functions(profile_dir(instance_id, home) / "config")
    capture |= profile["capture"]
    mcp |= profile["mcp"]
    version = runtime_version(home)
    recorded = _read(registry_path(home)).get("runtime_version")
    recorded = recorded if isinstance(recorded, str) else None
    # "Changed since Khipu last looked": a runtime Khipu has never recorded is
    # fine only when it is one whose behaviour was read from the binary.
    recheck = version is not None and recorded != version and (
        recorded is not None or version not in KNOWN_NO_INJECTION_RUNTIMES)
    return {
        "functions": {
            "capture": "installed" if capture else "missing",
            "start_block": "unsupported",
            "prompt_recall": "unsupported",
            "delegation": "unsupported",
            "mcp": "installed" if mcp else "missing",
        },
        "projects": {"registered": len(projects), "installed": installed_projects},
        "runtime": {"version": version, "checked_version": recorded, "recheck": recheck},
    }


# ---- writing (only inside a project the owner picked) -------------------------


def _stamp_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _refuse_private(root: Path, home: Path) -> str | None:
    """Khipu never writes T3's private state or another harness's home."""
    resolved = root.resolve()
    for private in (home / ".t3", home / ".gemini", home / ".cursor", home / ".claude", home / ".codex"):
        base = private.resolve()
        if resolved == base or base in resolved.parents:
            return f"{root} is inside {private}, which is not a project; pick a project folder"
    return None


def _rule_text() -> str:
    from khipu.recall_rule import RULE_MD

    return f"---\ntrigger: always_on\n---\n\n{RULE_MARK}\n\n{RULE_MD.strip()}\n{_ROUTING_RULE}"


def _hook_set(integ) -> dict:
    def handler(command: str, timeout: int) -> dict:
        return {"type": "command", "command": command, "timeout": timeout}

    return {
        "enabled": True,
        "SessionStart": [handler(integ.recall_hook(), integ.CURSOR_RECALL_TIMEOUT)],
        "PreInvocation": [handler(integ.prompt_recall_hook(), integ.PROMPT_RECALL_TIMEOUT)],
        "PostInvocation": [handler(integ.queue_only_stop_hook(), 20)],
        "Stop": [handler(integ.stop_hook(), 20)],
    }


def _record(home: Path | None, project: Path, version: str | None, *, remove: bool = False) -> None:
    from khipu import integrations as integ

    path = registry_path(home)
    data = integ._load_json(path)
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    key = str(project.resolve())
    if remove:
        projects.pop(key, None)
    else:
        projects[key] = {"added_at": (projects.get(key) or {}).get("added_at") or _stamp_now()}
    data.update(version=1, projects=projects)
    if version:
        data["runtime_version"] = version
    integ._write_json(path, data)


def install_project(project: str, *, dry_run: bool = False, home: Path | None = None) -> dict:
    """Add Khipu to one project's ``.agents`` folder. Re-running is a no-op."""
    from khipu import integrations as integ

    home = _home(home)
    root = Path(project).expanduser()
    out: dict[str, Any] = {"harness": "antigravity-t3", "project": str(root), "changes": [], "ok": True}
    if not root.is_dir():
        return dict(out, ok=False, error=f"{root} is not a folder")
    refused = _refuse_private(root, home)
    if refused:
        return dict(out, ok=False, error=refused)
    agents = root / AGENTS_DIR
    with integ._installing(dry=dry_run):
        want_hooks = _hook_set(integ)
        want_mcp = {"command": integ.mcp_launcher()}
    want_rule = _rule_text()

    hooks_file, mcp_file, rule_file = agents / "hooks.json", agents / "mcp_config.json", agents / "rules" / "khipu.md"
    hooks = integ._load_json(hooks_file)
    if hooks.get(HOOK_NAME) != want_hooks:
        out["changes"].append(f"{hooks_file}: set the {HOOK_NAME!r} hook set")
        if not dry_run:
            _keep_backup(out, integ, hooks_file)
            hooks[HOOK_NAME] = want_hooks
            integ._write_json(hooks_file, hooks)
    mcp = integ._load_json(mcp_file)
    if (mcp.get("mcpServers") or {}).get("khipu") != want_mcp:
        out["changes"].append(f"{mcp_file}: mcpServers.khipu -> {want_mcp['command']}")
        if not dry_run:
            _keep_backup(out, integ, mcp_file)
            mcp.setdefault("mcpServers", {})["khipu"] = want_mcp
            integ._write_json(mcp_file, mcp)
    current = rule_file.read_text(encoding="utf-8") if rule_file.is_file() else None
    if current != want_rule:
        out["changes"].append(f"{rule_file}: write the always-on rule")
        if not dry_run:
            if current is not None:
                _keep_backup(out, integ, rule_file)
            rule_file.parent.mkdir(parents=True, exist_ok=True)
            rule_file.write_text(want_rule, encoding="utf-8")
    if not dry_run:
        _record(home, root, runtime_version(home))
        out["status"] = project_status(root)
    out["notes"] = [RESTART_NOTE, LOCAL_FILES_NOTE]
    return out


def uninstall_project(project: str, *, dry_run: bool = False, home: Path | None = None) -> dict:
    """Take Khipu out of one project. Other hook sets, other MCP servers and a
    rule file Khipu did not write are left exactly as they are."""
    from khipu import integrations as integ

    home = _home(home)
    root = Path(project).expanduser()
    out: dict[str, Any] = {"harness": "antigravity-t3", "project": str(root), "changes": [], "ok": True}
    agents = root / AGENTS_DIR
    hooks_file, mcp_file, rule_file = agents / "hooks.json", agents / "mcp_config.json", agents / "rules" / "khipu.md"

    hooks = integ._load_json(hooks_file)
    if HOOK_NAME in hooks:
        out["changes"].append(f"{hooks_file}: remove the {HOOK_NAME!r} hook set")
        if not dry_run:
            _keep_backup(out, integ, hooks_file)
            hooks.pop(HOOK_NAME)
            _write_or_drop(integ, hooks_file, hooks)
    mcp = integ._load_json(mcp_file)
    if "khipu" in (mcp.get("mcpServers") or {}):
        out["changes"].append(f"{mcp_file}: remove mcpServers.khipu")
        if not dry_run:
            _keep_backup(out, integ, mcp_file)
            mcp["mcpServers"].pop("khipu")
            if not mcp["mcpServers"]:
                mcp.pop("mcpServers")
            _write_or_drop(integ, mcp_file, mcp)
    try:
        ours = rule_file.is_file() and RULE_MARK in rule_file.read_text(encoding="utf-8")
    except OSError:
        ours = False
    if ours:
        out["changes"].append(f"{rule_file}: remove the always-on rule")
        if not dry_run:
            _keep_backup(out, integ, rule_file)
            rule_file.unlink()
            for folder in (rule_file.parent, agents):
                try:
                    folder.rmdir()
                except OSError:
                    break
    if not dry_run:
        _record(home, root, None, remove=True)
        out["status"] = project_status(root)
    return out


def _keep_backup(out: dict, integ, path: Path) -> None:
    backup = integ._backup(path)
    if backup:
        out.setdefault("backups", []).append(backup)


def _write_or_drop(integ, path: Path, data: dict) -> None:
    """An emptied file Khipu created goes (its backup keeps the old bytes); a
    file with anything else in it is rewritten without Khipu's part."""
    if data:
        integ._write_json(path, data)
    else:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass


def projects_report(home: Path | None = None) -> dict:
    return {"runtime_version": runtime_version(home),
            "projects": [project_status(p) for p in registered_projects(home)]}
