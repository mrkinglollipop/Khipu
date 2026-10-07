# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""Tests for khipu.integrations — per-harness native packs (P3 step 4).

Every test runs against a TEMP home directory (HOME is patched and the module's
path constants are re-pointed), so nothing here can touch the real harness
configs. Asserts the load-bearing guarantees: install writes only Khipu-owned
entries, never edits a pre-existing legacy hook, is idempotent, backs up before
writing, and uninstall removes only what install added.
"""
from __future__ import annotations

import json
import os
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
            # gateway_token_file() reads khipu.paths.data_dir(), which is NOT
            # governed by the integ.HOME patch above — it reads Path.home()
            # itself (or KHIPU_DATA_DIR). Left unpatched, a Mac that already
            # has a real ~/.config/khipu/gateway-token staged (the maintainer's,
            # per the 2026-09-14 gap) leaks into every aegis install/verify
            # test here. Isolate it under the same temp home as everything else.
            mock.patch.dict(os.environ, {"KHIPU_DATA_DIR": str(self.home / ".config" / "khipu")}),
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
        # mcp + Stop + PreCompact + SessionEnd + SubagentStop + SessionStart recall
        # + UserPromptSubmit recall
        self.assertEqual(len(out["changes"]), 7)
        s = json.loads((self.home / ".claude" / "settings.json").read_text())
        pc = [h["command"] for e in s["hooks"]["PreCompact"] for h in e["hooks"]]
        self.assertIn("python3 /me/precompact_flush.py", pc)         # legacy untouched
        self.assertTrue(any("khipu-stop-hook" in c for c in pc))     # ours added
        self.assertTrue(any("khipu-stop-hook" in h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"]))
        # SessionEnd is the "quit without compacting" net (2026-08-17): the hook
        # is the harness's capture step now, so it must run when the session ends.
        self.assertTrue(any("khipu-stop-hook" in h["command"] for e in s["hooks"]["SessionEnd"] for h in e["hooks"]))
        # UserPromptSubmit (R1): the per-prompt recall push, alongside the
        # SessionStart one, both Khipu-owned.
        self.assertTrue(any("khipu-prompt-recall" in h["command"]
                             for e in s["hooks"]["UserPromptSubmit"] for h in e["hooks"]))
        st = integ.status("claude_code")
        self.assertEqual((st["extract"], st["hook_sessionend"]), ("installed", True))
        self.assertEqual(st["prompt_recall"], "installed")
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
        self.assertEqual(s["hooks"]["UserPromptSubmit"], [])
        st = integ.status("claude_code")
        self.assertFalse(st["mcp"] or st["hook_stop"] or st["hook_precompact"])
        self.assertEqual(st["prompt_recall"], "missing")

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


class ProbeTest(_TempHomeCase):
    def setUp(self):
        super().setUp()
        # _shim() is read-only outside install() now (B1): these probes exec
        # the shim through the shell, so a real space-free link must exist
        # first, same as any other harness would get from a real install.
        (self.home / ".claude").mkdir()
        integ.install("claude_code")

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


class ProbePromptRecallSnapshotTest(unittest.TestCase):
    """R1 follow-up: _probe_prompt_recall's topical assertion is conditional
    on the LOCAL snapshot being fresh (mocked here — no real subprocess, no
    real snapshot needed)."""

    def _run(self, *, snapshot_fresh, trivial_ctx="", topical_ctx=""):
        def _trivial_response(**_kw):
            return mock.Mock(returncode=0, stdout=json.dumps(
                {"hookSpecificOutput": {"additionalContext": trivial_ctx}} if trivial_ctx else {}
            ), stderr="")

        def _topical_response(**_kw):
            return mock.Mock(returncode=0, stdout=json.dumps(
                {"hookSpecificOutput": {"additionalContext": topical_ctx}} if topical_ctx else {}
            ), stderr="")

        with mock.patch(
            "khipu.hub_snapshot.snapshot_is_fresh", return_value=(snapshot_fresh, {})
        ), mock.patch.object(integ.subprocess, "run", side_effect=[
            _trivial_response(), _topical_response()
        ]):
            return integ._probe_prompt_recall("khipu-prompt-recall")

    def test_fresh_snapshot_requires_a_non_empty_topical_result(self):
        out = self._run(snapshot_fresh=True, topical_ctx="")
        self.assertFalse(out["ok"])
        self.assertIn("fresh", out["error"])

    def test_fresh_snapshot_with_a_real_hit_passes(self):
        out = self._run(snapshot_fresh=True, topical_ctx="## Prior work on this topic\n- x")
        self.assertTrue(out["ok"])
        self.assertEqual(out["topical_context_chars"], len("## Prior work on this topic\n- x"))

    def test_missing_snapshot_does_not_require_a_topical_hit(self):
        """No local replica: the hook falls back to the hub, where a
        legitimate timeout is the documented safe failure — must not flake
        verify() over it."""
        out = self._run(snapshot_fresh=False, topical_ctx="")
        self.assertTrue(out["ok"])

    def test_a_non_empty_trivial_prompt_always_fails_regardless_of_snapshot(self):
        out = self._run(snapshot_fresh=False, trivial_ctx="should not be here")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "trivial prompt was not empty")

    def test_repeat_calls_use_distinct_session_ids_not_a_fixed_literal(self):
        """Found live 2026-09-14: the probe used to send session_id="khipu-verify"
        on every call. khipu-prompt-recall's own dedup (recall_prompt.py) suppresses
        re-showing the same hit batch to the same session_id, so the SECOND (and
        every later) `integrations verify` on a Mac that had already run one replayed
        the first run's dedup file and got hits=[] back — read as a broken topical
        lane when dedup was working as designed. Two consecutive calls (snapshot
        fresh, subprocess mocked to inspect what was sent) must never reuse a
        session_id, in either the topical or the trivial call."""
        seen_session_ids = []

        def _record_and_respond(*_a, input=None, **_kw):  # noqa: A002
            payload = json.loads(input)
            seen_session_ids.append(payload.get("session_id"))
            ctx = "## Prior work on this topic\n- x" if payload.get("prompt") != "ok" else ""
            return mock.Mock(returncode=0, stdout=json.dumps(
                {"hookSpecificOutput": {"additionalContext": ctx}} if ctx else {}
            ), stderr="")

        with mock.patch(
            "khipu.hub_snapshot.snapshot_is_fresh", return_value=(True, {})
        ), mock.patch.object(integ.subprocess, "run", side_effect=_record_and_respond):
            out1 = integ._probe_prompt_recall("khipu-prompt-recall")
            out2 = integ._probe_prompt_recall("khipu-prompt-recall")

        self.assertTrue(out1["ok"])
        self.assertTrue(out2["ok"])
        self.assertEqual(len(seen_session_ids), 4)  # trivial+topical, twice
        self.assertEqual(len(seen_session_ids), len(set(seen_session_ids)),
                          f"a session_id repeated across probe calls: {seen_session_ids}")
        self.assertNotIn("khipu-verify", seen_session_ids)


class AegisIsolationTest(_TempHomeCase):
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

        # _shim() is read-only outside install() now (B1): a real link must
        # exist before recall_hook_cursor() returns something the shell can run.
        (self.home / ".claude").mkdir()
        integ.install("claude_code")
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
        # _shim() is read-only outside install() now (B1): a real link must
        # exist before aegis_capture_hook() returns something the shell can run.
        (self.home / ".grok").mkdir()
        integ.AEGIS_TOML.write_text('model = "grok"\n')
        integ.install("aegis")
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


class ShimReadOnlyTest(_TempHomeCase):
    """B1: _shim() must never create, delete or re-point a launcher symlink
    outside an explicit install() call — status/verify/doctor/every probe are
    read-only. See _installing()'s docstring in khipu/integrations.py."""

    def test_status_does_not_touch_a_link_pointing_elsewhere(self):
        (self.home / ".claude").mkdir()
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        mcp_link = shim_dir / "khipu-mcp"
        stop_link = shim_dir / "khipu-stop-hook"
        # Both point somewhere that is NOT this repo's real bin script — e.g. a
        # worktree removed since install, or a plain stale target (B5-shaped).
        # The targets must actually EXIST (state "elsewhere", not "dangling")
        # for `installed` to stay true here — a dangling launcher a pack's
        # config names IS reported broken; that is ShimDanglingTest's job.
        elsewhere = self.home / "nowhere"
        elsewhere.mkdir(parents=True)
        (elsewhere / "khipu-mcp").write_text("#!/bin/sh\n")
        (elsewhere / "khipu-stop-hook").write_text("#!/bin/sh\n")
        mcp_link.symlink_to(elsewhere / "khipu-mcp")
        stop_link.symlink_to(elsewhere / "khipu-stop-hook")
        (self.home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"khipu": {"command": str(mcp_link)}}}))
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": str(stop_link), "timeout": 20}]}],
            "PreCompact": [{"hooks": [{"type": "command", "command": str(stop_link), "timeout": 20}]}],
        }}))
        st = integ.status("claude_code")
        # The config names the link path, so status reports the pack installed —
        # it must not need the link's TARGET to be correct to see that.
        self.assertTrue(st["mcp"])
        self.assertTrue(st["hook_stop"] and st["hook_precompact"])
        self.assertTrue(st["installed"])
        # And it must not have touched either link while checking.
        self.assertEqual(os.readlink(mcp_link), str(self.home / "nowhere" / "khipu-mcp"))
        self.assertEqual(os.readlink(stop_link), str(self.home / "nowhere" / "khipu-stop-hook"))

    def test_install_repoints_a_link_pointing_elsewhere(self):
        (self.home / ".claude").mkdir()
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        link = shim_dir / "khipu-mcp"
        link.symlink_to(self.home / "nowhere" / "khipu-mcp")
        (self.home / ".claude.json").write_text("{}")
        integ.install("claude_code")
        self.assertEqual(Path(os.readlink(link)), integ._bin_script("khipu-mcp"))

    def test_fresh_machine_status_reports_not_installed_and_creates_nothing(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude.json").write_text("{}")
        shim_dir = self.home / ".config" / "khipu" / "bin"
        st = integ.status("claude_code")
        self.assertFalse(st["installed"])
        self.assertFalse(st["mcp"])
        self.assertFalse(shim_dir.exists())  # status created no link, no directory


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
        # _shim() is read-only outside install() now (B1): a real link must
        # exist before recall_hook() returns something the shell can run.
        (self.home / ".claude").mkdir()
        integ.install("claude_code")
        r = integ._probe_recall(integ.recall_hook())
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["chars"], 200)

    def test_recall_probe_cursor_shape(self):
        (self.home / ".claude").mkdir()
        integ.install("claude_code")
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
        # mcp + Stop + PreCompact + SessionEnd + SubagentStop + SessionStart + UserPromptSubmit
        self.assertEqual(len(out["changes"]), 7)
        t = tomllib.loads((self.home / ".codex" / "config.toml").read_text())
        self.assertEqual(sorted(t["mcp_servers"]), ["khipu", "node_repl"])
        h = json.loads((self.home / ".codex" / "hooks.json").read_text())
        pc = [x["command"] for e in h["hooks"]["PreCompact"] for x in e["hooks"]]
        self.assertIn("python3 '/me/precompact_flush.py'", pc)      # legacy kept
        self.assertTrue(any("khipu-stop-hook" in c for c in pc))
        self.assertTrue(any("khipu-recall-hook" in x["command"] for e in h["hooks"]["SessionStart"] for x in e["hooks"]))
        self.assertTrue(any("khipu-prompt-recall" in x["command"]
                             for e in h["hooks"]["UserPromptSubmit"] for x in e["hooks"]))
        st = integ.status("codex")
        self.assertTrue(st["mcp"] and st["hook_stop"] and st["hook_precompact"])
        self.assertEqual(st["recall_rule"], "installed")
        self.assertEqual(st["prompt_recall"], "installed")
        self.assertEqual(integ.install("codex")["changes"], [])   # idempotent
        integ.uninstall("codex")
        t = tomllib.loads((self.home / ".codex" / "config.toml").read_text())
        self.assertEqual(sorted(t["mcp_servers"]), ["node_repl"])
        h = json.loads((self.home / ".codex" / "hooks.json").read_text())
        self.assertEqual([x["command"] for e in h["hooks"]["PreCompact"] for x in e["hooks"]],
                         ["python3 '/me/precompact_flush.py'"])
        self.assertEqual(h["hooks"]["Stop"], [])
        self.assertEqual(h["hooks"]["UserPromptSubmit"], [])
        self.assertEqual(integ.status("codex")["prompt_recall"], "missing")


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



class LastBeatAtTest(_TempHomeCase):
    """`status()` now carries `last_beat_at` for every harness (docs/plans/
    2026-09-05-setup-that-cannot-strand-you.md, "Harness auto-verify"): the
    Harnesses pane polls this so a card can flip to Verified on its own once
    a real hook dispatch lands after Install, without a manual Verify click."""

    def test_status_reports_the_last_real_capture_over_a_bare_dispatch(self):
        beat = {"last_captured_at": "2026-09-04T00:00:00Z", "at": "2026-09-04T00:05:00Z"}
        with mock.patch("khipu.session_capture._read_beat", return_value=beat):
            st = integ.status("claude_code")
        self.assertEqual(st["last_beat_at"], "2026-09-04T00:00:00Z")

    def test_status_falls_back_to_the_bare_dispatch_when_nothing_was_captured(self):
        with mock.patch("khipu.session_capture._read_beat", return_value={"at": "2026-09-04T00:05:00Z"}):
            st = integ.status("claude_code")
        self.assertEqual(st["last_beat_at"], "2026-09-04T00:05:00Z")

    def test_status_last_beat_at_is_none_when_the_hook_has_never_run(self):
        with mock.patch("khipu.session_capture._read_beat", return_value={}):
            st = integ.status("claude_code")
        self.assertIsNone(st["last_beat_at"])


class StatusInstalledFlagTest(unittest.TestCase):
    def test_installed_is_derived_the_same_way_for_every_surface(self):
        from unittest import mock

        from khipu import integrations as integ

        cases = (
            ("claude_code", {"mcp": True, "hook_stop": True, "hook_precompact": True}, True),
            ("cursor", {"mcp": True, "hook_stop": True, "hook_precompact": False}, False),
            ("grok_bot", {"mcp": True, "hook_stop": False, "hook_precompact": False}, True),
        )
        for harness, raw, want in cases:
            with mock.patch.object(integ, "_guarded", return_value=dict(raw)), \
                    mock.patch.object(integ, "_last_beat_at", return_value=None):
                out = integ.status(harness)
            self.assertEqual(out.get("installed"), want, (harness, out))


class ShimDryRunTest(_TempHomeCase):
    """A dry-run install runs `_shim()` in PLAN mode — it reports the
    link path a real install would write and touches nothing, not even the
    directory."""

    def test_dry_run_creates_nothing_and_reports_the_link_path(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude.json").write_text("{}")
        shim_dir = self.home / ".config" / "khipu" / "bin"
        out = integ.install("claude_code", dry_run=True)
        self.assertFalse(shim_dir.exists(), "a dry run must not create the shim directory")
        change = next(c for c in out["changes"] if "mcpServers.khipu ->" in c)
        self.assertEqual(change.split(" -> ", 1)[1], str(shim_dir / "khipu-mcp"))
        self.assertEqual(integ.CLAUDE_JSON.read_text(), "{}", "a dry run must not write the config")

    def test_dry_run_does_not_repoint_an_existing_elsewhere_link(self):
        (self.home / ".claude").mkdir()
        (self.home / ".claude.json").write_text("{}")
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        link = shim_dir / "khipu-mcp"
        link.symlink_to(self.home / "nowhere" / "khipu-mcp")
        integ.install("claude_code", dry_run=True)
        self.assertEqual(os.readlink(link), str(self.home / "nowhere" / "khipu-mcp"))


class LauncherStatesTest(_TempHomeCase):
    def test_five_names_reported_as_missing_on_a_fresh_machine(self):
        states = {s["name"]: s for s in integ.launcher_states()}
        self.assertEqual(set(states), set(integ.LAUNCHER_NAMES))
        self.assertTrue(all(s["state"] == "missing" for s in states.values()))

    def test_not_a_link_when_a_regular_file_is_in_the_way(self):
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        (shim_dir / "khipu-mcp").write_text("not a symlink")
        s = next(s for s in integ.launcher_states() if s["name"] == "khipu-mcp")
        self.assertEqual(s["state"], "not_a_link")

    def test_dangling_when_the_target_does_not_exist(self):
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        (shim_dir / "khipu-mcp").symlink_to(self.home / "nowhere" / "khipu-mcp")
        s = next(s for s in integ.launcher_states() if s["name"] == "khipu-mcp")
        self.assertEqual(s["state"], "dangling")
        self.assertEqual(s["target"], str(self.home / "nowhere" / "khipu-mcp"))

    def test_current_when_it_points_at_this_codes_own_script(self):
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        (shim_dir / "khipu-mcp").symlink_to(integ._bin_script("khipu-mcp"))
        s = next(s for s in integ.launcher_states() if s["name"] == "khipu-mcp")
        self.assertEqual(s["state"], "current")

    def test_elsewhere_when_it_points_at_a_different_existing_file(self):
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        other = self.home / "nowhere" / "khipu-mcp"
        other.parent.mkdir(parents=True)
        other.write_text("#!/bin/sh\n")
        (shim_dir / "khipu-mcp").symlink_to(other)
        s = next(s for s in integ.launcher_states() if s["name"] == "khipu-mcp")
        self.assertEqual(s["state"], "elsewhere")


class ShimDanglingTest(_TempHomeCase):
    """A pack whose config names a dangling launcher reports
    `installed=False` and a `fix` string, and never repoints it itself —
    status/verify only inspect; repointing stays install()'s job."""

    def test_dangling_launcher_referenced_by_config_fails_installed(self):
        (self.home / ".claude").mkdir()
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        mcp_link = shim_dir / "khipu-mcp"
        mcp_link.symlink_to(self.home / "nowhere" / "khipu-mcp")  # target never created: dangling
        stop_link = shim_dir / "khipu-stop-hook"
        stop_link.symlink_to(integ._bin_script("khipu-stop-hook"))  # this one is healthy
        (self.home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"khipu": {"command": str(mcp_link)}}}))
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": str(stop_link)}]}]}}))
        st = integ.status("claude_code")
        self.assertFalse(st["launcher_ok"])
        self.assertFalse(st["installed"])
        self.assertIn("khipu integrations install claude_code", st["fix"])
        self.assertEqual(os.readlink(mcp_link), str(self.home / "nowhere" / "khipu-mcp"))


class LauncherHealthTest(_TempHomeCase):
    def test_clean_machine_is_ok_and_consistent(self):
        out = integ.launcher_health()
        self.assertTrue(out["ok"])
        self.assertTrue(out["consistent"])
        self.assertEqual(len(out["states"]), 5)

    def test_a_referenced_dangling_launcher_fails_ok(self):
        (self.home / ".claude").mkdir()
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        mcp_link = shim_dir / "khipu-mcp"
        mcp_link.symlink_to(self.home / "nowhere" / "khipu-mcp")
        (self.home / ".claude.json").write_text(json.dumps(
            {"mcpServers": {"khipu": {"command": str(mcp_link)}}}))
        self.assertFalse(integ.launcher_health()["ok"])

    def test_two_launchers_resolving_into_different_roots_is_inconsistent(self):
        shim_dir = self.home / ".config" / "khipu" / "bin"
        shim_dir.mkdir(parents=True)
        root_a, root_b = self.home / "repo-a", self.home / "repo-b"
        for root in (root_a, root_b):
            (root / "packages" / "cli" / "bin").mkdir(parents=True)
        (root_a / "packages" / "cli" / "bin" / "khipu-mcp").write_text("#!/bin/sh\n")
        (root_b / "packages" / "cli" / "bin" / "khipu-stop-hook").write_text("#!/bin/sh\n")
        (shim_dir / "khipu-mcp").symlink_to(root_a / "packages" / "cli" / "bin" / "khipu-mcp")
        (shim_dir / "khipu-stop-hook").symlink_to(root_b / "packages" / "cli" / "bin" / "khipu-stop-hook")
        out = integ.launcher_health()
        self.assertFalse(out["consistent"])
        self.assertTrue(out["ok"])  # inconsistency warns; must not fail `ok`


def _green_doctor_patches():
    """A complete green `cmd_doctor` baseline so a test can layer ONE
    override (`khipu.integrations.launcher_health`) on top — same recipe as
    test_cli_recall_quality.py's own copy, kept local for the same reason."""
    return [
        mock.patch("khipu.drift.status_payload", return_value={"latest_ingested_at": None}),
        mock.patch("khipu.hub_snapshot.maybe_refresh", return_value=None),
        mock.patch("khipu.hub_snapshot.snapshot_freshness", return_value={"ok": True}),
        mock.patch("khipu.hub_snapshot.snapshot_health", return_value={"ok": True}),
        mock.patch("khipu.keychain.secrets_status", return_value={"dsn_file": {"ok": True}}),
        mock.patch("khipu.drift.backup_health", return_value={"ok": True}),
        mock.patch("khipu.graph_backup.local_health", return_value={"ok": True}),
        mock.patch("khipu.graph_backup.offsite_health", return_value={"ok": True}),
        mock.patch("khipu.config.path_setting", return_value=None),
        mock.patch("khipu.outbox.status", return_value={"pending": 0}),
        mock.patch("khipu.outbox.drain", return_value={"failed": 0}),
        mock.patch("khipu.session_capture.queued_jobs", return_value=False),
        mock.patch("khipu.session_capture.drain", return_value=None),
        mock.patch("khipu.session_capture.liveness_all",
                    return_value={"ok": True, "red": [], "harnesses": {}}),
        mock.patch("khipu.git_sync_health.status", return_value={"ok": True}),
        mock.patch("khipu.jobs.job_status", return_value={}),
        mock.patch("khipu.jobs.index_freshness", return_value={"ok": True}),
        mock.patch("khipu.embed.coverage",
                    return_value={"episodes": {"missing": 0}, "topics": {"missing": 0}}),
        mock.patch("khipu.embed.literal_trgm_status", return_value={"ok": True}),
        mock.patch("khipu.hub_snapshot.prompt_recall_snapshot_status",
                    return_value={"ok": True}),
        mock.patch("khipu.probe.status", return_value={"ok": True, "reason": None}),
        mock.patch("khipu.drift.recall_quality", return_value={}),
        mock.patch("khipu.embed.topics_embed_lag_minutes", return_value={"ok": True, "lag_minutes": 0}),
        mock.patch("khipu.query_log.degraded_rate", return_value={"ok": True}),
        mock.patch("khipu.integrations.gateway_liveness_check", return_value={"ok": True}),
        mock.patch("khipu.integrations.aegis_gateway_check", return_value={"ok": True, "applicable": False}),
        mock.patch("khipu.session_capture.unknown_harness_heartbeats", return_value={"warnings": []}),
        mock.patch("khipu.jobs.nightly_step_health", return_value={
            "notes_reconcile_ok": {"ok": True}, "embed_provider_ok": {"ok": True},
            "commitments_hygiene_ok": {"ok": True}, "mark_stale_ok": {"ok": True}}),
    ]


class DoctorFoldsLauncherHealthTest(unittest.TestCase):
    """Doctor's `launchers_ok` gates the top-level `ok`; `consistent` is
    a warning only and must not."""

    def setUp(self):
        for p in _green_doctor_patches():
            p.start()
            self.addCleanup(p.stop)

    def _run_doctor(self):
        import io
        from contextlib import redirect_stdout

        from khipu import cli
        parser = cli.build_parser()
        args = parser.parse_args(["doctor"])
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_doctor(args)
        return rc, json.loads(buf.getvalue())

    def test_broken_launcher_fails_doctor(self):
        bad = {"states": [], "consistent": True, "ok": False}
        with mock.patch("khipu.integrations.launcher_health", return_value=bad):
            rc, out = self._run_doctor()
        self.assertEqual(rc, 2)
        self.assertFalse(out["ok"])
        self.assertFalse(out["launchers_ok"])

    def test_inconsistent_roots_warns_without_failing_doctor(self):
        mixed = {"states": [], "consistent": False, "ok": True, "warning": "mixed roots"}
        with mock.patch("khipu.integrations.launcher_health", return_value=mixed):
            rc, out = self._run_doctor()
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
        self.assertFalse(out["launchers"]["consistent"])

    def test_healthy_launchers_keep_doctor_green(self):
        good = {"states": [], "consistent": True, "ok": True}
        with mock.patch("khipu.integrations.launcher_health", return_value=good):
            rc, out = self._run_doctor()
        self.assertEqual(rc, 0)
        self.assertTrue(out["launchers_ok"])


class _ClaudeHomesCase(_TempHomeCase):
    """The Claude Code pack across several homes (docs/plans/2026-10-07-khipu-t3.md,
    slice A). T3's settings file lives in the temp HOME; the CLAUDE_CONFIG_DIR of
    the surrounding session (a suite launched from a second-account session
    inherits it) is cleared so no real home can ever be reached."""

    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.default = self.home / ".claude"
        self.second = self.home / ".claude-t3-second"

    def _green_probes(self):
        """Verify's live probes (MCP handshake, hooks, a real capture) need a
        hub; what these tests assert is which homes verify looks at."""
        for name in ("_probe_mcp", "_probe_hook", "_probe_native_extract", "_probe_recall",
                     "_probe_prompt_recall", "_probe_aegis_refusal", "_runtime"):
            p = mock.patch.object(integ, name, return_value={"ok": True})
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("khipu.probe.run_probe", return_value={"ok": True})
        p.start()
        self.addCleanup(p.stop)

    def _t3(self, *instances):
        path = self.home / ".t3" / "userdata" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"providerInstances": dict(instances)}))

    def _seed(self, *, link: bool, second_json: dict | None = None):
        """Default home with a legacy hook; a T3 second home whose settings.json
        either links to the default's or is its own file."""
        self.default.mkdir()
        legacy = {"hooks": {"PreCompact": [{"hooks": [{"type": "command",
                  "command": "python3 /me/precompact_flush.py", "timeout": 45}]}]}}
        (self.default / "settings.json").write_text(json.dumps(legacy))
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
        self.second.mkdir()
        if link:
            (self.second / "settings.json").symlink_to(self.default / "settings.json")
        else:
            (self.second / "settings.json").write_text("{}")
        (self.second / ".claude.json").write_text(json.dumps(second_json or {"oauthAccount": {"a": 1}}))
        self._t3(("claudeAgent", {"driver": "claudeAgent", "config": {"homePath": ""}}),
                 ("claudeAgent_secondary", {"driver": "claudeAgent", "displayName": "Secondary",
                                            "config": {"homePath": "~/.claude-t3-second"}}))

    def _shared_stop_hooks(self) -> int:
        s = json.loads((self.default / "settings.json").read_text())
        return sum("khipu-stop-hook" in h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"])

    def _mcp_servers(self, path: Path) -> dict:
        return json.loads(path.read_text())["mcpServers"]

    def _row(self, st: dict, label: str) -> dict:
        return next(r for r in st["homes"] if r["label"] == label)


class WriteJsonTest(_ClaudeHomesCase):
    def test_a_symlinked_file_stays_a_symlink_and_the_target_is_updated(self):
        real = self.home / "dotfiles" / "settings.json"
        real.parent.mkdir()
        real.write_text('{"a": 1}')
        link = self.home / "settings.json"
        link.symlink_to(real)
        integ._write_json(link, {"a": 2})
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.path.realpath(link), os.path.realpath(real))
        self.assertEqual(json.loads(real.read_text()), {"a": 2})
        self.assertEqual([p.name for p in real.parent.iterdir()], ["settings.json"])  # no temp left

    def test_the_existing_mode_survives_and_a_new_claude_json_is_private(self):
        f = self.home / ".claude.json"
        f.write_text("{}")
        os.chmod(f, 0o600)
        integ._write_json(f, {"k": 1})
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)
        fresh = self.home / "new" / ".claude.json"
        integ._write_json(fresh, {"k": 1})
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o600)

    def test_two_writes_never_share_a_temp_path_and_leave_none_behind(self):
        f = self.home / "x.json"
        seen = []
        real = os.replace
        with mock.patch.object(integ.os, "replace", side_effect=lambda a, b: (seen.append(str(a)), real(a, b))):
            integ._write_json(f, {"k": 1})
            integ._write_json(f, {"k": 2})
        self.assertEqual(len(set(seen)), 2)
        self.assertTrue(all(Path(p).parent == Path(os.path.realpath(f.parent)) for p in seen))
        self.assertEqual([p.name for p in f.parent.iterdir()], ["x.json"])

    def test_a_failed_write_removes_its_temp_file_and_keeps_the_target(self):
        f = self.home / "x.json"
        f.write_text('{"k": 0}')
        with mock.patch.object(integ.os, "replace", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                integ._write_json(f, {"k": 1})
        self.assertEqual(json.loads(f.read_text()), {"k": 0})
        self.assertEqual([p.name for p in f.parent.iterdir()], ["x.json"])

    def test_a_plain_file_is_written_as_before(self):
        f = self.home / "sub" / "x.json"
        integ._write_json(f, {"k": 1})
        self.assertEqual(json.loads(f.read_text()), {"k": 1})
        self.assertFalse(f.is_symlink())


class ClaudeHomesInstallTest(_ClaudeHomesCase):
    def test_install_reaches_every_home_found_each_with_its_own_files(self):
        self._seed(link=False)
        out = integ.install("claude_code")
        self.assertEqual([h["label"] for h in out["homes"]], ["Default", "T3 · Secondary"])
        for settings in (self.default / "settings.json", self.second / "settings.json"):
            s = json.loads(settings.read_text())
            self.assertTrue(any("khipu-stop-hook" in h["command"] for e in s["hooks"]["Stop"] for h in e["hooks"]))
            self.assertTrue(any("khipu-prompt-recall" in h["command"]
                                for e in s["hooks"]["UserPromptSubmit"] for h in e["hooks"]))
        self.assertIn("khipu", self._mcp_servers(self.home / ".claude.json"))
        second_json = json.loads((self.second / ".claude.json").read_text())
        self.assertEqual(second_json["mcpServers"]["khipu"]["command"], integ.mcp_launcher())
        self.assertEqual(second_json["oauthAccount"], {"a": 1}, "the account's own keys must survive")
        self.assertIn("other", self._mcp_servers(self.home / ".claude.json"))
        self.assertTrue(integ.status("claude_code")["installed"])

    def test_a_linked_home_keeps_its_link_and_the_hooks_are_not_duplicated(self):
        self._seed(link=True)
        out = integ.install("claude_code")
        self.assertTrue((self.second / "settings.json").is_symlink(), "install must not un-share the setup")
        self.assertEqual(os.path.realpath(self.second / "settings.json"),
                         os.path.realpath(self.default / "settings.json"))
        self.assertEqual(self._shared_stop_hooks(), 1)
        second = out["homes"][1]
        self.assertEqual(second["hooks_shared_with"], "Default")
        self.assertEqual(len(second["changes"]), 1)  # its own mcpServers entry, nothing else
        self.assertIn("mcpServers.khipu", second["changes"][0])
        self.assertEqual(self._mcp_servers(self.second / ".claude.json")["khipu"]["command"], integ.mcp_launcher())
        self.assertEqual(integ.install("claude_code")["changes"], [], "second run is a no-op")
        self.assertEqual(self._shared_stop_hooks(), 1)

    def test_installing_the_linked_home_alone_reuses_the_shared_hooks(self):
        self._seed(link=True)
        integ.install("claude_code", home=str(self.default))
        out = integ.install("claude_code", home=str(self.second))
        self.assertEqual(len(out["changes"]), 1)
        self.assertEqual(self._shared_stop_hooks(), 1)
        self.assertTrue((self.second / "settings.json").is_symlink())

    def test_installing_a_linked_home_first_writes_through_the_link(self):
        self._seed(link=True)
        integ.install("claude_code", home=str(self.second))
        self.assertTrue((self.second / "settings.json").is_symlink())
        self.assertEqual(self._shared_stop_hooks(), 1)
        pc = json.loads((self.default / "settings.json").read_text())["hooks"]["PreCompact"]
        self.assertIn("python3 /me/precompact_flush.py", [h["command"] for e in pc for h in e["hooks"]])

    def test_a_linked_home_whose_owner_does_not_exist_installs_the_hooks_itself(self):
        self.second.mkdir()
        (self.second / "settings.json").symlink_to(self.default / "settings.json")  # dangling: no ~/.claude
        self._t3(("claudeAgent_secondary", {"driver": "claudeAgent", "config": {"homePath": "~/.claude-t3-second"}}))
        out = integ.install("claude_code")
        self.assertNotIn("hooks_shared_with", out["homes"][1])
        self.assertTrue((self.second / "settings.json").is_symlink())
        self.assertEqual(self._shared_stop_hooks(), 1)
        self.assertTrue(integ.status("claude_code")["hook_stop"])

    def test_a_home_that_does_not_exist_yet_is_reported_not_created(self):
        self.default.mkdir()
        self._t3(("claudeAgent_secondary", {"driver": "claudeAgent", "config": {"homePath": "~/.claude-t3-second"}}))
        out = integ.install("claude_code")
        self.assertFalse(self.second.exists())
        self.assertFalse(out["homes"][1]["detected"])
        self.assertEqual(out["homes"][1]["changes"], [])

    def test_claude_config_dir_is_a_home_with_its_own_settings_and_claude_json(self):
        self.default.mkdir()
        other = self.home / "cfg"
        other.mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(other)
        out = integ.install("claude_code")
        self.assertEqual([h["label"] for h in out["homes"]], ["Default", "CLAUDE_CONFIG_DIR"])
        self.assertIn("khipu", self._mcp_servers(other / ".claude.json"))
        self.assertTrue((other / "settings.json").is_file())

    def test_a_home_named_explicitly_as_the_default_folder_gets_both_claude_json_files(self):
        # CLAUDE_CONFIG_DIR=~/.claude makes Claude read ~/.claude/.claude.json,
        # a different file from ~/.claude.json.
        self.default.mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.default)
        integ.install("claude_code")
        self.assertIn("khipu", self._mcp_servers(self.home / ".claude.json"))
        self.assertIn("khipu", self._mcp_servers(self.default / ".claude.json"))
        (self.default / ".claude.json").write_text("{}")
        self.assertFalse(integ.status("claude_code")["mcp"], "both files must carry khipu")

    def test_one_unreadable_config_aborts_that_home_only(self):
        self._seed(link=False)
        garbled = '{"oauthAccount": {"a": '
        (self.second / ".claude.json").write_text(garbled)
        out = integ.install("claude_code")
        self.assertTrue(out["aborted"])
        self.assertIn("refusing to overwrite", out["error"])
        self.assertEqual((self.second / ".claude.json").read_text(), garbled)
        self.assertIn("khipu", self._mcp_servers(self.home / ".claude.json"))
        self.assertTrue(out["homes"][1]["aborted"])
        self.assertNotIn("aborted", out["homes"][0])

    def test_dry_run_changes_nothing_in_any_home(self):
        self._seed(link=True)
        before = {p: p.read_text() for p in (self.default / "settings.json", self.home / ".claude.json",
                                             self.second / ".claude.json")}
        out = integ.install("claude_code", dry_run=True)
        self.assertTrue(out["changes"])
        self.assertEqual({p: p.read_text() for p in before}, before)

    def test_home_must_be_a_home_khipu_found(self):
        self._seed(link=False)
        with self.assertRaises(integ.UnknownClaudeHome):
            integ.install("claude_code", home=str(self.home / "nowhere"))
        with self.assertRaises(ValueError):
            integ.install("cursor", home=str(self.default))
        self.assertNotIn("mcpServers", json.loads((self.second / ".claude.json").read_text()))


class ClaudeHomesUninstallTest(_ClaudeHomesCase):
    def test_uninstalling_a_linked_home_removes_only_its_own_memory_tools_entry(self):
        self._seed(link=True)
        integ.install("claude_code")
        out = integ.uninstall("claude_code", home=str(self.second))
        self.assertEqual(len(out["changes"]), 1)
        self.assertIn("mcpServers.khipu", out["changes"][0])
        self.assertEqual(out["homes"][0]["hooks_kept"], "shared with Default")
        self.assertNotIn("khipu", self._mcp_servers(self.second / ".claude.json"))
        self.assertEqual(self._shared_stop_hooks(), 1, "hooks another home uses must stay")
        self.assertTrue((self.second / "settings.json").is_symlink())
        st = integ.status("claude_code")
        self.assertTrue(self._row(st, "Default")["installed"])
        self.assertFalse(self._row(st, "T3 · Secondary")["memory_tools_ok"])

    def test_the_owner_keeps_shared_hooks_while_a_linked_home_still_has_khipu(self):
        self._seed(link=True)
        integ.install("claude_code")
        first = integ.uninstall("claude_code", home=str(self.default))
        self.assertEqual(first["homes"][0]["hooks_kept"], "still used by T3 · Secondary")
        self.assertEqual(self._shared_stop_hooks(), 1)
        self.assertNotIn("khipu", self._mcp_servers(self.home / ".claude.json"))
        integ.uninstall("claude_code", home=str(self.second))
        last = integ.uninstall("claude_code", home=str(self.default))
        self.assertNotIn("hooks_kept", last["homes"][0])
        self.assertEqual(self._shared_stop_hooks(), 0)

    def test_uninstalling_everywhere_leaves_only_what_was_not_ours(self):
        self._seed(link=True)
        integ.install("claude_code")
        integ.uninstall("claude_code")
        s = json.loads((self.default / "settings.json").read_text())
        self.assertEqual([h["command"] for e in s["hooks"]["PreCompact"] for h in e["hooks"]],
                         ["python3 /me/precompact_flush.py"])
        self.assertEqual(s["hooks"]["Stop"], [])
        self.assertNotIn("khipu", self._mcp_servers(self.home / ".claude.json"))
        self.assertNotIn("khipu", self._mcp_servers(self.second / ".claude.json"))
        self.assertIn("other", self._mcp_servers(self.home / ".claude.json"))
        self.assertTrue((self.second / "settings.json").is_symlink())

    def test_unlinked_homes_uninstall_independently(self):
        self._seed(link=False)
        integ.install("claude_code")
        integ.uninstall("claude_code", home=str(self.second))
        self.assertEqual(json.loads((self.second / "settings.json").read_text())["hooks"]["Stop"], [])
        self.assertEqual(self._shared_stop_hooks(), 1)

    def test_dry_run_uninstall_reports_without_writing(self):
        self._seed(link=True)
        integ.install("claude_code")
        before = (self.default / "settings.json").read_text()
        out = integ.uninstall("claude_code", dry_run=True)
        self.assertTrue(out["changes"])
        self.assertEqual((self.default / "settings.json").read_text(), before)
        self.assertIn("khipu", self._mcp_servers(self.second / ".claude.json"))


class ClaudeHomesStatusTest(_ClaudeHomesCase):
    ROW_KEYS = {"path", "label", "source", "is_default", "exists", "settings_path", "mcp_paths", "linked_to",
                "hook_stop", "hook_precompact", "hook_sessionend", "hook_subagentstop", "recall_rule",
                "prompt_recall", "memory_tools_ok", "hooks_ok", "launcher_ok", "installed", "has_khipu"}

    def test_each_home_is_a_row_with_the_documented_keys(self):
        self._seed(link=True)
        integ.install("claude_code")
        st = integ.status("claude_code")
        json.dumps(st)  # the CLI prints it
        self.assertEqual([r["label"] for r in st["homes"]], ["Default", "T3 · Secondary"])
        for r in st["homes"]:
            self.assertEqual(set(r) - {"error", "aborted"}, self.ROW_KEYS)
        default, second = st["homes"]
        self.assertEqual(default["path"], str(self.default))
        self.assertEqual(default["source"], "Claude Code, and T3's “Claude” account")
        self.assertEqual(second["source"], "T3's “Secondary” account")
        self.assertEqual(second["mcp_paths"], [str(self.second / ".claude.json")])
        self.assertIsNone(default["linked_to"])
        self.assertEqual(second["linked_to"], {"label": "Default", "path": str(self.default)})
        for r in (default, second):
            self.assertTrue(r["hooks_ok"] and r["memory_tools_ok"] and r["installed"], r)

    def test_the_pack_is_not_installed_while_any_home_is_not(self):
        self._seed(link=False)
        integ.install("claude_code", home=str(self.default))
        st = integ.status("claude_code")
        self.assertTrue(self._row(st, "Default")["installed"])
        second = self._row(st, "T3 · Secondary")
        self.assertFalse(second["installed"])
        self.assertFalse(second["memory_tools_ok"] or second["hooks_ok"])
        self.assertTrue(st["detected"])
        self.assertFalse(st["installed"])
        self.assertFalse(st["mcp"] or st["hook_stop"])
        self.assertEqual(st["recall_rule"], "missing")

    def test_a_linked_home_reads_the_hooks_through_the_link(self):
        self._seed(link=True)
        integ.install("claude_code", home=str(self.default))
        second = self._row(integ.status("claude_code"), "T3 · Secondary")
        self.assertTrue(second["hooks_ok"], "its hooks are the Default home's")
        self.assertFalse(second["memory_tools_ok"])
        self.assertFalse(second["installed"])

    def test_a_home_that_does_not_exist_does_not_hold_the_pack_back(self):
        self.default.mkdir()
        (self.home / ".claude.json").write_text("{}")
        self._t3(("claudeAgent_secondary", {"driver": "claudeAgent", "config": {"homePath": "~/.claude-t3-second"}}))
        integ.install("claude_code")
        st = integ.status("claude_code")
        self.assertTrue(st["installed"])
        self.assertFalse(self._row(st, "T3 · claudeAgent_secondary")["exists"])

    def test_an_unreadable_file_is_that_homes_error_not_a_traceback(self):
        self._seed(link=False)
        integ.install("claude_code")
        (self.second / "settings.json").write_text("{broken")
        st = integ.status("claude_code")
        self.assertTrue(st["aborted"])
        self.assertIn("refusing", st["error"])
        self.assertIn("refusing", self._row(st, "T3 · Secondary")["error"])
        self.assertTrue(self._row(st, "Default")["installed"])
        self.assertFalse(st["installed"])

    def test_verify_scopes_to_the_homes_that_have_khipu_and_lists_the_rest(self):
        self._green_probes()
        self._seed(link=False)
        integ.install("claude_code", home=str(self.default))
        out = integ.verify("claude_code")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["not_installed_homes"], ["T3 · Secondary"])
        self.assertTrue(out["components"]["mcp"]["ok"])

    def _drop_mcp(self, path: Path):
        d = json.loads(path.read_text())
        d["mcpServers"].pop("khipu")
        path.write_text(json.dumps(d))

    def test_verify_fails_a_home_with_hooks_but_no_memory_tools_and_names_it(self):
        self._green_probes()
        self._seed(link=False)
        integ.install("claude_code")
        self._drop_mcp(self.home / ".claude.json")
        out = integ.verify("claude_code")
        self.assertFalse(out["ok"])
        self.assertEqual(out["not_installed_homes"], [])
        self.assertEqual(out["components"]["mcp"]["error"], "not installed in Default")
        self.assertEqual(out["components"]["install"], {"ok": False, "error": "Default is missing memory tools"})

    def test_verify_fails_a_home_with_memory_tools_but_no_hooks_and_names_it(self):
        self._green_probes()
        self._seed(link=False)
        integ.install("claude_code")
        (self.second / "settings.json").write_text("{}")
        out = integ.verify("claude_code")
        self.assertFalse(out["ok"])
        self.assertEqual(out["not_installed_homes"], [])
        self.assertEqual(out["components"]["hook"]["error"], "not installed in T3 · Secondary")
        self.assertEqual(out["components"]["install"]["error"],
                         "T3 · Secondary is missing Stop hook, PreCompact hook")

    def test_verify_fails_a_home_with_a_stale_khipu_command(self):
        self._green_probes()
        self._seed(link=False)
        integ.install("claude_code", home=str(self.default))
        (self.second / ".claude.json").write_text(
            json.dumps({"mcpServers": {"khipu": {"command": "/old/place/khipu-mcp"}}}))
        out = integ.verify("claude_code")
        self.assertFalse(out["ok"])
        self.assertEqual(out["not_installed_homes"], [])
        self.assertEqual(out["components"]["install"]["error"],
                         "T3 · Secondary is missing memory tools, Stop hook, PreCompact hook")

    def test_a_linked_home_with_only_the_owners_hooks_is_still_left_out(self):
        self._green_probes()
        self._seed(link=True)
        integ.install("claude_code", home=str(self.default))
        self._drop_mcp(self.home / ".claude.json")
        integ.install("claude_code", home=str(self.second))
        out = integ.verify("claude_code")
        self.assertEqual(out["not_installed_homes"], [])
        rows = {r["label"]: r for r in integ.status("claude_code")["homes"]}
        self.assertTrue(rows["Default"]["has_khipu"])
        self.assertTrue(rows["T3 · Secondary"]["has_khipu"])

    def test_has_khipu_is_false_only_for_a_home_with_no_khipu_entry_at_all(self):
        self._seed(link=True)
        rows = {r["label"]: r for r in integ.status("claude_code")["homes"]}
        self.assertFalse(rows["Default"]["has_khipu"])
        self.assertFalse(rows["T3 · Secondary"]["has_khipu"])
        integ.install("claude_code", home=str(self.default))
        rows = {r["label"]: r for r in integ.status("claude_code")["homes"]}
        self.assertTrue(rows["Default"]["has_khipu"])
        self.assertFalse(rows["T3 · Secondary"]["has_khipu"], "its hooks are the owner's")

    def test_verify_of_one_home_requires_that_home(self):
        self._green_probes()
        self._seed(link=False)
        integ.install("claude_code", home=str(self.default))
        self.assertTrue(integ.verify("claude_code", home=str(self.default))["ok"])
        out = integ.verify("claude_code", home=str(self.second))
        self.assertFalse(out["ok"])
        self.assertEqual(out["components"]["mcp"]["error"], "not installed in T3 · Secondary")
        with self.assertRaises(integ.UnknownClaudeHome):
            integ.verify("claude_code", home=str(self.home / "nowhere"))
        with self.assertRaises(ValueError):
            integ.verify("cursor", home=str(self.default))

    def test_verify_with_no_home_installed_fails_naming_them(self):
        self._green_probes()
        self._seed(link=False)
        out = integ.verify("claude_code")
        self.assertFalse(out["ok"])
        self.assertEqual(out["components"]["mcp"]["error"], "not installed in Default, T3 · Secondary")

    def test_verify_fails_for_a_home_whose_config_cannot_be_read(self):
        self._green_probes()
        self._seed(link=False)
        integ.install("claude_code")
        (self.second / "settings.json").write_text("{broken")
        self.assertFalse(integ.verify("claude_code")["ok"])

    def test_doctor_row_per_home(self):
        self._seed(link=True)
        integ.install("claude_code", home=str(self.default))
        rep = integ.claude_homes_report()
        self.assertEqual([r["label"] for r in rep["homes"]], ["Claude Code", "Claude Code (T3 · Secondary)"])
        self.assertEqual(rep["found"], 2)
        self.assertFalse(rep["all_installed"])
        self.assertEqual(rep["homes"][1]["linked_to"], {"label": "Default", "path": str(self.default)})
        integ.install("claude_code")
        self.assertTrue(integ.claude_homes_report()["all_installed"])

    def test_doctor_all_installed_is_false_when_no_home_exists(self):
        rep = integ.claude_homes_report()
        self.assertEqual(rep["found"], 0)
        self.assertFalse(rep["all_installed"])


class ClaudeHomesCliTest(_ClaudeHomesCase):
    def _run(self, *argv):
        import io
        from contextlib import redirect_stdout

        from khipu import cli
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_integrations(cli.build_parser().parse_args(["integrations", *argv]))
        return rc, buf.getvalue()

    def test_install_and_uninstall_take_one_home(self):
        self._seed(link=True)
        rc, raw = self._run("install", "claude_code", "--home", str(self.second), "--no-verify")
        self.assertEqual(rc, 0)
        out = json.loads(raw)[0]
        self.assertEqual([h["label"] for h in out["homes"]], ["T3 · Secondary"])
        self.assertNotIn("khipu", json.loads((self.home / ".claude.json").read_text())["mcpServers"])
        rc, raw = self._run("uninstall", "claude_code", "--home", str(self.second))
        self.assertEqual(rc, 0)
        self.assertNotIn("khipu", self._mcp_servers(self.second / ".claude.json"))

    def test_install_of_the_installed_home_verifies_only_it_and_verify_takes_one_home(self):
        self._green_probes()
        self._seed(link=False)
        rc, raw = self._run("install", "claude_code", "--home", str(self.default))
        self.assertEqual(rc, 0, raw)
        rc, raw = self._run("verify", "claude_code")
        self.assertEqual(rc, 0, raw)
        self.assertEqual(json.loads(raw)[0]["not_installed_homes"], ["T3 · Secondary"])
        rc, raw = self._run("verify", "claude_code", "--home", str(self.second))
        self.assertEqual(rc, 2)
        rc, raw = self._run("verify", "claude_code", "--home", str(self.home / "nowhere"))
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(raw)["ok"])
        rc, raw = self._run("verify", "all", "--home", str(self.default))
        self.assertEqual(rc, 2)

    def test_status_lists_the_homes(self):
        self._seed(link=True)
        rc, raw = self._run("status", "claude_code")
        self.assertEqual(rc, 0)
        self.assertEqual([r["label"] for r in json.loads(raw)[0]["homes"]], ["Default", "T3 · Secondary"])

    def test_an_unknown_home_or_a_non_claude_harness_is_refused_with_exit_2(self):
        self._seed(link=True)
        rc, raw = self._run("install", "claude_code", "--home", str(self.home / "nowhere"))
        self.assertEqual(rc, 2)
        self.assertFalse(json.loads(raw)["ok"])
        rc, raw = self._run("install", "all", "--home", str(self.default))
        self.assertEqual(rc, 2)
        self.assertIn("claude_code", json.loads(raw)["error"])
        self.assertNotIn("mcpServers", json.loads((self.second / ".claude.json").read_text()))


class DoctorListsClaudeHomesTest(_ClaudeHomesCase):
    def setUp(self):
        super().setUp()
        for p in _green_doctor_patches():
            p.start()
            self.addCleanup(p.stop)

    def test_doctor_has_one_row_per_home_and_an_uninstalled_home_does_not_fail_it(self):
        import io
        from contextlib import redirect_stdout

        from khipu import cli
        self._seed(link=True)
        integ.install("claude_code", home=str(self.default))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.cmd_doctor(cli.build_parser().parse_args(["doctor"]))
        out = json.loads(buf.getvalue())
        self.assertEqual([r["label"] for r in out["claude_homes"]["homes"]],
                         ["Claude Code", "Claude Code (T3 · Secondary)"])
        self.assertFalse(out["claude_homes"]["all_installed"])
        self.assertEqual(rc, 0)
        self.assertTrue(out["ok"])
