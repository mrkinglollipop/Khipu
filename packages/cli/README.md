# Khipu CLI (P1c)

```bash
export KHIPU_ROOT="/path/to/Khipu"   # optional; defaults to this checkout
export PYTHONPATH="$KHIPU_ROOT/packages/cli:$KHIPU_ROOT/.python_libs"

# One-time deps. .python_libs/ is gitignored (platform-specific wheels), so a fresh
# clone has no dependencies until this runs — rebuild it per machine, never copy it:
/opt/homebrew/bin/python3.11 -m pip install \
  --target "$KHIPU_ROOT/.python_libs" -r "$KHIPU_ROOT/packages/cli/requirements.txt"

# One-time DSN (password stays local, chmod 600):
printf '%s' 'postgresql://USER:PASSWORD@HOST:5432/DB?sslmode=verify-full' | python3 -m khipu secrets --set database_url

python3 -m khipu config --set memory_root /path/to/memory/conversations   # only if you have a legacy file wiki
python3 -m khipu status
python3 -m khipu doctor
python3 -m khipu activity           # recent capture_v2 → PG episodes
python3 -m khipu activity --show 42
python3 -m khipu revisions          # conflicts + recent LWW archive
python3 -m khipu search "khipu"
python3 -m khipu graph 'module:skills__shared_predictive_gates_scripts_signals_py' --hops 1
python3 -m khipu regen-memory
python3 -m khipu reconcile   # consolidate-style full sync
```

Env:

| Var | Meaning |
|---|---|
| `KHIPU_DATABASE_URL` | Prefer over Keychain/file |
| `KHIPU_DSN_FILE` | Default `~/.config/khipu/dsn` (fallback) |
| `KHIPU_KEYCHAIN` | `0` disables Keychain lookup |
| `KHIPU_MIRROR` | `0` disables capture write-through |
| `KHIPU_MIRROR_EMBED` | `1` enables embed-on-mirror (off by default) |
| `KHIPU_ROOT` | Repo root for imports |

DSN resolve order: **env → Keychain (`Khipu`/`database_url`) → file**.

Fail-open mirror is hooked from Memory `capture_v2.py` / `consolidate_nightly.py`.
