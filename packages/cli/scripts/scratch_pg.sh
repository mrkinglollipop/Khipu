#!/usr/bin/env bash
# A throwaway PostgreSQL for verifying SQL without touching a real hub.
#
#   scratch_pg.sh up        create the cluster if needed, start it, load the schema
#   scratch_pg.sh load      drop and rebuild the scratch database from ops/migrations
#   scratch_pg.sh dsn       print the DSN (export it as KHIPU_SCRATCH_DSN)
#   scratch_pg.sh status    is it running
#   scratch_pg.sh down      stop it
#   scratch_pg.sh destroy   stop it and delete the cluster
#
# The server listens on a Unix socket only, never on TCP, and trusts local
# connections: it is reachable by this user on this machine and by nothing else.
#
# Any PostgreSQL on PATH will do (initdb, pg_ctl, psql). Khipu's hub needs
# pgvector and property graphs; a stock server has neither, so the statements
# that need them fail one by one and are counted, and the two tables that carry
# vectors are replaced by stand-ins whose embedding column is real[]. Everything
# relational is real. Nothing here can rank by similarity: this verifies
# statements, constraints and migrations, not search quality.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
DIR="${KHIPU_SCRATCH_DIR:-${TMPDIR:-/tmp}/khipu-scratch-pg}"
# A socket path is limited to about 100 bytes, so it cannot live under a deep
# temporary directory.
SOCK="${KHIPU_SCRATCH_SOCK:-/tmp/khipu-scratch-sock-$(id -u)}"
PORT="${KHIPU_SCRATCH_PORT:-54329}"
DB="khipu_scratch"
ROLE="khipu_scratch"
# macOS refuses to start a postmaster without a valid locale in the environment.
export LC_ALL="${LC_ALL:-en_US.UTF-8}"

q() { psql -h "$SOCK" -p "$PORT" -U "$ROLE" "$@"; }

running() { pg_ctl -D "$DIR/data" status >/dev/null 2>&1; }

start() {
  mkdir -p "$DIR" "$SOCK"
  if [ ! -d "$DIR/data" ]; then
    initdb -D "$DIR/data" -U "$ROLE" --auth=trust -E UTF8 --no-locale >"$DIR/initdb.log" 2>&1
  fi
  if ! running; then
    pg_ctl -D "$DIR/data" -l "$DIR/server.log" -w start \
      -o "-c listen_addresses='' -c unix_socket_directories='$SOCK' -c port=$PORT -c fsync=off" >/dev/null
  fi
}

load() {
  q -d postgres -Atqc "DROP DATABASE IF EXISTS $DB" 2>/dev/null
  q -d postgres -Atqc "CREATE DATABASE $DB"
  local skipped=0 f out n
  for f in "$ROOT"/ops/migrations/*.sql; do
    out=$(q -d "$DB" -v ON_ERROR_STOP=0 -q -f "$f" 2>&1 || true)
    n=$(printf '%s\n' "$out" | grep -c "ERROR" || true)
    skipped=$((skipped + n))
  done
  q -d "$DB" -q <<'SQL'
CREATE TABLE IF NOT EXISTS memory_embeddings (
    profile       TEXT NOT NULL REFERENCES embedding_profiles(id),
    kind          TEXT NOT NULL,
    ref           TEXT NOT NULL,
    chunk_idx     INTEGER NOT NULL DEFAULT 0,
    chunk_text    TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    embedding     real[] NOT NULL,
    built_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (profile, kind, ref, chunk_idx)
);
CREATE INDEX IF NOT EXISTS idx_memory_embeddings_kind_ref ON memory_embeddings (kind, ref);
CREATE TABLE IF NOT EXISTS memory_query_cache (
    profile       TEXT NOT NULL REFERENCES embedding_profiles(id),
    query_hash    TEXT NOT NULL,
    embedding     real[] NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    hits          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (profile, query_hash)
);
SQL
  echo "schema loaded: $(q -d "$DB" -Atc "select count(*) from pg_tables where schemaname='public'") tables," \
       "$(q -d "$DB" -Atc "select count(*) from schema_migrations") migrations recorded," \
       "$skipped statements skipped (pgvector / property graph)"
}

dsn() { echo "postgresql://$ROLE@/$DB?host=$SOCK&port=$PORT"; }

case "${1:-help}" in
  up)      start; load; dsn ;;
  load)    running || start; load ;;
  dsn)     dsn ;;
  status)  if running; then echo "running: $(dsn)"; else echo "stopped"; fi ;;
  down)    running && pg_ctl -D "$DIR/data" -m fast -w stop >/dev/null; echo "stopped" ;;
  destroy) running && pg_ctl -D "$DIR/data" -m fast -w stop >/dev/null; rm -rf "$DIR" "$SOCK"; echo "destroyed" ;;
  *)       sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
