#!/usr/bin/env python3
"""Portable graphify nightly chain for Khipu Application Support installs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


def _app_support() -> Path:
    return Path.home() / "Library" / "Application Support" / "Khipu"


def _scripts_dir() -> Path:
    return Path(__file__).resolve().parent


def _state_dir() -> Path:
    d = _app_support() / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log_path() -> Path:
    return _state_dir() / "graphify_nightly.log"


def _graph_sqlite() -> Path:
    raw = (os.environ.get("KHIPU_GRAPH_SQLITE") or "").strip()
    if raw:
        return Path(raw).expanduser()
    out = _app_support() / "graph" / "graph.sqlite"
    out.parent.mkdir(parents=True, exist_ok=True)
    return out


def _khipu_root() -> Path | None:
    raw = (os.environ.get("KHIPU_ROOT") or os.environ.get("ALZY_ROOT") or "").strip()
    if raw:
        root = Path(raw).expanduser()
        if (root / "packages" / "cli").is_dir():
            return root
    return None


def _khipu_env(base: dict[str, str]) -> dict[str, str]:
    root = _khipu_root()
    parts: list[str] = []
    if root is not None:
        parts.extend(
            [
                str(root / "packages" / "cli"),
                str(root / "lib"),
                str(root / ".python_libs"),
            ]
        )
    extra = (base.get("PYTHONPATH") or "").strip()
    if extra:
        parts.append(extra)
    env = {**base, "PYTHONPATH": ":".join(p for p in parts if p)}
    env.setdefault("KHIPU_GRAPH_SQLITE", str(_graph_sqlite()))
    return env


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat()}] {msg}\n"
    print(line, end="")
    try:
        with open(_log_path(), "a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass


def run_step(
    label: str,
    script: str,
    *,
    timeout: int,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> int:
    log(f"step: {label}")
    cmd = [sys.executable, str(_scripts_dir() / script), *(args or [])]
    try:
        proc = subprocess.run(
            cmd,
            env=env or os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log(f"  {label} timed out (>{timeout}s)")
        return -1
    except OSError as exc:
        log(f"  {label} failed to launch: {exc}")
        return -1
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        log(f"  {label} rc={proc.returncode}: {tail}")
    else:
        lines = (proc.stdout or "").strip().splitlines()
        log(f"  {label} ok — {lines[-1] if lines else 'done'}")
    return proc.returncode


def _export_sources(env: dict[str, str]) -> Path | None:
    root = _khipu_root()
    if root is None:
        return None
    log("step: export graph sources")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "khipu.cli", "sources", "export"],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except OSError as exc:
        log(f"  sources export skipped (non-fatal): {exc}")
        return None
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        log(
            f"  sources export rc={proc.returncode}: "
            f"{tail[-1] if tail else 'failed'} — continuing with existing resolved file"
        )
        return None
    log("  sources export ok")
    resolved = (os.environ.get("KHIPU_GRAPH_SOURCES_RESOLVED") or "").strip()
    if resolved:
        return Path(resolved)
    try:
        for part in (env.get("PYTHONPATH") or "").split(":"):
            if not part:
                continue
            candidate = Path(part) / "khipu" / "sources.py"
            if candidate.is_file():
                import importlib.util

                spec = importlib.util.spec_from_file_location("khipu_sources", candidate)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return mod.resolved_path()
    except Exception:
        pass
    return _app_support() / "graph_sources.resolved.json"


def _should_skip_build(resolved: Path | None) -> tuple[bool, str]:
    if resolved is None or not resolved.is_file():
        return True, "no_resolved_sources"
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, "unreadable_resolved_sources"
    if not isinstance(raw, dict):
        return True, "invalid_resolved_sources"
    collectors = raw.get("collectors") if isinstance(raw.get("collectors"), dict) else {}
    code_roots = raw.get("code_roots") if isinstance(raw.get("code_roots"), list) else []
    any_on = any(v is not False for v in collectors.values())
    if not any_on and not code_roots:
        return True, "no_sources"
    return False, ""


def main() -> int:
    log("nightly graph refresh starting")
    resolved_override = (os.environ.get("KHIPU_GRAPH_SOURCES_RESOLVED") or "").strip()
    resolved = Path(resolved_override) if resolved_override else (
        _app_support() / "graph_sources.resolved.json"
    )
    skip, reason = _should_skip_build(resolved if resolved.is_file() else None)
    if skip:
        payload = {
            "ok": True,
            "skipped": reason,
            "message": "graph-build skipped: no graph sources configured",
        }
        print(json.dumps(payload))
        log(f"skipped: {reason}")
        return 0

    base_env = os.environ.copy()
    khipu_env = _khipu_env(base_env)
    exported = _export_sources(khipu_env)
    if exported is not None:
        khipu_env["KHIPU_GRAPH_SOURCES_RESOLVED"] = str(exported)
        resolved = exported
    skip, reason = _should_skip_build(resolved)
    if skip:
        payload = {
            "ok": True,
            "skipped": reason,
            "message": "graph-build skipped: no graph sources configured",
        }
        print(json.dumps(payload))
        log(f"skipped: {reason}")
        return 0

    rc = run_step(
        "semantic extract",
        "code_semantic_extractor.py",
        timeout=1800,
        env=khipu_env,
    )
    if rc != 0:
        log("  semantic step did not fully succeed — continuing with existing semantic_layer.json")

    build_args = ["--output", str(_graph_sqlite())]
    rc = run_step(
        "build graph",
        "build_graph.py",
        timeout=600,
        args=build_args,
        env=khipu_env,
    )
    if rc != 0:
        log("nightly FAILED — build_graph did not succeed")
        return 2

    rc = run_step("embed corpus", "embed_corpus.py", timeout=900, env=khipu_env)
    if rc != 0:
        log("  embed step did not fully succeed — new nodes may lack embeddings until the next run")

    rc = run_step(
        "embed edges",
        "embed_corpus.py",
        timeout=300,
        args=["--edges", "--prune-stale-edges"],
        env=khipu_env,
    )
    if rc == 3:
        log("  edge-embed step deferred — will retry next run")
    elif rc != 0:
        log("  edge-embed step did not fully succeed")

    if _khipu_root() is not None:
        log("step: khipu graph mirror")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "khipu.cli", "graph-sync"],
                env=khipu_env,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if proc.returncode == 0:
                log("  khipu graph mirror ok")
            else:
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()
                log(f"  khipu graph mirror rc={proc.returncode}: {tail[-1] if tail else 'failed'}")
        except OSError as exc:
            log(f"  khipu graph mirror skipped (non-fatal): {exc}")

    log("nightly complete — graph refreshed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
