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
  immediate failure      exits 3 while a cold attempt still fits the budget
  no answer in time      prints {} and exits 4 (no budget for a second attempt)
  no connection at all   prints nothing, exit 3 (the caller runs the one-shot
                         path instead)

The socket path is the service's: ``KHIPU_ISOLATED_DIR``/capture (an isolated
gate run), else ``KHIPU_CAPTURE_HOME``, then ``KHIPU_AEGIS_HOME``, then
``~/.grok/khipu``, plus ``recall.sock`` — the same resolution as
``khipu.session_capture.khipu_home``; failures log beside it.
"""
import json
import os
import socket
import sys
import time

CONNECT_TIMEOUT_S = 0.15
ANSWER_TIMEOUT_S = 1.6
NO_CONNECTION = 3
NO_BUDGET = 4
MAX_ANSWER_BYTES = 8 * 1024 * 1024


def socket_path():
    isolated = os.environ.get("KHIPU_ISOLATED_DIR")
    if isolated:
        # khipu.paths.ISOLATED_ENV: the gate's isolated run, its own capture
        # home (no service listens there, so the hook falls back to one-shot).
        return os.path.join(isolated, "capture", "recall.sock")
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
    is a successful protocol envelope, otherwise None."""
    if not line or not line.strip():
        return None
    try:
        out = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(out, dict) and out.get("ok") is True and isinstance(out.get("output"), dict):
        return out["output"]
    # A populated answer from an older service proves it retrieved context.
    if isinstance(out, dict) and ("hookSpecificOutput" in out or "additional_context" in out):
        return out
    return None


def log_failure(raw, reason, started):
    try:
        payload = json.loads(raw)
        session = payload.get("session_id") or payload.get("sessionId") or "?"
    except (ValueError, AttributeError):
        session = "?"
    try:
        directory = os.path.join(os.path.dirname(socket_path()), "logs")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "prompt-recall.log"), "a", encoding="utf-8") as f:
            stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            ms = (time.monotonic() - started) * 1000
            f.write(f"{stamp} [khipu-prompt-recall] session={session} reason={reason} ms={ms:.1f} hits=[] legs=[] degraded=None\n")
    except OSError:
        pass


def main(argv):
    shape = "cursor" if any(a in ("cursor", "--cursor") for a in argv[1:]) else "claude"
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    started = time.monotonic()
    deadline = started + ANSWER_TIMEOUT_S
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(CONNECT_TIMEOUT_S)
    try:
        sock.connect(socket_path())
    except OSError:
        sock.close()
        return NO_CONNECTION
    line = None
    try:
        request = json.dumps({"op": "recall", "raw": raw, "shape": shape,
                              "protocol": 1, "deadline": deadline}) + "\n"
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        sock.sendall(request.encode("utf-8"))
        line = read_answer(sock, deadline)
        out = as_hook_output(line)
        if out is not None and time.monotonic() < deadline:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
            if json.loads(line).get("ok") is True:
                sock.sendall(b'{"op":"ack"}\n')
            return 0
    except OSError:
        line = None
    finally:
        sock.close()
    remaining = deadline - time.monotonic()
    try:
        recorded_error = json.loads(line).get("ok") is False
    except (TypeError, ValueError, AttributeError):
        recorded_error = False
    if not recorded_error:
        log_failure(raw, "timeout>delivery deadline" if remaining <= 0 else "error: invalid or failed daemon response", started)
    if remaining >= 1.4:
        sys.stdout.write(str(deadline))
        return NO_CONNECTION
    sys.stdout.write("{}\n")
    return NO_BUDGET


if __name__ == "__main__":
    sys.exit(main(sys.argv))
