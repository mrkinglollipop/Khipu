#!/usr/bin/env bash
# Load a Gemini API key into the macOS Keychain (Khipu/gemini_api_key) from a
# file. Never prints the key. The app's Settings → Secrets does the same thing
# interactively; this is the scripted path.
#
#   usage: setup_mac_gemini_keychain.sh /path/to/key-file
#      or: KHIPU_GEMINI_KEY_FILE=/path/to/key-file setup_mac_gemini_keychain.sh
set -euo pipefail
KEY_FILE="${1:-${KHIPU_GEMINI_KEY_FILE:-}}"
if [[ -z "$KEY_FILE" || ! -f "$KEY_FILE" ]]; then
  echo "usage: $0 /path/to/key-file   (or set KHIPU_GEMINI_KEY_FILE)" >&2
  exit 2
fi
ROOT="${KHIPU_ROOT:-$(cd "$(dirname "$0")/../../.." && pwd)}"
PY="${KHIPU_PYTHON:-python3}"
export PYTHONPATH="$ROOT/packages/cli:$ROOT/.python_libs${PYTHONPATH:+:$PYTHONPATH}"
# The value travels on stdin, never as an argument (argv is visible in `ps`).
tr -d '\r\n' < "$KEY_FILE" | "$PY" -m khipu secrets --set gemini_api_key
