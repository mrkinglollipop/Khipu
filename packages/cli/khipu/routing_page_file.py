"""The routing page as a plain text file (routing slice 6, "Delivery").

Every saved fold writes the page (``routing_page.page``) to ``page.txt``
beside the view, atomically, with the moment it was rendered. The prompt
hook reads that file instead of the folded view, so a prompt's routing read
is one small file and never a fold; an old page is still sent, with its time.
Scope: jobs/2026-11-07/scope-routing-one-page.md section 2 ("How it reaches
an agent") and section 6 item 6.

The file is one header line, then the page exactly as an agent sees it::

    khipu-routing-page 1 {"rendered_at": ..., "version": ..., "epoch": ...,
                          "sig": ..., "valid_until": ..., "events": ..., "config": ...}
    ROUTING 16:31Z. The owner's record. ...

``sig`` is ``routing_page.signature``: it changes when a status word, the
owner's order or the owner's words change, never for a reading alone. It is
the page's revision in the delivery ledger, so the page is sent again only
when it changes. Nothing here reads the routing log or folds.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PAGE_NAME = "page.txt"
HEADER = "khipu-routing-page 1 "
# Read no more than this: the page is bounded by routing_page.LIMIT and its header
# a few hundred bytes. A larger file is not one Khipu wrote.
READ_MOST = 64 * 1024
# A page older than this is sent with its age, and the child that read it
# asks for a refold off the prompt path (``routing.refold_in_background``).
OLD_AFTER_S = 60.0


def path() -> Path:
    from khipu import routing

    return routing.routing_dir() / PAGE_NAME


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        out = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return out if out.tzinfo else out.replace(tzinfo=timezone.utc)


def render(view: dict, now: datetime | None = None) -> tuple[str, dict]:
    """(the page, its header) for ``view`` at ``now`` (default: the moment
    the view was folded, so the page says what that fold saw)."""
    from khipu import routing, routing_page

    when = now or _parse(view.get("folded_at")) or routing._now()
    # No measured spend or running jobs here: reading them means runner
    # telemetry and usage history, and this runs under the routing lock on
    # every save. A cap prints as set; ``khipu_route`` and ``khipu route``
    # print the page with them (``routing._page_inputs``).
    text = routing_page.page(view, when, None, None)
    fingerprint = view.get("fingerprint") if isinstance(view.get("fingerprint"), dict) else {}
    meta = {"rendered_at": _iso(when), "version": view.get("version"), "epoch": view.get("epoch"),
            "sig": routing_page.signature(view, when), "valid_until": view.get("valid_until"),
            "events": fingerprint.get("events"), "config": fingerprint.get("config")}
    return text, meta


def _log_failure(exc: BaseException) -> None:
    """One line in prompt-recall.log naming the exception class, nothing from
    the page or the view. Never raises."""
    try:
        from khipu import recall_prompt

        recall_prompt._log(f"routing page not written: {type(exc).__name__}", tag="khipu-routing-page")
    except Exception:  # noqa: BLE001 - logging never fails the save
        pass


def write(view: dict, now: datetime | None = None) -> bool:
    """Render ``view``'s page and replace ``page.txt`` with it atomically
    (a temp file in the same folder, fsynced, then renamed). Never raises:
    the view is already saved, and the next save writes the page again."""
    tmp: Path | None = None
    try:
        text, meta = render(view, now)
        target = path()
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=target.parent, delete=False, prefix=".page-", suffix=".tmp",
                                         encoding="utf-8") as fh:
            tmp = Path(fh.name)
            fh.write(HEADER + json.dumps(meta, sort_keys=True, ensure_ascii=True) + "\n" + text + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        tmp = None
        return True
    except Exception as exc:  # noqa: BLE001 - a page that could not be written costs nothing: the next save writes it
        _log_failure(exc)
        return False
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                tmp.unlink()


def read() -> dict | None:
    """``{"text", "rendered_at", "version", "epoch", "sig", "valid_until",
    "events", "config"}`` from ``page.txt``, or None when there is none
    Khipu wrote (missing, too large, no header, an unreadable header)."""
    try:
        with path().open("rb") as fh:
            raw = fh.read(READ_MOST + 1)
    except OSError:
        return None
    if len(raw) > READ_MOST:
        return None
    try:
        head, _, body = raw.decode("utf-8").partition("\n")
    except UnicodeDecodeError:
        return None
    if not head.startswith(HEADER):
        return None
    try:
        meta = json.loads(head[len(HEADER):])
    except ValueError:
        return None
    text = body.rstrip("\n")
    if not isinstance(meta, dict) or not text.strip() or not isinstance(meta.get("sig"), str) \
            or type(meta.get("version")) is not int or _parse(meta.get("rendered_at")) is None:
        return None
    return dict(meta, text=text)


def _stat(name) -> list | None:
    try:
        st = os.stat(name)
    except OSError:
        return None
    return [st.st_mtime_ns, st.st_size, st.st_ino]


def behind(page: dict, now: datetime) -> str | None:
    """Why the record may have moved past ``page`` (a word for the log), or
    None: the clock passed its ``valid_until``, the routing log or the
    config changed since its fold, or it is older than ``OLD_AFTER_S`` (so a
    reading that came in since is picked up). Two stats, never a read of
    the log or the view."""
    from khipu import routing
    from khipu.config import config_file

    due = _parse(page.get("valid_until"))
    if due is not None and now >= due:
        return "due"
    if page.get("events") is not None and _stat(routing.events_file()) != page.get("events"):
        return "inputs"
    if page.get("config") is not None and _stat(config_file()) != page.get("config"):
        return "inputs"
    rendered = _parse(page.get("rendered_at"))
    if rendered is not None and (now - rendered).total_seconds() > OLD_AFTER_S:
        return "age"
    return None


def age_line(page: dict, now: datetime, bound: float) -> str | None:
    """One line when the page is older than ``bound`` seconds (the setting
    ``routing_not_refreshed_after``; 0: never), so an old page is shown with
    its time rather than read as current."""
    from khipu import routing

    rendered = _parse(page.get("rendered_at"))
    if not bound or rendered is None or (now - rendered).total_seconds() <= bound:
        return None
    return (f"This page was rendered {routing._clock(rendered, now)}; the record may have moved since. "
            "`khipu_route` reads it now.")
