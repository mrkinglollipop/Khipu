"""``khipu_owner_act``: an owner action by direction, asked for over MCP.

The tool runs the owner command the CLI runs, with ``--owner-direction``
(``owner_direction.gate``), so there is one check, not two. The arguments become that command's argv from
a fixed table per action: each action takes only its own keys, every value
is typed, options are passed as ``--name=value`` and positionals follow the
command words with none starting with ``-`` (no ``--``: Python 3.11's argparse
rejects it before a ``nargs="*"`` positional), so no value can become a flag (``--from-app`` above all). The
session is never an argument: the check reads it from this process's
environment, which the harness set when it started the MCP server.

Scope: docs/plans/2026-10-16-owner-direction.md, section 2.2 and slice 3.
"""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout

# action: (required keys, optional keys). Every key is text unless named in
# _LISTS (a JSON list), _INTS (a whole number) or _FLAGS (true or false).
SPECS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "route.definition.set": (("definition", "value"), ()),
    "route.pause": (("account",), ("until",)),
    "route.lift": (("account",), ()),
    "route.clear": (("account",), ("report",)),
    "route.note.set": (("text",), ()),
    "route.note.end": ((), ()),
    "route.rule.add": (("text",), ("holds",)),
    "route.rule.edit": (("rule", "text"), ()),
    "route.rule.retire": (("rule",), ("expect",)),
    "route.rule.restore": (("rule",), ()),
    "route.rule.confirm": (("rule",), ("expect",)),
    "route.rule.undo": (("rule",), ("expect",)),
    "route.rule.dismiss": (("rule",), ("expect",)),
    "route.hold.add": (("text", "reason", "scope"), ("account", "until")),
    "route.hold.end": (("hold",), ()),
    "route.cap.set": (("account", "kind"), ("window", "pct", "slack", "usd", "days", "n", "revision", "purpose", "tier", "sensitive_only", "until")),
    "route.cap.remove": (("account", "kind"), ("window", "purpose")),
    "route.priority.set": (("entries",), ("until", "expect", "holds")),
    # One account's entry only: ``append`` words to its role or ``role`` in its place (one of the two).
    # ``exception`` (with ``until`` as its end) adds a direction that lapses on its own; ``end_exception`` ends one.
    # ``confirm_pending``: the owner's one yes also confirms the conversation's request waiting in the priority,
    # as the ask showed it (``routing.pending_priority_pin``, with ``expect``); always set by the hold itself
    # when a request waits, so its ask names that request, and only a confirmed hold's replay may pass it.
    "route.priority.amend": (("account", "expect"), ("append", "role", "until", "exception", "end_exception",
                                                     "share_pct", "holds", "confirm_pending")),
    "route.priority.confirm": ((), ("expect",)),
    "route.priority.undo": ((), ("expect",)),
    "route.priority.clear": ((), ("expect",)),
    # One conversation's pace override. ``subject`` is never the caller's to name: Khipu reads it from this
    # server's own environment and T3's record and freezes it into the ask (``whole_folder``: every conversation in this folder).
    # ``helpers`` false keeps the conversations it starts out of it.
    "route.override.grant": (("account", "request"), ("whole_folder", "subject", "until", "indefinite", "helpers",
                                                       "ceiling_pct", "window", "reason", "ref")),
    "route.override.end": (("override",), ()),
    "route.override.keep": (("override",), ()),
    "route.tier_table.set": (("tiers",), ("expect",)),
    "route.tier_table.confirm": ((), ("expect",)),
    "route.tier_table.clear": ((), ("expect",)),
    "accounts.track": (("entries",), ("expect",)),
    "accounts.rename": (("account", "name"), ("expect",)),
    # ``keep_priority``: this Mac stops reading the login, and the account stays in
    # the owner's priority, which every Mac shares.
    "accounts.stop": (("account",), ("expect", "keep_priority")),
    "accounts.move": (("account", "folder"), ("expect",)),
    "accounts.decline": (("harness",), ("folder",)),
    "accounts.offer_again": (("harness",), ("folder",)),
    "accounts.auto_track": (("state",), ()),
    "accounts.request.confirm": ((), ("expect",)),
    "accounts.request.undo": ((), ("expect",)),
    "config.usage_accounts": (("list",), ("expect",)),
    "config.routing_setting": (("key",), ("value",)),
    "config.answer_request.confirm": (("key",), ("expect",)),
    "config.answer_request.undo": (("key",), ("expect",)),
    "promote.gate_ignore": (("checks",), ()),
    "promote.gate_ignore.cancel": ((), ()),
    "ledger.restore": (("id",), ("project",)),
    "ledger.purge": (("id",), ("project",)),
    "ledger.project": (("project",), ("rename", "hide", "show")),
    "ledger.add": (("id", "title", "kind"), ("project", "parent", "state", "evidence", "owner", "note", "link")),
    "ledger.set": (("id",), ("project", "state", "evidence", "owner", "note", "title", "link", "if_state")),
    "ledger.remove": (("id",), ("project", "note", "children")),
    "work.hide": (("commitment",), ()),
    "work.unhide": (("commitment",), ()),
    "work.same": (("row", "commitment"), ()),
    "work.not_same": (("row", "commitment"), ()),
    # ``why`` is the change's reason (``reason`` is a hold's, from a fixed list).
    "owed.change": (("ids", "change", "why"), ("to", "label")),
    # The clean-up: ``plan`` (a file from ``khipu owed tidy --plan-out``, shown
    # to the owner first) to apply, or ``batch`` to undo; one, not both.
    "owed.tidy": (("project",), ("plan", "batch", "ids")),
    "owed.correct_job_origin": ((), ("project",)),
    "owed.correct_job_origin.confirm": (("request",), ()),
    "project.alias_add": (("alias", "canonical"), ()),
    "project.alias_remove": (("alias",), ()),
    "project.merge": (("a", "b", "show_as"), ()),
    "project.keep_separate": (("a", "b"), ()),
    "project.separate": (("name",), ()),
    "direction.off": ((), ()),
    "direction.on": ((), ()),
    "direction.acknowledge": ((), ()),
    "direction.set": (("key", "value"), ()),
    "direction.undo": (("id",), ("holds", "over_newer", "owner_words")),
    "forget.send": ((), ()),
    "forget.here": (("id",), ()),
    "forget.not_same": (("id",), ()),
}
_LISTS = {"entries", "list", "checks", "ids"}
_HOLDS = {"holds"}
_INTS = {"rule"}
_RULE_ENTITY = re.compile(r"rule-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_FLAGS = {"keep_priority", "hide", "show", "sensitive_only", "over_newer", "indefinite", "helpers", "whole_folder"}
_CHOICES = {"scope": ("account", "machine", "all"), "children": ("remove", "move-up"),
            "change": ("close", "reopen", "reassign"), "to": ("user", "assistant", "agent"),
            "reason": ("machine-busy", "disk-low", "keep", "outage", "other")}
_DIRECTION_KEYS = ("latest_window_h", "session_binding", "trust", "untrust")


class Refused(ValueError):
    """The call does not fit its action; ``code`` and plain words."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

    def refusal(self) -> dict:
        return {"ok": False, "code": self.code, "error": str(self)}


def _bad(message: str) -> Refused:
    return Refused("bad_args", message)


def _values(action: str, args) -> dict:
    """``args`` checked against the action's keys and types."""
    if not isinstance(args, dict):
        raise _bad("args must be an object with the action's arguments")
    required, optional = SPECS[action]
    unknown = sorted(set(args) - set(required) - set(optional))
    if unknown:
        raise _bad(f"{action} does not take {', '.join(unknown)}; it takes "
                   f"{', '.join(required + optional) or 'no arguments'}")
    missing = [k for k in required if args.get(k) is None]
    if missing:
        raise _bad(f"{action} needs {', '.join(missing)}")
    # The value schema depends on this enum, regardless of JSON key order.
    if action == "route.definition.set" and args["definition"] not in ("provider", "account", "lane_map"):
        raise _bad("definition must be provider, account or lane_map")
    out: dict = {}
    for key, value in args.items():
        if value is None:
            continue
        if action == "route.override.grant" and key == "subject":
            if not isinstance(value, dict):
                raise _bad("subject must be the frozen conversation subject shown in the ask")
            value = dict(value)
        elif action == "route.override.grant" and key == "ceiling_pct":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100:
                raise _bad("ceiling_pct must be above 0 and at most 100")
        elif action == "route.tier_table.set" and key == "tiers":
            from khipu.routing_advisory import valid_tiers

            if not valid_tiers(value):
                raise _bad("tiers must name every task class with a tier of opus, sonnet or haiku")
            value = dict(value)
        elif action == "route.definition.set" and key == "value":
            from khipu import routing_entries

            value = routing_entries.checked_definition(args.get("definition"), value)
            if value is None:
                raise _bad("value must be a valid provider, account or lane_map definition")
        elif key in _LISTS:
            if key == "checks" and isinstance(value, str):
                value = [c.strip() for c in value.split(",") if c.strip()]
            if not isinstance(value, list) or not value:
                raise _bad(f"{key} must be a list")
            if key == "checks" and not all(isinstance(c, str) and c and not c.startswith("-") for c in value):
                raise _bad("checks must be the checks' names")
            if key == "ids" and not all(type(c) is int and c > 0 for c in value):
                raise _bad("ids must be the items' ids, whole numbers above 0")
        elif key in _HOLDS:
            if not isinstance(value, dict) or not all(isinstance(hold, str) and choice in ("end", "keep")
                                                      for hold, choice in value.items()):
                raise _bad('holds must map each hold id to "end" or "keep"')
            value = dict(value)
        elif key in _INTS:
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value.strip())
            named = key == "rule" and isinstance(value, str) and bool(_RULE_ENTITY.fullmatch(value.strip()))
            if named:
                # A rule added while the hub numbers rules has no number yet: its uuid names it.
                value = value.strip()
            elif type(value) is not int or value < 1:
                raise _bad(f"{key} must be a whole number above 0")
        elif key == "pct":
            if isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    raise _bad("pct must be a number above 0 and at most 100") from None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 100:
                raise _bad("pct must be a number above 0 and at most 100")
        elif action.startswith("route.cap.") and key in ("slack", "usd", "days"):
            if isinstance(value, str):
                try:
                    value = float(value)
                except ValueError:
                    raise _bad(f"{key} must be a number") from None
            low = 0 if key == "slack" else 0
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not low <= value or (key == "slack" and value > 100) or (key != "slack" and value <= 0):
                raise _bad(f"{key} is out of range for a cap")
        elif action.startswith("route.cap.") and key == "n":
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value.strip())
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise _bad("n must be a whole number at least 1")
        elif action.startswith("route.cap.") and key == "window":
            from khipu.routing_pace import CAP_WINDOW_WORDS, cap_window

            if not cap_window(value):
                raise _bad(CAP_WINDOW_WORDS)
        elif action.startswith("route.cap.") and key == "purpose" and value not in ("job", "check"):
            raise _bad("cap purpose must be job or check")
        elif action.startswith("route.cap.") and key == "kind" \
                and value not in ("share", "per_day", "pace_target", "spend", "concurrency"):
            raise _bad("kind must be share, per_day, pace_target, spend or concurrency")
        elif key in _FLAGS:
            if not isinstance(value, bool):
                raise _bad(f"{key} must be true or false")
        elif key == "value" and isinstance(value, bool) and action == "config.routing_setting":
            # A switch said as JSON: the command takes it as a word.
            value = "true" if value else "false"
        elif key == "value" and isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        elif not isinstance(value, str) or not value.strip():
            raise _bad(f"{key} must be text")
        if isinstance(value, str) and "\x00" in value:
            raise _bad(f"{key} must be text")
        choices = _CHOICES.get(key)
        if choices and value not in choices:
            raise _bad(f"{key} must be one of: {', '.join(choices)}")
        out[key] = value
    if action.startswith("route.cap."):
        from khipu.routing_pace import cap_scope_error

        error = cap_scope_error(out.get("kind"), out.get("purpose", "job"),
                                tier=out.get("tier"), sensitive_only=out.get("sensitive_only"))
        if error:
            raise _bad(error)
    if action == "route.cap.set":
        from khipu.routing_pace import _CAP_FIELDS

        used = _CAP_FIELDS.get(out.get("kind")) or set()
        extra = sorted(set(out) & {"pct", "slack", "usd", "days", "n"} - used)
        if extra:
            raise _bad(f"a {out.get('kind')} cap does not take {', '.join(extra)}")
    if action == "route.override.grant" and bool(out.get("indefinite")) == bool(out.get("until")):
        raise _bad("an override takes exactly one of until or indefinite")
    return out


def _with_frozen_subject(args) -> dict:
    """The conversation an override is for, read here from this server's own
    environment and T3's record (never from the call), and frozen into the
    ask the owner answers. ``Refused`` when Khipu can't tell who is asking."""
    from khipu import conversation_overrides

    if not isinstance(args, dict):
        raise _bad("route.override.grant takes arguments")
    if "subject" in args:
        raise _bad("the conversation an override is for is Khipu's to name, not the call's")
    subject, why = conversation_overrides.subject(scope="project" if args.get("whole_folder") is True else "conversation")
    if subject is None:
        raise Refused("unknown_conversation", why or "Khipu can't tell which conversation this is.")
    return dict(args, subject=subject)


def _opts(values: dict, *keys: str) -> list[str]:
    """``--name=value`` options: a value joined to its flag is never read as
    a flag of its own."""
    out = []
    for key in keys:
        value = values.get(key)
        if key == "sensitive_only" and value is False:
            out.append("--no-sensitive-only")
            continue
        if value is None or value is False:
            continue
        flag = "--" + key.replace("_", "-")
        if value is True:
            out.append(flag)
        else:
            out.append(f"{flag}={json.dumps(value) if isinstance(value, (dict, list)) else value}")
    return out


def _bare(value: str, key: str) -> str:
    """A value that has to stand alone in argv (``config --set``'s): never
    one that starts as a flag."""
    if value.startswith("-"):
        raise _bad(f"{key} can't start with -")
    return value


def full_argv(head: list[str], positionals: list[str], owner: list[str]) -> list[str]:
    """The whole argv: the command words, then the positionals, then every
    option. No ``--``, and the positionals stay next to the command because
    Python 3.11's argparse closes a ``nargs="*"`` positional at the first
    option, leaving later ones unrecognised. A positional that starts with
    ``-`` is refused, since nothing else keeps it from being read as a flag."""
    for value in positionals:
        _bare(value, "a value")
    n = 0
    while n < len(head) and not head[n].startswith("-"):
        n += 1
    return head[:n] + positionals + head[n:] + owner


def argv(action: str, values: dict) -> tuple[list[str], list[str]]:
    """(the command and its options, its positionals) for ``action``."""
    from khipu import routing

    v = values
    verb = action.rsplit(".", 1)[-1]
    if action == "route.definition.set":
        return ["route", "definition", "set", "--value=" + json.dumps(v["value"])], [v["definition"]]
    if action in ("route.pause", "route.lift", "route.clear"):
        return ["route", verb, *_opts(v, "until", "report")], [v["account"]]
    if action == "route.note.set":
        return ["route", "note", "set"], [v["text"]]
    if action == "route.note.end":
        return ["route", "note", "end"], []
    if action == "route.rule.add":
        return ["route", "rule", "add", *_opts(v, "holds")], [v["text"]]
    if action == "route.rule.edit":
        return ["route", "rule", "edit"], [str(v["rule"]), v["text"]]
    if action.startswith("route.rule."):
        return ["route", "rule", verb, *_opts(v, "expect")], [str(v["rule"])]
    if action == "route.hold.add":
        scope = v["scope"]
        if (scope == "account") != ("account" in v):
            raise _bad("a hold on an account names the account (scope account); machine and all take none")
        where = _opts(v, "account") if scope == "account" else [f"--{scope}"]
        return ["route", "hold", *_opts(v, "reason", "until"), *where], [v["text"]]
    if action == "route.hold.end":
        return ["route", "hold"], ["end", v["hold"]]
    if action == "route.override.grant":
        if not isinstance(v.get("subject"), dict):
            raise _bad("an override needs the conversation Khipu froze into its ask")
        return ["route", "override", "grant", "--subject=" + json.dumps(v["subject"], sort_keys=True),
                *(["--no-children"] if v.get("helpers") is False else []),
                *_opts(v, "until", "indefinite", "ceiling_pct", "window", "reason", "ref", "request")], [v["account"]]
    if action in ("route.override.end", "route.override.keep"):
        return ["route", "override", verb], [v["override"]]
    if action.startswith("route.cap."):
        return ["route", "cap", verb, *_opts(v, "window", "kind", "pct", "slack", "usd", "days", "n",
                                             "revision", "purpose", "tier", "sensitive_only", "until")], [v["account"]]
    if action == "route.priority.amend":
        if sum(k in v for k in ("append", "role", "exception", "end_exception")) != 1:
            raise _bad("route.priority.amend takes append (words added to the role), role (its new words), "
                       "exception (a direction with an end, with until) or end_exception (an exception's id), one of the four")
        return ["route", "priority", "amend",
                *_opts(v, "append", "role", "until", "exception", "end_exception", "share_pct", "holds",
                       "expect", "confirm_pending")], [v["account"]]
    if action.startswith("route.priority."):
        return ["route", "priority", verb, *_opts(v, "entries", "until", "expect", "holds")], []
    if action == "route.tier_table.set":
        return ["route", "tier-table", "set", "--tiers=" + json.dumps(v["tiers"]), *_opts(v, "expect")], []
    if action.startswith("route.tier_table."):
        return ["route", "tier-table", verb, *_opts(v, "expect")], []
    if action == "accounts.track":
        return ["accounts", "track", *_opts(v, "entries", "expect")], []
    if action in ("accounts.rename", "accounts.stop", "accounts.move"):
        return ["accounts", verb, *_opts(v, "account", "name", "folder", "expect", "keep_priority")], []
    if action in ("accounts.decline", "accounts.offer_again"):
        return ["accounts", verb.replace("_", "-"), *_opts(v, "harness", "folder")], []
    if action == "accounts.auto_track":
        if v["state"] not in ("on", "off"):
            raise _bad("state must be on or off")
        return ["accounts", "auto-track"], [v["state"]]
    if action.startswith("accounts.request."):
        return ["accounts", "request", *_opts(v, "expect")], [verb]
    if action == "config.usage_accounts":
        return ["config", "--set", "usage_accounts", json.dumps(v["list"]), *_opts(v, "expect")], []
    if action == "config.routing_setting" or action.startswith("config.answer_request."):
        # Only the routing record's settings: any other config key is not
        # an owner action this tool takes.
        if v["key"] not in routing.SETTINGS:
            raise _bad(f"{v['key']} is not a routing setting")
        if action != "config.routing_setting":
            return ["config", "--answer-request", v["key"], verb, *_opts(v, "expect")], []
        if v.get("value") is None:
            return ["config", f"--unset={v['key']}"], []
        return ["config", "--set", v["key"], _bare(v["value"], "value")], []
    if action == "promote.gate_ignore":
        return ["config", "--set", "promote_gate_ignore", _bare(",".join(v["checks"]), "checks")], []
    if action == "promote.gate_ignore.cancel":
        return ["config", "--unset=promote_gate_ignore"], []
    if action == "ledger.project":
        if v.get("hide") and v.get("show"):
            raise _bad("a project is hidden or shown, not both")
        return ["ledger", "project", "--json", *_opts(v, "project", "rename", "hide", "show")], []
    if action.startswith("ledger."):
        keys = ("project", "title", "kind", "parent", "state", "evidence", "owner", "note", "link",
                "if_state", "children")
        return ["ledger", verb, "--json", *_opts(v, *keys)], [v["id"]]
    if action in ("work.hide", "work.unhide"):
        return ["work", verb], [v["commitment"]]
    if action == "owed.change":
        if (v["change"] == "reassign") != ("to" in v):
            raise _bad("a reassign names the new owner (to); close and reopen take none")
        return ["owed", f"--{v['change']}={','.join(str(c) for c in v['ids'])}", f"--reason={v['why']}",
                *_opts(v, "to", "label")], []
    if action == "owed.correct_job_origin":
        return ["owed", "correct-job-origin", "--request", *_opts(v, "project")], []
    if action == "owed.correct_job_origin.confirm":
        return ["owed", "correct-job-origin", f"--confirm={v['request']}"], []
    if action == "owed.tidy":
        if ("plan" in v) == ("batch" in v):
            raise _bad("owed.tidy names the plan to apply or the batch to undo, one of the two")
        if "ids" in v and "batch" not in v:
            raise _bad("owed.tidy ids select items to undo and require a batch")
        return ["owed", "tidy", *_opts(v, "project"), f"--apply={v['plan']}" if "plan" in v
                else f"--undo={v['batch']}", *([f"--ids={','.join(str(c) for c in v['ids'])}"]
                                             if "ids" in v else [])], []
    if action in ("work.same", "work.not_same"):
        return ["work", verb.replace("_", "-")], [v["row"], v["commitment"]]
    if action.startswith("project."):
        # Names are checked here as the CLI checks them: a refusal before the gate.
        from khipu import project_group

        why = project_group.check_names([v[k] for k in ("alias", "canonical", "a", "b", "name", "show_as") if k in v])
        if why:
            raise _bad(why)
    if action == "project.alias_add":
        return ["project", "alias", "add"], [v["alias"], v["canonical"]]
    if action == "project.alias_remove":
        return ["project", "alias", "remove"], [v["alias"]]
    if action == "project.merge":
        return ["project", "merge", *_opts(v, "show_as")], [v["a"], v["b"]]
    if action == "project.keep_separate":
        return ["project", "keep-separate"], [v["a"], v["b"]]
    if action == "project.separate":
        return ["project", "separate"], [v["name"]]
    if action in ("direction.off", "direction.on", "direction.acknowledge"):
        return ["owner-direction", action.partition(".")[2]], []
    if action == "direction.set":
        if v["key"] not in _DIRECTION_KEYS:
            raise _bad(f"key must be one of: {', '.join(_DIRECTION_KEYS)}")
        return ["owner-direction", "set"], [v["key"], v["value"]]
    if action == "direction.undo":
        return ["owner-direction", "undo", *_opts(v, "holds", "over_newer", "owner_words")], [v["id"]]
    if action == "forget.send":
        return ["episode", "forget-pending", "--send"], []
    if action == "forget.here":
        return ["episode", "forget-pending", f"--forget-here={v['id']}"], []
    if action == "forget.not_same":
        return ["episode", "forget-pending", f"--not-same={v['id']}"], []
    raise Refused("not_by_direction", "That change can't be made by direction.")


def owner_turn() -> bool:
    """Whether this call comes from a conversation the owner typed in, the
    only place a forgotten episode's summary may be shown (choosing
    forget.here or forget.not_same). Any doubt answers no."""
    from khipu import owner_direction

    try:
        owner_direction.check("forget.here", reading=True)
    except Exception:  # noqa: BLE001 - any doubt keeps the text back
        return False
    return True


def without_summaries(out: dict) -> dict:
    """``unsent_forgets`` output with each listed id kept and every other
    item field (the summary) dropped; an item that is not an id entry is
    dropped whole."""
    return dict(out, ids=[{"id": item.get("id")} for item in out.get("ids") or [] if isinstance(item, dict)])


def _forget_list() -> dict:
    from khipu import forget

    out = forget.unsent_forgets()
    return out if owner_turn() else without_summaries(out)


# Actions that only read, answered without refusing: the forgets this Mac
# holds that the hub lacks (design 6.2), each bare id, with its hub summary
# only when the owner-direction check passes, so the owner can choose
# forget.here or forget.not_same. The app reads its list through
# ``khipu episode forget-pending`` instead.
def _overrides_mine() -> dict:
    """The override that applies to this conversation, if any: what it hears
    about itself and nothing about anyone else's."""
    from khipu import conversation_overrides as overrides, routing

    who = overrides.resolve()
    if who["subject"] is None:
        return {"ok": True, "available": False, "reason": who["reason"], "overrides": []}
    now = routing._now()
    return {"ok": True, "available": True, "overrides": [overrides.describe(r, now) | {"account": r["account"]}
                                                          for r in overrides.matches(who, now).values()]}


def _walkthrough(leave=None, keep=None, restart=False) -> dict:
    """One clean-up item at a time (``priority_cleanup``). Only a
    conversation the owner is typing in is shown the items themselves."""
    from khipu import conversation_overrides as overrides

    if not owner_turn():
        return {"ok": True, "available": False,
                "line": "Only a conversation the owner is typing in can walk through the clean-up."}
    return overrides.walkthrough(leave=leave, keep_pair=keep, restart=restart is True)


_READS = {"forget.list": _forget_list, "route.overrides.mine": _overrides_mine}
# Reads that take a few arguments (the walk-through's bookmark: leave an item, keep a pair, start over).
_ARG_READS = {"route.walkthrough": (_walkthrough, ("leave", "keep", "restart"))}


def _parsed(text: str) -> dict:
    text = text.strip()
    try:
        out = json.loads(text)
        return out if isinstance(out, dict) else {"result": out}
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        try:
            out = json.loads(line)
        except ValueError:
            continue
        if isinstance(out, dict):
            return out
    start = text.find("{")
    if start >= 0:
        try:
            out = json.loads(text[start:])
            if isinstance(out, dict):
                return out
        except ValueError:
            pass
    return {"text": text}


def act(action, args) -> tuple[bool, dict]:
    """Run one owner action by direction. (ok, the command's JSON answer),
    or (False, ``{ok: false, code, error}``) when refused."""
    from khipu import owner_direction

    if isinstance(action, str) and action in _READS:
        # An inspection: never a change, a send or a direction check.
        if not isinstance(args, dict) or args:
            return False, _bad(f"{action} takes no arguments").refusal()
        return True, _READS[action]()
    if isinstance(action, str) and action in _ARG_READS:
        reader, allowed = _ARG_READS[action]
        if not isinstance(args, dict) or set(args) - set(allowed):
            return False, _bad(f"{action} takes only {', '.join(allowed)}").refusal()
        return True, reader(**args)
    if action == CONFIRM:
        return _confirm(args)
    if action == DECLINE:
        return _decline(args)
    held = action == REQUEST
    if held:
        if not isinstance(args, dict) or set(args) - {"action", "args"} or not isinstance(args.get("action"), str):
            return False, _bad(f"{REQUEST} takes action and args, the change to hold").refusal()
        action, args = args["action"], args.get("args") or {}
    if not isinstance(action, str) or action not in owner_direction.ACTIONS or action not in SPECS:
        return False, {"ok": False, "code": "not_by_direction", "error": "That change can't be made by direction."}
    pending = None
    try:
        if action == "route.override.grant":
            args = _with_frozen_subject(args)
        values = _values(action, args)
        pending = _priority_pending(action, values, asked=held)
        if pending is None and not owner_direction.REQUEST_APPLIED.get():
            from khipu import needs_you

            pending = needs_you.prepare_answer(action, values)
        if pending is not None:
            values = pending[0]
        head, positionals = argv(action, values)
        full = full_argv(head, positionals, ["--owner-direction"])
    except Refused as err:
        return False, err.refusal()
    if action in owner_direction.PLANNED_ASKS or owner_direction.class_c(action, values):
        # Class C, or a clean-up an agent planned: always held, with its own words.
        from khipu import needs_you

        ask = owner_direction.PLANNED_ASKS.get(action) or needs_you.ask_for(action, values)
        return _held_on_a_read(_hold(action, values, ask=ask))
    token = owner_direction.ANSWERING.set(owner_direction.signature(action, values))
    try:
        proof = None
        if not held:
            try:
                proof = owner_direction.check(action)
            except owner_direction.DirectionRefused as refused:
                if refused.code == "not_confirmed":
                    return False, refused.refusal()  # the turn that read the list: no hold, nothing to ask yet
                # An answer the owner has not been shown here is held and asked;
                # anything else runs the command, which refuses as before.
                held = refused.code == "confirm_needed"
                if pending is not None and not held:
                    return False, refused.refusal()
        if held:
            if pending is not None:
                return _held_on_a_read(_hold(action, values, ask=pending[1]))
            from khipu import needs_you

            return _held_on_a_read(_hold(action, values, ask=needs_you.ask_for(action, values)))
        from khipu import owner_background

        if proof and owner_background.is_long(action, values):
            return owner_background.start_authorized(action, values, proof)
        return _run(action, full)
    finally:
        owner_direction.ANSWERING.reset(token)


def _held_on_a_read(hold: tuple[bool, dict]) -> tuple[bool, dict]:
    """A hold that started from the read that showed it (``owner_direction._seed``):
    when the owner already replied to that read, this is his one yes, on the
    confirm's own proof. With no reply it stays held, and says what to ask."""
    ok, out = hold
    if not ok or not out.pop("seeded", False):
        return hold
    done_ok, done = _confirm({"request": out["request"]})
    if done_ok or done.get("code") != "not_confirmed":
        return done_ok, done
    return True, out


def _priority_pending(action: str, values: dict, *, asked: bool) -> tuple[dict, str] | None:
    """For a priority amend the agent holds (``direction.request``) while a
    conversation's request waits in the priority: (its values with
    ``confirm_pending``, the pin of the request and the priority it
    replaced as shown, the ask naming every
    change that request makes the owner's and the amendment), so one yes
    settles both. The amend's ``expect`` must be the version the ask shows:
    a newer request is refused at the confirm, never confirmed unseen.
    ``Refused`` naming route.priority.confirm and route.priority.undo when
    the request can't be shown whole in the ask. An amend made at once
    (``asked`` false) never takes the waiting request live with it: refused
    naming the settles, and the request stays waiting. None otherwise."""
    if action != "route.priority.amend":
        return None
    from khipu import routing

    view = routing.read_view()
    held = view.get("priority") or {}
    version = view.get("priority_version") or "p0"
    if not held.get("pending"):
        if values.get("confirm_pending"):
            raise _bad("confirm_pending is for a priority with a conversation's request waiting, and none is")
        return None
    if str(values.get("expect") or "").strip() != version:
        raise Refused("version_moved", f"the priority changed after it was read (it is now version {version}); "
                                       "read it again")
    if not asked:
        raise Refused("priority_pending", routing.pending_amend_refusal(version))
    changes = routing.pending_priority_changes(view)
    if held.get("proposal_withheld") or not changes:
        raise Refused("priority_pending", routing.pending_amend_refusal(version, combine=False))
    unnamed = routing.pending_priority_unnamed(view)
    if unnamed:
        # The yes would confirm what the ask can't show: never combined.
        raise Refused("priority_pending", routing.pending_amend_refusal(version, combine=False)
                      + f"; it changes {', '.join(unnamed)} in a way the ask can't put in words, so the amend "
                        "can't confirm it too")
    # A conversation's words stay in their quotes (``their_words``).
    words = "; ".join(routing.their_words(c) for c in changes)
    from khipu import owner_direction

    asked_as = held.get("owner_request")
    amend = owner_direction.ask_line(action, {k: v for k, v in values.items() if k not in ("confirm_pending", "expect")})
    ask = (f"A priority request from {held.get('pending_by') or 'a conversation'} is waiting for you (version "
           f"{version}" + (f", asked as \"{routing.their_words(asked_as)}\"" if asked_as else "") + f"). Confirming it makes these yours: "
           f"{words}. Confirm that request, then " + amend[len("Confirm "):])
    # Whatever the caller passed, the pin is the request as this ask shows it.
    return dict(values, confirm_pending=routing.pending_priority_pin(view)), ask


def apply_request(kind: str, out, *, only: str | None = None) -> dict:
    """A conversation's request (``khipu_route_priority``, ``khipu_route_rule``)
    taken on the owner's own typed turn applies at once: the same confirm the
    owner would give (``route.priority.confirm`` / ``route.rule.confirm`` by
    direction), so it is logged with the evidence it relied on, receipted to
    every conversation and undone in one step. A call that does not pass the
    owner check (a dispatched job, a headless turn, a handoff, a T3 turn
    without proof) leaves the request pending exactly as before; so does a
    crossed or refused request. ``out`` is the request's answer.

    ``only`` (an amend of one account's entry): the amend rebuilds the whole
    stored priority, so it also carries whatever another conversation left
    pending in the other entries. It applies at once only when the request
    would make live nothing but that account's own entry: every other entry
    must equal the owner's confirmed priority it replaced. Otherwise it is
    left pending, as before."""
    from khipu import owner_direction, routing

    if not isinstance(out, dict) or out.get("ok") is False:
        return out
    try:
        if kind == "priority":
            view = routing.read_view()
            stored = view.get("priority_stored") or {}
            mine = routing.conversation_author().get("session")
            # Only this conversation's own request: a newer one from another
            # conversation that landed in between is never confirmed unseen.
            if not stored.get("pending") or not mine or (stored.get("written_by") or {}).get("session") != mine:
                return out
            if only is not None:
                owned = (stored.get("replaced") or {}).get("entries")
                try:
                    key = routing._resolve_account_key(only)
                except routing.RouteError:
                    return out
                if not isinstance(owned, list):
                    return out
                was = {e.get("account"): e for e in owned}
                now_ = {e.get("account"): e for e in stored.get("entries") or []}
                if any(was.get(a) != now_.get(a) for a in set(was) | set(now_) if a != key):
                    return out
            action, args = "route.priority.confirm", {"expect": routing.pending_priority_pin(view)}
        else:
            number, entity = out.get("n"), out.get("entity")
            # A rule written while the hub numbers rules has no number until a sync
            # brings it: it is found, and named in the confirm, by its uuid.
            rule = next((r for r in routing.read_view().get("rules") or []
                         if (r["n"] == number if type(number) is int else r.get("entity") == entity)), None)
            mine = routing.conversation_author().get("session")
            if (rule is None or not rule.get("pending") or not mine
                    or (rule.get("pending_author") or {}).get("session") != mine
                    or (type(number) is not int and not isinstance(entity, str))):
                return out
            action, args = "route.rule.confirm", {"rule": number if type(number) is int else entity,
                                                  "expect": rule["revision"]}
        # Never a hold: a turn that cannot be proved owner's is left alone.
        checking = owner_direction.REQUEST_APPLIED.set(True)
        try:
            owner_direction.check(action)
        finally:
            owner_direction.REQUEST_APPLIED.reset(checking)
    except (owner_direction.DirectionRefused, routing.RouteError, StopIteration, KeyError):
        return out
    token = owner_direction.REQUEST_APPLIED.set(True)
    try:
        ok, result = act(action, args)
    finally:
        owner_direction.REQUEST_APPLIED.reset(token)
    if not ok:
        return dict(out, instant={"applied": False, "code": result.get("code"), "error": result.get("error")})
    line = result.get("owner_action")
    return dict(out, pending=False, applied=True,
                instant={"applied": True, "owner_action": line,
                         "undo": f"khipu owner-direction undo {line}" if line else None})


# A change held for the owner to confirm, and its confirmation
# (``owner_direction.hold`` / ``confirm``). Not owner actions themselves:
# a hold changes nothing, and a confirm applies the held action, which is
# logged as that action with the confirming message named. A decline drops a
# held request on the owner's no; nothing was changed, so nothing is undone.
REQUEST, CONFIRM, DECLINE = "direction.request", "direction.confirm", "direction.decline"


def _hold(action: str, values: dict, *, ask: str | None = None) -> tuple[bool, dict]:
    from khipu import owner_direction

    try:
        request = owner_direction.hold(action, values, ask=ask)
    except owner_direction.DirectionRefused as refused:
        return False, refused.refusal()
    return True, {"ok": True, "applied": False, "pending": True, "request": request["id"], "ask": request["ask"],
                  **({"seeded": True} if request.get("seeded") else {}),
                  "line": (f"Nothing is changed yet. Ask the owner: \"{request['ask']}\" Once they answer in this "
                           f"conversation, call khipu_owner_act with {CONFIRM} and "
                           f"{{\"request\": \"{request['id']}\"}}; if they say no, {DECLINE} with the same "
                           "request. In another conversation, read the full held item first, then the owner can answer once.")}


def _confirm(args, *, terminal: bool = False) -> tuple[bool, dict]:
    from khipu import owner_direction

    if not isinstance(args, dict) or set(args) != {"request"} or not isinstance(args["request"], str):
        return False, _bad(f"{CONFIRM} takes request, the held request's id").refusal()
    settled = _settled_elsewhere(args["request"])
    if settled:
        return True, settled
    try:
        request, stamp = owner_direction.confirm(args["request"], terminal=terminal)
    except owner_direction.DirectionRefused as refused:
        return False, refused.refusal()
    if request.get("remote"):
        return _answered_for_another_mac(request, stamp, "confirm")
    from khipu import owner_background

    if owner_background.is_long(request["action"], request.get("args") or {}):
        return owner_background.start(request)
    return _apply_confirmed(request, stamp)


def _settled_elsewhere(request_id: str) -> dict | None:
    """A request another Mac holds and already settled: the owner's answer here
    changes nothing and is not sent (``routing_replication.settled_elsewhere``)."""
    if not request_id.startswith("rm-"):
        return None
    from khipu import routing_replication

    return routing_replication.settled_elsewhere(request_id)


def _apply_confirmed(request: dict, stamp: dict) -> tuple[bool, dict]:
    ok, result = apply_confirmed(request, stamp)
    return ok, dict(result or {}, request=request["id"], confirmed=stamp["confirmed"])


def apply_confirmed(request: dict, stamp: dict) -> tuple[bool, dict | None]:
    """Run a request ``owner_direction.confirm`` claimed (state ``applying``)
    and record how it ended. Used for the owner's yes typed here and for one
    typed on another Mac (``routing_replication.apply_remote_answers``)."""
    from khipu import owner_direction

    action, ok, result = request["action"], False, None
    try:
        head, positionals = argv(action, _values(action, request.get("args") or {}))
        full = full_argv(head, positionals, ["--owner-direction"])
        with owner_direction.confirmed_replay(action, stamp, files=request.get("files")):
            ok, result = _run(action, full)
    except Refused as err:
        result = err.refusal()
    finally:
        try:
            owner_direction.settle(request["id"], ok, result)
        except OSError:
            pass
    return ok, result


def _answered_for_another_mac(request: dict, stamp: dict, answer: str) -> tuple[bool, dict]:
    """The owner answered, here, a request another of his Macs is holding:
    send his answer through the routing record, where that Mac applies it."""
    from khipu import owner_direction, routing_replication

    sent = False
    try:
        sent = routing_replication.answer_remote(request, stamp, answer)
    except Exception:  # noqa: BLE001 - said plainly below; the row stays answerable
        sent = False
    try:
        owner_direction.settle(request["id"], sent, {"code": "not_sent"}, state_name="answered" if sent else None)
    except OSError:
        pass
    if not sent:
        return False, {"ok": False, "code": "not_sent", "request": request["id"],
                       "error": "Khipu could not record your answer to send to your other Mac. Nothing was changed."}
    return True, {"ok": True, "applied": False, "sent": True, "request": request["id"],
                  "line": "Your answer is on its way to the other Mac, which applies it within a few minutes."}


def _decline(args, *, terminal: bool = False) -> tuple[bool, dict]:
    from khipu import owner_direction

    if not isinstance(args, dict) or set(args) != {"request"} or not isinstance(args["request"], str):
        return False, _bad(f"{DECLINE} takes request, the held request's id").refusal()
    settled = _settled_elsewhere(args["request"])
    if settled:
        return True, settled
    try:
        request = owner_direction.decline(args["request"], terminal=terminal)
    except owner_direction.DirectionRefused as refused:
        return False, refused.refusal()
    if request.get("remote"):
        from khipu import routing_replication

        try:
            routing_replication.answer_remote(request, None, "decline")
        except Exception:  # noqa: BLE001 - the no is recorded here; the other Mac's request ends by itself
            pass
        return True, {"ok": True, "declined": True, "applied": False, "sent": True, "request": request["id"],
                      "line": "Your no is on its way to the other Mac, which drops the request. Nothing was changed.",
                      "owner_action": request.get("owner_action"), "receipt": request.get("receipt")}
    return True, {"ok": True, "declined": True, "applied": False, "request": request["id"],
                  "line": "The held request is dropped. Nothing was changed.",
                  "owner_action": request.get("owner_action"), "receipt": request.get("receipt")}


def settle_terminal(action: str, args: dict) -> tuple[bool, dict]:
    """Settle one fully displayed Needs-you item after ``typed_yes`` proved a
    direct terminal.  This follows the same hold/replay path as the app and
    MCP; only terminal proof replaces a conversation's recorded reply."""
    if action in (CONFIRM, DECLINE):
        request = args.get("request") if isinstance(args, dict) else None
        if not isinstance(request, str):
            return False, _bad("the held request is missing").refusal()
        return (_confirm({"request": request}, terminal=True) if action == CONFIRM
                else _decline({"request": request}, terminal=True))
    from khipu import needs_you, owner_direction

    try:
        full = _values(action, args)
        prepared = needs_you.prepare_answer(action, full)
        if prepared:
            full, ask = prepared
        else:
            ask = None
        held = owner_direction.hold(action, full, ask=ask, terminal=True)
    except (owner_direction.DirectionRefused, Refused) as refused:
        return False, refused.refusal()
    return _confirm({"request": held["id"]}, terminal=True)


def _run(action: str, full: list[str]) -> tuple[bool, dict]:
    from khipu import cli

    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            ns = cli.build_parser().parse_args(full)
        except SystemExit:
            return False, {"ok": False, "code": "bad_args", "error": f"Those arguments don't fit {action}."}
        # The command reaches its owner gate only through the direction branch.
        if (getattr(ns, "from_app", False) or getattr(ns, "at_owner_request", None) is not None
                or getattr(ns, "owner", None) is True or getattr(ns, "dry_run", False) or ns.by_direction is not True):
            return False, {"ok": False, "code": "bad_args", "error": f"Those arguments don't fit {action}."}
        rc = int(ns.func(ns) or 0)
    result = _parsed(out.getvalue())
    if rc != 0 or result.get("ok") is False:
        result = dict(result, ok=False)
        result.setdefault("code", result.get("reason") or "refused")
        result.setdefault("error", err.getvalue().strip() or "The change was refused.")
        return False, result
    return True, result
