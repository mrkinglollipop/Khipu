"""A throwaway PostgreSQL for tests that need the real schema.

One cluster per test session, started under a temporary folder on loopback
with fsync off, never the live hub and never an external DSN. A template
database holds migrations 0000 through 0029; each test gets its own database
cut from that template (public schema), so tests never share rows and the
slow part (the migration chain) runs once. Skips only when initdb or pg_ctl
are missing.

Use it by importing the fixtures into a test module::

    from tests.hermetic_pg import hub, pg_cluster  # noqa: F401

``hub.connect()`` returns a psycopg connection (autocommit off) that is closed
for you; ``hub.dsn`` is the database's DSN.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

OWNER = "owner.json"


def _process_start(pid: int) -> str | None:
    result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart=", "-o", "stat="],
                            capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"},
                            timeout=5)
    if result.returncode == 1 and not result.stdout.strip():
        return None
    result.check_returncode()
    fields = result.stdout.split()
    if not fields or fields[-1].startswith("Z"):
        return None
    return " ".join(fields[:-1])


def _owner_alive(owner: dict) -> bool:
    return _process_start(owner["pid"]) == owner["start"]


def _read_owner(root: Path) -> dict | None:
    try:
        owner = json.loads((root / OWNER).read_text())
        if (type(owner["pid"]) is int and owner["pid"] > 0
                and isinstance(owner["start"], str) and owner["start"]):
            return owner
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _stop_orphan(root: Path, pg_ctl: str) -> None:
    result = subprocess.run([pg_ctl, "-D", str(root / "data"), "-m", "immediate", "stop"],
                            capture_output=True, timeout=60)
    if result.returncode == 0 or not (root / "data" / "postmaster.pid").exists():
        shutil.rmtree(root, ignore_errors=True)


def _watch_cluster(root: Path, pg_ctl: str) -> None:
    owner = _read_owner(root)
    if owner is None:
        return
    while (root / "data" / "postmaster.pid").exists():
        if not _owner_alive(owner):
            _stop_orphan(root, pg_ctl)
            return
        time.sleep(2)


def _sweep_orphans(temp_root: Path, pg_ctl: str) -> None:
    candidates = list(temp_root.glob("khipu-hermetic-pg-*"))
    candidates.extend(temp_root.glob("kt-*/khipu-hermetic-pg-*"))
    for root in candidates:
        if root.is_symlink() or root.parent.is_symlink() or not root.is_dir():
            continue
        owner = _read_owner(root)
        if owner is not None and not _owner_alive(owner):
            _stop_orphan(root, pg_ctl)


def _system_temp() -> Path:
    root = Path(tempfile.gettempdir())
    while root.name.startswith("kt-"):
        root = root.parent
    return root


# The detached watcher needs only the standard library, even after pytest dies.
if __name__ == "__main__":
    _watch_cluster(Path(sys.argv[1]), sys.argv[2])
    sys.exit(0)

import pytest


REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "ops" / "migrations"
INITDB, PG_CTL = shutil.which("initdb"), shutil.which("pg_ctl")
TEMPLATE = "khipu_template_0029"
LAST_BEFORE = "0029"
THIS_MIGRATION = "0030_forgotten_sources.sql"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def migration_files(through: str) -> list[Path]:
    """Every migration file whose number is at or below ``through``."""
    return [p for p in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")) if p.name[:4] <= through]


# 0001 builds an SQL/PGQ property graph, which only PostgreSQL 19 can parse.
# Nothing here reads the graph, so on an older server the temporary copies of
# the chain (never the repo's files) drop those two statements.
_PROPERTY_GRAPH = re.compile(r"^(DROP|CREATE) PROPERTY GRAPH\b.*?;\s*$", re.S | re.M)


def _server_major() -> int:
    """The major version of the postgres binary beside pg_ctl."""
    binary = Path(PG_CTL).parent / "postgres" if PG_CTL else Path("postgres")
    out = subprocess.run([str(binary), "--version"], capture_output=True, text=True).stdout
    m = re.search(r"\)\s*(\d+)", out)
    return int(m.group(1)) if m else 0


def chain_dir(root: Path, through: str) -> Path:
    """A folder holding copies of migrations 0000 through ``through``."""
    out = root / f"chain_{through}"
    out.mkdir(parents=True, exist_ok=True)
    legacy = _server_major() < 19
    for p in migration_files(through):
        text = p.read_text(encoding="utf-8")
        if legacy:
            text = _PROPERTY_GRAPH.sub("", text)
        (out / p.name).write_text(text, encoding="utf-8")
    return out


def migration_sql(name: str = THIS_MIGRATION) -> str:
    return (MIGRATIONS / name).read_text(encoding="utf-8")


class Hub:
    """One test's database."""

    def __init__(self, cluster: "Cluster", name: str):
        self.cluster, self.name = cluster, name
        self.dsn = cluster.dsn(name)
        self._conns: list = []

    def connect(self):
        import psycopg

        conn = psycopg.Connection.connect(self.dsn)
        self._conns.append(conn)
        return conn

    def migrate(self, directory: Path | None = None, conn=None):
        """Apply the pending migrations of ``directory`` (default: the repo's
        full chain) through khipu.migrate.run."""
        from khipu import migrate

        own = conn or self.connect()
        return migrate.run(conn=own, directory=directory or MIGRATIONS)

    def close(self):
        for conn in self._conns:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._conns.clear()


class Cluster:
    def __init__(self, root: Path, port: int):
        self.root, self.port = root, port
        self.data = root / "data"
        self.env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
        self.watchdog: subprocess.Popen | None = None

    def dsn(self, dbname: str) -> str:
        return f"postgresql://khipu@127.0.0.1:{self.port}/{dbname}"

    def admin(self):
        import psycopg

        conn = psycopg.Connection.connect(self.dsn("postgres"), autocommit=True)
        return conn

    def start(self) -> None:
        owner = {"pid": os.getpid(), "start": _process_start(os.getpid())}
        if owner["start"] is None:
            raise RuntimeError("cannot identify the PostgreSQL test owner")
        (self.root / OWNER).write_text(json.dumps(owner))
        subprocess.run([INITDB, "-D", str(self.data), "-U", "khipu", "--auth=trust", "-E", "UTF8",
                        "--locale=C"], check=True, capture_output=True, env=self.env, timeout=120)
        subprocess.run([PG_CTL, "-D", str(self.data), "-l", str(self.root / "log"), "-w", "-t", "60",
                        "-o", f"-c unix_socket_directories='' -c listen_addresses=127.0.0.1 "
                              f"-p {self.port} -c fsync=off -c max_connections=60",
                        "start"], check=True, capture_output=True, env=self.env, timeout=120)
        try:
            self.watchdog = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).resolve()), str(self.root), PG_CTL],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True, env=self.env)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        subprocess.run([PG_CTL, "-D", str(self.data), "-m", "immediate", "stop"],
                       capture_output=True, env=self.env, timeout=60)
        if self.watchdog is not None:
            self.watchdog.wait(timeout=10)

    def build_template(self) -> None:
        from khipu import migrate

        chain = chain_dir(self.root, LAST_BEFORE)
        with self.admin() as admin:
            admin.execute(f"CREATE DATABASE {TEMPLATE}")
        import psycopg

        with psycopg.Connection.connect(self.dsn(TEMPLATE)) as conn:
            result = migrate.run(conn=conn, directory=chain)
            assert result["pending"] == [], result
            assert result["ran"][-1].startswith(LAST_BEFORE), result

    def new_database(self) -> Hub:
        name = "t_" + uuid.uuid4().hex[:12]
        with self.admin() as admin:
            admin.execute(f"CREATE DATABASE {name} TEMPLATE {TEMPLATE}")
        return Hub(self, name)

    def drop(self, hub: Hub) -> None:
        hub.close()
        with self.admin() as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {hub.name} WITH (FORCE)")


@pytest.fixture(scope="session")
def pg_cluster():
    if not INITDB or not PG_CTL:
        pytest.skip("needs PostgreSQL's initdb and pg_ctl on PATH")
    _sweep_orphans(_system_temp(), PG_CTL)
    root = Path(tempfile.mkdtemp(prefix="khipu-hermetic-pg-"))
    cluster = Cluster(root, _free_port())
    started = False
    try:
        cluster.start()
        started = True
        cluster.build_template()
        yield cluster
    finally:
        if started:
            cluster.stop()
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def hub(pg_cluster):
    from khipu import db

    db._TABLE_COLUMNS_CACHE.clear()
    fresh = pg_cluster.new_database()
    try:
        yield fresh
    finally:
        pg_cluster.drop(fresh)
        db._TABLE_COLUMNS_CACHE.clear()
