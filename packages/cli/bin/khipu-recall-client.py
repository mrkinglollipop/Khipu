# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu-recall-client — ask the warm recall service (khipu.recall_daemon) for
one prompt's answer. Standard library only and no khipu import, so it starts
under ``python3 -S -E -B`` in a few milliseconds; the whole point of the
service is that a prompt no longer pays for a fresh interpreter importing the
package.

Reads the hook payload on stdin. Argument: ``claude`` (default) or ``cursor``.

  answer arrives         prints it, exit 0
  no answer in time, an empty or non-object answer, or the service reporting
  an error              prints {} and exits 0 (the prompt has waited long
                         enough; there is no second attempt)
  no connection at all   prints nothing, exit 3 (the caller runs the one-shot
                         path instead)

The socket path is the service's: ``KHIPU_CAPTURE_HOME``, then
``KHIPU_AEGIS_HOME``, then ``~/.grok/khipu``, plus ``recall.sock`` — the same
resolution as ``khipu.session_capture.khipu_home``.
"""
import json
import os
import socket
import sys
import time

CONNECT_TIMEOUT_S = 0.15
ANSWER_TIMEOUT_S = 1.6
NO_CONNECTION = 3
MAX_ANSWER_BYTES = 8 * 1024 * 1024


def socket_path():
    home = (
        os.environ.get("KHIPU_CAPTURE_HOME")
        or os.environ.get("KHIPU_AEGIS_HOME")
        or os.path.join(os.path.expanduser("~"), ".grok", "khipu")
    )
    return os.path.join(home, "recall.sock")


def read_answer(sock, deadline):
    """The first line the service sends, or None when none arrives in time."""
    buf = b""
    while b"\n" not in buf:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or len(buf) > MAX_ANSWER_BYTES:
            return None
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(65536)
        except OSError:
            return None
        if not chunk:
            break
        buf += chunk
    return buf.split(b"\n", 1)[0]


def as_hook_output(line):
    """The object to print for the service's answer line: the answer when it
    is a JSON object that is not a bare error, otherwise {}."""
    if not line or not line.strip():
        return {}
    try:
        out = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    if not isinstance(out, dict) or set(out) == {"error"}:
        return {}
    return out


def main(argv):
    shape = "cursor" if any(a in ("cursor", "--cursor") for a in argv[1:]) else "claude"
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_S)
    try:
        sock.connect(socket_path())
    except OSError:
        sock.close()
        return NO_CONNECTION
    started = time.monotonic()
    line = None
    try:
        request = json.dumps({"op": "recall", "raw": raw, "shape": shape}) + "\n"
        sock.settimeout(ANSWER_TIMEOUT_S)
        sock.sendall(request.encode("utf-8"))
        line = read_answer(sock, started + ANSWER_TIMEOUT_S)
    except OSError:
        line = None
    finally:
        sock.close()
    sys.stdout.write(json.dumps(as_hook_output(line)) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
