"""khipu promote and a database that is behind the new code: the refusal names
the pending migrations, and --apply-migrations applies them with the staged
slot's code before the gate runs (nothing is applied without the flag)."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from khipu import integrations
from khipu import promote as pm

_BASE = {"ok": False, "imports": {"failed": {}}, "config": {"ok": True}, "dsn_present": True}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(integrations, "HOME", h)
    monkeypatch.setenv("KHIPU_DATA_DIR", str(h / ".config" / "khipu"))
    return h


def _checkout(path: Path) -> Path:
    (path / "packages" / "cli" / "khipu").mkdir(parents=True, exist_ok=True)
    return path


class FakeDb:
    """A database behind the new code, reached through fake `khipu` subprocesses
    that record which python/cwd (the staged slot's) ran them."""

    def __init__(self, pending, fail_on=None, other_red=False):
        self.pending, self.fail_on, self.other_red = list(pending), fail_on, other_red
        self.calls: list[list[str]] = []
        self.roots: list[str | None] = []

    def run(self, argv, **kw):
        self.calls.append(argv)
        self.roots.append((kw.get("env") or {}).get("KHIPU_ROOT"))
        tail = argv[argv.index("khipu") + 1:] if "khipu" in argv else argv
        if tail[:1] == ["doctor"]:
            out = {**_BASE, "schema_migrations": {"pending": list(self.pending)},
                   "schema_migrations_ok": not self.pending}
            if self.other_red:
                out["outbox_ok"] = False
            out["ok"] = not self.pending and not self.other_red
            return subprocess.CompletedProcess(argv, 0 if out["ok"] else 2, json.dumps(out), "")
        if tail[:1] == ["migrate"]:
            if "--dry-run" in tail:
                return subprocess.CompletedProcess(argv, 0, json.dumps({"pending": list(self.pending)}), "")
            applied = []
            for name in list(self.pending):
                if name == self.fail_on:
                    return subprocess.CompletedProcess(argv, 1, "", f"Traceback\nRuntimeError: {name} broke")
                self.pending.remove(name)
                applied.append(name)
            return subprocess.CompletedProcess(argv, 0, json.dumps({"pending": [], "ran": applied}), "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def migrate_calls(self):
        return [c for c in self.calls if "migrate" in c]


def _promotable(home, monkeypatch, tmp_path, db):
    a, b = pm.slots()
    _checkout(a)
    assert pm.flip(a)["ok"]
    monkeypatch.setattr(pm, "_repo", lambda: tmp_path)
    monkeypatch.setattr(pm, "_fetch_and_resolve", lambda repo, ref: {"ok": True, "commit": "c0ffee"})
    monkeypatch.setattr(pm, "_prepare_slot", lambda slot, commit, repo: (_checkout(slot), {"ok": True})[1])
    monkeypatch.setattr(pm, "_head", lambda root: "headsha" if root else None)
    monkeypatch.setattr(pm, "precompile", lambda root, **kw: {"ok": True})
    monkeypatch.setattr(pm, "_before_flip", lambda: {"ok": True})
    monkeypatch.setattr(pm, "_after_flip", lambda: {"kicked": []})
    monkeypatch.setattr(pm, "hub_wait_s", lambda: 0)
    monkeypatch.setattr(pm, "hub_reachable", lambda **kw: True)
    monkeypatch.setattr(pm, "_run", db.run)
    for name in ("recall", "capture", "version"):
        monkeypatch.setitem(pm.GATE_STEPS, name, lambda c, name=name: {"ok": True, "ms": 1, "detail": name})
    return a, b


def test_red_only_for_pending_migrations_names_them_and_the_flag(home, monkeypatch, tmp_path):
    db = FakeDb(["0052_x", "0053_y"])
    a, _b = _promotable(home, monkeypatch, tmp_path, db)
    out = pm.promote("origin/main")
    assert not out["ok"] and out["failed_step"] == "doctor"
    detail = out["gate"][0]["detail"]
    assert "0052_x" in detail and "0053_y" in detail
    assert "behind the new code" in detail and "--apply-migrations" in detail
    assert detail != "red: schema_migrations"
    assert out["gate"][0]["pending_migrations"] == ["0052_x", "0053_y"]
    text = pm.format_result(out)
    assert "0052_x" in text and "live is unchanged" in text
    assert db.migrate_calls() == []  # nothing is applied without the flag
    assert pm.active_slot() == a


def test_the_flag_applies_them_with_the_staged_code_and_the_gate_then_passes(home, monkeypatch, tmp_path):
    db = FakeDb(["0052_x", "0053_y"])
    _a, b = _promotable(home, monkeypatch, tmp_path, db)
    out = pm.promote("origin/main", apply_migrations=True)
    assert out["ok"], out
    assert out["migrations"]["applied"] == ["0052_x", "0053_y"]
    assert pm.active_slot() == b
    migrate_roots = [r for c, r in zip(db.calls, db.roots) if "migrate" in c]
    assert migrate_roots and set(migrate_roots) == {str(b)}  # the staged slot's code
    kinds = ["migrate" if "migrate" in c else "doctor" if "doctor" in c else "other" for c in db.calls]
    assert kinds.index("migrate") < kinds.index("doctor")  # before the gate runs
    text = pm.format_result(out)
    assert "applied 0052_x" in text and "applied 0053_y" in text


def test_a_failing_migration_stops_the_promote_and_live_is_unchanged(home, monkeypatch, tmp_path):
    db = FakeDb(["0052_x", "0053_y"], fail_on="0053_y")
    a, _b = _promotable(home, monkeypatch, tmp_path, db)
    out = pm.promote("origin/main", apply_migrations=True)
    assert not out["ok"] and out["failed_step"] == "migrations" and not out.get("gate")
    assert "0053_y failed" in out["error"] and "partly ahead" in out["error"]
    assert out["migrations"]["applied"] == ["0052_x"]
    assert pm.active_slot() == a
    text = pm.format_result(out)
    assert "0053_y failed" in text and "live is unchanged" in text and "applied 0052_x" in text


def test_red_for_another_reason_reads_as_before(home, monkeypatch, tmp_path):
    db = FakeDb([], other_red=True)
    _promotable(home, monkeypatch, tmp_path, db)
    out = pm.promote("origin/main")
    assert not out["ok"] and out["gate"][0]["detail"] == "red: outbox"
    # Pending migrations next to another red keep the plain red list.
    db2 = FakeDb(["0052_x"], other_red=True)
    monkeypatch.setattr(pm, "_run", db2.run)
    out2 = pm.promote("origin/main")
    assert out2["gate"][0]["detail"] == "red: schema_migrations, outbox"
    assert "pending_migrations" in out2["gate"][0]


def test_the_flag_with_nothing_pending_is_a_no_op(home, monkeypatch, tmp_path):
    db = FakeDb([])
    _a, b = _promotable(home, monkeypatch, tmp_path, db)
    out = pm.promote("origin/main", apply_migrations=True)
    assert out["ok"], out
    assert out["migrations"] == {"ok": True, "pending": [], "applied": []}
    assert [c[-1] for c in db.migrate_calls()] == ["--dry-run"]  # listed, never run
    assert pm.active_slot() == b
    assert "nothing pending" in pm.format_result(out)


def test_the_flag_is_refused_beside_a_non_promotion(home, capsys):
    from khipu import cli

    assert cli.main(["promote", "--status", "--apply-migrations"]) == 2
    assert "promotion only" in capsys.readouterr().out
