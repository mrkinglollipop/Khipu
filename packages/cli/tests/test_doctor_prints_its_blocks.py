"""Every check doctor gates on is printed with its evidence: a block whose
``X_ok`` reaches the report also reaches it as ``X``. recall_eval was green
in doctor with no number behind it because its block was computed and dropped."""
from __future__ import annotations

import inspect
import re

from khipu import cli


# dsn_file's evidence is printed inside the ``secrets`` block.
_NESTED = {"dsn_file"}


def _keys(fn) -> set[str]:
    return set(re.findall(r'"([a-z_]+)":', inspect.getsource(fn)))


def test_each_local_block_with_a_printed_verdict_is_printed_too():
    local = _keys(cli._doctor_local_checks)
    body = _keys(cli._cmd_doctor_body)
    missing = sorted(k for k in local
                     if f"{k}_ok" in local and f"{k}_ok" in body and k not in body and k not in _NESTED)
    assert missing == [], f"doctor prints a verdict without its block: {missing}"
