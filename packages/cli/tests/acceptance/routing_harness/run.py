"""One bounded headless scenario per harness, shared by all session tests."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .received import ReceiptError, read


class Unavailable(Exception):
    """A named prerequisite is absent; the caller must report a skip."""


HARNESS_BINARIES = {"claude": "claude", "codex": "codex", "t3": "t3", "grok": "grok", "aegis": "aegis"}
MODELS = {"claude": "haiku", "codex": "gpt-6-luna"}
TOOL_SENTINEL = "synthetic tool input"
ROUTE_OPENING = "SYNTHETIC-ROUTING-OPENING"
ROUTE_END = "SYNTHETIC-ROUTING-END"
OVERSIZED_RULES = tuple(f"{ROUTE_OPENING}-{i} " + "~" * 2750 + f" {ROUTE_END}-{i}" for i in range(4))
ADVISORY = "This child got no routing: add `khipu route --brief` to its brief."


@dataclass
class Scenario:
    parent: object
    children: list
    sentinel: str
    child_sentinel: str
    evidence: Path
    attachments: list | None = None


def environment(root):
    # Allowlist, not a growing denylist: no DSN, API key, inherited session,
    # vendor home, proxy or MCP endpoint can leak into the isolated run.
    env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "LANG", "LC_ALL", "TMPDIR") if key in os.environ}
    cli = Path(__file__).resolve().parents[3]
    env.update(HOME=str(root), CODEX_HOME=str(root / ".codex"), CLAUDE_CONFIG_DIR=str(root / ".claude"),
               GROK_HOME=str(root / ".grok"), KHIPU_DATA_DIR=str(root / "data"),
               KHIPU_KEYCHAIN="0", KHIPU_FEATURE_ROUTING_PUSH="1",
               PYTHONPATH=os.pathsep.join([str(cli), *[p for p in sys.path if Path(p).name == ".python_libs"]]),
               PYTHONPYCACHEPREFIX=str(root / "pyc"))
    return env


def execute(argv, *, root, env, timeout=120):
    """Consume a run even on timeout; kill its process group, including children."""
    stdout = root / "stdout.jsonl"
    stderr = root / "stderr.txt"
    with stdout.open("wb") as out, stderr.open("wb") as err:
        process = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err, start_new_session=True)
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            (root / "exit.json").write_text(json.dumps({"status": "timeout"}))
            raise ReceiptError("headless scenario timed out; see private evidence") from None
    (root / "exit.json").write_text(json.dumps({"status": status}))
    if status:
        raise ReceiptError(f"headless scenario exited {status}; see private evidence")


class Session:
    def __init__(self, root, auth_root=None, adapters=None, replay_root=None):
        self.root = root
        self.auth_root = auth_root
        self.adapters = adapters or {}
        self.replay_root = replay_root
        self.results = {}

    def get(self, harness):
        if harness not in HARNESS_BINARIES:
            raise ReceiptError("unknown harness")
        if harness in self.results:
            result = self.results[harness]
            if isinstance(result, Exception):
                raise result
            return result
        # Reserve before starting anything; errors and unavailable runs are
        # cached too. Pytest retries cannot spend a second model turn.
        self.results[harness] = ReceiptError("scenario did not complete")
        try:
            if self.replay_root:
                root = self.replay_root / harness
                if not (root / "run.json").is_file():
                    raise Unavailable(f"{harness}: no saved received-context scenario")
                result = load_native(root, harness)
            else:
                result = self._run(harness)
        except Exception as exc:
            self.results[harness] = exc
            raise
        self.results[harness] = result
        return result

    def _run(self, harness):
        binary = shutil.which(HARNESS_BINARIES[harness])
        if not binary:
            raise Unavailable(f"{harness}: harness binary absent")
        root = self.root / harness
        root.mkdir(parents=True, mode=0o700)
        root.chmod(0o700)
        sentinel, child_sentinel = (f"RECEIVED-{uuid.uuid4().hex}" for _ in range(2))
        env = environment(root)
        env.update(ROUTING_RIG_SENTINEL=sentinel, ROUTING_RIG_CHILD_SENTINEL=child_sentinel,
                   ROUTING_RIG_PYTHON=sys.executable)
        prepare(root, oversized=harness == "claude")
        if harness not in MODELS:
            return self._adapter(harness, binary, root, env, sentinel, child_sentinel)
        credential = "auth.json" if harness == "codex" else ".credentials.json"
        source = self.auth_root / harness / credential if self.auth_root else None
        if source is None or not source.is_file():
            raise Unavailable(f"{harness}: no isolated subscription credential supplied with --routing-auth-root")
        home = root / (".codex" if harness == "codex" else ".claude")
        home.mkdir(mode=0o700)
        target = home / credential
        # Copy credential bytes without decoding or logging them. No user
        # settings, hooks, packs, MCP servers or rules are copied.
        with source.open("rb") as src, target.open("xb") as dst:
            target.chmod(0o600)
            shutil.copyfileobj(src, dst)
        hook = Path(__file__).with_name("hook.py")
        import shlex
        command = shlex.join([sys.executable, str(hook)])
        hooks = {name: [{"hooks": [{"type": "command", "command": command, "timeout": 10}]}]
                 for name in ("SessionStart", "SubagentStart", "PreToolUse")}
        from khipu.integrations import DELEGATION_MATCHER

        hooks["PreToolUse"][0]["matcher"] = DELEGATION_MATCHER
        (root / "probe.txt").write_text(TOOL_SENTINEL + "\n")
        session_id = str(uuid.uuid4())
        (root / "run.json").write_text(json.dumps({"session_id": session_id, "sentinel": sentinel,
                                                  "child_sentinel": child_sentinel,
                                                  "tool_sentinel": TOOL_SENTINEL}))
        if harness == "claude":
            settings = root / "settings.json"
            settings.write_text(json.dumps({"hooks": hooks}))
            agents = {"routing-probe": {"description": "Synthetic received-context probe", "model": "haiku",
                                        "prompt": "Use Bash once with command cat probe.txt, then answer OK. Do nothing else."}}
            prompt = (f"Use Agent once with subagent_type routing-probe. Its prompt must be: {child_sentinel}. "
                      "Do not include routing or your context in its prompt. Wait for it, then answer OK.")
            argv = [binary, "-p", prompt, "--model", MODELS[harness], "--effort", "low",
                    "--max-turns", "4", "--session-id", session_id, "--settings", str(settings),
                    "--setting-sources", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                    "--agents", json.dumps(agents), "--tools", "Agent,Bash", "--allowedTools", "Agent", "Bash(cat probe.txt)",
                    "--output-format", "stream-json", "--verbose"]
        else:
            (home / "hooks.json").write_text(json.dumps({"hooks": hooks}))
            (home / "config.toml").write_text('model = "gpt-6-luna"\nmodel_reasoning_effort = "low"\n'
                                             '[features]\nmulti_agent = true\n')
            prompt = ("Call spawn_agent exactly once with model gpt-6-luna, reasoning_effort low, "
                      f"fork_context false, and message '{child_sentinel}. Read probe.txt once, then answer OK. "
                      "Do nothing else.'. Do not put routing or your context in that message. "
                      "Wait for the child and answer OK. Do nothing else.")
            argv = [binary, "exec", "--skip-git-repo-check", "--ignore-rules", "--json",
                    "--dangerously-bypass-hook-trust", "-m", MODELS[harness], "-s", "read-only", prompt]
        try:
            execute(argv, root=root, env=env)
        finally:
            target.unlink(missing_ok=True)
        if (root / "forbidden-network").exists():
            raise ReceiptError("hook attempted a forbidden connection")
        return load_native(root, harness)

    def _adapter(self, harness, binary, root, env, sentinel, child_sentinel):
        """An optional local client driver for clients without a stable CLI.

        The driver must use the client's native pack/transport and return
        native received transcripts, not a rewritten assertion summary.
        Its capability and price selection are reviewed before provisioning.
        """
        adapter = self.adapters.get(harness)
        if not adapter:
            raise Unavailable(f"{harness}: no isolated headless received-context adapter provisioned")
        argv = [str(Path(adapter).resolve()), binary, str(root)]
        execute(argv, root=root, env=env)
        try:
            manifest = json.loads((root / "receipts.json").read_text())
            if set(manifest) != {"parents", "children"} or not manifest["parents"]:
                raise ValueError
            parents, children = [], []
            for entries, output, expected in ((manifest["parents"], parents, sentinel),
                                               (manifest["children"], children, child_sentinel)):
                for entry in entries:
                    if set(entry) != {"format", "path", "channel"}:
                        raise ValueError
                    path = (root / entry["path"]).resolve()
                    if not path.is_relative_to(root.resolve()):
                        raise ValueError
                    output.append(read(path, entry["format"]).calibrate(
                        expected, channel=entry["channel"], before_first=entry["channel"] != "tool"))
        except (OSError, ValueError, KeyError, TypeError):
            raise ReceiptError("invalid adapter received-transcript manifest") from None
        return Scenario(parents[0], children, sentinel, child_sentinel, root, parents)


def load_native(root, harness):
    """Re-read the same native artifacts without another headless/model run."""
    if harness not in MODELS:
        raise Unavailable(f"{harness}: native replay requires its provisioned adapter")
    try:
        if json.loads((root / "exit.json").read_text()) != {"status": 0}:
            raise ReceiptError("saved headless scenario did not exit successfully")
        metadata = json.loads((root / "run.json").read_text())
        if set(metadata) != {"session_id", "sentinel", "child_sentinel", "tool_sentinel"}:
            raise ValueError
        if any(not isinstance(value, str) or not value for value in metadata.values()):
            raise ValueError
    except (OSError, ValueError, TypeError):
        raise ReceiptError("invalid saved scenario metadata") from None
    if (root / "forbidden-network").exists():
        raise ReceiptError("hook attempted a forbidden connection")
    if harness == "claude":
        home = root / ".claude"
        candidates = list((home / "projects").rglob(f"{metadata['session_id']}.jsonl"))
        children = list((home / "projects").rglob("agent-*.jsonl"))
        channel = "user"
    else:
        candidates, children = [], []
        for path in (root / ".codex" / "sessions").rglob("*.jsonl"):
            try:
                with path.open() as stream:
                    first = json.loads(stream.readline())
                source = first.get("payload", {}).get("source")
            except (OSError, ValueError, AttributeError):
                raise ReceiptError("invalid native session metadata") from None
            (children if isinstance(source, dict) and "subagent" in source else candidates).append(path)
        channel = "developer"
    if len(candidates) != 1 or len(children) != 1:
        raise ReceiptError("expected one parent and one child received transcript")
    parent = read(candidates[0], harness).calibrate(metadata["sentinel"], channel=channel)
    # Current Codex encrypts NEW_TASK payloads. A native tool-result input
    # with known file contents calibrates the child without decrypting it or
    # treating an assistant's answer as proof of receipt.
    child = read(children[0], harness).calibrate(metadata["tool_sentinel"], channel="tool", before_first=False)
    return Scenario(parent, [child], metadata["sentinel"], metadata["child_sentinel"], root, [parent])


def prepare(root, *, oversized):
    data = root / "data"
    log = data / "state" / "routing" / "events.jsonl"
    log.parent.mkdir(parents=True)
    (data / "config.json").write_text(json.dumps({
        "usage_accounts": [{"label": "Synthetic Codex", "harness": "codex"}],
        "routing_reset_check": False, "routing_start_budget_ms": 5000,
        "routing_prompt_budget_ms": 1000,
    }))
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rules = OVERSIZED_RULES if oversized else (ROUTE_OPENING + " synthetic rule " + ROUTE_END,)
    rows = [{"type": "rule_add", "id": f"synthetic-rule-{i}", "seq": i + 1, "n": i + 1, "text": words,
             "written_at": stamp, "written_by": {"kind": "owner"}, "via": "app"}
            for i, words in enumerate(rules)]
    log.write_text("\n".join(map(json.dumps, rows)) + "\n")
