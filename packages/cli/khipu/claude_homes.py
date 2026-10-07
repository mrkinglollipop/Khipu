# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""The Claude Code config homes Khipu installs into.

Claude Code keeps its settings, hooks, transcripts and (since 2026) its
``.claude.json`` under one config folder. The usual folder is ``~/.claude``,
but ``CLAUDE_CONFIG_DIR`` moves it, and T3 Code runs each Claude account with
its own ``CLAUDE_CONFIG_DIR`` (docs/plans/2026-10-07-khipu-t3.md). Khipu used
to know only ``~/.claude``.

Where ``.claude.json`` lives (checked on this Mac, 2026-10-07): a session with
no ``CLAUDE_CONFIG_DIR`` reads ``~/.claude.json``; a session WITH one reads
``<that folder>/.claude.json`` even when the folder is ``~/.claude`` (the
stub ``~/.claude/.claude.json`` has no ``mcpServers``). So a home reached only
by default uses ``~/.claude.json``, and every home reached through an explicit
``CLAUDE_CONFIG_DIR`` (the environment or a T3 instance's ``homePath``) also
uses its own ``<home>/.claude.json``. ``ClaudeHome.claude_jsons`` lists all
that apply, primary first.

Discovery only reads: ``~/.claude`` always, ``CLAUDE_CONFIG_DIR`` when set, and
each Claude instance in T3's settings (khipu.t3). Homes that resolve to the
same real folder are one home; the first label found wins.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from khipu import t3

DEFAULT_LABEL = "Default"


@dataclass
class ClaudeHome:
    path: Path
    label: str
    sources: list[str]
    claude_jsons: list[Path]
    is_default: bool = False
    # Filled by settings_owners(): the home whose settings.json this one's
    # resolves to, or None when it has its own.
    linked_to: "ClaudeHome | None" = field(default=None, repr=False, compare=False)

    @property
    def settings_path(self) -> Path:
        return self.path / "settings.json"

    @property
    def real(self) -> Path:
        return Path(os.path.realpath(self.path))

    @property
    def exists(self) -> bool:
        return self.path.is_dir()


def describe_sources(sources: list[str]) -> str:
    """"A", or "A, and B" / "A, B, and C" — the one-line source description."""
    if len(sources) <= 1:
        return "".join(sources)
    return ", ".join(sources[:-1]) + ", and " + sources[-1]


def _expand(raw: str, home: Path) -> Path | None:
    """An absolute path from a configured value; ``~`` means ``home``. Relative
    values have no anchor Khipu can trust, so they are skipped."""
    raw = raw.strip()
    if raw == "~" or raw.startswith("~/"):
        return home / raw[2:] if raw != "~" else home
    p = Path(raw)
    return p if p.is_absolute() else None


def discover(
    *,
    home: Path | None = None,
    default_dir: Path | None = None,
    default_json: Path | None = None,
    environ: Mapping[str, str] | None = None,
    t3_settings: Path | None = None,
) -> list[ClaudeHome]:
    """Every Claude home Khipu can find, default first. Never raises."""
    home = home if home is not None else Path.home()
    environ = os.environ if environ is None else environ
    default_dir = default_dir if default_dir is not None else home / ".claude"
    default_json = default_json if default_json is not None else home / ".claude.json"
    homes: list[ClaudeHome] = []
    by_real: dict[str, ClaudeHome] = {}

    def add(path: Path, label: str, source: str, claude_json: Path) -> ClaudeHome:
        real = os.path.realpath(path)
        found = by_real.get(real)
        if found is None:
            found = ClaudeHome(path=path, label=label, sources=[source], claude_jsons=[claude_json])
            homes.append(found)
            by_real[real] = found
            return found
        if source not in found.sources:
            found.sources.append(source)
        if all(os.path.realpath(j) != os.path.realpath(claude_json) for j in found.claude_jsons):
            found.claude_jsons.append(claude_json)
        return found

    add(default_dir, DEFAULT_LABEL, "Claude Code", default_json).is_default = True
    settings = t3_settings if t3_settings is not None else t3.settings_path(home)
    for inst in t3.claude_instances(settings):
        source = f"T3's “{inst['name']}” account"
        if not inst["enabled"]:
            source += " (disabled in T3)"
        if not inst["home_path"]:
            # T3 runs it without CLAUDE_CONFIG_DIR: the default home.
            add(default_dir, DEFAULT_LABEL, source, default_json)
            continue
        path = _expand(inst["home_path"], home)
        if path is not None:
            add(path, f"T3 · {inst['name']}", source, path / ".claude.json")
    env_dir = (environ.get("CLAUDE_CONFIG_DIR") or "").strip()
    if env_dir:
        path = _expand(env_dir, home)
        if path is not None:
            add(path, "CLAUDE_CONFIG_DIR", "CLAUDE_CONFIG_DIR", path / ".claude.json")
    settings_owners(homes)
    return homes


def settings_owners(homes: list[ClaudeHome]) -> dict[str, ClaudeHome]:
    """Homes whose ``settings.json`` resolves to the same file share their
    hooks. For each group one home is the owner (the one whose file really
    lives in its own folder, else the first found); the rest are linked to it.
    Sets ``linked_to`` on the linked homes and returns ``{str(real): owner}``
    for them."""
    groups: dict[str, list[ClaudeHome]] = {}
    for h in homes:
        h.linked_to = None
        groups.setdefault(os.path.realpath(h.settings_path), []).append(h)
    linked: dict[str, ClaudeHome] = {}
    for target, members in groups.items():
        if len(members) < 2:
            continue
        owner = next((m for m in members if os.path.realpath(m.path / "settings.json") == str(m.real / "settings.json")),
                     members[0])
        for m in members:
            if m is not owner:
                m.linked_to = owner
                linked[str(m.real)] = owner
    return linked


def transcript_home(transcript: str | os.PathLike, homes: list[ClaudeHome]) -> ClaudeHome | None:
    """The home whose ``projects/`` folder holds this Claude transcript."""
    if not transcript:
        return None
    candidates = {str(transcript), os.path.realpath(transcript)}
    for h in homes:
        for root in {str(h.path), str(h.real)}:
            prefix = root.rstrip("/") + "/projects/"
            if any(c.startswith(prefix) for c in candidates):
                return h
    return None
