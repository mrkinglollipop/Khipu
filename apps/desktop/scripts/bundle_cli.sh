#!/usr/bin/env bash
# Stage Contents/Resources/khipu for Tauri bundling: Python CLI, wheels, ops bits,
# compat matrix floor, and bin wrappers. Called from release_macos.sh before
# `tauri build` (and may be run alone to refresh khipu-resources/).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DESKTOP="$ROOT/apps/desktop"
OUT="$DESKTOP/khipu-resources"
COMPAT="$ROOT/docs/compat/khipu-graphify-postgres.json"

CPYTHON_RELEASE="${KHIPU_CPYTHON_RELEASE:-20260901}"
CPYTHON_FILE="cpython-3.11.16+${CPYTHON_RELEASE}-aarch64-apple-darwin-install_only.tar.gz"
CPYTHON_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${CPYTHON_RELEASE}/${CPYTHON_FILE}"

if [[ ! -f "$COMPAT" ]]; then
  echo "missing compatibility matrix: $COMPAT" >&2
  exit 1
fi

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "bundle_cli.sh: macOS only" >&2
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "bundle_cli.sh: macOS arm64 only (portable v1)" >&2
  exit 1
fi

echo "staging bundled CLI -> $OUT"
rm -rf "$OUT"
mkdir -p "$OUT"/{bin,lib,ops/docker,packages}

rsync -a \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.ruff_cache' \
  "$ROOT/packages/cli/" "$OUT/packages/cli/"

rsync -a "$ROOT/ops/migrations/" "$OUT/ops/migrations/"
cp "$ROOT/ops/docker/Dockerfile.pgvector" "$OUT/ops/docker/Dockerfile.pgvector"

cp "$COMPAT" "$OUT/info.json"
shasum -a 256 "$OUT/info.json" | awk '{print $1}' >"$OUT/info.json.sha256"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
ARCHIVE="$TMP/$CPYTHON_FILE"
echo "fetching $CPYTHON_URL"
if ! curl -fsSL --retry 3 --retry-delay 2 -o "$ARCHIVE" "$CPYTHON_URL"; then
  echo "failed to download CPython standalone (no Homebrew fallback)" >&2
  exit 1
fi
mkdir -p "$OUT/python"
tar -xzf "$ARCHIVE" -C "$OUT/python" --strip-components=1

PY="$OUT/python/bin/python3.11"
if [[ ! -x "$PY" ]]; then
  echo "standalone extract missing $PY" >&2
  exit 1
fi

"$PY" -m pip install --disable-pip-version-check -q --no-compile \
  --target "$OUT/lib" -r "$ROOT/packages/cli/requirements.txt"
find "$OUT" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true

cat >"$OUT/bin/khipu" <<'EOF'
#!/bin/sh
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export KHIPU_ROOT="$ROOT"
PY="${KHIPU_PYTHON:-$ROOT/python/bin/python3.11}"
export PYTHONPATH="$ROOT/packages/cli:$ROOT/lib${PYTHONPATH:+:$PYTHONPATH}"
# Bytecode cache goes OUTSIDE the signed .app bundle — see khipu.paths.pycache_dir.
# This wrapper ships INSIDE Contents/Resources, so without this every `khipu`
# invocation wrote __pycache__/*.pyc next to the signed code and broke the seal
# ("Khipu is damaged", 0.3.15, withdrawn). mkdir failure is fail-open: a missing
# PYTHONPYCACHEPREFIX dir just disables caching.
PYTHONPYCACHEPREFIX="${HOME}/Library/Caches/Khipu/pycache"
mkdir -p "$PYTHONPYCACHEPREFIX" 2>/dev/null
export PYTHONPYCACHEPREFIX
exec "$PY" -m khipu "$@"
EOF
chmod +x "$OUT/bin/khipu"

patch_bundled_bin() {
  local name="$1"
  local src="$ROOT/packages/cli/bin/$name"
  local dst="$OUT/bin/$name"
  if [[ ! -f "$src" ]]; then
    echo "missing bin script: $src" >&2
    exit 1
  fi
  python3 - "$src" "$dst" <<'PY'
import sys
from pathlib import Path

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
text = src.read_text(encoding="utf-8")
text = text.replace(
    'ROOT="${KHIPU_ROOT:-$(cd "$SELF_DIR/../../.." 2>/dev/null && pwd)}"',
    'ROOT="${KHIPU_ROOT:-$(cd "$SELF_DIR/.." 2>/dev/null && pwd)}"',
)
text = text.replace("/.python_libs", "/lib")
text = text.replace(
    "${KHIPU_PYTHON:-/opt/homebrew/bin/python3.11}",
    "${KHIPU_PYTHON:-$ROOT/python/bin/python3.11}",
)
dst.write_text(text, encoding="utf-8")
PY
  chmod +x "$dst"
}

for script in khipu-mcp khipu-stop-hook khipu-recall-hook khipu-aegis-capture; do
  patch_bundled_bin "$script"
done

echo "bundled CLI staged ($(du -sh "$OUT" | awk '{print $1}'))"
