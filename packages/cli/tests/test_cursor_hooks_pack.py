"""Cursor pack, slice B (docs/plans/2026-10-27-cursor-antigravity-hooks.md 5.2):
one command string per hook across packs, beforeSubmitPrompt and postToolUse
entries, status keys and proof, and the migration off ``--cursor``.

Hermetic: a temp HOME, every path constant re-pointed, nothing real touched."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import integrations as integ

FOREIGN = {
    "sessionStart": [{"command": "\"/harness/session_start.sh\"", "timeout": 5}],
    "beforeSubmitPrompt": [{"command": "\"/harness/before_submit_prompt_chain.sh\"", "timeout": 10}],
    "postToolUse": [{"command": "\"/harness/audit.sh\"", "matcher": "Shell"}],
    "stop": [{"command": "\"/harness/stop.sh\"", "timeout": 30}],
    "preToolUse": [{"command": "\"/harness/guard.sh\"", "failClosed": True}],
    "afterFileEdit": [{"command": "\"/harness/fmt.sh\""}],
}


class _Home(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="khipu-cursor-b-"))
        self._patches = [
            mock.patch.object(integ, "HOME", self.home),
            mock.patch.object(integ, "CLAUDE_JSON", self.home / ".claude.json"),
            mock.patch.object(integ, "CLAUDE_SETTINGS", self.home / ".claude" / "settings.json"),
            mock.patch.object(integ, "CURSOR_MCP", self.home / ".cursor" / "mcp.json"),
            mock.patch.object(integ, "CURSOR_HOOKS", self.home / ".cursor" / "hooks.json"),
            mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.home / ".config" / "khipu")}),
        ]
        for p in self._patches:
            p.start()
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def old_recall(self) -> str:
        """What an install before this slice wrote: the shim link plus ``--cursor``."""
        return f"{integ._shim_dir() / 'khipu-recall-hook'} --cursor"

    def hooks(self) -> dict:
        return json.loads((self.home / ".cursor" / "hooks.json").read_text())

    def seed(self, hooks: dict) -> None:
        (self.home / ".cursor" / "hooks.json").write_text(json.dumps({"version": 1, "hooks": hooks}))


def _khipu(entries: list[dict]) -> list[dict]:
    return [e for e in entries if "khipu-" in e.get("command", "")]


def _foreign(entries: list[dict]) -> list[dict]:
    return [e for e in entries if "khipu-" not in e.get("command", "")]


class CursorHooksInstallTest(_Home):
    def test_install_writes_every_hook_with_the_claude_packs_command_strings(self):
        self.seed(copy.deepcopy(FOREIGN))
        integ.install("cursor")
        h = self.hooks()["hooks"]
        want = {
            "stop": integ.stop_hook(), "preCompact": integ.stop_hook(), "sessionEnd": integ.stop_hook(),
            "subagentStop": integ.stop_hook(), "sessionStart": integ.recall_hook(),
            "beforeSubmitPrompt": integ.prompt_recall_hook(), "postToolUse": integ.delegation_hook(),
        }
        for event, cmd in want.items():
            ours = _khipu(h[event])
            self.assertEqual([e["command"] for e in ours], [cmd], event)
            self.assertNotIn(" ", cmd, event)  # no flags, no wrapper
        by = {e: _khipu(h[e])[0] for e in want}
        self.assertEqual(by["sessionStart"]["timeout"], integ.CURSOR_RECALL_TIMEOUT)
        self.assertEqual(by["beforeSubmitPrompt"]["timeout"], integ.PROMPT_RECALL_TIMEOUT)
        self.assertEqual(by["postToolUse"]["timeout"], integ.DELEGATION_TIMEOUT)
        self.assertEqual(by["postToolUse"]["matcher"], "Task|Shell")

    def test_no_permission_style_hook_is_ever_written(self):
        """No preToolUse (or subagentStart) entry, and no entry of ours carries a
        field that could decide a permission prompt."""
        self.seed(copy.deepcopy(FOREIGN))
        integ.install("cursor")
        h = self.hooks()["hooks"]
        self.assertEqual(h["preToolUse"], FOREIGN["preToolUse"])
        self.assertNotIn("subagentStart", h)
        for event, entries in h.items():
            for e in _khipu(entries):
                self.assertTrue(set(e) <= {"command", "timeout", "matcher"}, (event, e))

    def test_foreign_hooks_survive_install_untouched_and_in_order(self):
        self.seed(copy.deepcopy(FOREIGN))
        integ.install("cursor")
        h = self.hooks()["hooks"]
        for event, entries in FOREIGN.items():
            self.assertEqual(_foreign(h[event]), entries, event)
            self.assertEqual(h[event][: len(entries)], entries, event)  # ours are appended after

    def test_reinstall_is_a_noop_and_writes_no_backup(self):
        self.seed(copy.deepcopy(FOREIGN))
        integ.install("cursor")
        before = (self.home / ".cursor" / "hooks.json").read_text()
        again = integ.install("cursor")
        self.assertEqual(again["changes"], [])
        self.assertNotIn("backups", again)
        self.assertEqual((self.home / ".cursor" / "hooks.json").read_text(), before)

    def test_dry_run_changes_nothing(self):
        self.seed(copy.deepcopy(FOREIGN))
        before = (self.home / ".cursor" / "hooks.json").read_text()
        out = integ.install("cursor", dry_run=True)
        self.assertTrue(any("beforeSubmitPrompt" in c for c in out["changes"]))
        self.assertTrue(any("postToolUse" in c for c in out["changes"]))
        self.assertEqual((self.home / ".cursor" / "hooks.json").read_text(), before)

    def test_old_cursor_flag_entry_is_repointed_in_place(self):
        old = self.old_recall()
        self.seed({"sessionStart": [{"command": "\"/harness/session_start.sh\"", "timeout": 5},
                                    {"command": old, "timeout": 30}]})
        out = integ.install("cursor")
        ss = self.hooks()["hooks"]["sessionStart"]
        self.assertEqual([e["command"] for e in ss],
                         ["\"/harness/session_start.sh\"", integ.recall_hook()])  # same slot, no duplicate
        self.assertTrue(any("sessionStart" in c and "khipu-recall-hook" in c for c in out["changes"]))
        self.assertEqual(integ.install("cursor")["changes"], [])
        self.assertTrue(integ.status("cursor")["hook_sessionstart"])

    def test_drifted_timeout_or_matcher_on_the_new_entries_is_put_back(self):
        integ.install("cursor")
        h = self.hooks()
        _khipu(h["hooks"]["postToolUse"])[0].update(matcher="Shell", timeout=60)
        _khipu(h["hooks"]["beforeSubmitPrompt"])[0]["timeout"] = 60
        (self.home / ".cursor" / "hooks.json").write_text(json.dumps(h))
        integ.install("cursor")
        h = self.hooks()["hooks"]
        self.assertEqual(_khipu(h["postToolUse"])[0]["matcher"], "Task|Shell")
        self.assertEqual(_khipu(h["postToolUse"])[0]["timeout"], integ.DELEGATION_TIMEOUT)
        self.assertEqual(_khipu(h["beforeSubmitPrompt"])[0]["timeout"], integ.PROMPT_RECALL_TIMEOUT)

    def test_the_stop_family_keeps_an_owner_edited_timeout(self):
        integ.install("cursor")
        h = self.hooks()
        _khipu(h["hooks"]["stop"])[0]["timeout"] = 45
        (self.home / ".cursor" / "hooks.json").write_text(json.dumps(h))
        self.assertEqual(integ.install("cursor")["changes"], [])
        self.assertEqual(_khipu(self.hooks()["hooks"]["stop"])[0]["timeout"], 45)


class CursorHooksUninstallTest(_Home):
    def test_uninstall_removes_every_khipu_entry_and_leaves_foreign_ones_identical(self):
        self.seed(copy.deepcopy(FOREIGN))
        integ.install("cursor")
        integ.uninstall("cursor")
        h = self.hooks()["hooks"]
        for event, entries in FOREIGN.items():
            self.assertEqual(h[event], entries, event)
        for event, entries in h.items():
            self.assertEqual(_khipu(entries), [], event)
        self.assertEqual(json.loads((self.home / ".cursor" / "mcp.json").read_text()),
                         {"mcpServers": {"other": {"command": "x"}}})

    def test_uninstall_also_clears_a_khipu_entry_someone_added_to_pretooluse(self):
        self.seed({"preToolUse": [{"command": "\"/harness/guard.sh\""},
                                  {"command": integ.delegation_hook(), "matcher": "Task|Shell"}],
                   "subagentStart": [{"command": integ.delegation_hook()}]})
        integ.uninstall("cursor")
        h = self.hooks()["hooks"]
        self.assertEqual(h["preToolUse"], [{"command": "\"/harness/guard.sh\""}])
        self.assertEqual(h["subagentStart"], [])

    def test_uninstall_with_nothing_of_ours_does_not_rewrite_the_file(self):
        self.seed(copy.deepcopy(FOREIGN))
        before = (self.home / ".cursor" / "hooks.json").read_text()
        out = integ.uninstall("cursor")
        self.assertNotIn("backups", out)
        self.assertEqual((self.home / ".cursor" / "hooks.json").read_text(), before)


class CursorHooksStatusTest(_Home):
    def test_status_before_and_after_install(self):
        st = integ.status("cursor")
        self.assertEqual((st["prompt_recall"], st["delegation_push"], st["hook_sessionstart"]),
                         ("missing", "missing", False))
        integ.install("cursor")
        st = integ.status("cursor")
        self.assertEqual((st["prompt_recall"], st["delegation_push"], st["hook_sessionstart"], st["mcp"]),
                         ("installed", "installed", True, True))
        self.assertEqual(st["recall_rule"], "project_scoped")
        self.assertEqual(st["extract"], "installed")
        self.assertTrue(st["launcher_ok"])
        names = {x["name"] for x in st["launchers"]}
        self.assertTrue({"khipu-prompt-recall", "khipu-delegation-hook"} <= names)

    def test_proof_says_what_a_real_run_has_and_has_not_shown(self):
        proof = integ.status("cursor")["proof"]
        self.assertEqual(set(proof), {"mcp", "capture", "session_recall", "prompt_recall", "delegation_push"})
        self.assertTrue(set(proof.values()) <= {"hermetic", "unproven"}
                        | {v for v in proof.values() if v.startswith(("real-run ", "not-seen "))})
        # Context at a prompt or a tool call is the unproven part (the docs
        # disagree with the shipped binary); nothing claims it works. Slice C's
        # real runs never saw beforeSubmitPrompt fire (CLI, T3 logs).
        self.assertEqual(proof["prompt_recall"], "not-seen 2026-10-27")
        self.assertEqual(proof["delegation_push"], "unproven")
        proof["prompt_recall"] = "real-run 2099-01-01"  # a copy: the module's table is untouched
        self.assertEqual(integ.status("cursor")["proof"]["prompt_recall"], "not-seen 2026-10-27")

    def test_a_removed_entry_reads_missing_not_installed(self):
        integ.install("cursor")
        h = self.hooks()
        h["hooks"]["postToolUse"] = []
        h["hooks"]["beforeSubmitPrompt"] = []
        (self.home / ".cursor" / "hooks.json").write_text(json.dumps(h))
        st = integ.status("cursor")
        self.assertEqual((st["prompt_recall"], st["delegation_push"]), ("missing", "missing"))


def _cursor_loader_merge(cursor_hooks: dict, claude_settings: dict) -> dict:
    """Cursor's rule for third-party hooks: load ``~/.claude/settings.json``,
    map each Claude event to Cursor's, and drop an imported hook only when its
    command string equals one Cursor already has for that event."""
    merged = {event: [e.get("command") for e in entries] for event, entries in cursor_hooks.get("hooks", {}).items()}
    for claude_event, event in integ._CURSOR_IMPORT_EVENTS.items():
        for group in claude_settings.get("hooks", {}).get(claude_event, []):
            for h in group.get("hooks", []):
                cmd = h.get("command")
                if cmd and cmd not in merged.setdefault(event, []):
                    merged[event].append(cmd)
    return merged


class CursorDedupeTest(_Home):
    OWNED = ("stop", "preCompact", "sessionEnd", "subagentStop", "sessionStart", "beforeSubmitPrompt")

    def _claude(self) -> dict:
        (self.home / ".claude").mkdir(exist_ok=True)
        integ.install("claude_code")
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def _khipu_runs(self, merged: dict, event: str) -> list[str]:
        return [c for c in merged.get(event, []) if "khipu-" in c]

    def test_each_khipu_hook_runs_once_per_event_when_cursor_imports_the_claude_entries(self):
        integ.install("cursor")
        merged = _cursor_loader_merge(self.hooks(), self._claude())
        for event in self.OWNED:
            self.assertEqual(len(self._khipu_runs(merged, event)), 1, (event, merged.get(event)))
        # postToolUse is Cursor-only (Claude's delegation hook is PreToolUse, which
        # imports as preToolUse): one run there too.
        self.assertEqual(len(self._khipu_runs(merged, "postToolUse")), 1)
        st = integ.status("cursor")["third_party_import"]
        self.assertTrue(st["claude_settings"])
        self.assertEqual(st["double_firing"], [])

    def test_the_old_cursor_flag_install_double_fires_and_status_says_where(self):
        claude = self._claude()
        self.seed({"sessionStart": [{"command": self.old_recall(), "timeout": 30}]})
        merged = _cursor_loader_merge(self.hooks(), claude)
        self.assertEqual(len(self._khipu_runs(merged, "sessionStart")), 2)  # the bug this slice fixes
        self.assertEqual(integ.status("cursor")["third_party_import"]["double_firing"], ["sessionStart"])
        integ.install("cursor")
        merged = _cursor_loader_merge(self.hooks(), claude)
        self.assertEqual(len(self._khipu_runs(merged, "sessionStart")), 1)
        self.assertEqual(integ.status("cursor")["third_party_import"]["double_firing"], [])

    def test_no_claude_settings_means_no_import(self):
        integ.install("cursor")
        st = integ.status("cursor")["third_party_import"]
        self.assertEqual(st, {"claude_settings": False, "events": [], "double_firing": []})


class CursorVerifyShapesTest(_Home):
    def test_the_prompt_probe_sends_a_cursor_envelope_when_asked(self):
        seen: list[dict] = []

        class _P:
            returncode, stdout, stderr = 0, "{}", ""

        def fake_run(cmd, **kw):
            seen.append(json.loads(kw["input"]))
            return _P()

        with mock.patch("subprocess.run", side_effect=fake_run):
            integ._probe_prompt_recall("khipu-prompt-recall", cursor=True)
            integ._probe_prompt_recall("khipu-prompt-recall")
        cursor_calls, plain_calls = seen[:2], seen[2:]
        for payload in cursor_calls:
            self.assertEqual(payload["hook_event_name"], "beforeSubmitPrompt")
            self.assertIn("cursor_version", payload)
            self.assertEqual(payload["conversation_id"], payload["session_id"])
        for payload in plain_calls:
            self.assertNotIn("cursor_version", payload)

    def test_the_recall_probe_payload_is_detected_as_cursor(self):
        from khipu import hook_shape

        payload = json.loads(integ._CURSOR_PROBE_PAYLOAD)
        self.assertEqual(hook_shape.detect(payload, {}), ("cursor", "cursor"))


if __name__ == "__main__":
    unittest.main()
