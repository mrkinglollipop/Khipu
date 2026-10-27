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
    "route.rule.retire": (("rule",), ()),
    "route.rule.restore": (("rule",), ()),
    "route.rule.confirm": (("rule",), ("expect",)),
    "route.rule.undo": (("rule",), ()),
    "route.rule.dismiss": (("rule",), ()),
    "route.hold.add": (("text", "reason", "scope"), ("account", "until")),
    "route.hold.end": (("hold",), ()),
    "route.cap.set": (("account", "kind"), ("window", "pct", "slack", "usd", "days", "n", "revision", "purpose", "tier", "sensitive_only", "until")),
    "route.cap.remove": (("account", "kind"), ("window", "purpose")),
    "route.priority.set": (("entries",), ("until", "expect", "holds")),
    # One account's entry only: ``append`` words to its role or ``role`` in its place (one of the two).
    # ``exception`` (with ``until`` as its end) adds a direction that lapses on its own; ``end_exception`` ends one.
    "route.priority.amend": (("account", "expect"), ("append", "role", "until", "exception", "end_exception", "holds")),
    "route.priority.confirm": ((), ("expect",)),
    "route.priority.undo": ((), ("expect",)),
    "route.priority.clear": ((), ("expect",)),
    "route.tier_table.set": (("tiers",), ()),
    "route.tier_table.confirm": ((), ()),
    "route.tier_table.clear": ((), ()),
    "accounts.track": (("entries",), ("expect",)),
    "accounts.rename": (("account", "name"), ("expect",)),
    "accounts.stop": (("account",), ("expect",)),
    "accounts.move": (("account", "folder"), ("expect",)),
    "accounts.decline": (("harness",), ("folder",)),
    "accounts.offer_again": (("harness",), ("folder",)),
    "accounts.auto_track": (("state",), ()),
    "accounts.request.confirm": ((), ("expect",)),
    "accounts.request.undo": ((), ("expect",)),
    "config.usage_accounts": (("list",), ("expect",)),
    "config.routing_setting": (("key",), ("value",)),
    "config.answer_request.confirm": (("key",), ()),
    "config.answer_request.undo": (("key",), ()),
    "promote.gate_ignore": (("checks",), ()),
    "promote.gate_ignore.cancel": ((), ()),
    "ledger.import": (("file",), ("project", "catch_up")),
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
    "owed.tidy": (("project",), ("plan", "batch")),
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
    # Undone through direction.undo with the id this returns.
    "outbox.merge_preferences": ((), ()),
}
_LISTS = {"entries", "list", "checks", "ids"}
_HOLDS = {"holds"}
_INTS = {"rule"}
_FLAGS = {"catch_up", "hide", "show", "sensitive_only", "over_newer"}
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
        if action == "route.tier_table.set" and key == "tiers":
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
            if type(value) is not int or value < 1:
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
        elif action.startswith("route.cap.") and key == "window" and value not in ("weekly", "five_hour"):
            raise _bad("window must be weekly or five_hour")
        elif action.startswith("route.cap.") and key == "purpose" and value not in ("job", "check"):
            raise _bad("cap purpose must be job or check")
        elif action.startswith("route.cap.") and key == "kind" \
                and value not in ("share", "per_day", "pace_target", "spend", "concurrency"):
            raise _bad("kind must be share, per_day, pace_target, spend or concurrency")
        elif key in _FLAGS:
            if not isinstance(value, bool):
                raise _bad(f"{key} must be true or false")
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
    return out


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
    if action.startswith("route.cap."):
        return ["route", "cap", verb, *_opts(v, "window", "kind", "pct", "slack", "usd", "days", "n",
                                             "revision", "purpose", "tier", "sensitive_only", "until")], [v["account"]]
    if action == "route.priority.amend":
        if sum(k in v for k in ("append", "role", "exception", "end_exception")) != 1:
            raise _bad("route.priority.amend takes append (words added to the role), role (its new words), "
                       "exception (a direction with an end, with until) or end_exception (an exception's id), one of the four")
        return ["route", "priority", "amend",
                *_opts(v, "append", "role", "until", "exception", "end_exception", "holds", "expect")], [v["account"]]
    if action.startswith("route.priority."):
        return ["route", "priority", verb, *_opts(v, "entries", "until", "expect", "holds")], []
    if action == "route.tier_table.set":
        return ["route", "tier-table", "set", "--tiers=" + json.dumps(v["tiers"])], []
    if action.startswith("route.tier_table."):
        return ["route", "tier-table", verb], []
    if action == "accounts.track":
        return ["accounts", "track", *_opts(v, "entries", "expect")], []
    if action in ("accounts.rename", "accounts.stop", "accounts.move"):
        return ["accounts", verb, *_opts(v, "account", "name", "folder", "expect")], []
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
            return ["config", "--answer-request", v["key"], verb], []
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
        keys = ("project", "catch_up", "title", "kind", "parent", "state", "evidence", "owner", "note", "link",
                "if_state", "children")
        return ["ledger", verb, "--json", *_opts(v, *keys)], [v["file"] if verb == "import" else v["id"]]
    if action in ("work.hide", "work.unhide"):
        return ["work", verb], [v["commitment"]]
    if action == "owed.change":
        if (v["change"] == "reassign") != ("to" in v):
            raise _bad("a reassign names the new owner (to); close and reopen take none")
        return ["owed", f"--{v['change']}={','.join(str(c) for c in v['ids'])}", f"--reason={v['why']}",
                *_opts(v, "to", "label")], []
    if action == "owed.tidy":
        if ("plan" in v) == ("batch" in v):
            raise _bad("owed.tidy names the plan to apply or the batch to undo, one of the two")
        return ["owed", "tidy", *_opts(v, "project"), f"--apply={v['plan']}" if "plan" in v
                else f"--undo={v['batch']}"], []
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
    if action == "outbox.merge_preferences":
        return ["outbox", "merge-line-preferences", "--apply"], []
    raise Refused("not_by_direction", "That change can't be made by direction.")


def owner_turn() -> bool:
    """Whether this call comes from a conversation the owner typed in, the
    only place a forgotten episode's summary may be shown (choosing
    forget.here or forget.not_same). Any doubt answers no."""
    from khipu import owner_direction

    try:
        owner_direction.check("forget.here")
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
_READS = {"forget.list": _forget_list}


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
    from khipu import cli, owner_direction

    if isinstance(action, str) and action in _READS:
        # An inspection: never a change, a send or a direction check.
        if not isinstance(args, dict) or args:
            return False, _bad(f"{action} takes no arguments").refusal()
        return True, _READS[action]()
    if not isinstance(action, str) or action not in owner_direction.ACTIONS or action not in SPECS:
        return False, {"ok": False, "code": "not_by_direction", "error": "That change can't be made by direction."}
    try:
        head, positionals = argv(action, _values(action, args))
    except Refused as err:
        return False, err.refusal()
    try:
        full = full_argv(head, positionals, ["--owner-direction"])
    except Refused as err:
        return False, err.refusal()
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
