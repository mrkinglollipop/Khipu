"""Who is making the current request, for server-stamped provenance.

The HTTPS gateway sets the calling token's label around each ``handle_message``
call (``gateway_request``). The gateway serves every request on its own thread
and a new thread starts with an empty context, so one request never sees
another's label. Everything else (CLI, hooks, the stdio MCP server) runs with
no label and writes as this machine. Scope:
docs/plans/2026-10-11-gateway-provenance.md, phase 1.
"""

from __future__ import annotations

import contextlib
import contextvars
import os
from collections.abc import Iterator

# Same variable khipu.mcp_server.mark_gateway_active sets; read here, not
# imported, so the capture path does not load the MCP server.
GATEWAY_ACTIVE_ENV = "KHIPU_GATEWAY_ACTIVE"

_GATEWAY_LABEL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "khipu_gateway_label", default=None
)


@contextlib.contextmanager
def gateway_request(label: str) -> Iterator[None]:
    """Mark the enclosed work as one gateway request from token ``label``."""
    if not isinstance(label, str) or not label:
        raise ValueError("a gateway request needs the calling token's label")
    token = _GATEWAY_LABEL.set(label)
    try:
        yield
    finally:
        _GATEWAY_LABEL.reset(token)


def gateway_label() -> str | None:
    """The calling token's label inside a gateway request, else None."""
    return _GATEWAY_LABEL.get()


def gateway_process() -> bool:
    return (os.environ.get(GATEWAY_ACTIVE_ENV) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def request_origin() -> str | None:
    """The origin a row written now must carry.

    ``gateway:<label>`` inside a gateway request; None in the gateway process
    outside one (a thread a request started, an outbox replay), because the
    caller is unknown there and the container's own machine id would claim the
    row for the wrong owner; otherwise this machine's ``local:<hash>``, which
    raises ``MachineIdentityError`` when the machine has no readable identity.
    """
    label = gateway_label()
    if label:
        return f"gateway:{label}"
    if gateway_process():
        return None
    from khipu.machine_identity import local_origin

    return local_origin()
