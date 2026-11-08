"""Routing slice 4: advice reads the same facts as the page. Scope:
jobs/2026-11-07/scope-routing-one-page.md section 6 item 4, decision 2 (R2).

A hold closes an account; an account under 10% left ranks below accounts with
room; an account whose ``ends`` has passed is absent; a ``use: second_opinion``
provider is off build and fix lists and first for a check or review. Advice
stays guidance: it never blocks anything.

The ten questions below are Matt's routing questions with his expected first
pick, derived from the scope's rules and the order in its section 2 (Codex,
Grok, Claude, Claude second, Muse, Kimi, GLM, DeepSeek). The views are built
from structured facts only, as the record carries them. The first pick is the
first ranked account that may go; "nobody" when none has room."""
from __future__ import annotations

from datetime import timedelta

import pytest

from khipu import routing, routing_advice, routing_providers

pytest_plugins = ("tests.test_routing_acceptance",)

ORDER = ["codex|", "grok|", "claude|", "claude|second", "muse|", "kimi|", "glm|", "deepseek|"]
LABELS = {"codex|": "Codex", "grok|": "Grok", "claude|": "Claude", "claude|second": "Claude second",
          "muse|": "Muse", "kimi|": "Kimi", "glm|": "GLM", "deepseek|": "DeepSeek"}
HARNESS = {"codex|": "codex", "grok|": "grok", "claude|": "claude", "claude|second": "claude",
           "muse|": "muse", "kimi|": "kimi", "glm|": "glm", "deepseek|": "deepseek"}
PROVIDERS = {
    "muse": {"id": "muse", "label": "Muse", "family": "Meta", "models_allowed": ["muse-wonder-full-1.0"],
             "use": "second_opinion"},
    "kimi": {"id": "kimi", "label": "Kimi", "family": "Moonshot", "models_allowed": ["k3.1-pro"]},
    "glm": {"id": "glm", "label": "GLM", "family": "Zhipu", "models_allowed": ["glm-5.3", "glm-5.3-flash"]},
    "deepseek": {"id": "deepseek", "label": "DeepSeek", "family": "DeepSeek",
                 "models_allowed": ["deepseek-flash"]},
}
# What each account reads today (scope section 1/2), as a percent used.
USED = {"codex|": 83, "grok|": 61, "claude|": 68, "claude|second": 1, "muse|": 33,
        "kimi|": 96.3, "glm|": 96, "deepseek|": None}


def _entry(key, now, *, used="today", terms=None, state=routing.OPEN, caps=None, pace_extra=None):
    used = USED[key] if used == "today" else used
    entry = {"key": key, "label": LABELS[key], "harness": HARNESS[key], "state": state,
             "caps": list(caps or []), "models": [], "pace": [], "terms": terms or {},
             "witness": {"at": routing._iso(now)}}
    if used is not None:
        pace = {"window": "weekly", "used_pct": used, "buckets": 96, "why": None}
        pace.update(pace_extra or {})
        entry["pace"].append(pace)
    return entry


def _view(now, *, order=None, holds=(), providers=PROVIDERS, changes=None):
    """A folded view with the page's eight accounts. ``changes`` maps a key to
    keyword arguments for ``_entry``; an absent key keeps today's reading."""
    changes = changes or {}
    keys = ORDER
    accounts = [_entry(k, now, **changes.get(k, {})) for k in keys]
    entries = [{"account": k, "label": LABELS[k], "role": None} for k in (order if order is not None else keys)]
    return {"accounts": accounts, "priority": {"entries": entries}, "holds": list(holds),
            "definitions": {"providers": dict(providers)}}


def _no_jobs(key):
    """Grok's "no jobs until Monday", as slice 5 writes it: a real hold."""
    return {"scope": {"kind": "account", "account": key}, "until": None, "reason": "overpaced"}


def _ahead(key="codex|"):
    """A weekly pace target the account is ahead of: held back for jobs and checks."""
    return {"caps": [{"kind": "pace_target", "window": "weekly", "purpose": "job", "pct": 55, "slack": 5,
                      "revision": "pt"}],
            "pace_extra": {"pace_target": {"verdict": "ahead", "allowance_pct": 51, "used_pct": 83}}}


def _share_reached(used, pct):
    return {"used": used, "caps": [{"kind": "share", "window": "weekly", "pct": pct, "revision": "sh"}],
            "pace_extra": {"share_cap": {"pct": pct, "verdict": "cap_reached"}}}


def _pick(ranked):
    return next((r["label"] for r in ranked if r["verdict"] == "go"), "nobody")


def _ask(replay, view, **question):
    return routing_advice.rank(view, replay.now, **question)


def _questions(now):
    gone = routing._iso(now - timedelta(days=1))
    ahead = _ahead()
    both_out = {"state": routing.LIMITED}
    return [
        # 1. Today's build question: Grok's no-jobs hold closes it, Codex is ahead of its pace, Muse is
        #    second opinions only, Kimi and GLM are nearly out. Claude is the first with room.
        ("today's build, Grok held", dict(holds=[_no_jobs("grok|")], changes={"codex|": ahead}),
         dict(kind="build"), "Claude"),
        # 2. Muse sits ahead of Claude in the order, but it is second opinions only: off the build list.
        ("a build with Muse listed first", dict(order=["muse|", "claude|", "codex|"], holds=[_no_jobs("grok|")]),
         dict(kind="build"), "Claude"),
        # 3. Both Claude accounts are at their limit: Muse is not a builder, Kimi and GLM are nearly out,
        #    so the paid account that has room is the pick.
        ("a build with both Claude accounts limited",
         dict(holds=[_no_jobs("grok|")], changes={"codex|": ahead, "claude|": both_out,
                                                   "claude|second": both_out}),
         dict(kind="build"), "DeepSeek"),
        # 4. The old "use GLM and Kimi for every job" order, both about 96% used: Claude second has room.
        ("a build with Kimi and GLM first in the order",
         dict(order=["kimi|", "glm|", "claude|second", "claude|"], holds=[_no_jobs("grok|")],
              changes={"claude|": both_out}),
         dict(kind="fix"), "Claude second"),
        # 5. Kimi's plan ended yesterday and the fold dropped it from the order; it must not be offered
        #    when nobody else has room.
        ("a build when the only open account's plan ended",
         dict(order=["claude|", "codex|"], holds=[_no_jobs("grok|")],
              changes={"claude|": both_out, "claude|second": both_out, "codex|": ahead, "muse|": both_out,
                       "glm|": {"state": routing.LIMITED}, "deepseek|": both_out,
                       "kimi|": {"used": 50, "terms": {"ends": gone}}}),
         dict(kind="build"), "nobody"),
        # 6. A second opinion goes to the account that exists for it, ahead of Claude in the order.
        ("a second opinion", dict(holds=[_no_jobs("grok|")], changes={"codex|": ahead}),
         dict(kind="check"), "Muse"),
        # 7. Orchestration while Codex, first in the order, is at its ceiling: Claude.
        ("orchestration with Codex at its ceiling",
         dict(holds=[_no_jobs("grok|")], changes={"codex|": _share_reached(96, 95)}),
         dict(purpose="orchestration"), "Claude"),
        # 8. A sensitive-code build: Grok held, Codex ahead of its pace; Claude. Never Grok, GLM, DeepSeek
        #    or Kimi (checked in the test itself).
        ("a sensitive-code build", dict(holds=[_no_jobs("grok|")], changes={"codex|": ahead}),
         dict(kind="build", sensitive=True), "Claude"),
        # 9. Codex's small share (only when necessary) is used up: Grok is held, so Claude.
        ("a build with Codex's small share reached",
         dict(holds=[_no_jobs("grok|")], changes={"codex|": _share_reached(25, 20)}),
         dict(kind="build"), "Claude"),
        # 10. The no-jobs hold has lapsed and Codex is ahead of its pace: Grok, first in the order, has room.
        ("a build after Grok's hold lapsed", dict(changes={"codex|": ahead}),
         dict(kind="build"), "Grok"),
    ]


def _run(replay):
    rows = []
    for name, view_args, question, expected in _questions(replay.now):
        ranked = _ask(replay, _view(replay.now, **view_args), **question)
        rows.append((name, expected, _pick(ranked), ranked))
    return rows


def test_the_ten_questions_get_matts_first_pick(replay):
    rows = _run(replay)
    wrong = [(name, expected, got) for name, expected, got, _ in rows if got != expected]
    table = "\n".join(f"{i}. {name}: expected {expected}, got {got}"
                      for i, (name, expected, got, _) in enumerate(rows, 1))
    assert len(rows) == 10
    # Target: at least 9 of 10.
    assert len(rows) - len(wrong) >= 9, table
    # And in fact all ten, so a regression names its question.
    assert not wrong, table


def test_a_sensitive_build_never_leads_with_grok_glm_deepseek_or_kimi(replay):
    # Sensitive code is not a provider fact (nothing in the record says so), so
    # this holds because of the order and the facts above, not a rule advice reads.
    name, expected, got, ranked = _run(replay)[7]
    assert name == "a sensitive-code build"
    assert ranked[0]["label"] not in ("Grok", "GLM", "DeepSeek", "Kimi")


def test_the_questions_show_the_new_facts_not_the_old_ones(replay):
    """Each fact changes some answer: with the rule off, the first pick moves."""
    rows = {name: ranked for name, _e, _g, ranked in _run(replay)}
    muse = next(r for r in rows["a build with Muse listed first"] if r["label"] == "Muse")
    assert muse["eligible"] is False and "second opinions" in muse["why_ranked"]
    kimi = next(r for r in rows["a build with Kimi and GLM first in the order"] if r["label"] == "Kimi")
    assert kimi["verdict"] == "go" and kimi["nearly_out"] == pytest.approx(3.7)
    assert "nearly out (3.7% left)" in kimi["why_ranked"]
    labels = [r["label"] for r in rows["a build when the only open account's plan ended"]]
    assert "Kimi" not in labels
    held = next(r for r in rows["today's build, Grok held"] if r["label"] == "Grok")
    assert held["verdict"] == "closed" and held["reason"] == "held"


# ---- the rules one by one ---------------------------------------------------------------------


def test_nearly_out_ranks_below_room_whatever_the_order(replay):
    view = _view(replay.now, order=["kimi|", "claude|second"], changes={"claude|": {"state": routing.LIMITED}})
    ranked = _ask(replay, view, kind="build")
    go = [r["label"] for r in ranked if r["verdict"] == "go"]
    assert go.index("Claude second") < go.index("Kimi")


def test_nearly_out_still_beats_a_hold_back_and_is_never_a_gate(replay):
    view = _view(replay.now, order=["codex|", "kimi|"], changes={"codex|": _ahead()})
    ranked = _ask(replay, view, kind="build")
    codex = next(r for r in ranked if r["label"] == "Codex")
    kimi = next(r for r in ranked if r["label"] == "Kimi")
    assert kimi["verdict"] == "go" and codex["verdict"] == "hold_back"
    assert kimi["rank"] < codex["rank"]
    assert kimi["advisory"] is True


@pytest.mark.parametrize("used,flag", [(89.9, False), (90, False), (90.1, True), (99.9, True)])
def test_the_ten_percent_line_is_the_pages_line(replay, used, flag):
    entry = _entry("kimi|", replay.now, used=used)
    assert (routing_advice.nearly_out(entry) is not None) is flag


def test_a_window_with_nothing_left_is_closed_not_nearly_out(replay):
    # 100% used is the plan's limit, not "nearly out": it closes for jobs elsewhere.
    entry = _entry("kimi|", replay.now, used=100)
    assert routing_advice.nearly_out(entry) is None


def test_nearly_out_reads_the_longest_window(replay):
    entry = _entry("claude|", replay.now, used=30)
    entry["pace"].append({"window": "five_hour", "used_pct": 97, "buckets": 12, "why": None})
    assert routing_advice.nearly_out(entry) is None
    entry["pace"][0]["used_pct"] = 95
    assert routing_advice.nearly_out(entry) == pytest.approx(5)


def test_an_unplaceable_reading_never_makes_an_account_nearly_out(replay):
    entry = _entry("kimi|", replay.now, used=96)
    entry["pace"][0]["why"] = "reset passed"
    assert routing_advice.nearly_out(entry) is None


def test_an_ended_plan_is_absent_and_closed_when_named(replay):
    ended = routing._iso(replay.now - timedelta(seconds=1))
    view = _view(replay.now, changes={"muse|": {"terms": {"ends": ended}}})
    assert "Muse" not in [r["label"] for r in _ask(replay, view, kind="check")]
    named = routing.advise("Muse", purpose="check", now=replay.now, view=view)
    assert named["verdict"] == "closed" and named["reason"].startswith("plan ended")


def test_a_plan_that_has_not_ended_is_offered_and_a_bare_date_lasts_the_day(replay):
    later = routing._iso(replay.now + timedelta(hours=1))
    view = _view(replay.now, changes={"muse|": {"terms": {"ends": later}}})
    assert "Muse" in [r["label"] for r in _ask(replay, view, kind="check")]
    today = replay.now.strftime("%Y-%m-%d")
    view = _view(replay.now, changes={"muse|": {"terms": {"ends": today}}})
    assert "Muse" in [r["label"] for r in _ask(replay, view, kind="check")]
    yesterday = (replay.now - timedelta(days=1)).strftime("%Y-%m-%d")
    view = _view(replay.now, changes={"muse|": {"terms": {"ends": yesterday}}})
    assert "Muse" not in [r["label"] for r in _ask(replay, view, kind="check")]


@pytest.mark.parametrize("question,offered", [
    ({"kind": "build"}, False), ({"kind": "fix"}, False), ({"purpose": "job"}, False),
    ({"purpose": "orchestration"}, False), ({"kind": "check"}, True), ({"purpose": "check"}, True)])
def test_a_second_opinion_account_takes_only_checks_and_reviews(replay, question, offered):
    ranked = _ask(replay, _view(replay.now), **question)
    muse = next(r for r in ranked if r["label"] == "Muse")
    assert (muse["eligible"] is True) is offered
    assert (muse["verdict"] == "go") is offered


def test_naming_a_second_opinion_account_answers_for_it(replay):
    view = _view(replay.now)
    out = routing.advise("Muse", kind="build", now=replay.now, view=view)
    assert out["eligible"] is True and out["verdict"] == "go"


def test_a_second_opinion_account_leads_a_check_only_among_accounts_with_room(replay):
    view = _view(replay.now, changes={"muse|": {"used": 95}})
    ranked = _ask(replay, view, kind="check")
    assert [r["label"] for r in ranked if r["verdict"] == "go"][0] != "Muse"
    view = _view(replay.now, changes={"muse|": {"state": routing.LIMITED}})
    assert _pick(_ask(replay, view, kind="check")) != "Muse"


def test_on_request_keeps_its_meaning(replay):
    providers = dict(PROVIDERS, muse=dict(PROVIDERS["muse"], use="on_request"))
    view = _view(replay.now, providers=providers)
    for question in ({"kind": "build"}, {"kind": "check"}):
        muse = next(r for r in _ask(replay, view, **question) if r["label"] == "Muse")
        assert muse["eligible"] is False and muse["why_ranked"] == "provider is on request only"


def test_a_hold_still_closes_an_account_for_every_question(replay):
    view = _view(replay.now, holds=[_no_jobs("grok|")])
    for question in ({"kind": "build"}, {"kind": "check"}, {"purpose": "orchestration"}):
        grok = next(r for r in _ask(replay, view, **question) if r["label"] == "Grok")
        assert grok["verdict"] == "closed" and grok["reason"] == "held"


# ---- `use: second_opinion` through the record --------------------------------------------------


def test_second_opinion_is_a_provider_use_the_record_accepts():
    base = {"id": "muse", "label": "Muse", "family": "Meta"}
    assert routing_providers.checked(dict(base, use="second_opinion"))["use"] == "second_opinion"
    assert routing_providers.checked(dict(base, use="on_request"))["use"] == "on_request"
    assert routing_providers.checked(dict(base, use="normal"))["use"] == "normal"
    assert routing_providers.checked(dict(base, use="second opinion")) is None
    assert routing_providers.checked(dict(base, use="reviews_only")) is None


def test_second_opinion_reads_through_the_view_and_the_provider_line(replay):
    routing.set_definition("provider", {"id": "muse", "label": "Muse", "family": "Meta",
                                        "models_allowed": ["muse-wonder-full-1.0"], "use": "second_opinion",
                                        "probe": "none", "lane_names": ["muse"]}, via="app")
    stored = routing.read_view()["definitions"]["providers"]["muse"]
    assert stored["use"] == "second_opinion"
    assert routing_providers.line(stored).endswith("use: second_opinion")
    assert any("second opinions and reviews only" in w for w in routing_providers.advisory(stored, None))


def test_second_opinion_through_the_real_fold_and_advise(replay):
    routing.set_definition("provider", {"id": "muse", "label": "Muse", "family": "Meta", "use": "second_opinion",
                                        "probe": "none", "lane_names": ["muse"]}, via="app")
    routing.set_definition("account", {"id": "muse:main", "label": "Muse", "provider": "muse"}, via="app")
    build = routing.advise(kind="build", now=replay.now)["ranked"]
    check = routing.advise(kind="check", now=replay.now)["ranked"]
    muse_build = next(r for r in build if r["label"] == "Muse")
    muse_check = next(r for r in check if r["label"] == "Muse")
    assert muse_build["eligible"] is False and muse_check["eligible"] is True
