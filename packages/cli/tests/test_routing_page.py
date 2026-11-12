"""The routing page (routing slice 3): ``routing.page(view, now, conversation)``
and ``khipu route --page``. Pure: the tests hand it views and a clock, and it
may read nothing. Scope: jobs/2026-11-07/scope-routing-one-page.md section 2."""
from __future__ import annotations

import copy
import re
from datetime import datetime, timedelta, timezone

import pytest

from khipu import routing, routing_page

pytest_plugins = ("tests.test_routing_acceptance",)

NOW = datetime(2026, 11, 7, 20, 10, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pace(window, used, resets, *, allowance=None, share=None, share_verdict="underpacing", slack_target=None):
    pace = {"window": window, "used_pct": used, "cycle_resets_at": _iso(resets), "plan": {"verdict": "underpacing"},
            "pace_target": None, "share_cap": None}
    if allowance is not None:
        pace["pace_target"] = {"allowance_pct": allowance, "used_pct": used, "pct": 100.0, "slack": 0.0,
                               "verdict": "ahead" if used > allowance else "within"}
    if share is not None:
        pace["share_cap"] = {"pct": share, "verdict": share_verdict}
    return pace


def _entry(key, label, harness, *, state="open", caps=(), pace=(), terms=None, **more):
    entry = {"key": key, "label": label, "harness": harness, "state": state, "configured": True, "caps": list(caps),
             "pace": list(pace), "models": [], "until": None, "witness": {"source": "probe"}}
    if terms:
        entry["terms"] = terms
    entry.update(more)
    return entry


def _role(item_key, label, text, **extra):
    return {"account": item_key, "label": label, "role": text, "until": None, "in_settings": True, **extra}


def _view(*, hold=True, rules=None, roles=None):
    """A realistic view: all eight accounts, in an owner's order, with every cap kind, a hold, and the plan terms."""
    wk = NOW + timedelta(days=2, hours=10)
    accounts = [
        _entry("codex|", "Codex", "codex",
               caps=[{"kind": "pace_target", "pct": 100.0, "slack": 0.0, "window": "weekly", "revision": "cap-01a15fe6-6db7"},
                     {"kind": "pace_target", "pct": 100.0, "slack": 5.0, "window": "weekly", "purpose": "check"},
                     {"kind": "share", "pct": 95.0, "window": "weekly"}],
               pace=[_pace("weekly", 83.0, wk, allowance=51.0, share=95.0)],
               terms={"plan_change": {"to": "Pro 100", "on": "2026-11-08"}, "resets_held": 2}),
        _entry("grok|", "Grok", "grok",
               caps=[{"kind": "pace_target", "pct": 100.0, "slack": 0.0, "window": "weekly"},
                     {"kind": "share", "pct": 76.0, "purpose": "job", "window": "weekly"}],
               pace=[_pace("weekly", 61.0, NOW + timedelta(days=3), allowance=70.0, share=76.0)],
               terms={"ceiling_with_ok": 90, "plan_change": {"to": "a smaller plan"}}),
        _entry("claude|", "Claude", "claude",
               caps=[{"kind": "pace_target", "pct": 100.0, "slack": 5.0, "window": "weekly"},
                     {"kind": "pace_target", "pct": 100.0, "slack": 20.0, "window": "weekly", "purpose": "check",
                      "sensitive_only": True},
                     {"kind": "share", "pct": 91.0, "window": "weekly"}],
               pace=[_pace("weekly", 68.0, NOW + timedelta(days=1, hours=18), allowance=71.0, share=91.0)],
               terms={"resets_held": 1}),
        _entry("claude:second", "Claude second", "claude",
               pace=[_pace("weekly", 1.0, NOW + timedelta(days=5))], terms={"resets_held": 1}),
        _entry("muse|", "Muse", "muse", pace=[_pace("weekly", 33.0, NOW + timedelta(days=1, hours=4))],
               terms={"ends": "2026-11-13"}),
        _entry("kimi:main", "Kimi", "kimi",
               caps=[{"kind": "concurrency", "n": 3, "window": None}],
               pace=[_pace("monthly", 96.3, datetime(2026, 11, 28, tzinfo=timezone.utc))],
               terms={"ends": "2026-11-27"}),
        _entry("glm:main", "GLM", "glm", caps=[{"kind": "concurrency", "n": 1}],
               pace=[_pace("weekly", 96.0, NOW + timedelta(days=3, hours=4))], terms={"ends": "2026-11-20"}),
        _entry("deepseek:main", "DeepSeek", "deepseek", caps=[{"kind": "spend", "usd": 35.0, "days": 7.0}],
               pace=[]),
    ]
    texts = roles or {
        "codex|": "Small share, even pace, no burn mode.",
        "grok|": "Even pace, no catch-up bursts.",
        "claude|": "Orchestrator first; jobs pause above even pace +5 unless the work really needs Claude.",
        "claude:second": "Never Fable unless named.",
        "muse|": "Blind second opinions on the riskiest work.",
        "kimi:main": "Limit is 5 h: about 3-4 tasks, then 4 h off.",
        "glm:main": "One at a time.",
        "deepseek:main": "Paid per use.",
    }
    entries = [_role(a["key"], a["label"], texts[a["key"]]) for a in accounts]
    holds = []
    if hold:
        holds.append({"id": "hold-5c1f0a9e77b24d03", "entity": "hold:hold-5c1f0a9e77b24d03",
                      "revision": "01a17083-c656-74ec-adff-e53835e2d363",
                      "scope": {"kind": "account", "account": "grok|"}, "scope_label": "Grok", "reason": "keep",
                      "text": "We are overpaced on grok/cursor plan.", "until": _iso(NOW + timedelta(days=1, hours=3)),
                      "owner": True, "written_by": {"kind": "owner"}, "via": "app", "at": _iso(NOW), "remind": False})
    rules = rules if rules is not None else [
        "WHICH MODEL: Opus: scopes, designs, mocks, rulings, the hardest security and privacy code, and any fix "
        "Sonnet already failed. Unsure: use Opus and say why. Sonnet: most coding from a brief, fix rounds, tests, "
        "docs. Haiku: searches, counts, rebases, cleanup. Helpers never inherit Opus.",
        "WHICH MODEL: Codex Sol: cross-family checks, narrow re-checks, a few builds. Cursor Auto: builds and fixes. "
        "Grok 4.7: audits and second opinions, not privacy or money.",
        "NEVER: Grok, GLM, DeepSeek or Kimi on privacy, safety, sign-in, permission or money code. Any -fast tier.",
        "NEVER: A key's value to any model. Portfolio data to anyone but the model doing finance work.",
        "CHECKS: Cursor Auto may build sensitive code; Claude or Codex checks each result. A lane never confirms "
        "its own verdict. The final check before merge is cross-family.",
        "MACS: Before a job, compare load per core and start on the lower. Both above about 2x cores: queue it.",
    ]
    return {"record": True, "version": 41, "epoch": "e-7f3a", "accounts": accounts, "holds": holds,
            "rules": [{"n": i, "text": t, "pending": False, "confirmed": True, "entity": f"rule-{i:08x}1d4c"}
                      for i, t in enumerate(rules, 1)],
            "priority": {"entries": entries, "until": None, "pending": False, "confirmed": True},
            "definitions": {"providers": {
                "muse": {"id": "muse", "use": "second_opinion"},
                "kimi": {"id": "kimi", "models_allowed": ["k3.1-pro"]},
                "glm": {"id": "glm", "models_allowed": ["glm-5.3", "glm-5.3-flash"]},
                "deepseek": {"id": "deepseek", "models_allowed": ["deepseek-flash"]}}}}


def _line(text: str, label: str) -> str:
    """The one account line that starts with this label (numbered or listed)."""
    found = [ln for ln in text.splitlines() if re.match(rf"^(?:\d+\. |- ){re.escape(label)}: ", ln)]
    assert len(found) == 1, (label, text)
    return found[0]


def _page(view=None, **kw):
    return routing_page.page(view or _view(), NOW, **kw)


# ---- the limit ------------------------------------------------------------------------------


def test_the_realistic_page_is_under_the_limit_with_all_eight_accounts():
    text = _page()
    assert 1500 < len(text) <= routing_page.LIMIT, len(text)
    for label in ("Codex", "Grok", "Claude", "Claude second", "Muse", "Kimi", "GLM", "DeepSeek"):
        _line(text, label)
    assert routing.page(_view(), NOW) == text  # the same function through routing.page


def test_a_record_with_todays_long_roles_and_rules_still_fits_and_keeps_every_account_fact():
    long_role = "Small share, even pace; keep it for jobs where a different model family matters. " * 14
    roles = {k: long_role for k in ("codex|", "grok|", "claude|", "claude:second", "muse|", "kimi:main",
                                    "glm:main", "deepseek:main")}
    roles["muse|"] = "Blind second opinions on the riskiest work. " + long_role
    rules = [f"Rule {i}: " + "A standing rule of the owner, in full sentences, that runs on. " * 9 for i in range(18)]
    text = _page(_view(roles=roles, rules=rules))
    assert len(text) <= routing_page.LIMIT
    # Facts survive whatever is trimmed: every status word, cap, the hold, the 90%.
    assert "HOLD BACK" in _line(text, "Codex") and "ceiling 95%" in _line(text, "Codex")
    assert "NO JOBS until" in _line(text, "Grok") and "up to 90%" in _line(text, "Grok")
    assert "NEARLY OUT" in _line(text, "Kimi") and "SECOND OPINIONS ONLY" in _line(text, "Muse")
    assert re.search(r"\d+ more rule lines \(cut from [A-Z ,]+\): run `khipu route --full`", text)


def test_the_limit_holds_even_when_the_accounts_alone_would_not_fit():
    view = _view()
    for entry in view["accounts"]:
        entry["caps"] = entry["caps"] * 12
    assert len(_page(view)) <= routing_page.LIMIT


# ---- standing rules outrank exceptions for space ----------------------------------------------

RULES_10 = [
    "WHICH MODEL: " + "Opus: scopes, designs, rulings and the hardest code; Sonnet: most coding. " * 3,
    "WHICH MODEL: " + "Codex Sol: cross-family checks, narrow re-checks and a few builds. " * 3,
    "NEVER: " + "Grok, GLM, DeepSeek or Kimi on privacy, safety, sign-in, permission or money code. " * 3,
    "NEVER: " + "A key's value to any model; portfolio data to anyone but the finance model. " * 3,
    "CHECKS: " + "Cursor Auto may build sensitive code; Claude or Codex checks each result. " * 3,
    "CHECKS: " + "A lane never confirms its own verdict; the last check before merge is cross-family. " * 3,
    "MACS: " + "Before a job, compare load per core and start on the lower; both high: queue it. " * 3,
    "Resets: " + "A reset is never pressed to make room; plan around the real reset time. " * 3,
    "Reviews: " + "Findings go back to a builder with the exact fix, never a paraphrase. " * 3,
    "Cost: " + "Pick the cheapest model that does the job well; quality wins ties. " * 3,
]


def _exception(n, *, ago_hours, until_days=2, text=None):
    return {"id": f"x{n:08x}", "text": text or f"Exception number {n}: " + "use it only when the work really needs it, "
            "and say why in one line. " * 4, "until": _iso(NOW + timedelta(days=until_days)),
            "at": _iso(NOW - timedelta(hours=ago_hours)), "by": "owner"}


def _with_exceptions(view, counts, *, ago_hours=10):
    for entry in view["priority"]["entries"]:
        label = entry["label"]
        for n in range(counts.get(label, 0)):
            entry.setdefault("exceptions", []).append(_exception(n + 1, ago_hours=ago_hours + n))
    return view


def _rules_of(view):
    return [text for _head, text in routing_page._rules(view)]


@pytest.mark.parametrize("prefix,kept", [
    ("Matt 2026-11-12 09:30:", ""), ("Matt 2026-11-12 ~09:30Z:", ""),
    ("Matt 2026-11-12 09:30Z, in this conversation:", "in this conversation: "),
    ("Matt 2026-11-12, in this conversation:", "in this conversation: "),
    ("Reaffirmed by Matt 2026-11-12 ~09:30, on the main Mac:", "on the main Mac: "),
    ("Reaffirmed by Matt 2026-11-12, here:", "here: "),
])
def test_page_attribution_keeps_the_day_before_cutting(prefix, kept):
    words = prefix + " Keep Codex available for cross-family review."
    view = _view()
    item = view["priority"]["entries"][0]
    item["role"] = words
    item["exceptions"] = [_exception(1, ago_hours=1, text=words)]
    before = copy.deepcopy(view)
    text = _page(view)
    assert _line(text, "Codex").count(f"(11-12) {kept}Keep Codex") == 2
    assert prefix not in text
    assert view == before
    facts = routing_page.facts(view, NOW)["accounts"][0]
    assert facts["role"] == words
    assert facts["exceptions"] == [{k: item["exceptions"][0][k] for k in ("id", "text", "until")}]


@pytest.mark.parametrize("words", [
    "Keep Matt 2026-11-12 09:30Z: in the middle.", "Matt says: keep this.",
    "Someone 2026-11-12 09:30Z: keep this.", "Matt 2026-11-12 tomorrow: keep this.",
    "Matt presses resets; say when one is worth it.",
    "Matt 2026-11-12 06:56:30Z is when Codex resets.", "Matt 2026-11-12:06:56Z resets.",
])
def test_text_without_a_leading_stamp_is_unchanged(words):
    assert routing_page._page_words(words) == words


# Only the "Matt <date> [time]" stamp goes. Everything after it stays word for
# word, whether it is a place ("Olivewood Bible Study:") or a scope or
# condition ("Codex:", "at the limit:", "until the build is out:"): no pattern
# can tell those apart, so none is guessed at (review 2026-11-12).
@pytest.mark.parametrize("words", [
    "Matt 2026-11-12, use Claude until Codex resets at 06:56Z",
    "Matt 2026-11-12, until the Bible Study build is out: no Xcode builds on the M5.",
    "Matt 2026-11-12, no Grok builds this week. Reason: the xAI week is shared.",
    "Matt 2026-11-12, no Grok builds: the xAI week is shared.",
    "Matt 2026-11-12, Codex first: Claude only for checks",
    "Matt 2026-11-12 ~01:00Z, never Fable on second: it bills main",
    "Matt 2026-11-12, Codex: run it to its limit",
    "Matt 2026-11-12, on Codex: ration nothing",
    "Matt 2026-11-12, at the limit: tell him",
    "Matt 2026-11-12, Grok Bot Only: pause",
    "Matt 2026-11-12, Use Codex: keep Claude for checks.",
    "Matt 2026-11-12, in doubt: use Opus.",
    "Matt 2026-11-12: 06:56Z is when Codex resets.",
    'Matt 2026-11-11 ~23:35Z, Olivewood orchestrator: "run workflows on it instead of main too."',
    'Matt 2026-11-11, Olivewood: ~20:50Z "Reset button hit..."',
    "Reaffirmed by Matt 2026-11-11 ~22:30Z, Olivewood Bible Study: \"dont forget\"",
    "Matt 2026-11-12: Keep this.",
])
def test_only_the_stamp_is_shortened_and_every_word_after_it_is_kept(words):
    out = routing_page._page_words(words)
    assert out.startswith(f"({words.split('Matt ', 1)[1][5:10]}) ")
    rest = out[len("(11-12) "):]
    assert words.endswith(rest) and len(rest) >= len(words) - len("Reaffirmed by Matt 2026-11-12 ~22:30Z, ")
    assert "Matt 20" not in out


@pytest.mark.parametrize("words,expected", [
    ('Matt 2026-11-11 ~13:30Z, Olivewood Bible Study: "Lean more"', '(11-11) Olivewood Bible Study: "Lean more"'),
    ("Matt 2026-11-12: Keep this.", "(11-12) Keep this."),
    ("Reaffirmed by Matt 2026-11-12: Keep this.", "(11-12) Keep this."),
    ("Matt 2026-11-12, at the limit: tell him", "(11-12) at the limit: tell him"),
    ("Matt 2026-11-12: 06:56Z is when Codex resets.", "(11-12) 06:56Z is when Codex resets."),
    ("Matt 2026-11-12 06:56:30Z: Codex resets.", "(11-12) Codex resets."),
])
def test_page_attribution_examples(words, expected):
    assert routing_page._page_words(words) == expected


def _fresh_fixture():
    now = datetime(2026, 11, 12, 20, 10, tzinfo=timezone.utc)
    rules = [f"{head}: Rule {i}: " + "Keep the owner words intact and route by the current account record. " * 4
             for i, head in enumerate(("WHICH MODEL", "NEVER", "CHECKS", "MACS") * 3)]
    view = _view(rules=rules)
    delta = now - NOW
    for account in view["accounts"]:
        for pace in account.get("pace", []):
            pace["cycle_resets_at"] = _iso(routing._parse(pace["cycle_resets_at"]) + delta)
    for hold in view["holds"]:
        hold["until"] = _iso(routing._parse(hold["until"]) + delta)
    for entry in view["priority"]["entries"]:
        entry["role"] = "Matt 2026-11-12 ~19:00Z, in chat: " + (
            "Keep this account for work it fits; use even pace and review the result. " * 8)
    for label, ages in (("Codex", (1, 2, 72)), ("Claude second", (1, 72, 73))):
        item = next(e for e in view["priority"]["entries"] if e["label"] == label)
        item["exceptions"] = [dict(_exception(i, ago_hours=age), at=_iso(now - timedelta(hours=age)),
                                   until=_iso(now + timedelta(days=2)),
                                   text="Matt 2026-11-12 ~19:10Z, in this chat: " +
                                   f"Newest {label} direction {i}: keep this lane open for the cross-family review "
                                   "and use the standing rules to choose each job carefully. " * 2)
                              for i, age in enumerate(ages, 1)]
    return view, now


def test_fresh_realistic_page_keeps_directions_and_measures_rules():
    view, now = _fresh_fixture()
    before = copy.deepcopy(view)
    text = routing_page.page(view, now)
    rules = _rules_of(view)
    kept = sum(rule in text for rule in rules)
    assert 3200 < sum(map(len, rules)) < 3500
    assert len(text) <= routing_page.LIMIT
    cut = routing_page.left_out(view, now)
    assert kept + sum(count for _head, count in cut) == 12
    if kept < 12:
        all_at_floor = routing_page._render(view, now, {}, {}, routing_page._rules(view), 12,
                                           routing_page._WIDTHS[-2], 0, routing)
        assert len(all_at_floor) > routing_page.LIMIT
        footer = next(line for line in text.splitlines() if "more rule lines" in line)
        assert all(head in footer for head, _n in cut)
    else:
        assert "more rule lines" not in text
    for label in ("Codex", "Claude second"):
        assert f"(11-12) in this chat: Newest {label} direction 1:" in _line(text, label)
    assert "Matt 2026-" not in text
    assert view == before


def test_fresh_floor_survives_each_width_and_old_exceptions_collapse_at_zero():
    view, now = _fresh_fixture()
    for widths in routing_page._WIDTHS[:-1]:
        text = routing_page._render(view, now, {}, {}, [], 0, widths, 140, routing)
        for label in ("Codex", "Claude second"):
            line = _line(text, label)
            quote = re.search(r'Exception until [^“]+“([^”]+)”', line)[1]
            expected = routing_page._cut(_page_words_for_test(view, label), max(widths[1], 110))
            assert quote == expected
            if widths[1] == 0:
                assert line.count("Exception until") == 1
                assert "+2 more: khipu route --full" in line
    old = _with_exceptions(_view(), {"Codex": 3}, ago_hours=72)
    text = routing_page._render(old, NOW, {}, {}, [], 0, (90, 0, 60), 0, routing)
    assert "3 exceptions: khipu route --full" in _line(text, "Codex")
    assert "Exception until" not in _line(text, "Codex")
    last = routing_page._render(view, now, {}, {}, [], 0, (0, 0, 0), 0, routing)
    assert "Exception until" not in _line(last, "Codex")


def _page_words_for_test(view, label):
    item = next(e for e in view["priority"]["entries"] if e["label"] == label)
    return routing_page._page_words(item["exceptions"][0]["text"])


@pytest.mark.parametrize("at", [None, "bad time", _iso(NOW + timedelta(hours=1)), _iso(NOW - timedelta(hours=49))])
def test_missing_invalid_future_and_old_times_have_no_fresh_floor(at):
    assert routing_page._fresh_exception([dict(_exception(1, ago_hours=1), at=at)], NOW, routing) is None


def test_rule_omissions_report_headings_while_fresh_direction_stays_at_floor(monkeypatch):
    view, now = _fresh_fixture()
    view["rules"] *= 3
    attempts = []
    render = routing_page._render

    def tracked(*args):
        attempts.append((args[5], args[6]))
        return render(*args)

    monkeypatch.setattr(routing_page, "_render", tracked)
    text = routing_page.page(view, now)
    first_drop = next(i for i, (keep, _widths) in enumerate(attempts) if keep < len(view["rules"]))
    assert [widths for _keep, widths in attempts[:first_drop]] == list(routing_page._WIDTHS[:-1])
    assert attempts[first_drop][1] == routing_page._WIDTHS[-2]
    cut = routing_page.left_out(view, now)
    assert len(text) <= routing_page.LIMIT
    assert cut and sum(n for _head, n in cut) > 0
    footer = next(line for line in text.splitlines() if "more rule lines" in line)
    assert all(head in footer for head, _n in cut)
    assert "(11-12) in this chat: Newest Codex direction 1:" in _line(text, "Codex")


def test_twelve_standing_rules_fit_with_eight_long_account_roles():
    rules = [f"{head}: Rule {i}: " + "Keep the owner words intact and route by the current account record. " * 4
             for i, head in enumerate(("WHICH MODEL", "NEVER", "CHECKS", "MACS") * 3)]
    view = _view(rules=rules)
    for entry in view["priority"]["entries"]:
        entry["role"] = "Keep this account for the work it fits; use even pace and review the result. " * 8
    assert 3200 < sum(len(t) for t in _rules_of(view)) < 3500
    text = _page(view)
    assert len(text) <= routing_page.LIMIT
    for rule in _rules_of(view):
        assert rule in text
    for account in view["accounts"]:
        _line(text, account["label"])
    assert "more rule lines" not in text
    assert routing_page.left_out(view, NOW) == []


def test_six_exceptions_on_one_account_never_cost_the_page_a_rule_line():
    view = _with_exceptions(_view(rules=RULES_10), {"Codex": 6, "Grok": 3})
    assert 2000 < sum(len(t) for t in _rules_of(view)) < 3000
    text = _page(view)
    assert len(text) <= routing_page.LIMIT
    for rule in _rules_of(view):
        assert rule in text, rule[:40]
    assert "more rule lines" not in text
    assert routing_page.left_out(view, NOW) == []


def test_only_the_two_newest_exceptions_show_and_older_ones_collapse_to_a_count():
    view = _with_exceptions(_view(), {"Codex": 6})
    # Written out of order: the newest is the one whose `at` is latest.
    view["priority"]["entries"][0]["exceptions"].reverse()
    codex = _line(_page(view), "Codex")
    assert codex.count("Exception until") == 2
    assert "Exception number 1:" in codex and "Exception number 2:" in codex  # newest (ago 10h, 11h)
    for old in (3, 4, 5, 6):
        assert f"Exception number {old}:" not in codex
    assert "+4 older: khipu route --full" in codex
    assert "older" not in _line(_page(_with_exceptions(_view(), {"Grok": 2})), "Grok")


def test_an_ended_exception_is_never_shown_or_counted():
    view = _with_exceptions(_view(), {"Codex": 3})
    view["priority"]["entries"][0]["exceptions"][0]["until"] = _iso(NOW - timedelta(minutes=1))
    codex = _line(_page(view), "Codex")
    assert "Exception number 1:" not in codex and "Exception number 2:" in codex and "older" not in codex


def test_old_exception_text_gives_way_before_any_rule_line_is_touched():
    long = "an exception quote that goes on and on about one more temporary thing " * 3
    view = _view(rules=RULES_10)
    for entry in view["priority"]["entries"]:
        entry["exceptions"] = [_exception(n, ago_hours=72 + n, text=long) for n in range(1, 7)]
    text = _page(view)
    assert len(text) <= routing_page.LIMIT
    for rule in _rules_of(view):
        assert rule in text
    # Every account line still says it has exceptions, as a count at the least.
    for label in ("Codex", "Grok", "Claude", "Muse"):
        assert "khipu route --full" in _line(text, label)
    # A hold's words outlast the exception quotes: they say why Grok takes no jobs.
    assert "overpaced" in _line(text, "Grok")


def test_a_rule_line_goes_only_when_the_page_cannot_fit_even_with_bare_counts_and_the_page_names_the_headings():
    rules = RULES_10 + [f"Rule {i}: " + "A standing rule that runs on in full sentences. " * 12 for i in range(8)]
    view = _with_exceptions(_view(rules=rules), {"Codex": 6})
    text = _page(view)
    assert len(text) <= routing_page.LIMIT
    cut = routing_page.left_out(view, NOW)
    assert cut and sum(n for _h, n in cut) == len(rules) - sum(1 for t in _rules_of(view) if t in text)
    assert cut[-1][0] == "RULES"
    footer = next(ln for ln in text.splitlines() if "more rule lines" in ln)
    assert "RULES" in footer and "khipu route --full" in footer
    # The lines that stay are the top ones, whole.
    kept = [t for t in _rules_of(view) if t in text]
    assert kept == _rules_of(view)[: len(kept)]


def test_the_signature_moves_when_an_exception_is_added_or_ends_even_one_collapsed_into_the_count():
    base = _with_exceptions(_view(), {"Codex": 3})
    sig = routing_page.signature(base, NOW)
    older = copy.deepcopy(base)  # an older exception: collapsed on the page, still in the signature
    older["priority"]["entries"][0]["exceptions"].append(_exception(9, ago_hours=500))
    assert "+2 older" in _line(_page(older), "Codex") and "+1 older" in _line(_page(base), "Codex")
    assert routing_page.signature(older, NOW) != sig
    ended = copy.deepcopy(base)
    ended["priority"]["entries"][0]["exceptions"][2]["until"] = _iso(NOW - timedelta(minutes=1))  # a collapsed one
    assert routing_page.signature(ended, NOW) != sig
    assert routing_page.signature(copy.deepcopy(base), NOW) == sig


# ---- every fact prints on its own account's line --------------------------------------------


def test_each_status_word_follows_the_records_facts():
    text = _page()
    assert _line(text, "Codex").split(": ", 1)[1].startswith("HOLD BACK.")  # 83% used against an even pace of 51%
    assert _line(text, "Grok").split(": ", 1)[1].startswith("NO JOBS until Sun 23:10Z")  # the hold
    assert _line(text, "Claude").split(": ", 1)[1].startswith("OK.")
    assert _line(text, "Claude second").split(": ", 1)[1].startswith("OK.")
    assert _line(text, "Muse").split(": ", 1)[1].startswith("SECOND OPINIONS ONLY.")
    assert _line(text, "Kimi").split(": ", 1)[1].startswith("NEARLY OUT.")  # 3.7% of the month left
    assert _line(text, "GLM").split(": ", 1)[1].startswith("NEARLY OUT.")
    assert _line(text, "DeepSeek").split(": ", 1)[1].startswith("LAST RESORT.")  # a spend cap, last in the order


def test_the_other_states_have_their_own_words():
    view = _view(hold=False)
    by = {a["label"]: a for a in view["accounts"]}
    by["Claude"].update(state="limited", until=_iso(NOW + timedelta(hours=3)))
    by["Muse"].update(state="down", reason="not signed in")
    by["Claude second"].update(state="paused", until=None)
    text = _page(view)
    assert "LIMIT REACHED, back 23:10Z" in _line(text, "Claude")
    assert "DOWN: not signed in" in _line(text, "Muse")
    assert "PAUSED until lifted" in _line(text, "Claude second")
    assert "HOLD BACK" in _line(text, "Codex")


def test_percent_left_and_reset_print_on_the_account_line():
    text = _page()
    assert "17% left, resets Tue 06:10Z" in _line(text, "Codex")
    assert "used 83%, even pace 51%" in _line(text, "Codex")
    assert "3.7% of the month left, resets Nov 28 00:00Z" in _line(text, "Kimi")
    assert "99% left" in _line(text, "Claude second")


def test_every_cap_prints_on_its_own_account_line_and_nowhere_else():
    text = _page()
    expected = {
        "Codex": ["even pace", "even pace +5 for checks", "ceiling 95%"],
        "Grok": ["even pace", "ceiling 76%"],
        "Claude": ["even pace +5", "even pace +20 for checks (sensitive work only)", "ceiling 91%"],
        "Kimi": ["3 at a time"],
        "GLM": ["one at a time"],
        "DeepSeek": ["spend cap $35 in 7 d"],
    }
    for label, caps in expected.items():
        line = _line(text, label)
        assert "Caps: " in line
        for cap in caps:
            assert cap in line, (label, cap, line)
    for label in ("Claude second", "Muse"):
        assert "Caps:" not in _line(text, label)
    assert text.count("ceiling 95%") == 1 and text.count("ceiling 76%") == 1 and text.count("ceiling 91%") == 1
    assert text.count("spend cap $35") == 1


def test_measured_spend_and_running_jobs_print_when_the_caller_read_them():
    text = _page(readings={"deepseek:main": {"spend": {"usd": 0.59}}, "glm:main": {"concurrency": {"jobs": 1}}})
    assert "spend cap $35 in 7 d ($0.59 spent)" in _line(text, "DeepSeek")
    assert "one at a time (1 running)" in _line(text, "GLM")


def test_the_90_percent_prints_on_groks_line_only():
    text = _page()
    assert "With the owner's okay, up to 90%" in _line(text, "Grok")
    assert text.count("up to 90%") == 1


def test_a_hold_prints_on_the_held_account_with_its_end_and_the_owners_words():
    text = _page()
    line = _line(text, "Grok")
    assert "NO JOBS until Sun 23:10Z: “We are overpaced on grok/cursor plan.”" in line
    assert text.count("NO JOBS") == 1 and text.count("overpaced") == 1
    assert "NO JOBS" not in _page(_view(hold=False))
    # A hold on a Mac or on everything is its own line, not an account's.
    view = _view(hold=False)
    view["holds"] = [{"id": "h-1", "scope": {"kind": "machine", "mac": "m1"}, "scope_label": "Mac m1",
                      "reason": "machine_busy", "text": "load is high", "until": None, "owner": True}]
    text = _page(view)
    assert "Hold on Mac m1: until lifted: “load is high”." in text and "NO JOBS" not in text


def test_a_conversations_override_prints_on_its_account_line_and_in_one_line_of_its_own():
    override = {"id": "ov-1", "ceiling_pct": 95.0, "window": "weekly", "until": _iso(NOW + timedelta(days=1)),
                "account": "claude|"}
    text = _page(conversation={"claude|": override})
    line = _line(text, "Claude")
    assert "Override for this conversation: may run to 95% of its week until Sun 20:10Z." in line
    assert [ln for ln in text.splitlines() if ln.startswith("This conversation: Claude may run ahead of pace")]
    assert text.count("Override for this conversation") == 1
    # No conversation, no override line anywhere.
    plain = _page()
    assert "override" not in plain.lower() and "This conversation" not in plain
    # An indefinite one names its review.
    review = _page(conversation={"claude|": {"indefinite": True, "review_at": _iso(NOW + timedelta(days=14))}})
    assert "may run ahead of pace, no end (review Nov 21 20:10Z)" in _line(review, "Claude")


def test_plan_terms_print_on_the_account_they_belong_to():
    text = _page()
    assert "Plan changes to Pro 100 on Nov 8." in _line(text, "Codex")
    assert "Plan changes to a smaller plan, date not set." in _line(text, "Grok")
    assert "Ends Nov 13." in _line(text, "Muse") and "Ends Nov 27." in _line(text, "Kimi")
    assert "Ends Nov 20." in _line(text, "GLM")
    assert "Resets held: Codex 2, Claude 1, Claude second 1." in text
    assert "k3.1-pro only" in _line(text, "Kimi") and "glm-5.3, glm-5.3-flash only" in _line(text, "GLM")


def test_the_owners_rules_render_under_their_headings_and_other_rules_under_rules():
    view = _view(rules=["WHICH MODEL: Opus: scopes.", "NEVER: a -fast tier.", "CHECKS: cross-family.",
                        "MACS: lower load first.", "Some other standing rule."])
    view["rules"].append({"n": 9, "text": "Pending words", "pending": True, "confirmed": False, "entity": "r9"})
    text = _page(view)
    order = [text.index(h) for h in ("\nWHICH MODEL\n", "\nNEVER\n", "\nCHECKS\n", "\nMACS\n", "\nRULES\n")]
    assert order == sorted(order)
    assert "Opus: scopes." in text and "Some other standing rule." in text and "WHICH MODEL: " not in text
    assert "Pending words" not in text  # a rule awaiting the owner's yes is not the owner's words yet


# ---- who is not on the page ----------------------------------------------------------------


def test_an_ended_account_is_absent_even_when_the_view_still_lists_it():
    later = datetime(2026, 11, 14, 0, 5, tzinfo=timezone.utc)  # Muse's bare date ends the day after
    text = routing_page.page(_view(), later)
    assert "Muse" not in text
    assert "Kimi" in text and "GLM" in text
    # The day itself it is still usable.
    assert "Muse" in routing_page.page(_view(), datetime(2026, 11, 13, 23, 59, tzinfo=timezone.utc))
    # Not in the order, still ended: also absent.
    view = _view()
    view["priority"]["entries"] = [e for e in view["priority"]["entries"] if e["label"] != "Muse"]
    assert "Muse" not in routing_page.page(view, later)
    assert "Muse" in routing_page.page(view, NOW)


def test_an_ended_account_is_absent_from_a_real_fold(replay):
    now = routing._now()
    for ident, family, ends in (("muse:main", "Meta", routing._iso(now + timedelta(hours=1))),
                                ("kimi:main", "Moonshot", None)):
        provider = ident.partition(":")[0]
        routing.set_definition("provider", {"id": provider, "label": provider.title(), "family": family,
                                            "probe": "none", "lane_names": [provider]}, via="app")
        routing.set_definition("account", {"id": ident, "label": provider.title(), "provider": provider,
                                           **({"ends": ends} if ends else {})}, via="app")
    routing.set_priority([{"account": "Kimi", "role": "builds"}, {"account": "Muse", "role": "second opinions"}],
                         via="app", expect=routing.priority_version())
    before = routing_page.page(routing.read_view(now), now)
    assert "Muse" in before and "Kimi" in before
    later = now + timedelta(hours=2)
    after = routing_page.page(routing.read_view(later), later)
    assert "Muse" not in after and "Kimi" in after


# ---- no raw ids, hashes or keys ------------------------------------------------------------


def test_no_raw_ids_hashes_or_keys_appear():
    view = _view()
    view["accounts"][2]["witness"] = {"id": "peff41a49a9fe", "evidence": "usage probe", "source": "probe"}
    view["priority"]["entries"][0]["exceptions"] = [
        {"id": "x240d89c3", "text": "Use it only when necessary. sk-ant-api03-AbCdEf0123456789AbCdEf0123456789", "until":
         _iso(NOW + timedelta(days=2)), "by": "owner"}]
    view["holds"][0]["text"] = "key sk-proj-0123456789abcdef0123456789abcdef is not for jobs"
    text = _page(view)
    for raw in ("hold-5c1f0a9e77b24d03", "01a17083-c656", "cap-01a15fe6", "peff41a49a9fe", "x240d89c3", "rule-0000",
                "claude:second", "kimi:main", "glm:main", "deepseek:main", "e-7f3a", "sk-ant-", "sk-proj-",
                "AbCdEf0123456789", "revision", "codex|", "grok|"):
        assert raw not in text, raw
    assert "|" not in text
    assert not re.search(r"\b[0-9a-f]{12,}\b", text)
    assert not re.search(r"\b[0-9a-f]{8}-[0-9a-f]{4}-", text)


# ---- purity --------------------------------------------------------------------------------


def test_the_page_is_deterministic_for_a_fixed_now_and_leaves_the_view_alone():
    view = _view()
    frozen = copy.deepcopy(view)
    first, second = _page(view), _page(copy.deepcopy(view))
    assert first == second == _page(view)
    assert view == frozen
    assert routing_page.page(view, NOW + timedelta(minutes=3)) != first  # it prints the time it was drawn


def test_the_page_reads_and_folds_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the page touched the record, the clock or the cache")

    from khipu import usage

    for name in ("read_view", "fold", "_now", "_inputs", "settings", "plan_facts", "cap_statuses"):
        monkeypatch.setattr(routing, name, boom)
    monkeypatch.setattr(usage, "_load_cache", boom)
    monkeypatch.setattr("builtins.open", boom)
    text = routing_page.page(_view(), NOW, conversation={"claude|": {"ceiling_pct": 95, "until": _iso(NOW + timedelta(hours=2))}})
    assert "HOLD BACK" in text


def test_no_record_is_one_line():
    assert routing_page.page({"record": False, "accounts": []}, NOW) == "ROUTING: no routing record on this Mac."


# ---- khipu route --page -------------------------------------------------------------------


def test_khipu_route_page_prints_the_page_from_the_record(replay, capsys):
    from khipu import cli

    now = routing._now()
    routing.set_definition("provider", {"id": "muse", "label": "Muse", "family": "Meta", "probe": "none",
                                        "lane_names": ["muse"]}, via="app")
    routing.set_definition("account", {"id": "muse:main", "label": "Muse", "provider": "muse",
                                       "ends": routing._iso(now + timedelta(days=3))}, via="app")
    routing.set_priority([{"account": "Muse", "role": "Blind second opinions."}], via="app",
                         expect=routing.priority_version())
    assert cli.main(["route", "--page"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("ROUTING ") and "1. Muse: " in out and len(out) <= routing_page.LIMIT
    assert "Blind second opinions." in out
    assert cli.main(["route", "--page", "--json"]) == 0
    assert '"chars"' in capsys.readouterr().out
    for other in (["--since", "1"], ["--view", "rules"]):
        assert cli.main(["route", "--page", *other]) != 0
        capsys.readouterr()
    # --summary and --brief print the same page.
    for other in (["--summary"], ["--brief"]):
        assert cli.main(["route", *other]) == 0
        assert capsys.readouterr().out.startswith("ROUTING ")


def test_second_opinions_only_comes_from_the_provider_fact_not_the_role_words():
    """The page and advice read the same fact: ``use: second_opinion`` on the
    provider. A role that merely mentions second opinions changes nothing."""
    view = _view()
    now = NOW
    del view["definitions"]["providers"]["muse"]
    text = routing_page.page(view, now)
    assert "SECOND OPINIONS ONLY" not in _line(text, "Muse")
    view["definitions"]["providers"]["muse"] = {"id": "muse", "use": "second_opinion"}
    assert "SECOND OPINIONS ONLY" in _line(routing_page.page(view, now), "Muse")


def test_a_new_page_limit_resends_the_page_once(monkeypatch):
    # Raising the limit puts back rule lines a running conversation was never
    # sent; the signature must move so the page goes out again.
    view = _view()
    sig = routing_page.signature(view, NOW)
    monkeypatch.setattr(routing_page, "LIMIT", routing_page.LIMIT + 1000)
    assert routing_page.signature(view, NOW) != sig
    assert routing_page.signature(copy.deepcopy(view), NOW) == routing_page.signature(view, NOW)


@pytest.mark.parametrize("label", ["Codex", "Claude second"])
def test_an_exception_ageing_past_48_hours_changes_the_signature(label):
    view = _with_exceptions(_view(), {label: 1}, ago_hours=47)
    item = next(e for e in view["priority"]["entries"] if e["label"] == label)
    sig = routing_page.signature(view, NOW)
    # Moving the timestamp within the fresh window alone should not resend.
    item["exceptions"][0]["at"] = _iso(NOW - timedelta(hours=46))
    assert routing_page.signature(view, NOW) == sig
    item["exceptions"][0]["at"] = _iso(NOW - timedelta(hours=49))
    assert routing_page.signature(view, NOW) != sig
