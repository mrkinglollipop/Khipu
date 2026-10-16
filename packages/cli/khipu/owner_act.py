"""``khipu_owner_act``: an owner action by direction, asked for over MCP.

The tool runs the owner command the CLI runs, with ``--owner-words``,
``--owner-turn`` and ``--owner-session`` (``owner_direction.gate``), so
there is one check, not two. The arguments become that command's argv from
a fixed table per action: each action takes only its own keys, every value
is typed, options are passed as ``--name=value`` and positionals after
``--``, so no value can become a flag (``--from-app`` above all). The
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
    "route.pause": (("account",), ("until",)),
    "route.lift": (("account",), ()),
    "route.clear": (("account",), ("report",)),
    "route.note.set": (("text",), ()),
    "route.note.end": ((), ()),
    "route.rule.add": (("text",), ()),
    "route.rule.edit": (("rule", "text"), ()),
    "route.rule.retire": (("rule",), ()),
    "route.rule.restore": (("rule",), ()),
    "route.rule.confirm": (("rule",), ()),
    "route.rule.undo": (("rule",), ()),
    "route.rule.dismiss": (("rule",), ()),
    "route.hold.add": (("text", "reason", "scope"), ("account", "until")),
    "route.hold.end": (("hold",), ()),
    "route.priority.set": (("entries",), ("until", "expect")),
    "route.priority.confirm": ((), ("expect",)),
    "route.priority.undo": ((), ("expect",)),
    "route.priority.clear": ((), ("expect",)),
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
    "ledger.add": (("id", "title", "kind"), ("project", "parent", "state", "evidence", "owner", "note", "link")),
    "ledger.set": (("id",), ("project", "state", "evidence", "owner", "note", "title", "link", "if_state")),
    "ledger.remove": (("id",), ("project", "note", "children")),
    "work.hide": (("commitment",), ()),
    "work.unhide": (("commitment",), ()),
    "work.same": (("row", "commitment"), ()),
    "work.not_same": (("row", "commitment"), ()),
    "direction.off": ((), ()),
    "direction.set": (("key", "value"), ()),
    "direction.undo": (("id",), ()),
}
_LISTS = {"entries", "list", "checks"}
_INTS = {"rule"}
_FLAGS = {"catch_up"}
_CHOICES = {"scope": ("account", "machine", "all"), "children": ("remove", "move-up"),
            "reason": ("machine-busy", "disk-low", "keep", "outage", "other")}
_DIRECTION_KEYS = ("latest_window_h", "earlier_window_min", "session_binding", "trust", "untrust")


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
    out: dict = {}
    for key, value in args.items():
        if value is None:
            continue
        if key in _LISTS:
            if key == "checks" and isinstance(value, str):
                value = [c.strip() for c in value.split(",") if c.strip()]
            if not isinstance(value, list) or not value:
                raise _bad(f"{key} must be a list")
            if key == "checks" and not all(isinstance(c, str) and c and not c.startswith("-") for c in value):
                raise _bad("checks must be the checks' names")
        elif key in _INTS:
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value.strip())
            if type(value) is not int or value < 1:
                raise _bad(f"{key} must be a whole number above 0")
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
    return out


def _opts(values: dict, *keys: str) -> list[str]:
    """``--name=value`` options: a value joined to its flag is never read as
    a flag of its own."""
    out = []
    for key in keys:
        value = values.get(key)
        if value is None or value is False:
            continue
        flag = "--" + key.replace("_", "-")
        if value is True:
            out.append(flag)
        else:
            out.append(f"{flag}={json.dumps(value) if isinstance(value, list) else value}")
    return out


def _bare(value: str, key: str) -> str:
    """A value that has to stand alone in argv (``config --set``'s): never
    one that starts as a flag."""
    if value.startswith("-"):
        raise _bad(f"{key} can't start with -")
    return value


def argv(action: str, values: dict) -> tuple[list[str], list[str]]:
    """(the command and its options, its positionals) for ``action``."""
    from khipu import routing

    v = values
    verb = action.rsplit(".", 1)[-1]
    if action in ("route.pause", "route.lift", "route.clear"):
        return ["route", verb, *_opts(v, "until", "report")], [v["account"]]
    if action == "route.note.set":
        return ["route", "note", "set"], [v["text"]]
    if action == "route.note.end":
        return ["route", "note", "end"], []
    if action == "route.rule.add":
        return ["route", "rule", "add"], [v["text"]]
    if action == "route.rule.edit":
        return ["route", "rule", "edit"], [str(v["rule"]), v["text"]]
    if action.startswith("route.rule."):
        return ["route", "rule", verb], [str(v["rule"])]
    if action == "route.hold.add":
        scope = v["scope"]
        if (scope == "account") != ("account" in v):
            raise _bad("a hold on an account names the account (scope account); machine and all take none")
        where = _opts(v, "account") if scope == "account" else [f"--{scope}"]
        return ["route", "hold", *_opts(v, "reason", "until"), *where], [v["text"]]
    if action == "route.hold.end":
        return ["route", "hold"], ["end", v["hold"]]
    if action.startswith("route.priority."):
        return ["route", "priority", verb, *_opts(v, "entries", "until", "expect")], []
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
    if action.startswith("ledger."):
        keys = ("project", "catch_up", "title", "kind", "parent", "state", "evidence", "owner", "note", "link",
                "if_state", "children")
        return ["ledger", verb, "--json", *_opts(v, *keys)], [v["file"] if verb == "import" else v["id"]]
    if action in ("work.hide", "work.unhide"):
        return ["work", verb], [v["commitment"]]
    if action in ("work.same", "work.not_same"):
        return ["work", verb.replace("_", "-")], [v["row"], v["commitment"]]
    if action == "direction.off":
        return ["owner-direction", "off"], []
    if action == "direction.set":
        if v["key"] not in _DIRECTION_KEYS:
            raise _bad(f"key must be one of: {', '.join(_DIRECTION_KEYS)}")
        return ["owner-direction", "set"], [v["key"], v["value"]]
    if action == "direction.undo":
        return ["owner-direction", "undo"], [v["id"]]
    raise Refused("not_by_direction", "That change can't be made by direction.")


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


def act(action, args, words, turn=None, owner_session=None) -> tuple[bool, dict]:
    """Run one owner action by direction. (ok, the command's JSON answer),
    or (False, ``{ok: false, code, error}``) when refused."""
    from khipu import cli, owner_direction

    if not isinstance(action, str) or action not in owner_direction.ACTIONS or action not in SPECS:
        return False, {"ok": False, "code": "not_by_direction", "error": "That change can't be made by direction."}
    if not isinstance(words, str) or not words.strip():
        return False, {"ok": False, "code": "not_the_owners_words",
                       "error": "words is needed: the owner's words, quoted exactly from their message."}
    for name, value in (("turn", turn), ("owner_session", owner_session)):
        if value is not None and (not isinstance(value, str) or not value.strip() or "\x00" in value):
            return False, {"ok": False, "code": "bad_args", "error": f"{name} must be text"}
    try:
        head, positionals = argv(action, _values(action, args))
    except Refused as err:
        return False, err.refusal()
    owner = [f"--owner-words={words}"]
    if turn is not None:
        owner.append(f"--owner-turn={turn.strip()}")
    if owner_session is not None:
        owner.append(f"--owner-session={owner_session.strip()}")
    full = head + owner + (["--", *positionals] if positionals else [])
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            ns = cli.build_parser().parse_args(full)
        except SystemExit:
            return False, {"ok": False, "code": "bad_args", "error": f"Those arguments don't fit {action}."}
        # The command reaches its owner gate only through the direction branch.
        if (getattr(ns, "from_app", False) or getattr(ns, "at_owner_request", None) is not None
                or getattr(ns, "owner", None) is True or getattr(ns, "dry_run", False) or ns.owner_words != words):
            return False, {"ok": False, "code": "bad_args", "error": f"Those arguments don't fit {action}."}
        rc = int(ns.func(ns) or 0)
    result = _parsed(out.getvalue())
    if rc != 0 or result.get("ok") is False:
        result = dict(result, ok=False)
        result.setdefault("code", result.get("reason") or "refused")
        result.setdefault("error", err.getvalue().strip() or "The change was refused.")
        return False, result
    return True, result
