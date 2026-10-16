"""config.json has one write path: ``config.mutate_config``.

Every writer in the package reads, changes and writes the document under one
lock, so a save of one setting can never write back a stale copy over another
writer's save. The source scan below fails when a module writes the file any
other way; the runtime checks pin what the path itself promises.
"""
from __future__ import annotations

import ast
import json
import re
import sys
import threading
from pathlib import Path

import pytest

from khipu import config

PACKAGE = Path(config.__file__).resolve().parent

# The lock and the loader behind the one path: config.py's own.
PRIVATE = {"_write_config", "config_write_lock", "_load_config_for_mutation"}
# Reads that use the strict loader without writing: (module, name) -> why.
PRIVATE_READS = {("join.py", "_load_config_for_mutation"):
                 "refuses a corrupt document before an import changes anything; writes go through mutate_config"}
# Writers that may call save_config: inside these blocks the document is the held one.
HOLDERS = {"mutate_config", "_locked"}
# Functions that save the document their caller holds: (module, name); every
# call of one must itself be inside a HOLDERS block.
HELD_SAVERS = {("accounts.py", "_commit")}
WRITES = {"write_text", "write_bytes", "replace", "rename", "unlink", "write_private_file", "_restore_file",
          "open", "dump"}
# Modules writing a config.json that is not this install's: module -> why.
OTHER_FILES = {"promote.py": "the copy a promotion slot gets, in that slot's own folder",
               "routing_replay.py": "a replay fixture in a temporary folder"}


def _modules():
    for path in sorted(PACKAGE.rglob("*.py")):
        if path.name != "config.py":
            yield path, ast.parse(path.read_text(encoding="utf-8"))


def _name(func) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _parents(tree):
    out = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def test_no_module_takes_the_config_plumbing():
    found = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            word = node.attr if isinstance(node, ast.Attribute) else node.name if isinstance(node, ast.alias) else None
            if word in PRIVATE and (path.name, word) not in PRIVATE_READS:
                found.append(f"{path.relative_to(PACKAGE)}:{getattr(node, 'lineno', '?')} {word}")
    assert not found, "write config.json only through config.mutate_config: " + ", ".join(found)


def test_every_save_is_inside_the_one_write_path():
    found = []
    for path, tree in _modules():
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _name(node.func) == "save_config"):
                continue
            if not _held(node, parents, path.name):
                found.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (path.name, _name(node.func)) in HELD_SAVERS \
                    and not _held(node, parents, None):
                found.append(f"{path.relative_to(PACKAGE)}:{node.lineno} {_name(node.func)}")
    assert not found, "save_config outside a mutate_config block: " + ", ".join(found)


def _held(node, parents, module) -> bool:
    """Whether ``node`` sits in a HOLDERS block, or (``module`` given) in a
    HELD_SAVERS function of that module."""
    up = parents.get(node)
    while up is not None:
        if isinstance(up, ast.With) and any(isinstance(item.context_expr, ast.Call)
                                            and _name(item.context_expr.func) in HOLDERS for item in up.items):
            return True
        if module and isinstance(up, ast.FunctionDef) and (module, up.name) in HELD_SAVERS:
            return True
        up = parents.get(up)
    return False


def test_no_module_writes_the_config_file_itself():
    found = []
    for path, tree in _modules():
        # Names a module gives the config file's path.
        aliases = {"config_file", "CONFIG_NAME"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and re.fullmatch(r"(\w+\.)?config_file\(\)|.*/ *(\w+\.)?CONFIG_NAME",
                                                             ast.unparse(node.value)):
                aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict) and any(isinstance(k, ast.Name) and k.id in aliases for k in node.keys):
                # A table of files to write back later (a whole-file restore).
                found.append(f"{path.relative_to(PACKAGE)}:{node.lineno} {ast.unparse(node)[:80]}")
            if not (isinstance(node, ast.Call) and _name(node.func) in WRITES):
                continue
            text = ast.unparse(node)
            if _name(node.func) == "open" and not any(m in text for m in ("'w", "'a", "'x")):
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | \
                    {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            if names & aliases or ("config.json" in text and path.name not in OTHER_FILES):
                found.append(f"{path.relative_to(PACKAGE)}:{node.lineno} {text[:80]}")
    assert not found, "config.json written outside config.mutate_config: " + ", ".join(found)


def test_the_scan_catches_a_stale_writer(tmp_path, monkeypatch):
    (tmp_path / "bad.py").write_text(
        "from khipu.config import load_config, save_config, config_file\n"
        "def stale():\n    data = load_config()\n    save_config(data)\n"
        "def direct():\n    path = config_file()\n    path.write_text('{}')\n")
    (tmp_path / "good.py").write_text(
        "from khipu import config\n"
        "def fine():\n    with config.mutate_config() as data:\n        config.save_config(data)\n")
    monkeypatch.setattr(sys.modules[__name__], "PACKAGE", tmp_path)
    with pytest.raises(AssertionError, match=r"bad\.py:4\b"):
        test_every_save_is_inside_the_one_write_path()
    with pytest.raises(AssertionError, match=r"bad\.py:7"):
        test_no_module_writes_the_config_file_itself()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("KHIPU_DATA_DIR", str(tmp_path))
    return tmp_path


def test_a_copy_read_earlier_is_refused(data_dir):
    stale = config.load_config()
    with pytest.raises(RuntimeError):
        config.save_config(stale)
    with config.mutate_config():
        with pytest.raises(RuntimeError):
            config.save_config(stale)
    assert not config.config_file().exists()


def test_nested_blocks_share_one_document_and_keep_both_changes(data_dir):
    with config.mutate_config() as outer:
        outer["a"] = 1
        with config.mutate_config() as inner:
            assert inner is outer
            inner["b"] = 2
        assert json.loads(config.config_file().read_text()) == {"a": 1, "b": 2}
        outer["c"] = 3
    assert config.load_config() == {"a": 1, "b": 2, "c": 3}


def test_a_failed_block_writes_nothing(data_dir):
    config.set_capture_mode("hub")
    with pytest.raises(ValueError), config.mutate_config() as data:
        data["capture_mode"] = "legacy"
        raise ValueError("stop")
    assert config.load_config() == {"capture_mode": "hub"}


def test_concurrent_writers_of_different_settings_keep_every_change(data_dir):
    barrier = threading.Barrier(8)

    def write(i):
        barrier.wait(3)
        with config.mutate_config() as data:
            data[f"k{i}"] = i

    threads = [threading.Thread(target=write, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert config.load_config() == {f"k{i}": i for i in range(8)}


def _account_list_writes(tree) -> list:
    """Each place a module changes ``usage_accounts`` itself: a subscript
    store or delete, or a ``pop``/``setdefault``/``update`` naming it."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            for t in targets:
                if isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant) \
                        and t.slice.value == "usage_accounts":
                    out.append(node)
        elif isinstance(node, ast.Call) and _name(node.func) in ("pop", "setdefault", "update") and any(
                isinstance(a, ast.Constant) and a.value == "usage_accounts" for a in node.args):
            out.append(node)
    return out


def test_the_account_list_has_one_writer():
    # The account list changes only through accounts.py's operations: one
    # store (``_store``), called only by ``_commit``, which every operation
    # and every generic save of the list goes through with its events.
    found = []
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = _parents(tree)
        for node in _account_list_writes(tree):
            up = parents.get(node)
            while up is not None and not isinstance(up, ast.FunctionDef):
                up = parents.get(up)
            where = f"{path.relative_to(PACKAGE)}:{node.lineno}"
            if path.name != "accounts.py" or up is None or up.name != "_store":
                found.append(where)
        if path.name == "accounts.py":
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and _name(node.func) == "_store":
                    up = parents.get(node)
                    while up is not None and not isinstance(up, ast.FunctionDef):
                        up = parents.get(up)
                    if up is None or up.name != "_commit":
                        found.append(f"accounts.py:{node.lineno} _store")
    assert not found, "usage_accounts written outside accounts._commit: " + ", ".join(found)
