"""Read-only reader for Antigravity's SQLite trajectory (T3's managed runtime).

T3's antigravity-acp runtime hands the hooks a ``transcriptPath`` ending in
``.db`` instead of the JSONL the standalone app writes. The ``steps`` table
holds one protobuf blob per step (no published schema). Fields found by
decoding a real runtime-1.3.0 session (2026-10-29):

* step_type 14 (user input): field 19 → field 2 is the text (field 19 → 3 → 1
  repeats it). It is T3's ``<t3_code_instructions>`` block with what the user
  typed inside ``<user_request>…</user_request>``.
* step_type 15 (model response): field 20 → field 1 is the reply text (field
  20 → 8 repeats it).

Nothing is ever written beside the database: a WAL-mode file (or one with a
-wal/-shm next to it) is copied to a private temp dir and read there.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

USER_STEP = 14
MODEL_STEP = 15
_MAX_DEPTH = 1  # the text sits at most one message down from the payload root
# A step bigger than this is skipped unread: SQLite lets any value sit in the
# blob column, and a payload is copied before it is parsed.
MAX_PAYLOAD = 8 << 20
_OPEN, _CLOSE = "<user_request>", "</user_request>"
_INSTRUCTIONS_END = "</t3_code_instructions>"


def is_db(path) -> bool:
    return str(path).lower().endswith(".db")


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if b < 0x80:
            return n, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def _fields(buf: bytes) -> list[tuple[int, bytes]]:
    """The length-delimited fields of one protobuf message, in order. Raises
    ValueError when ``buf`` is not a well-formed message."""
    out: list[tuple[int, bytes]] = []
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError("field 0")
        if wire == 0:
            _, i = _varint(buf, i)
        elif wire == 2:
            size, i = _varint(buf, i)
            if i + size > len(buf):
                raise ValueError("truncated")
            out.append((field, buf[i:i + size]))
            i += size
        elif wire == 1:
            i += 8
        elif wire == 5:
            i += 4
        else:
            raise ValueError("wire type")
        if i > len(buf):
            raise ValueError("truncated")
    return out


def _child(buf: bytes, *path: int) -> bytes | None:
    """The first length-delimited value at ``path`` (field numbers), or None."""
    for field, value in _fields(buf):
        if field == path[0]:
            if len(path) == 1:
                return value
            try:
                found = _child(value, *path[1:])
            except ValueError:
                continue
            if found is not None:
                return found
    return None


def _text(payload: bytes, *paths: tuple[int, ...]) -> str:
    for path in paths:
        try:
            raw = _child(payload, *path)
            if raw:
                return raw.decode("utf-8").strip()
        except (ValueError, UnicodeDecodeError, IndexError):
            continue
    return ""


def _typed_part(text: str) -> str:
    """What the person typed: T3 wraps it in its instruction block."""
    if text.lstrip().startswith("<t3_code_instructions>"):
        # The wrapper's own pair: its opening tag is the first after the
        # instruction block closes (the instructions may name the tag), its
        # closing tag the last (the request itself may name either tag).
        head = text.find(_INSTRUCTIONS_END)
        start = text.find(_OPEN, head + len(_INSTRUCTIONS_END)) if head >= 0 else -1
        end = text.rfind(_CLOSE)
        text = text[start + len(_OPEN):end].strip() if 0 <= start < end else ""
    from khipu.t3 import strip_handoff
    return strip_handoff(text).strip()


def decode_step(step_type: int, payload) -> str:
    """The text of one step ('' when it carries none or cannot be decoded)."""
    if not isinstance(payload, (bytes, bytearray, memoryview)) or len(payload) > MAX_PAYLOAD:
        return ""
    try:
        data = bytes(payload)
        if step_type == USER_STEP:
            return _typed_part(_text(data, (19, 2), (19, 3, 1)))
        if step_type == MODEL_STEP:
            return _text(data, (20, 1), (20, 8))
    except Exception:  # noqa: BLE001 - an unknown payload is skipped
        pass
    return ""


def file_identity(path) -> list:
    """Inode, size and mtime of the database and of its -wal ([] when absent):
    every write lands in one of the two, so an equal identity means the
    steps are as they were. Raises OSError when the database is gone."""
    info = Path(path).stat()
    out = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    try:
        wal = Path(f"{path}-wal").stat()
        out += [wal.st_ino, wal.st_size, wal.st_mtime_ns]
    except OSError:
        pass
    return out


def _wal_mode(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            head = fh.read(20)
    except OSError:
        return False
    return len(head) >= 20 and head[18] == 2 and head[19] == 2


_TURN_ROWS = ("SELECT idx, step_type, step_payload FROM steps WHERE step_type IN (?, ?) "
              "AND typeof(step_payload) = 'blob' AND length(step_payload) <= ? ORDER BY idx",
              (USER_STEP, MODEL_STEP, MAX_PAYLOAD))


def _query(uri: str, query: tuple = _TURN_ROWS) -> list[tuple]:
    conn = sqlite3.connect(uri, uri=True, timeout=0.25)
    try:
        return conn.execute(*query).fetchall()
    finally:
        conn.close()


def _rows(path: Path, query: tuple = _TURN_ROWS) -> list[tuple]:
    path = Path(path)
    plain = not _wal_mode(path) and not any(
        Path(f"{path}{s}").exists() for s in ("-wal", "-shm"))
    if plain:
        for mode in ("mode=ro", "immutable=1"):
            try:
                return _query(f"file:{path.as_posix().replace('?', '%3f').replace('#', '%23')}?{mode}", query)
            except sqlite3.Error:
                continue
    tmp = tempfile.mkdtemp(prefix="khipu-ag-")
    try:
        copy = Path(tmp) / "t.db"
        shutil.copyfile(path, copy)
        wal = Path(f"{path}-wal")
        if wal.exists():
            shutil.copyfile(wal, Path(f"{copy}-wal"))
        return _query(f"file:{copy.as_posix()}", query)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_turns(path) -> list[tuple[int, str, str]]:
    """``[(step idx, "user"|"assistant", text)]`` in step order. Never raises."""
    try:
        rows = _rows(Path(path))
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[int, str, str]] = []
    for idx, step_type, payload in rows:
        text = decode_step(step_type, payload)
        if text:
            out.append((int(idx), "user" if step_type == USER_STEP else "assistant", text))
    return out


def user_steps(path) -> list[int] | None:
    """Index of every user step, readable or not, in step order; None when
    the db cannot be read."""
    try:
        return [int(r[0]) for r in _rows(Path(path), ("SELECT idx FROM steps WHERE step_type = ? ORDER BY idx",
                                                       (USER_STEP,)))]
    except Exception:  # noqa: BLE001
        return None


def last_user_input(path) -> tuple[str, str] | None:
    """(step key, text) of the newest user turn, the shape the JSONL reader returns."""
    for idx, role, text in reversed(read_turns(path)):
        if role == "user":
            return f"step-{idx}", text
    return None


def as_jsonl(path) -> bytes:
    """The turns as the Antigravity JSONL lines the capture reader already
    parses, so offsets and de-duplication work unchanged. ``khipu_db_step``
    marks a line as a database step: its index identifies the turn when a
    rewritten row sends capture back to the stream's start."""
    lines = []
    for idx, role, text in read_turns(path):
        lines.append(json.dumps({"type": "USER_INPUT" if role == "user" else "PLANNER_RESPONSE",
                                 "step_index": idx, "khipu_db_step": idx, "content": text},
                                ensure_ascii=False))
    return ("\n".join(lines) + "\n").encode("utf-8") if lines else b""
