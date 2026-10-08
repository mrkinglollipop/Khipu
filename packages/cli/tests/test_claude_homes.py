# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu.t3 and khipu.claude_homes — slice A of docs/plans/2026-10-07-khipu-t3.md.

Discovery of Claude config homes (``~/.claude``, ``CLAUDE_CONFIG_DIR``, T3's
Claude instances), read-only and quiet on anything unexpected; T3's hand-over
stripping and helper-session marker. Every test uses a temp home and an
explicit ``environ`` so the real CLAUDE_CONFIG_DIR and ~/.t3 are never read.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from khipu import claude_homes, t3
from tests.fixtures.t3 import THREAD, handoff_wrapper


def _t3_settings(home: Path, instances) -> Path:
    path = home / ".t3" / "userdata" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"providers": {}, "providerInstances": instances}))
    return path


def _claude(home_path: str | None, display: str | None = None, enabled: bool = True) -> dict:
    inst = {"driver": "claudeAgent", "enabled": enabled, "config": {"binaryPath": "", "homePath": home_path or ""}}
    if display:
        inst["displayName"] = display
    return inst


class _TempHome(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="khipu-homes-")).resolve()

    def discover(self, **kw):
        kw.setdefault("environ", {})
        return claude_homes.discover(home=self.home, **kw)


class DiscoveryTest(_TempHome):
    def test_default_home_is_always_there_even_with_nothing_on_disk(self):
        homes = self.discover()
        self.assertEqual([h.path for h in homes], [self.home / ".claude"])
        self.assertTrue(homes[0].is_default)
        self.assertFalse(homes[0].exists)
        self.assertEqual(homes[0].claude_jsons, [self.home / ".claude.json"])

    def test_missing_t3_settings_is_quiet(self):
        self.assertEqual(len(self.discover()), 1)

    def test_garbled_or_reshaped_t3_settings_is_quiet(self):
        path = self.home / ".t3" / "userdata" / "settings.json"
        path.parent.mkdir(parents=True)
        for content in ("{not json", "[]", '"text"', "{}", '{"providerInstances": []}',
                        '{"providerInstances": {"a": "x", "b": {"driver": "claudeAgent", "config": 5}}}'):
            with self.subTest(content=content):
                path.write_text(content)
                homes = self.discover()
                self.assertEqual([h.path for h in homes], [self.home / ".claude"])

    def test_claude_config_dir_is_a_home_with_its_own_claude_json(self):
        other = self.home / "elsewhere"
        homes = self.discover(environ={"CLAUDE_CONFIG_DIR": str(other)})
        self.assertEqual([h.label for h in homes], ["Default", "CLAUDE_CONFIG_DIR"])
        self.assertEqual(homes[1].claude_jsons, [other / ".claude.json"])
        self.assertEqual(homes[1].sources, ["CLAUDE_CONFIG_DIR"])

    def test_t3_instances_become_homes_and_a_tilde_means_the_home_dir(self):
        _t3_settings(self.home, {
            "claudeAgent": _claude(None),
            "claudeAgent_secondary": _claude("~/.claude-t3-second", "Secondary"),
        })
        homes = self.discover()
        self.assertEqual([h.label for h in homes], ["Default", "T3 · Secondary"])
        second = homes[1]
        self.assertEqual(second.path, self.home / ".claude-t3-second")
        self.assertEqual(second.claude_jsons, [self.home / ".claude-t3-second" / ".claude.json"])
        # T3 runs the empty-homePath instance from the default home: it joins that row.
        self.assertEqual(claude_homes.describe_sources(homes[0].sources),
                         "Claude Code, and T3's “Claude” account")
        self.assertEqual(claude_homes.describe_sources(second.sources), "T3's “Secondary” account")

    def test_an_instance_without_a_display_name_is_named_by_its_id(self):
        _t3_settings(self.home, {"claudeAgent_work": _claude(str(self.home / "w"))})
        self.assertEqual(self.discover()[1].label, "T3 · claudeAgent_work")

    def test_a_disabled_instance_is_still_found_and_says_so(self):
        _t3_settings(self.home, {"claudeAgent_x": _claude(str(self.home / "x"), "X", enabled=False)})
        self.assertIn("(disabled in T3)", self.discover()[1].sources[0])

    def test_other_drivers_and_relative_paths_are_ignored(self):
        _t3_settings(self.home, {
            "codex": {"driver": "codex", "config": {"homePath": str(self.home / "codex")}},
            "claudeAgent_rel": _claude("relative/dir"),
        })
        self.assertEqual([h.path for h in self.discover()], [self.home / ".claude"])

    def test_two_spellings_of_one_real_folder_are_one_home(self):
        real = self.home / "real-home"
        real.mkdir()
        (self.home / "alias").symlink_to(real)
        _t3_settings(self.home, {"a": _claude(str(real), "Real"), "b": _claude(str(self.home / "alias"), "Alias")})
        homes = self.discover(environ={"CLAUDE_CONFIG_DIR": str(real)})
        self.assertEqual(len(homes), 2)  # Default + one
        self.assertEqual(homes[1].label, "T3 · Real")  # first label found wins
        self.assertEqual(len(homes[1].sources), 3)

    def test_case_aliases_are_one_home_when_the_volume_supports_them(self):
        real = self.home / "ClaudeHome"
        real.mkdir()
        alias = self.home / "claudehome"
        if not alias.exists():
            self.skipTest("case-sensitive filesystem")
        homes = self.discover(default_dir=real, environ={"CLAUDE_CONFIG_DIR": str(alias)})
        self.assertEqual(len(homes), 1)
        self.assertIn("CLAUDE_CONFIG_DIR", homes[0].sources)

    def test_the_default_folder_named_explicitly_is_still_one_home_with_two_claude_jsons(self):
        # A session with CLAUDE_CONFIG_DIR=~/.claude reads ~/.claude/.claude.json,
        # not ~/.claude.json, so the one home carries both.
        default = self.home / ".claude"
        homes = self.discover(environ={"CLAUDE_CONFIG_DIR": str(default)})
        self.assertEqual(len(homes), 1)
        self.assertEqual(homes[0].claude_jsons, [self.home / ".claude.json", default / ".claude.json"])

    def test_discovery_never_writes(self):
        _t3_settings(self.home, {"a": _claude(str(self.home / "w"), "W")})
        before = sorted(p for p in self.home.rglob("*"))
        self.discover(environ={"CLAUDE_CONFIG_DIR": str(self.home / "e")})
        self.assertEqual(sorted(p for p in self.home.rglob("*")), before)


class SettingsOwnersTest(_TempHome):
    def _two_homes(self, link: bool):
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text("{}")
        second = self.home / "second"
        second.mkdir()
        if link:
            (second / "settings.json").symlink_to(self.home / ".claude" / "settings.json")
        else:
            (second / "settings.json").write_text("{}")
        return self.discover(environ={"CLAUDE_CONFIG_DIR": str(second)})

    def test_a_home_whose_settings_link_to_another_is_linked_to_it(self):
        default, second = self._two_homes(link=True)
        self.assertIsNone(default.linked_to)
        self.assertIs(second.linked_to, default)

    def test_separate_settings_files_are_not_linked(self):
        default, second = self._two_homes(link=False)
        self.assertIsNone(default.linked_to)
        self.assertIsNone(second.linked_to)

    def test_two_homes_linked_to_one_outside_file_share_it_and_the_first_owns(self):
        shared = self.home / "dotfiles" / "settings.json"
        shared.parent.mkdir()
        shared.write_text("{}")
        for name in (".claude", "second"):
            (self.home / name).mkdir()
            (self.home / name / "settings.json").symlink_to(shared)
        default, second = self.discover(environ={"CLAUDE_CONFIG_DIR": str(self.home / "second")})
        self.assertIsNone(default.linked_to)
        self.assertIs(second.linked_to, default)

    def test_owner_beats_a_linked_member_even_when_the_link_is_first(self):
        default, linked = self._two_homes(link=True)
        claude_homes.settings_owners([linked, default])
        self.assertIsNone(default.linked_to)
        self.assertIs(linked.linked_to, default)

    def test_case_alias_of_shared_settings_has_the_same_owner(self):
        default, second = self._two_homes(link=False)
        alias = self.home / ".CLAUDE"
        if not alias.exists():
            self.skipTest("case-sensitive filesystem")
        second.settings_path.unlink()
        second.settings_path.symlink_to(alias / "settings.json")
        claude_homes.settings_owners([second, default])
        self.assertIsNone(default.linked_to)
        self.assertIs(second.linked_to, default)


class TranscriptHomeTest(_TempHome):
    def test_a_transcript_under_a_homes_projects_folder_belongs_to_it(self):
        _t3_settings(self.home, {"s": _claude("~/.claude-t3-second", "Secondary")})
        homes = self.discover()
        tp = self.home / ".claude-t3-second" / "projects" / "-tmp-x" / "s.jsonl"
        self.assertEqual(claude_homes.transcript_home(tp, homes).label, "T3 · Secondary")
        self.assertIsNone(claude_homes.transcript_home(self.home / "other" / "projects" / "x.jsonl", homes))
        self.assertIsNone(claude_homes.transcript_home("", homes))

    def test_a_folder_that_merely_starts_with_a_homes_name_does_not_count(self):
        homes = self.discover()
        tp = self.home / ".claude-extra" / "projects" / "x" / "s.jsonl"
        self.assertIsNone(claude_homes.transcript_home(tp, homes))

    def test_a_symlinked_home_directory_attributes_its_real_transcript(self):
        real = self.home / "real-home"
        (real / "projects" / "p").mkdir(parents=True)
        alias = self.home / "alias-home"
        alias.symlink_to(real)
        homes = self.discover(environ={"CLAUDE_CONFIG_DIR": str(alias)})
        transcript = real / "projects" / "p" / "session.jsonl"
        self.assertEqual(claude_homes.transcript_home(transcript, homes).label, "CLAUDE_CONFIG_DIR")

    def test_case_alias_attributes_a_transcript_when_supported(self):
        root = self.home / ".Claude"
        (root / "projects" / "p").mkdir(parents=True)
        alias = self.home / ".claude"
        if not alias.exists():
            self.skipTest("case-sensitive filesystem")
        homes = self.discover(default_dir=alias)
        self.assertEqual(claude_homes.transcript_home(root / "projects" / "p" / "s.jsonl", homes).label,
                         "Default")


class T3FactsTest(unittest.TestCase):
    def test_handoff_is_stripped_to_what_the_user_typed(self):
        wrapped = handoff_wrapper("so does the recall block show up now?")
        self.assertTrue(wrapped.startswith("Context handoff (full_thread_summary):"))
        self.assertIn("Source item range:", wrapped)
        self.assertIn("Recover omitted history using t3_thread_read(", wrapped)
        self.assertIn("Selected 5 intact items; omitted 57 items.", wrapped)
        self.assertEqual(t3.strip_handoff(wrapped), "so does the recall block show up now?")

    def test_the_first_marker_after_the_last_historical_item_wins(self):
        typed = "quoting a hand-over: \n\nUser message:\nnot me"
        # A marker inside an earlier historical item must not cut the real text short.
        wrapped = handoff_wrapper("typed once").replace(
            "Linked settings.json", "Linked\n\nUser message:\nsettings.json", 1)
        self.assertEqual(t3.strip_handoff(wrapped), "typed once")
        self.assertEqual(t3.strip_handoff(handoff_wrapper(typed)), typed)

    def test_an_empty_typed_message_leaves_nothing(self):
        self.assertEqual(t3.strip_handoff(handoff_wrapper("")), "")

    def test_a_trimmed_empty_handoff_still_means_no_user_text(self):
        self.assertEqual(t3.strip_handoff(handoff_wrapper("").rstrip()), "")

    def test_no_items_still_strips_after_the_header(self):
        wrapped = (f"Context handoff (full_thread_summary):\n"
                   f"Provider context handoff. Thread: {THREAD}.\n\nUser message:\nhello")
        self.assertEqual(t3.strip_handoff(wrapped), "hello")

    def test_a_nonheader_prefix_is_preserved(self):
        prefixed = "\n\n" + handoff_wrapper("hello")
        self.assertEqual(t3.strip_handoff(prefixed), prefixed)

    def test_ordinary_text_and_unmarked_handoffs_come_back_unchanged(self):
        for text in ("fix the bug", "", "Context handoff mentioned mid-sentence",
                     "Context handoff (x): no marker here"):
            with self.subTest(text=text):
                self.assertEqual(t3.strip_handoff(text), text)
        # a user message marker without the prefix is the user's own text
        plain = "see:\n\nUser message:\nthis"
        self.assertEqual(t3.strip_handoff(plain), plain)

    def test_helper_session_marker(self):
        self.assertTrue(t3.is_helper_session("/var/folders/ab/cd/T/t3code-claude-title-x1y2z3"))
        self.assertTrue(t3.is_helper_session("/private/var/folders/q1/zk48y_7n6b75mtc9vb8fv2p40000gn/T/t3code-claude-title-6ihnNA"))
        self.assertTrue(t3.is_helper_session(str(Path(tempfile.gettempdir()).resolve() / "t3code-claude-title-x")))
        self.assertTrue(t3.is_helper_session("/tmp/t3code-claude-title-x"))
        self.assertTrue(t3.is_helper_session("/private/tmp/t3code-claude-title-x"))
        self.assertFalse(t3.is_helper_session("/work/t3code-claude-title-parser/src"))
        self.assertFalse(t3.is_helper_session("/work/t3code-claude-title-parser"))
        self.assertFalse(t3.is_helper_session("/Users/me/code/project"))
        self.assertFalse(t3.is_helper_session(None))
        self.assertFalse(t3.is_helper_session(""))

    def test_thread_id_in_the_fixture_is_the_one_the_plan_reads_from_the_header(self):
        self.assertIn(f"Thread: {THREAD}.", handoff_wrapper("x"))
