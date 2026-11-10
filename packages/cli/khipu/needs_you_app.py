"""Needs you in the app (slice B5): read the one list and answer an item.

The read is ``needs_you.waiting`` as the MCP tool gives it, never recorded as
a conversation's read, with three fields more on each item: ``from_words``
(who asked and on which Mac, in words), ``mac`` and ``ago``, and ``expect``:
the pin the item's own confirm or undo already carries (``expect`` in its
args), or null when its actions carry none. The MCP answer never carries them.

An answer is one item by its key, Yes or No, from the app. The CLI first
proves the app itself started the process (``routing._caller``, the check
every ``--from-app`` write makes); then the item's own settle runs through
``owner_act.settle_from_app``, the hold and replay the terminal's typed yes
uses. Nothing here decides who may settle what. One key per answer, so an
item that can't be taken back (``permanent``) is always answered on its own.
The answer sends back the item's ``expect``: when the item has a pin and the
one sent is missing or no longer the item's, nothing changes and the answer
is ``changed``, so the app shows the item again before the owner answers.

The answer is a receipt the app shows: ``{text, action_id, undoable,
saved_on, not_reached}``. ``action_id`` is the owner-actions line the app's
Undo runs on. ``saved_on`` names this Mac once the change is written here;
``not_reached`` names the Mac an answer was sent to and has not applied yet.
Whether another Mac's own copy has caught up is not read here.

A sign-in item changes nothing: its Yes returns ``signin_account`` (the
label, harness and login folder of the account), so the app opens the
sign-in it already has.
"""
from __future__ import annotations

from khipu import needs_you, owner_direction, routing

THIS_MAC = "this Mac"


def this_mac() -> str:
    """What the owner calls this Mac (``routing_mac_names``), else "this Mac"."""
    try:
        names = routing.settings().get("routing_mac_names") or {}
        mine = routing._local_mac()
    except Exception:  # noqa: BLE001 - a name is a nicety; the fallback is true
        return THIS_MAC
    name = names.get(mine) if isinstance(names, dict) and mine else None
    return name.strip() if isinstance(name, str) and name.strip() else THIS_MAC


def pin_of(settle) -> str | None:
    """The pin an item's own confirm or undo carries (``expect``), or None."""
    for side in ("confirm", "undo"):
        offered = (settle or {}).get(side) if isinstance(settle, dict) else None
        pin = ((offered or {}).get("args") or {}).get("expect") if isinstance(offered, dict) else None
        if pin is not None:
            return str(pin)
    return None


def item_fields(by, who: str, ago: str | None, settle=None) -> dict:
    """``from_words``, ``mac``, ``ago`` and ``expect`` for one item (``needs_you._finished``)."""
    pin = pin_of(settle)
    if isinstance(by, dict) and by.get("kind") == "other_mac":
        mac = by.get("mac") or "your other Mac"
        return {"from_words": f"From {who}.", "mac": mac, "ago": ago, "expect": pin}
    mac = this_mac()
    if by is None:
        return {"from_words": f"Khipu noticed this on {mac}.", "mac": mac, "ago": ago, "expect": pin}
    return {"from_words": f"From {who} on {mac}.", "mac": mac, "ago": ago, "expect": pin}


def _undoable(line: dict | None) -> bool:
    return bool(line) and not line.get("no_undo") and line.get("undo", True) is not None \
        and line.get("state") not in ("failed", "undone", "cancelled", "unconfirmed")


def last_answer() -> dict | None:
    """The owner's latest answer to something waiting, for the empty list:
    the newest owner-actions line that settled a held request."""
    try:
        rows = owner_direction.actions()
    except Exception:  # noqa: BLE001 - the empty state just says nothing waits
        return None
    for line in reversed(rows):
        confirmed = line.get("confirmed") or {}
        declined = line.get("action") == owner_direction.DECLINE_ACTION
        if not declined and not confirmed.get("request"):
            continue
        if line.get("state") == "failed":
            continue
        if declined:
            text = (line.get("receipt") or {}).get("text") or "You said no to a held request. Nothing was changed."
        else:
            text = f"You said yes: {needs_you.ACTION_WORDS.get(line.get('action'), 'make a change')}."
        return {"text": text, "at": line.get("at"), "ago": needs_you._ago(line.get("at"), routing._now()),
                "action_id": line.get("id"), "undoable": _undoable(line)}
    return None


def read(key: str | None = None) -> dict:
    """The list as the app shows it, or with ``key`` that one item with its
    words whole (the read ``needs-you --item`` makes). Each item says whether
    its words were cut (``cut``); an item read by its key never is. Reading
    changes nothing."""
    token = needs_you.FOR_APP.set(True)
    try:
        answer = needs_you.waiting(key=key, record_read=False)
    finally:
        needs_you.FOR_APP.reset(token)
    if isinstance(answer, dict):
        answer["this_mac"] = this_mac()
        for item in answer.get("items") or []:
            if isinstance(item, dict):
                item["cut"] = item.get("cut") is True
        if not answer.get("total"):
            answer["last_answer"] = last_answer()
    return answer


NOT_FOUND = "That item is no longer waiting. Nothing was changed; read the list again."


def read_one(key: str) -> tuple[int, dict]:
    """(exit code, JSON) for one item read whole by its key; ``not_found``
    when no item has that key now."""
    answer = read(key)
    items = answer.get("items") if isinstance(answer, dict) else None
    if not items or len(items) != 1 or items[0].get("key") != key:
        return _no("not_found", NOT_FOUND, key=key)
    return 0, dict(answer, key=key)


def _line_for(out: dict) -> dict | None:
    """The owner-actions line this answer wrote: the id the command gave, or
    the line whose confirm names the held request it settled."""
    ident = out.get("owner_action")
    if isinstance(ident, str):
        return owner_direction._find(ident) or {"id": ident}
    request = out.get("request")
    if not isinstance(request, str):
        return None
    for line in reversed(owner_direction.actions()):
        if (line.get("confirmed") or {}).get("request") == request:
            return line
    return None


def _receipt(item: dict, yes: bool, out: dict) -> dict:
    mac = this_mac()
    if out.get("already_settled"):
        return {"text": out.get("line") or "It was already settled on your other Mac.", "action_id": None,
                "undoable": False, "saved_on": [], "not_reached": []}
    if out.get("sent"):
        where = item.get("mac") or "your other Mac"
        return {"text": out.get("line") or f"Your answer is on its way to {where}.", "action_id": None,
                "undoable": False, "saved_on": [mac], "not_reached": [where]}
    line = _line_for(out)
    if isinstance(out.get("receipt"), str) and out["receipt"].strip():
        text = out["receipt"]
    elif out.get("started"):
        text = out.get("line") or "Started. It finishes in the background; Needs you says if it doesn't."
    elif yes:
        text = f"You said yes. {item.get('confirm_does') or 'Done.'}"
    else:
        text = f"You said no. {item.get('undo_does') or 'Done.'}"
    return {"text": text, "action_id": (line or {}).get("id"), "undoable": _undoable(line),
            "saved_on": [mac], "not_reached": []}


def _signin_account(item: dict) -> dict | None:
    from khipu import usage

    label = item["key"].partition(":")[2]
    for account in usage.all_accounts():
        if account.get("label") == label and not account.get("invalid"):
            return {"label": label, "harness": account.get("harness"), "home": account.get("home") or None}
    return None


def _no(code: str, error: str, **extra) -> tuple[int, dict]:
    return 2, {"ok": False, "code": code, "error": error, **extra}


CHANGED = "This changed since you saw it, so nothing was changed. Look at it again, then answer."


def answer(key: str, said: str, expect: str | None = None) -> tuple[int, dict]:
    """(exit code, JSON) for the app's Yes or No on the item ``key``, with
    the ``expect`` pin the app read. The caller has already proved the app
    started this process."""
    if said not in ("yes", "no"):
        return _no("bad_args", "The answer is yes or no.")
    yes = said == "yes"
    shown = read(key)
    items = shown.get("items") if isinstance(shown, dict) else None
    if not items or len(items) != 1 or items[0].get("key") != key:
        return _no("not_found", NOT_FOUND)
    item = items[0]
    pin = pin_of(item.get("settle"))
    if pin is not None and expect != pin:
        return _no("changed", CHANGED, key=key)
    if item["kind"] == "signin":
        if not yes:
            return _no("no_action", "Nothing to say no to: the account stays signed out until you sign in.")
        account = _signin_account(item)
        if account is None:
            return _no("not_found", "That account is no longer in your account list.")
        return 0, {"ok": True, "applied": False, "signin_account": account,
                   "line": "Nothing was changed. Sign in with the account's own program."}
    offered = (item.get("settle") or {}).get("confirm" if yes else "undo")
    if not isinstance(offered, dict):
        if yes and item.get("decline_only"):
            return _no("decline_only", "This item can only be declined: say no to drop it.")
        return _no("no_action", item.get("do") or (item.get("confirm_does") if yes else item.get("undo_does"))
                   or "This item has no action to apply.")
    from khipu import owner_act

    ok, out = owner_act.settle_from_app(offered.get("action"), offered.get("args") or {})
    if not ok:
        out = dict(out or {}, ok=False)
        out.setdefault("code", "refused")
        out.setdefault("error", "The change was refused. Nothing was changed.")
        return 2, out
    result = {"ok": True, "key": key, "answer": said, "receipt": _receipt(item, yes, out or {})}
    for name in ("applied", "declined", "started", "sent", "already_settled", "request"):
        if name in (out or {}):
            result[name] = out[name]
    return 0, result


ONLY_THE_APP = ("Only the Khipu app can answer this way; it was not the app that asked, so nothing was changed. "
                "Answer in a conversation, or with needs-you --yes or --no on a terminal.")


def run_cli(args) -> int:
    """``khipu needs-you --from-app`` reads the app's list, and with
    ``--item KEY`` one item whole;
    ``khipu needs-you --from-app --answer KEY yes|no`` answers one item.
    Always JSON."""
    from khipu.cli import _strict_json

    def say(code: int, out: dict) -> int:
        print(_strict_json(out, indent=2, default=str))
        return code

    if args.answer:
        if not args.from_app:
            return say(*_no("owner_only", ONLY_THE_APP))
        if not routing._caller()["app"]:
            return say(*_no("owner_only", ONLY_THE_APP))
        key, said = args.answer
        return say(*answer(key, said, getattr(args, "expect", None)))
    if args.yes or args.no:
        return say(*_no("bad_args", "The app answers with --answer KEY yes or no."))
    if args.item:
        return say(*read_one(args.item))
    return say(0, read())
