"""Tests for khipu.integrations — per-harness native packs (P3 step 4).

Every test runs against a TEMP home directory (HOME is patched and the module's
path constants are re-pointed), so nothing here can touch the real harness
configs. Asserts the load-bearing guarantees: install writes only Khipu-owned
entries, never edits a pre-existing legacy hook, is idempotent, backs up before
writing, and uninstall removes only what install added.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from khipu import integrations as integ


class _TempHomeCase(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="khipu-integ-"))
        self._patches = [
            mock.patch.object(integ, "HOME", self.home),
            mock.patch.object(integ, "CLAUDE_JSON", self.home / ".claude.json"),
            mock.patch.object(integ, "CLAUDE_SETTINGS", self.home / ".claude" / "settings.json"),
            mock.patch.object(integ, "CURSOR_MCP", self.home / ".cursor" / "mcp.json"),
            mock.patch.object(integ, "CURSOR_HOOKS", self.home / ".cursor" / "hooks.json"),
            mock.patch.object(integ, "AEGIS_TOML", self.home / ".grok" / "config.toml"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()


class ClaudeCodePackTest(_TempHomeCase):
    def _seed(self):
        (self.home / ".claude").mkdir()
        legacy = {"hooks": {"PreCompact": [{"hooks": [{"type": "command",
                  "command": "python3 /me/precompact_flush.py", "timeout": 45}]}]}}
        (self.home / ".claude" / "settings.json").write_text(json.dumps(legacy))
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))

    def test_install_adds_alongside_legacy_and_backs_up(self):
        self._seed()
        out = integ.install("claude_code")
        self.assertTrue(out["detected"])
        self.assertEqual(len(out["changes"]), 5)  # mcp + Stop + PreCompact + SessionEnd + SessionStart recall
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        pc = [h["command"] for e in s["hooks"]["PreCompact"] for h in e["hooks"]]
        self.assertIn("python3 /me/precompact_flush.py", pc)         # legacy untouched
        self.assertTrue(any("khipu-stop-hook" in c for c in pc))     # ours added
        self.assertTrue(any("khipu-stop-hook" in h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"]))
        # SessionEnd is the "quit without compacting" net (2026-08-17): the hook
        # is the harness's capture step now, so it must run when the session ends.
        self.assertTrue(any("khipu-stop-hook" in h["command"] for e in s["hooks"]["SessionEnd"] for h in e["hooks"]))
        st = integ.status("claude_code")
        self.assertEqual((st["extract"], st["hook_sessionend"]), ("installed", True))
        d = json.loads((self.home / ".claude.json").read_text())
        self.assertIn("other", d["mcpServers"])                       # other servers kept
        self.assertEqual(d["mcpServers"]["khipu"]["command"], integ.mcp_launcher())
        self.assertTrue(any(b and ".bak-khipu-" in b for b in out["backups"]))

    def test_install_is_idempotent(self):
        self._seed()
        integ.install("claude_code")
        again = integ.install("claude_code")
        self.assertEqual(again["changes"], [])
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        ours = [h for e in s["hooks"]["Stop"] for h in e["hooks"] if "khipu-stop-hook" in h["command"]]
        self.assertEqual(len(ours), 1)

    def test_uninstall_removes_only_ours(self):
        self._seed()
        integ.install("claude_code")
        out = integ.uninstall("claude_code")
        self.assertTrue(out["changes"])
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        pc = [h["command"] for e in s["hooks"]["PreCompact"] for h in e["hooks"]]
        self.assertEqual(pc, ["python3 /me/precompact_flush.py"])
        self.assertNotIn("khipu", json.loads((self.home / ".claude.json").read_text())["mcpServers"])
        st = integ.status("claude_code")
        self.assertFalse(st["mcp"] or st["hook_stop"] or st["hook_precompact"])

    def test_undetected_is_reported_not_errored(self):
        out = integ.install("claude_code")
        self.assertFalse(out["detected"])
        self.assertEqual(out["changes"], [])


class CursorPackTest(_TempHomeCase):
    def test_install_uninstall_roundtrip_keeps_legacy(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "hooks.json").write_text(json.dumps(
            {"version": 1, "hooks": {"stop": [{"command": "\"/x/stop.sh\"", "timeout": 30}]}}))
        (self.home / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {}}))
        integ.install("cursor")
        h = json.loads((self.home / ".cursor" / "hooks.json").read_text())
        self.assertEqual(len(h["hooks"]["stop"]), 2)
        self.assertEqual(len(h["hooks"]["preCompact"]), 1)
        self.assertEqual(integ.install("cursor")["changes"], [])
        integ.uninstall("cursor")
        h = json.loads((self.home / ".cursor" / "hooks.json").read_text())
        self.assertEqual(h["hooks"]["stop"], [{"command": "\"/x/stop.sh\"", "timeout": 30}])
        self.assertEqual(h["hooks"]["preCompact"], [])


class AegisPackTest(_TempHomeCase):
    def test_toml_blocks_added_replaced_removed_and_parse(self):
        import tomllib

        (self.home / ".grok").mkdir()
        base = 'model = "grok"\n\n[mcp_servers.xc-mcp]\ncommand = "/opt/x"\nenabled = true\n'
        (self.home / ".grok" / "config.toml").write_text(base)
        out = integ.install("aegis")
        self.assertEqual(len(out["changes"]), 2)
        text = (self.home / ".grok" / "config.toml").read_text()
        t = tomllib.loads(text)                                # must remain valid TOML
        self.assertEqual(sorted(t["mcp_servers"]), ["khipu", "xc-mcp"])
        # Aegis gets ONE Khipu hook per event: the capture trigger. The tail-sync
        # hook is deliberately absent — it cannot run in Aegis's hook sandbox
        # (it reads the legacy Memory tree and needs PG). Audit 2026-08-17.
        for ev in ("Stop", "PreCompact", "SessionEnd"):
            handlers = t["hooks"][ev][0]["hooks"]
            self.assertEqual(len(handlers), 1, ev)
            self.assertIn("khipu-aegis-capture", handlers[0]["command"])
            self.assertNotIn(" ", handlers[0]["command"])            # space-free shim paths (B8)
            self.assertEqual(handlers[0]["env"], {"KHIPU_HARNESS": "aegis"})  # pack signature
        self.assertNotIn("khipu-stop-hook", text)
        self.assertEqual(integ.status("aegis")["extract"], "installed")
        self.assertEqual(integ.status("claude_code")["extract"], "missing")   # nothing installed in this HOME
        self.assertEqual(integ.install("aegis")["changes"], [])   # idempotent
        integ.uninstall("aegis")
        t = tomllib.loads((self.home / ".grok" / "config.toml").read_text())
        self.assertEqual(sorted(t["mcp_servers"]), ["xc-mcp"])
        self.assertNotIn("hooks", t)
        self.assertEqual(t["model"], "grok")

    def test_status_sees_native_toml_tables_without_pack_marker(self):
        """Aegis persists hooks as [[hooks.Stop.hooks]] + [hooks.Stop.hooks.env],
        not the installer comment block. Status must not report extract missing."""
        (self.home / ".grok").mkdir()
        shim = integ.aegis_capture_hook()
        mcp = integ.mcp_launcher()
        (self.home / ".grok" / "config.toml").write_text(
            "[mcp_servers.khipu]\n"
            f'command = "{mcp}"\n'
            "enabled = true\n"
            "startup_timeout_sec = 30\n"
            "\n"
            "[[hooks.Stop]]\n"
            "[[hooks.Stop.hooks]]\n"
            'type = "command"\n'
            f'command = "{shim}"\n'
            "timeout = 15\n"
            "[hooks.Stop.hooks.env]\n"
            'KHIPU_HARNESS = "aegis"\n'
            "\n"
            "[[hooks.PreCompact]]\n"
            "[[hooks.PreCompact.hooks]]\n"
            'type = "command"\n'
            f'command = "{shim}"\n'
            "timeout = 15\n"
            "[hooks.PreCompact.hooks.env]\n"
            'KHIPU_HARNESS = "aegis"\n'
            "\n"
            "[[hooks.SessionEnd]]\n"
            "[[hooks.SessionEnd.hooks]]\n"
            'type = "command"\n'
            f'command = "{shim}"\n'
            "timeout = 15\n"
            "[hooks.SessionEnd.hooks.env]\n"
            'KHIPU_HARNESS = "aegis"\n'
        )
        st = integ.status("aegis")
        self.assertTrue(st["mcp"])
        self.assertTrue(st["hook_stop"] and st["hook_precompact"])
        self.assertEqual(st["extract"], "installed")
        self.assertNotIn("khipu-pack", (self.home / ".grok" / "config.toml").read_text())


class ProbeTest(unittest.TestCase):
    def test_hook_probe_real_binary_exits_zero(self):
        """The shipped khipu-stop-hook must never block a session: exit 0 always,
        even here where PG may or may not be reachable."""
        r = integ._probe_hook(integ.stop_hook())
        self.assertTrue(r["ok"], r)
        self.assertLess(r["ms"], 30_000)

    def test_hook_probe_runs_through_the_shell_like_the_harnesses_do(self):
        """Regression for 2026-08-17: the raw repo path has a space, every harness
        runs hook commands via `sh -c`, and the old exec-style probe passed while
        the real hook died. The probe must fail on the raw path and pass on the shim."""
        raw = str(integ._root() / "packages" / "cli" / "bin" / "khipu-stop-hook")
        self.assertIn(" ", raw)  # the whole point — if this moves, the test is moot
        self.assertFalse(integ._probe_hook(raw)["ok"])
        self.assertNotIn(" ", integ.stop_hook())
        self.assertTrue(integ._probe_hook(integ.stop_hook())["ok"])


class AegisIsolationTest(unittest.TestCase):
    """Aegis is its own harness (maintainer, 2026-08-17). Exactly ONE Khipu script may
    run there — khipu-aegis-capture, via the Aegis pack's KHIPU_HARNESS=aegis
    mark. The Stop hook and the recall hook must refuse under Aegis's runner env
    by EVERY route, mark included: the Stop hook's own header says "Never in
    Aegis" because its work needs paths the sandbox denies, and Aegis's
    SessionStart discards stdout so there is nothing for a recall rule to reach.

    This class used to assert the opposite for the Stop hook — that the mark
    made it run — which is khipu-aegis-capture's rule applied to a script that
    does not share it, and it was contradicted by
    test_aegis_pack_commands_never_touch_denied_paths two tests below: the
    "passing" behavior wrote ~/Library/Logs/khipu/stop-hook.log, in the tree
    that test forbids (audit 2026-08-18). Real scripts, both ways."""

    def _run(self, script: str, extra: dict) -> tuple[int, bool, str]:
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory(prefix="khipu-iso-") as home:
            env = {k: v for k, v in os.environ.items() if k != "KHIPU_HARNESS"}
            env.update({"GROK_HOOK_EVENT": "stop", "GROK_HOOK_NAME": "user:stop[0].hooks[0]",
                        "HOME": home, "KHIPU_AEGIS_PROBE": "1"}, **extra)
            p = subprocess.run([str(Path(integ._root()) / "packages" / "cli" / "bin" / script)],
                               input='{"hookEventName":"stop","sessionId":"iso"}', capture_output=True,
                               text=True, timeout=60, env=env)
            logged = any((Path(home) / "Library" / "Logs" / "khipu").glob("*.log"))
            return p.returncode, logged, p.stdout

    def test_stop_hook_refuses_aegis_by_every_route(self):
        for extra in ({"KHIPU_HARNESS": "aegis"}, {}):
            with self.subTest(marked=bool(extra)):
                rc, logged, _ = self._run("khipu-stop-hook", extra)
                self.assertEqual((rc, logged), (0, False))

    def test_recall_hook_refuses_aegis_by_every_route(self):
        """A bare {} and nothing else — an emitted rule is a breach even though
        Aegis would discard it, because the guard is the thing being checked."""
        for extra in ({"KHIPU_HARNESS": "aegis"}, {}):
            with self.subTest(marked=bool(extra)):
                rc, logged, out = self._run("khipu-recall-hook", extra)
                self.assertEqual((rc, logged, out.strip()), (0, False, "{}"))
                self.assertNotIn("additionalContext", out)
                self.assertNotIn("additional_context", out)

    def test_recall_hook_cursor_shape_refuses_aegis_mark(self):
        """Installed Cursor command must refuse on KHIPU_HARNESS=aegis alone."""
        import os
        import subprocess

        cmd = integ.recall_hook_cursor()
        env = {k: v for k, v in os.environ.items() if k not in ("GROK_HOOK_EVENT", "GROK_HOOK_NAME")}
        env["KHIPU_HARNESS"] = "aegis"
        p = subprocess.run(cmd, shell=True, input="{}", capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout.strip(), "{}")
        self.assertNotIn("additional_context", p.stdout)
        self.assertNotIn("additionalContext", p.stdout)

    def test_the_recall_rule_is_still_emitted_outside_aegis(self):
        """The guard must not have turned every harness into a refusal."""
        import os
        import subprocess
        env = {k: v for k, v in os.environ.items()
               if k not in ("KHIPU_HARNESS", "GROK_HOOK_EVENT", "GROK_HOOK_NAME")}
        p = subprocess.run([str(Path(integ._root()) / "packages" / "cli" / "bin" / "khipu-recall-hook")],
                           input="{}", capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(p.returncode, 0)
        self.assertIn("additionalContext", p.stdout)

    def test_aegis_capture_refuses_compat_and_runs_natively(self):
        rc, _, out = self._run("khipu-aegis-capture", {})
        self.assertEqual((rc, out.strip()), (0, ""))                # refused: no probe JSON
        rc, _, out = self._run("khipu-aegis-capture", {"KHIPU_HARNESS": "aegis"})
        self.assertEqual(rc, 0)
        self.assertIn('"due"', out)                                 # ran (probe mode prints)

    def test_aegis_pack_commands_never_touch_denied_paths(self):
        """Aegis's sandbox denies ~/Library and ~/.config; a shipped Aegis hook
        that references them is the silent-failure bug of 2026-08-17."""
        script = (Path(integ._root()) / "packages" / "cli" / "bin" / "khipu-aegis-capture").read_text()
        code = "\n".join(ln for ln in script.splitlines() if not ln.lstrip().startswith("#"))
        for denied in ("Library/Logs", ".config/khipu"):
            self.assertNotIn(denied, code, f"Aegis hook must not write to {denied}")
        # And the module it runs must default its working area inside ~/.grok.
        import os
        from unittest import mock as _m
        with _m.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KHIPU_AEGIS_HOME", None)
            from khipu import aegis_capture as ac
            self.assertIn("/.grok/", str(ac.khipu_home()))

    def test_verify_isolation_probe(self):
        # The probe targets the hook Aegis actually runs (the capture hook).
        r = integ._probe_aegis_isolation(integ.aegis_capture_hook())
        self.assertTrue(r["ok"], r)


class ShimRepointTest(_TempHomeCase):
    def test_install_repoints_entries_written_with_the_raw_path(self):
        (self.home / ".claude").mkdir()
        raw = str(integ._root() / "packages" / "cli" / "bin" / "khipu-stop-hook")
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": raw, "timeout": 20}]}]}}))
        (self.home / ".claude.json").write_text("{}")
        out = integ.install("claude_code")
        self.assertTrue(any("khipu-stop-hook ->" in c for c in out["changes"]), out["changes"])
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"]]
        self.assertEqual(cmds, [integ.stop_hook()])           # re-pointed, not duplicated
        self.assertNotIn(" ", cmds[0])
        self.assertTrue((self.home / ".config" / "khipu" / "bin" / "khipu-stop-hook").is_symlink())
        self.assertEqual(integ.install("claude_code")["changes"], [])  # idempotent after


if __name__ == "__main__":
    unittest.main()


class RecallRuleTest(_TempHomeCase):
    def test_claude_gets_sessionstart_recall_hook_and_status_reports_it(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "python3 /me/other.py"}]}]}}))
        (self.home / ".claude.json").write_text("{}")
        integ.install("claude_code")
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        ss = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]]
        self.assertIn("python3 /me/other.py", ss)                       # legacy kept
        self.assertTrue(any("khipu-recall-hook" in c for c in ss))
        self.assertEqual(integ.status("claude_code")["recall_rule"], "installed")
        integ.uninstall("claude_code")
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        self.assertEqual([h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]],
                         ["python3 /me/other.py"])

    def test_cursor_rule_is_project_scoped_and_only_written_with_project(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text("{}")
        (self.home / ".cursor" / "hooks.json").write_text(json.dumps({
            "version": 1,
            "hooks": {
                "sessionStart": [
                    {"command": "\"/harness/session_start.sh\"", "timeout": 5},
                ],
            },
        }))
        proj = self.home / "someproj"
        proj.mkdir()
        integ.install("cursor")                                          # no --project
        self.assertFalse((proj / ".cursor" / "rules" / "khipu.mdc").exists())
        integ.install("cursor", project=str(proj))
        mdc = proj / ".cursor" / "rules" / "khipu.mdc"
        self.assertTrue(mdc.is_file())
        self.assertIn("alwaysApply: true", mdc.read_text())
        self.assertIn("khipu_search", mdc.read_text())
        h = json.loads((self.home / ".cursor" / "hooks.json").read_text())
        ss = h["hooks"]["sessionStart"]
        self.assertEqual(ss[0]["command"], "\"/harness/session_start.sh\"")  # kept
        self.assertEqual(ss[0]["timeout"], 5)
        ours = [e for e in ss if "khipu-recall-hook" in e["command"]]
        self.assertEqual(len(ours), 1)
        self.assertIn("--cursor", ours[0]["command"])
        self.assertEqual(ours[0]["timeout"], integ.CURSOR_RECALL_TIMEOUT)
        self.assertTrue(integ.status("cursor")["hook_sessionstart"])
        self.assertEqual(integ.install("cursor", project=str(proj))["changes"], [])  # idempotent
        integ.uninstall("cursor", project=str(proj))
        self.assertFalse(mdc.exists())
        h2 = json.loads((self.home / ".cursor" / "hooks.json").read_text())
        self.assertEqual(
            [e["command"] for e in h2["hooks"]["sessionStart"]],
            ["\"/harness/session_start.sh\""],
        )
        self.assertEqual(integ.status("cursor")["recall_rule"], "project_scoped")
        self.assertFalse(integ.status("cursor")["hook_sessionstart"])

    def test_recall_probe_real_hook(self):
        r = integ._probe_recall(integ.recall_hook())
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["chars"], 200)

    def test_recall_probe_cursor_shape(self):
        r = integ._probe_recall(integ.recall_hook_cursor())
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["chars"], 200)

    def test_cursor_verify_probes_sessionstart_recall_when_installed(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text("{}")
        (self.home / ".cursor" / "hooks.json").write_text("{}")
        integ.install("cursor")
        with mock.patch.object(integ, "_probe_mcp", return_value={"ok": True}), mock.patch.object(
            integ, "_probe_hook", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_probe_native_extract", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_runtime", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_probe_recall", return_value={"ok": True, "chars": 400}
        ) as probe, mock.patch.object(
            integ, "_probe_aegis_refusal", return_value={"ok": True, "refused_marked": True}
        ) as refuse, mock.patch(
            "khipu.probe.run_probe", return_value={"ok": True, "harness": "cursor"}
        ):
            out = integ.verify("cursor")
        self.assertIn("recall", out["components"])
        self.assertTrue(out["components"]["recall"]["ok"])
        self.assertIn("recall_probe", out["components"])
        self.assertTrue(out["components"]["recall_probe"]["ok"])
        probe.assert_called_once()
        self.assertIn("--cursor", probe.call_args[0][0])
        recall_refuse = [
            c for c in refuse.call_args_list
            if c.args and "khipu-recall-hook" in str(c.args[0])
        ]
        self.assertEqual(len(recall_refuse), 1)
        self.assertIn("--cursor", recall_refuse[0].args[0])

    def test_cursor_verify_fails_when_recall_probe_fails(self):
        """W6.1: a red recall probe must fail verify even when every other
        component (hook, mcp, extract, recall-rule, runtime) is green — the
        probe is the only component that proves capture-then-search actually
        works end-to-end."""
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text("{}")
        (self.home / ".cursor" / "hooks.json").write_text("{}")
        integ.install("cursor")
        with mock.patch.object(integ, "_probe_mcp", return_value={"ok": True}), mock.patch.object(
            integ, "_probe_hook", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_probe_native_extract", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_runtime", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_probe_recall", return_value={"ok": True, "chars": 400}
        ), mock.patch.object(
            integ, "_probe_aegis_refusal", return_value={"ok": True, "refused_marked": True}
        ), mock.patch(
            "khipu.probe.run_probe",
            return_value={"ok": False, "harness": "cursor", "error": "nonce never surfaced"},
        ):
            out = integ.verify("cursor")
        self.assertFalse(out["components"]["recall_probe"]["ok"])
        self.assertFalse(out["ok"])

    def test_cursor_verify_probe_crash_is_a_failed_component_not_a_raise(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text("{}")
        (self.home / ".cursor" / "hooks.json").write_text("{}")
        integ.install("cursor")
        with mock.patch.object(integ, "_probe_mcp", return_value={"ok": True}), mock.patch.object(
            integ, "_probe_hook", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_probe_native_extract", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_runtime", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_probe_recall", return_value={"ok": True, "chars": 400}
        ), mock.patch.object(
            integ, "_probe_aegis_refusal", return_value={"ok": True, "refused_marked": True}
        ), mock.patch(
            "khipu.probe.run_probe", side_effect=RuntimeError("boom")
        ):
            out = integ.verify("cursor")  # must not raise
        self.assertFalse(out["components"]["recall_probe"]["ok"])
        self.assertIn("boom", out["components"]["recall_probe"]["error"])
        self.assertFalse(out["ok"])


class CursorVerifyRuleStaleTest(_TempHomeCase):
    """rule_stale: the Cursor recall rule is per-project (khipu.mdc), so a
    version bump that changes cursor_mdc() leaves an already-installed
    project's rule file stale until `integrations install cursor --project`
    is re-run there. verify() must say so rather than silently reporting a
    green recall component while the actual file on disk is out of date."""

    def _verify_with_stubs(self, project=None):
        with mock.patch.object(integ, "_probe_mcp", return_value={"ok": True}), mock.patch.object(
            integ, "_probe_hook", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_probe_native_extract", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_runtime", return_value={"ok": True}
        ), mock.patch.object(
            integ, "_probe_recall", return_value={"ok": True, "chars": 400}
        ), mock.patch.object(
            integ, "_probe_aegis_refusal", return_value={"ok": True, "refused_marked": True}
        ), mock.patch(
            "khipu.probe.run_probe", return_value={"ok": True, "harness": "cursor"}
        ):
            return integ.verify("cursor", project=project)

    def test_no_project_reports_null_with_note(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text("{}")
        (self.home / ".cursor" / "hooks.json").write_text("{}")
        integ.install("cursor")
        out = self._verify_with_stubs(project=None)
        self.assertIsNone(out["rule_stale"])
        self.assertIn("per-project", out["note"])

    def test_freshly_installed_project_rule_is_not_stale(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text("{}")
        (self.home / ".cursor" / "hooks.json").write_text("{}")
        proj = self.home / "someproj"
        proj.mkdir()
        integ.install("cursor")
        integ.install("cursor", project=str(proj))
        out = self._verify_with_stubs(project=str(proj))
        self.assertFalse(out["rule_stale"])

    def test_edited_or_outdated_rule_file_is_stale(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text("{}")
        (self.home / ".cursor" / "hooks.json").write_text("{}")
        proj = self.home / "someproj"
        proj.mkdir()
        integ.install("cursor")
        integ.install("cursor", project=str(proj))
        mdc = proj / ".cursor" / "rules" / "khipu.mdc"
        mdc.write_text("stale content from a previous cursor_mdc() version\n")
        out = self._verify_with_stubs(project=str(proj))
        self.assertTrue(out["rule_stale"])

    def test_missing_rule_file_for_a_project_is_stale(self):
        (self.home / ".cursor").mkdir()
        (self.home / ".cursor" / "mcp.json").write_text("{}")
        (self.home / ".cursor" / "hooks.json").write_text("{}")
        proj = self.home / "someproj"
        proj.mkdir()
        integ.install("cursor")  # never installed --project
        out = self._verify_with_stubs(project=str(proj))
        self.assertTrue(out["rule_stale"])


class CodexPackTest(_TempHomeCase):
    def setUp(self):
        super().setUp()
        self._cx = [
            mock.patch.object(integ, "CODEX_TOML", self.home / ".codex" / "config.toml"),
            mock.patch.object(integ, "CODEX_HOOKS", self.home / ".codex" / "hooks.json"),
        ]
        for p in self._cx:
            p.start()

    def tearDown(self):
        for p in self._cx:
            p.stop()
        super().tearDown()

    def test_toml_mcp_plus_claude_shaped_hooks_roundtrip(self):
        import tomllib

        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "config.toml").write_text('hooks = true\n\n[mcp_servers.node_repl]\ncommand = "node"\n')
        (self.home / ".codex" / "hooks.json").write_text(json.dumps(
            {"hooks": {"PreCompact": [{"hooks": [{"type": "command", "command": "python3 '/me/precompact_flush.py'", "timeout": 45}]}]}}))
        out = integ.install("codex")
        self.assertTrue(out["detected"])
        self.assertEqual(len(out["changes"]), 5)   # mcp + Stop + PreCompact + SessionEnd + SessionStart
        t = tomllib.loads((self.home / ".codex" / "config.toml").read_text())
        self.assertEqual(sorted(t["mcp_servers"]), ["khipu", "node_repl"])
        h = json.loads((self.home / ".codex" / "hooks.json").read_text())
        pc = [x["command"] for e in h["hooks"]["PreCompact"] for x in e["hooks"]]
        self.assertIn("python3 '/me/precompact_flush.py'", pc)      # legacy kept
        self.assertTrue(any("khipu-stop-hook" in c for c in pc))
        self.assertTrue(any("khipu-recall-hook" in x["command"] for e in h["hooks"]["SessionStart"] for x in e["hooks"]))
        st = integ.status("codex")
        self.assertTrue(st["mcp"] and st["hook_stop"] and st["hook_precompact"])
        self.assertEqual(st["recall_rule"], "installed")
        self.assertEqual(integ.install("codex")["changes"], [])   # idempotent
        integ.uninstall("codex")
        t = tomllib.loads((self.home / ".codex" / "config.toml").read_text())
        self.assertEqual(sorted(t["mcp_servers"]), ["node_repl"])
        h = json.loads((self.home / ".codex" / "hooks.json").read_text())
        self.assertEqual([x["command"] for e in h["hooks"]["PreCompact"] for x in e["hooks"]],
                         ["python3 '/me/precompact_flush.py'"])
        self.assertEqual(h["hooks"]["Stop"], [])


class UnreadableConfigTest(_TempHomeCase):
    """`_load_json` used to return {} for a config it could not parse, and every
    caller then did read -> modify -> write. One bad read of ~/.claude.json —
    most plausibly a partial read while Claude Code is saving it — replaced 77 KB
    of MCP servers and 41 projects with Khipu's key alone (audit 2026-08-17).
    """

    def _seed_claude(self):
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        (self.home / ".claude" / "settings.json").write_text("{}")

    def test_a_truncated_config_aborts_instead_of_overwriting(self):
        self._seed_claude()
        # Exactly what a partial read of a large file looks like.
        truncated = '{"mcpServers": {"other": {"command": "x"}}, "projects": {"a"'
        integ.CLAUDE_JSON.write_text(truncated)
        out = integ.install("claude_code")
        self.assertTrue(out.get("aborted"), out)
        self.assertIn("refusing to overwrite", out["error"])
        self.assertEqual(integ.CLAUDE_JSON.read_text(), truncated,
                         "the unreadable file must be left exactly as found")

    def test_a_json_array_is_refused_too(self):
        self._seed_claude()
        integ.CLAUDE_JSON.write_text('["not", "an", "object"]')
        out = integ.install("claude_code")
        self.assertTrue(out.get("aborted"))
        self.assertEqual(integ.CLAUDE_JSON.read_text(), '["not", "an", "object"]')

    def test_a_byte_order_mark_is_tolerated_not_treated_as_corruption(self):
        self._seed_claude()
        integ.CLAUDE_JSON.write_text('\ufeff{"mcpServers": {"other": {"command": "x"}}}',
                                     encoding="utf-8")
        out = integ.install("claude_code")
        self.assertFalse(out.get("aborted"), out)
        d = json.loads(integ.CLAUDE_JSON.read_text(encoding="utf-8-sig"))
        self.assertIn("other", d["mcpServers"], "the pre-existing server must survive")
        self.assertIn("khipu", d["mcpServers"])

    def test_an_absent_or_empty_config_is_still_a_normal_install(self):
        self._seed_claude()
        for content in (None, "", "   \n"):
            with self.subTest(content=content):
                if content is None:
                    integ.CLAUDE_JSON.unlink(missing_ok=True)
                else:
                    integ.CLAUDE_JSON.write_text(content)
                out = integ.install("claude_code")
                self.assertFalse(out.get("aborted"), out)
                self.assertIn("khipu", json.loads(integ.CLAUDE_JSON.read_text())["mcpServers"])

    def test_status_reports_the_problem_rather_than_raising(self):
        self._seed_claude()
        integ.CLAUDE_JSON.write_text("{broken")
        st = integ.status("claude_code")
        self.assertTrue(st.get("aborted"))
        self.assertIn("refusing", st["error"])

    def test_a_path_with_regex_escapes_is_written_literally(self):
        """re.sub interprets backslash escapes and \\g<n> in a literal
        replacement string, so a shim path containing either would have been
        rewritten into a corrupt TOML block (audit 2026-08-17)."""
        (self.home / ".grok").mkdir(parents=True, exist_ok=True)
        integ.AEGIS_TOML.write_text('[mcp_servers.khipu]\ncommand = "old"\n')
        nasty = '/tmp/kh\\g<0>ipu/bin/khipu-mcp'
        with mock.patch.object(integ, "mcp_launcher", lambda: nasty):
            integ.install("aegis")
        self.assertIn(nasty, integ.AEGIS_TOML.read_text())

