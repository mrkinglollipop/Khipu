"""Database passwords stay off argv; provider key files must be owner-only regular files."""
from __future__ import annotations

import os
import subprocess
from unittest import mock

import pytest

from khipu import components_backup as cb
from khipu import keychain

SECRET = "s3cr3t-pw-NOT-REAL"


def _ok(*a, **k):
    return subprocess.CompletedProcess(a[0] if a else [], 0, "", "")


def test_docker_exec_dump_keeps_the_password_off_argv():
    seen = []
    with mock.patch.object(cb, "_docker", side_effect=lambda cmd, **k: (seen.append((list(cmd), k)), _ok(cmd))[1]):
        cb._docker_exec_pg("khipu-pg", ["pg_dump"], password=SECRET)
    cmd, kwargs = seen[0]
    assert not any(SECRET in part for part in cmd), f"password is on the docker argv: {cmd}"
    assert "PGPASSWORD" in cmd and kwargs["env"]["PGPASSWORD"] == SECRET


def test_host_pg_dump_keeps_the_dsn_password_off_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_PG_DUMP", "/usr/bin/true")
    monkeypatch.setenv("HOME", str(tmp_path))
    seen = []
    import khipu.db as db
    monkeypatch.setattr(db, "resolve_dsn", lambda: f"postgresql://khipu:{SECRET}@127.0.0.1:5432/khipu")
    monkeypatch.setattr(db, "conninfo_with_local_root_cert", lambda d: d)
    monkeypatch.setattr(cb, "_run", lambda cmd, **k: (seen.append((list(cmd), k)), _ok(cmd))[1])
    try:
        cb._dump_live_db(tmp_path)
    except Exception:
        pass
    assert seen, "host pg_dump was not reached"
    cmd, kwargs = seen[0]
    assert not any(SECRET in part for part in cmd), f"DSN password is on the pg_dump argv: {cmd}"
    assert kwargs["env"]["PGPASSWORD"] == SECRET


def test_host_pg_restore_keeps_the_dsn_password_off_argv(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_PG_RESTORE", "/usr/bin/true")
    seen = []
    monkeypatch.setattr(cb, "_run", lambda cmd, **k: (seen.append((list(cmd), k)), _ok(cmd))[1])
    cb._restore_dump_file(tmp_path / "x.dump", container="c",
                          dsn=f"postgresql://khipu:{SECRET}@127.0.0.1:5432/khipu", password=SECRET)
    cmd, kwargs = seen[0]
    assert not any(SECRET in part for part in cmd), f"DSN password is on the pg_restore argv: {cmd}"
    assert kwargs["env"]["PGPASSWORD"] == SECRET


def test_docker_run_keeps_postgres_password_off_argv():
    seen = []

    def docker(cmd, **k):
        seen.append((list(cmd), k))
        # A missing leftover volume answers non-zero.
        return subprocess.CompletedProcess(cmd, 1 if cmd[:2] == ["volume", "inspect"] else 0, "", "")

    with mock.patch.object(cb, "_cleanup_drill"), mock.patch.object(cb, "_docker", side_effect=docker):
        cb._start_drill_cluster("img", 54338, SECRET)
    runs = [(cmd, k) for cmd, k in seen if cmd and cmd[0] == "run"]
    assert runs, seen
    cmd, kwargs = runs[0]
    assert not any(SECRET in part for part in cmd), f"password is on the docker run argv: {cmd}"
    assert "POSTGRES_PASSWORD" in cmd and kwargs["env"]["POSTGRES_PASSWORD"] == SECRET


@pytest.mark.parametrize("provider", ["gemini", "voyage", "anthropic"])
def test_key_file_symlink_or_loose_mode_is_refused(monkeypatch, tmp_path, provider):
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    for var in ("GEMINI_API_KEY", "VOYAGE_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    real = tmp_path / "real.txt"
    real.write_text("key-from-a-world-readable-file\n")
    os.chmod(real, 0o644)
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    fn = {"gemini": keychain.resolve_gemini_key, "voyage": keychain.resolve_voyage_key,
          "anthropic": keychain.resolve_anthropic_key}[provider]
    for target in (link, real):
        with pytest.raises(RuntimeError):
            fn(key_file=target)


def test_owner_only_key_file_is_still_read(monkeypatch, tmp_path):
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    path = tmp_path / "key.txt"
    path.write_text("owner-only-key\n")
    os.chmod(path, 0o600)
    assert keychain.resolve_gemini_key(key_file=path) == "owner-only-key"


@pytest.mark.parametrize("provider", ["gemini", "voyage", "anthropic", "deepseek"])
def test_a_refused_key_file_is_named_with_its_reason(monkeypatch, tmp_path, provider):
    monkeypatch.setenv("KHIPU_KEYCHAIN", "0")
    for var in ("GEMINI_API_KEY", "VOYAGE_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    shared = tmp_path / "shared"
    shared.mkdir()
    os.chmod(shared, 0o775)
    path = shared / "key.txt"
    path.write_text("owner-only-key\n")
    os.chmod(path, 0o600)
    fn = {"gemini": keychain.resolve_gemini_key, "voyage": keychain.resolve_voyage_key,
          "anthropic": keychain.resolve_anthropic_key,
          "deepseek": keychain.resolve_deepseek_key}[provider]
    with pytest.raises(RuntimeError) as err:
        fn(key_file=path)
    message = str(err.value)
    assert str(path) in message and "group" in message, message
    assert "owner-only-key" not in message
