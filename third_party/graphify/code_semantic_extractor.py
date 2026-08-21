"""Semantic layer extractor — Gemini 2.5 Flash-Lite over workspace Python files.

Produces structured JSON descriptions (summary, domain tags, function purposes)
for each .py file and writes the aggregate semantic_layer.json consumed by the
graph builder (build_graph.collect_code_semantic). Uses only stdlib + urllib;
caches each result by file content SHA-256 so re-runs are free for unchanged
files. The slow LLM pass is decoupled from the fast build_graph.py rebuild.

Model: gemini-2.5-flash-lite (cheap, structured-JSON via responseMimeType).
Calls include exponential-backoff retry on 429/5xx so a full run does not
collapse under rate limiting.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from code_ast_extractor import SKIP_DIRS, slug

WORKSPACE = Path(__file__).resolve().parent
API_KEY_FILE = Path(
    os.environ.get(
        "KHIPU_GEMINI_KEY_FILE",
        str(Path.home() / ".config" / "khipu" / "gemini_key"),
    )
)
CACHE_DIR = Path(
    os.environ.get(
        "KHIPU_GRAPHIFY_SEMANTIC_CACHE",
        str(Path.home() / "Library" / "Application Support" / "Khipu" / "state" / "semantic_cache"),
    )
)
OUT_PATH = Path(
    os.environ.get(
        "KHIPU_GRAPHIFY_SEMANTIC_LAYER",
        str(Path.home() / "Library" / "Application Support" / "Khipu" / "state" / "semantic_layer.json"),
    )
)
MODEL = 'gemini-2.5-flash-lite'
MAX_CHARS = 24000
WORKERS = 3
MAX_RETRIES = 5

SYSTEM_PROMPT = (
    'You are a senior code analyst. Given one Python source file, produce a '
    'concise structured semantic description. Respond with VALID JSON ONLY, '
    'no markdown fences, no prose.'
)

USER_PROMPT_TEMPLATE = (
    'Analyze this Python file and return JSON with exactly these keys:\n'
    '  "summary": a 1-2 sentence plain-English description of what the file does\n'
    '  "domain_tags": an array of 3-6 short lowercase-hyphenated topic tags '
    '(e.g. "options-pricing", "pdf-rendering", "graph-extraction")\n'
    '  "functions": an array of objects {"name": <function name>, "purpose": '
    '<one-line purpose>} for the most important top-level functions/classes '
    '(max 12)\nFile path: {relpath}\n\n```python\n{content}\n```'
)


def find_code_files(roots: list[Path]) -> list[tuple[Path, Path]]:
    """Return (file_path, workspace_root) for each .py under the given roots."""
    files: list[tuple[Path, Path]] = []
    for workspace in roots:
        if not workspace.is_dir():
            continue
        for root, dirnames, filenames in os.walk(workspace):
            dirnames[:] = [
                d for d in dirnames
                if d not in SKIP_DIRS and not d.startswith('.')
            ]
            for name in filenames:
                if name.endswith('.py'):
                    files.append((Path(root) / name, workspace))
    return sorted(files, key=lambda pair: str(pair[0]))


def code_roots_from_resolved() -> list[Path]:
    """Resolve extractor roots from graph_sources.resolved.json."""
    override = (os.environ.get('KHIPU_GRAPH_SOURCES_RESOLVED') or '').strip()
    resolved = Path(override) if override else (
        Path.home() / 'Library' / 'Application Support' / 'Khipu' / 'graph_sources.resolved.json'
    )
    if not resolved.is_file():
        return []
    try:
        raw = json.loads(resolved.read_text(encoding='utf-8'))
    except (OSError, ValueError) as e:
        msg = f'unreadable graph_sources.resolved.json at {resolved}: {e}'
        print(f'ERROR: {msg}', file=sys.stderr)
        raise RuntimeError(msg) from e
    if not isinstance(raw, dict):
        msg = f'graph_sources.resolved.json at {resolved} is not a JSON object'
        print(f'ERROR: {msg}', file=sys.stderr)
        raise RuntimeError(msg)
    coll = raw.get('collectors') if isinstance(raw.get('collectors'), dict) else {}
    if coll.get('code_semantic') is False:
        return []
    roots = raw.get('code_roots')
    if not isinstance(roots, list):
        return []
    return [Path(p) for p in roots if Path(p).is_dir()]


def find_code_files_legacy(workspace: Path) -> list[Path]:
    return [p for p, _ in find_code_files([workspace])]


def call_gemini(api_key: str, relpath: str, content: str) -> dict:
    content = content[:MAX_CHARS]
    # .replace (not .format) — the template contains literal JSON braces.
    prompt = USER_PROMPT_TEMPLATE.replace('{relpath}', relpath).replace(
        '{content}', content)
    url = (
        f'https://generativelanguage.googleapis.com/v1beta/models/'
        f'{MODEL}:generateContent?key={api_key}'
    )
    body = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'systemInstruction': {'parts': [{'text': SYSTEM_PROMPT}]},
        'generationConfig': {
            'maxOutputTokens': 1024,
            'responseMimeType': 'application/json',
        },
    }
    data = json.dumps(body).encode('utf-8')

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(
                url, data=data,
                headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=60) as resp:
                parsed = json.loads(resp.read().decode('utf-8'))
            candidates = parsed.get('candidates', [])
            text = ''
            if candidates:
                parts = candidates[0].get('content', {}).get('parts', [])
                text = ''.join(p.get('text', '') for p in parts)
            result = json.loads(text)
            usage = parsed.get('usageMetadata', {})
            return {
                'summary': result.get('summary', ''),
                'domain_tags': result.get('domain_tags', []),
                'functions': result.get('functions', []),
                '_input_tokens': usage.get('promptTokenCount', 0),
                '_output_tokens': usage.get('candidatesTokenCount', 0),
                '_status': 'ok',
            }
        except urllib.error.HTTPError as e:
            # Retry on rate-limit / transient server errors with backoff.
            if e.code in (429, 500, 502, 503) and attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt + 1)
                continue
            return {'_status': 'error', '_error': f'HTTP {e.code}'}
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(2 ** attempt + 1)
                continue
            return {'_status': 'error', '_error': str(e)}
    return {'_status': 'error', '_error': 'retries exhausted'}


def process_file(api_key: str, workspace: Path, fpath: Path) -> dict:
    relpath = fpath.relative_to(workspace).as_posix()
    try:
        text = fpath.read_text(encoding='utf-8', errors='ignore')
    except Exception:
        text = ''
    sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
    cache_file = CACHE_DIR / f'{sha}.json'
    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding='utf-8'))
        data['_cache_hit'] = True
        data['relpath'] = relpath
        return data
    result = call_gemini(api_key, relpath, text)
    result['relpath'] = relpath
    result['_cache_hit'] = False
    if result.get('_status') == 'ok':
        cache_file.write_text(json.dumps(result), encoding='utf-8')
    return result


def collect_code_semantic(roots: list[Path], now_iso: str) -> tuple[list, list]:
    """Read semantic_layer.json -> graph nodes/edges. Used by build_graph.py.

    Emits domain-concept nodes (shared across files, so modules tagged with the
    same domain become cross-linked), module-summary concept nodes, and
    per-function purpose concept nodes. Returns ([], []) gracefully if the
    semantic layer has not been built yet. Edges to function nodes that do not
    resolve (nested defs etc.) are harmlessly dropped by build_graph Phase 3.
    """
    if not OUT_PATH.exists():
        return [], []
    try:
        layer = json.loads(OUT_PATH.read_text(encoding='utf-8'))
    except Exception:
        return [], []
    nodes: dict = {}
    edges: list = []

    def _relpath_in_roots(relpath: str) -> bool:
        for root in roots:
            if (root / relpath).is_file():
                return True
        return False

    def _node(nid: str, name: str) -> None:
        nodes[nid] = {
            'id': nid, 'type': 'concept', 'bucket': 'code', 'name': name,
            'payload': json.dumps({}), 'source_path': None,
            'built_at': now_iso, 'frozen': 0,
        }

    def _edge(src: str, dst: str, etype: str) -> None:
        edges.append({
            'src': src, 'dst': dst, 'type': etype, 'weight': None,
            'payload': json.dumps({}), 'built_at': now_iso,
        })

    for f in layer.get('files', []):
        relpath = f.get('relpath', '')
        if not relpath or not _relpath_in_roots(relpath):
            continue
        mslug = slug(relpath)
        module_id = f'module:{mslug}'
        summary = (f.get('summary') or '').strip()
        if summary:
            sid = f'concept:smry__{mslug}'
            _node(sid, summary[:400])
            _edge(sid, module_id, 'rationale_for')
        for tag in f.get('domain_tags', []) or []:
            tag = str(tag).strip().lower()
            if not tag:
                continue
            did = f'concept:domain__{slug(tag)}'
            _node(did, f'domain: {tag}')
            _edge(module_id, did, 'concept_link')
        for fn in f.get('functions', []) or []:
            fname = str(fn.get('name', '')).strip()
            purpose = str(fn.get('purpose', '')).strip()
            if not fname or not purpose:
                continue
            pid = f'concept:fnp__{mslug}__{slug(fname)}'
            _node(pid, f'{fname}: {purpose}'[:200])
            _edge(pid, f'function:{mslug}::{fname}', 'rationale_for')
    print(f'code_semantic: {len(nodes)} nodes, {len(edges)} edges '
          f'from semantic_layer.json')
    return list(nodes.values()), edges


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    roots = code_roots_from_resolved()
    if not roots:
        print(
            'code_semantic: skipped (no ingest roots); '
            'keeping existing semantic_layer.json'
        )
        return

    api_key = API_KEY_FILE.read_text(encoding='utf-8').strip().splitlines()[0].strip()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    all_file_pairs = find_code_files(roots)
    all_files = [p for p, _ in all_file_pairs]
    total = len(all_files)

    uncached: list[tuple[Path, Path]] = []
    for fpath, workspace in all_file_pairs:
        text = fpath.read_text(encoding='utf-8', errors='ignore')
        sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
        if not (CACHE_DIR / f'{sha}.json').exists():
            uncached.append((fpath, workspace))

    if args.dry_run:
        uncached_count = len(uncached)
        est_input = uncached_count * 3500
        est_output = uncached_count * 500
        # Gemini 2.5 Flash-Lite: ~$0.10/M input, ~$0.40/M output.
        cost = (est_input / 1_000_000) * 0.10 + (est_output / 1_000_000) * 0.40
        print(
            f'Files: {total}, uncached: {uncached_count}, '
            f'est. cost ${cost:.2f}'
        )
        return

    work_list = uncached[: args.limit] if args.limit else uncached
    cached_results: list[dict] = []
    for fpath, workspace in all_file_pairs:
        if (fpath, workspace) not in work_list:
            text = fpath.read_text(encoding='utf-8', errors='ignore')
            sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
            cache_file = CACHE_DIR / f'{sha}.json'
            if cache_file.exists():
                data = json.loads(cache_file.read_text(encoding='utf-8'))
                data['relpath'] = fpath.relative_to(workspace).as_posix()
                data['_cache_hit'] = True
                cached_results.append(data)

    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fut_to_path = {
            ex.submit(process_file, api_key, workspace, fpath): (fpath, workspace)
            for fpath, workspace in work_list
        }
        for fut in concurrent.futures.as_completed(fut_to_path):
            results.append(fut.result())

    all_results = cached_results + results
    all_results.sort(key=lambda x: x.get('relpath', ''))

    ok_results = [r for r in all_results if r.get('_status') == 'ok']
    errors = sum(1 for r in all_results if r.get('_status') == 'error')
    cache_hits = sum(1 for r in all_results if r.get('_cache_hit'))
    # Cost/token totals count ONLY fresh API calls this run — cache hits carry
    # stored token counts from when they were created and must not be re-summed
    # (otherwise the reported cost balloons on every cache-heavy nightly run).
    fresh = [r for r in ok_results if not r.get('_cache_hit')]
    input_tokens = sum(r.get('_input_tokens', 0) for r in fresh)
    output_tokens = sum(r.get('_output_tokens', 0) for r in fresh)

    out = {
        'built_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'model': MODEL,
        'files': [
            {
                'relpath': r['relpath'],
                'summary': r.get('summary', ''),
                'domain_tags': r.get('domain_tags', []),
                'functions': r.get('functions', []),
            }
            for r in ok_results
        ],
    }
    OUT_PATH.write_text(json.dumps(out, indent=2), encoding='utf-8')

    cost = (input_tokens / 1_000_000) * 0.10 + (output_tokens / 1_000_000) * 0.40
    err_files = [
        r.get('relpath', '?')
        for r in all_results
        if r.get('_status') == 'error'
    ]
    print(
        f'total={total} cache_hits={cache_hits} api_calls={len(results)} '
        f'errors={errors} in_tok={input_tokens} out_tok={output_tokens} '
        f'cost=${cost:.2f}'
    )
    if err_files:
        print('error_files=' + ','.join(err_files[:20]))


if __name__ == '__main__':
    main()
