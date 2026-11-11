"""The routing page as every reader is handed it, for tests that check what
agents see (it replaced the old summary renderer)."""
from __future__ import annotations


def page_text(view=None, *, now=None) -> str:
    from khipu import routing, routing_page

    now = now or routing._now()
    view = routing.read_view(now) if view is None else view
    return routing_page.page(view, now)
