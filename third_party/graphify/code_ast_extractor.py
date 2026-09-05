"""Standalone stdlib-only AST extractor for the workspace knowledge graph.

Replaces graphify's code/AST layer. Walks the workspace's Python source,
builds module/function/class/concept nodes and defines/imports/calls_function/
inherits/rationale_for edges using only the stdlib `ast` module. Imported by
build_graph.py as the `collect_code_ast` collector.
"""

from __future__ import annotations

import ast
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SKIP_DIRS: set[str] = {
    ".python_libs",
    ".graphify-venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "ARCHIVE",
    "_archive",
    "_backups",
    "News Archive",
    "Training Data",
    "Reports",
    "outputs",
    "outputs_tmp",
    "Research",
    "massive_days",
    "Frozen Tell",
    "graphify-out",
    "API Keys",
    "My Portfolio",
    "Private Reports",
    "Fonts",
    "Marketing",
    "state",
    "text_cache",
    "data",
    "tests",
    "stock-analysis",
}


def slug(text: str) -> str:
    """Lowercase and replace non-alphanumeric chars with underscore."""
    return "".join(c if c.isalnum() else "_" for c in text.lower())


def _get_docstring_concept(
    owner_id: str, docstring: str, relpath: str, now_iso: str
) -> dict[str, Any]:
    """Create a concept node for a docstring."""
    owner_slug = slug(owner_id)
    concept_id = f"concept:{owner_slug}__doc"
    # Collapse whitespace/newlines to single spaces, take first 80 chars
    name = " ".join(docstring.split())[:80]
    payload = json.dumps({})
    return {
        "id": concept_id,
        "type": "concept",
        "bucket": "code",
        "name": name,
        "payload": payload,
        "source_path": relpath,
        "built_at": now_iso,
        "frozen": 0,
    }


def collect_code_ast(
    roots: list[Path], now_iso: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Walk each code root, parse Python files, emit code nodes and edges."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    # Deferred collections
    base_records: list[tuple[str, str]] = []  # (class_id, base_name)
    call_records: list[tuple[str, str]] = []  # (scope_id, callee_name)
    import_records: list[tuple[str, str]] = []  # (module_id, imported_stem)

    # For later resolution
    func_name_to_ids: dict[str, list[str]] = defaultdict(list)
    class_name_to_ids: dict[str, list[str]] = defaultdict(list)
    module_stem_to_ids: dict[str, list[str]] = defaultdict(list)

    parsed = 0
    skipped = 0

    for workspace in roots:
        if not workspace.is_dir():
            continue
        for root, dirnames, filenames in os.walk(workspace):
            # Prune in place
            dirnames[:] = [
                d
                for d in dirnames
                if d not in SKIP_DIRS and not d.startswith(".")
            ]
            filenames.sort()  # deterministic
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = Path(root) / fname
                try:
                    relpath = fpath.relative_to(workspace).as_posix()
                except ValueError:
                    continue
                module_slug = slug(relpath)
                module_id = f"module:{module_slug}"
                module_name = fname

                try:
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                    tree = ast.parse(text, filename=str(fpath))
                except (SyntaxError, ValueError):
                    skipped += 1
                    continue

                parsed += 1

                # Emit module node
                if module_id not in node_ids:
                    node_ids.add(module_id)
                    payload = json.dumps({"relpath": relpath})
                    nodes.append(
                        {
                            "id": module_id,
                            "type": "module",
                            "bucket": "code",
                            "name": module_name,
                            "payload": payload,
                            "source_path": relpath,
                            "built_at": now_iso,
                            "frozen": 0,
                        }
                    )
                    module_stem = Path(relpath).stem.lower()
                    module_stem_to_ids[module_stem].append(module_id)

                # Collect imports for this module
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            stem = alias.name.split(".")[-1].lower()
                            import_records.append((module_id, stem))
                    elif isinstance(node, ast.ImportFrom):
                        mod = node.module or ""
                        stem = mod.split(".")[-1].lower() if mod else ""
                        if stem:
                            import_records.append((module_id, stem))

                # Scope stack and traversal
                scope_stack: list[str] = [module_id]
                qualname_stack: list[str] = []

                def visit_body(body: list[ast.stmt]) -> None:
                    nonlocal scope_stack, qualname_stack
                    for stmt in body:
                        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            qualname_stack.append(stmt.name)
                            qualname = ".".join(qualname_stack)
                            func_id = f"function:{module_slug}::{qualname}"
                            if func_id not in node_ids:
                                node_ids.add(func_id)
                                payload = json.dumps({})
                                nodes.append(
                                    {
                                        "id": func_id,
                                        "type": "function",
                                        "bucket": "code",
                                        "name": stmt.name,
                                        "payload": payload,
                                        "source_path": relpath,
                                        "built_at": now_iso,
                                        "frozen": 0,
                                    }
                                )
                                func_name_to_ids[stmt.name].append(func_id)
                            edges.append(
                                {
                                    "src": scope_stack[-1],
                                    "dst": func_id,
                                    "type": "defines",
                                    "weight": None,
                                    "payload": json.dumps({}),
                                    "built_at": now_iso,
                                }
                            )
                            doc = ast.get_docstring(stmt)
                            if doc:
                                cnode = _get_docstring_concept(
                                    func_id, doc, relpath, now_iso
                                )
                                if cnode["id"] not in node_ids:
                                    node_ids.add(cnode["id"])
                                    nodes.append(cnode)
                                edges.append(
                                    {
                                        "src": cnode["id"],
                                        "dst": func_id,
                                        "type": "rationale_for",
                                        "weight": None,
                                        "payload": json.dumps({}),
                                        "built_at": now_iso,
                                    }
                                )
                            scope_stack.append(func_id)
                            visit_body(stmt.body)
                            scope_stack.pop()
                            qualname_stack.pop()

                        elif isinstance(stmt, ast.ClassDef):
                            qualname_stack.append(stmt.name)
                            qualname = ".".join(qualname_stack)
                            class_id = f"class:{module_slug}::{qualname}"
                            if class_id not in node_ids:
                                node_ids.add(class_id)
                                payload = json.dumps({})
                                nodes.append(
                                    {
                                        "id": class_id,
                                        "type": "class",
                                        "bucket": "code",
                                        "name": stmt.name,
                                        "payload": payload,
                                        "source_path": relpath,
                                        "built_at": now_iso,
                                        "frozen": 0,
                                    }
                                )
                                class_name_to_ids[stmt.name].append(class_id)
                            edges.append(
                                {
                                    "src": scope_stack[-1],
                                    "dst": class_id,
                                    "type": "defines",
                                    "weight": None,
                                    "payload": json.dumps({}),
                                    "built_at": now_iso,
                                }
                            )
                            for base in stmt.bases:
                                if isinstance(base, ast.Name):
                                    base_records.append((class_id, base.id))
                            doc = ast.get_docstring(stmt)
                            if doc:
                                cnode = _get_docstring_concept(
                                    class_id, doc, relpath, now_iso
                                )
                                if cnode["id"] not in node_ids:
                                    node_ids.add(cnode["id"])
                                    nodes.append(cnode)
                                edges.append(
                                    {
                                        "src": cnode["id"],
                                        "dst": class_id,
                                        "type": "rationale_for",
                                        "weight": None,
                                        "payload": json.dumps({}),
                                        "built_at": now_iso,
                                    }
                                )
                            scope_stack.append(class_id)
                            visit_body(stmt.body)
                            scope_stack.pop()
                            qualname_stack.pop()

                        else:
                            # Collect calls inside current scope
                            for sub in ast.walk(stmt):
                                if isinstance(sub, ast.Call):
                                    if isinstance(sub.func, ast.Name):
                                        callee = sub.func.id
                                    elif isinstance(sub.func, ast.Attribute):
                                        callee = sub.func.attr
                                    else:
                                        continue
                                    enclosing = scope_stack[-1]
                                    # Prefer nearest function over module
                                    for s in reversed(scope_stack):
                                        if s.startswith("function:"):
                                            enclosing = s
                                            break
                                    call_records.append((enclosing, callee))

                visit_body(tree.body)

                # Module docstring
                doc = ast.get_docstring(tree)
                if doc:
                    cnode = _get_docstring_concept(
                        module_id, doc, relpath, now_iso
                    )
                    if cnode["id"] not in node_ids:
                        node_ids.add(cnode["id"])
                        nodes.append(cnode)
                    edges.append(
                        {
                            "src": cnode["id"],
                            "dst": module_id,
                            "type": "rationale_for",
                            "weight": None,
                            "payload": json.dumps({}),
                            "built_at": now_iso,
                        }
                    )

    # Resolve deferred edges
    seen_edge_triples: set[tuple[str, str, str]] = set()

    # calls_function
    for scope_id, callee_name in call_records:
        matches = func_name_to_ids.get(callee_name, [])
        if 1 <= len(matches) <= 8:
            for m in matches:
                if scope_id == m:
                    continue
                triple = (scope_id, m, "calls_function")
                if triple not in seen_edge_triples:
                    seen_edge_triples.add(triple)
                    edges.append(
                        {
                            "src": scope_id,
                            "dst": m,
                            "type": "calls_function",
                            "weight": None,
                            "payload": json.dumps({}),
                            "built_at": now_iso,
                        }
                    )

    # inherits
    for subclass_id, base_name in base_records:
        matches = class_name_to_ids.get(base_name, [])
        if len(matches) == 1:
            dst = matches[0]
            if subclass_id == dst:
                continue
            triple = (subclass_id, dst, "inherits")
            if triple not in seen_edge_triples:
                seen_edge_triples.add(triple)
                edges.append(
                    {
                        "src": subclass_id,
                        "dst": dst,
                        "type": "inherits",
                        "weight": None,
                        "payload": json.dumps({}),
                        "built_at": now_iso,
                    }
                )

    # imports
    for module_id, stem in import_records:
        matches = module_stem_to_ids.get(stem, [])
        if len(matches) == 1:
            dst = matches[0]
            if module_id == dst:
                continue
            triple = (module_id, dst, "imports")
            if triple not in seen_edge_triples:
                seen_edge_triples.add(triple)
                edges.append(
                    {
                        "src": module_id,
                        "dst": dst,
                        "type": "imports",
                        "weight": None,
                        "payload": json.dumps({}),
                        "built_at": now_iso,
                    }
                )

    # Dedup nodes (keep first occurrence order)
    final_nodes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for n in nodes:
        if n["id"] not in seen:
            seen.add(n["id"])
            final_nodes.append(n)

    print(
        f"code_ast_extractor: parsed {parsed} files, skipped {skipped}, "
        f"{len(final_nodes)} nodes, {len(edges)} edges"
    )
    return final_nodes, edges


if __name__ == "__main__":
    import os

    # Workspace-root marker subdir — same knob as build_graph.py's
    # GRAPHIFY_STATE_SUBDIR (default "state"); set KHIPU_GRAPHIFY_STATE_DIR if
    # a maintainer's workspace nests graphify state under a different name.
    marker = (os.environ.get("KHIPU_GRAPHIFY_STATE_DIR") or "state").strip()
    cwd = Path.cwd()
    workspace = cwd
    for p in [cwd] + list(cwd.parents):
        if (p / marker).is_dir():
            workspace = p
            break
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    collect_code_ast(workspace, now_iso)
