# --bypass-harness (sonnet lane) — authored directly by the dispatched
# on-sub Sonnet build agent for this phase (brief: "do not delegate to other
# agents"); there is no further agent to route this to.
"""khipu-recall-client — ask the warm recall service (khipu.recall_daemon) for
one prompt's answer. Standard library only and no khipu package import, so it
starts under ``python3 -S -E -B`` in a few milliseconds; the whole point of
the service is that a prompt no longer pays for a fresh interpreter importing
the package. The one exception is ``khipu/wire.py``, loaded by file path
(stdlib only, no package ``__init__``), so what it prints is strict JSON.

Reads the hook payload on stdin. The payload decides the output shape
(``claude``, ``cursor`` or ``antigravity``, by ``khipu/hook_shape.py`` loaded
by file path); an argument of ``cursor`` or ``--cursor`` (an older install)
only counts when the payload names no Cursor or Antigravity.

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
import importlib.util
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
# The hook's routing switches (khipu.recall_prompt.ROUTING_SWITCHES): the
# service reads them as this hook's, not as its own environment's.
ROUTING_SWITCHES = ("ROUTING_NOW_OFF", "KHIPU_FEATURE_ROUTING_PUSH")
# Who is calling (khipu.hook_shape.IDENTITY_ENV): sent too, so the service
# tells Cursor's duplicate pair apart by the hook's environment, not its own.
HOOK_IDENTITY = ("KHIPU_HARNESS", "GROK_HOOK_NAME", "GROK_HOOK_EVENT", "CODEX_THREAD_ID", "CLAUDECODE",
                 "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CODEX_HOME", "CODEX_SANDBOX",
                 "CLAUDE_PROJECT_DIR", "CURSOR_VERSION", "CURSOR_PROJECT_DIR")


def load_wire():
    """khipu/wire.py beside this script: ``../khipu`` in the repo,
    ``../packages/cli/khipu`` in the app bundle's ``bin``; None if missing."""
    here = os.path.dirname(os.path.realpath(__file__))
    for rel in (("..", "khipu"), ("..", "packages", "cli", "khipu")):
        path = os.path.join(here, *rel, "wire.py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("khipu_wire", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


def load_hook_shape():
    """khipu/hook_shape.py beside this script, as ``load_wire`` finds wire.py;
    None if missing."""
    here = os.path.dirname(os.path.realpath(__file__))
    for rel in (("..", "khipu"), ("..", "packages", "cli", "khipu")):
        path = os.path.join(here, *rel, "hook_shape.py")
        if os.path.isfile(path):
            spec = importlib.util.spec_from_file_location("khipu_hook_shape", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    return None


def output_shape(raw, argv):
    """claude, cursor or antigravity: the payload's, else the argument's."""
    hinted = "cursor" if any(a in ("cursor", "--cursor") for a in argv[1:]) else "claude"
    try:
        payload = json.loads(raw)
        module = load_hook_shape()
        if module is not None and isinstance(payload, dict):
            return module.resolve_shape(payload, os.environ, argv[1:] + (["--cursor"] if hinted == "cursor" else []))
    except Exception:  # noqa: BLE001 - the argument still decides
        pass
    return hinted


def hook_stdout(out):
    """The answer as strict JSON (no lone surrogate escape, which Foundation
    and serde_json reject), or None when wire.py cannot be found and the
    answer would not encode strictly."""
    try:
        wire = load_wire()
    except (OSError, ImportError, SyntaxError):
        wire = None
    if wire is not None:
        return wire.dumps(out, ensure_ascii=True)
    text = json.dumps(out, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return None
    return json.dumps(out)


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
    if isinstance(out, dict) and ("hookSpecificOutput" in out or "additional_context" in out or "injectSteps" in out):
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
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    shape = output_shape(raw, argv)
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
        env = {k: os.environ[k] for k in ROUTING_SWITCHES + HOOK_IDENTITY if k in os.environ}
        request = json.dumps({"op": "recall", "raw": raw, "shape": shape,
                              "protocol": 1, "deadline": deadline, "env": env}) + "\n"
        sock.settimeout(max(0.001, deadline - time.monotonic()))
        sock.sendall(request.encode("utf-8"))
        line = read_answer(sock, deadline)
        out = as_hook_output(line)
        text = hook_stdout(out) if out is not None else None
        if text is not None and time.monotonic() < deadline:
            sys.stdout.write(text + "\n")
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
    sys.stdout.write((hook_stdout({}) or "{}") + "\n")
    return NO_BUDGET


if __name__ == "__main__":
    sys.exit(main(sys.argv))
