"""Read-only slice 13 review; all note text is synthetic."""
import builtins
import copy
import io
import json
import os
import socket
import sqlite3
import statistics
import subprocess
import time

import pytest

from khipu import recall_prompt as rp, routing_scanner as scanner


@pytest.fixture(autouse=True)
def hermetic(monkeypatch, tmp_path):
    monkeypatch.setenv('KHIPU_DATA_DIR', str(tmp_path / 'data'))
    from khipu import db
    def forbidden(*a, **kw):
        raise AssertionError('Unexpected external operation')
    monkeypatch.setattr(db, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)


def hit(body, *, ident='note:synthetic', kind='topic', snippet=None):
    return dict(kind=kind, id=ident, label='Synthetic note', status='active',
                snippet=body if snippet is None else snippet, body=body,
                frontmatter={'note_source': '/never/open/synthetic.md'})


@pytest.mark.parametrize('text', [
    'Codex is first choice for builds',
    'Main account tight until Mon 2026-10-19',
    'Codex is paused on 2026-10-19.',
    'Codex is limited (2026-10-19).',
    'Codex is tight with 80% used.',
    'Codex is low with 9% remaining.',
    'Haiku stays on the PRIMARY account',
    'Sonnet is first choice for builds.',
    '| Codex | paused | until 2026-10-19 |',
    '| Account | State | Until |\n|---|---|---|\n| Codex | paused | 2026-10-19 |',
    '> Current routing: Codex is paused until 2026-10-19.',
    'Codex was down yesterday; Codex is first choice for builds now.',
    'Prefer Codex for builds; reach it with codex exec.',
    '1. **Codex** — first choice for builds',
    '1. Codex — builds',
], ids=['first-choice', 'live-main', 'date-on', 'date-bare', 'level-used',
        'level-remaining', 'haiku-primary', 'sonnet-first', 'table-row',
        'state-table', 'current-blockquote', 'mixed-history-current',
        'ordering-with-howto', 'bold-ranked', 'ranked-description'])
def test_current_claims(text):
    assert scanner.scan_text('/synthetic/AGENTS.md', text), 'Current claim was missed'


@pytest.mark.parametrize('text', [
    '| Model | Good for | Weak at | Evidence |\n|---|---|---|---|\n| Codex | Only small patches; first drafts | Low-level parsing | 2026-10-19 |',
    '> History: Codex was paused on 2026-10-19.',
    'Run codex exec to reach the Codex lane.',
    '```python\n# Synthetic parser fixture, not current routing\nclaim = "Codex is paused until 2026-10-19"\n```',
    '~~~text\nCodex is paused until 2026-10-19\n~~~',
    'Codex is good for first drafts and low-level parsing.',
    'Codex supports only local execution in this example.',
    'History: on 2026-10-19 Matt paused Codex; the order changed.',
    'To reach Codex, open the terminal first.',
], ids=['capability-table', 'quoted-history', 'lane-command', 'backtick-fence',
        'tilde-fence', 'capability-prose', 'capability-only', 'past-record', 'howto-prose'])
def test_clean_shapes(text):
    assert not scanner.scan_text('/synthetic/note.md', text), 'Non-routing text was marked'


def test_scanner_no_io(monkeypatch):
    attempts = []
    def forbidden(*a, **kw):
        attempts.append(True)
        raise AssertionError('Scanner attempted filesystem access')
    with monkeypatch.context() as m:
        for mod, attr in [(builtins, 'open'), (io, 'open'), (os, 'open'),
                          (os, 'listdir'), (os, 'scandir'), (os, 'write')]:
            m.setattr(mod, attr, forbidden)
        result = scanner.scan_text('/API Keys/never-open', 'Codex is paused until 2026-10-19')
    assert result and not attempts


def test_hit_and_text_preserved():
    row = hit('Codex is paused until 2026-10-19')
    before = copy.deepcopy(row)
    block = rp.render_block([row])
    assert row == before
    assert row['id'] in block and row['snippet'] in block
    assert scanner.SUPERSEDED_MARK in block


def test_mark_does_not_drop_hits(monkeypatch):
    body = 'Codex is paused until 2026-10-19. ' + 'Synthetic context. ' * 8
    rows = [hit(body, ident=f'note:synthetic-{i}') for i in range(3)]
    with monkeypatch.context() as m:
        m.setattr(scanner, 'scan_text', lambda *args: [])
        baseline = rp.render_block(rows)
    assert all(row['id'] in baseline for row in rows), 'Baseline fixture must fit'
    block = rp.render_block(rows)
    assert all(row['id'] in block for row in rows), 'Advisory mark evicted a hit'


def test_scanner_exception_cannot_break_render(monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError('synthetic scanner failure')
    monkeypatch.setattr(scanner, 'scan_text', broken)
    row = hit('Codex is paused until 2026-10-19')
    block = rp.render_block([row])
    assert row['id'] in block


def test_scanner_exception_cannot_break_prior_work(monkeypatch):
    rows = [hit('Codex is paused until 2026-10-19')]
    monkeypatch.setattr(rp, '_search_hits', lambda *a, **kw: {'hits': rows, 'legs': ['synthetic']})
    monkeypatch.setattr(rp, '_deliverable_context_line', lambda *a, **kw: '')
    def broken(*a, **kw):
        raise RuntimeError('synthetic scanner failure')
    monkeypatch.setattr(scanner, 'scan_text', broken)
    out = rp.prior_work_for_prompt('Review the routing scanner synthetic contract')
    assert rows[0]['id'] in out['context']


def test_nonimported_history_not_marked():
    row = dict(kind='episode', id='12345', label='Synthetic historical summary',
               snippet='We chose Codex first for builds on 2026-10-19.')
    assert scanner.SUPERSEDED_MARK not in rp.render_block([row])


def test_actual_snapshot_search_loses_mark(monkeypatch, tmp_path):
    from khipu import hub_snapshot as hs
    from khipu import notes
    body = 'Synthetic capability evidence. ' * 40 + '\nCodex is paused until 2026-10-19'
    path = tmp_path / 'synthetic.md'
    path.write_text(body)
    topic = notes._note_topic_dict(path, project=None)
    con = sqlite3.connect(':memory:')
    con.execute('CREATE TABLE topics (slug TEXT, title TEXT, body TEXT, updated_at TEXT, created_at TEXT, status TEXT, frontmatter TEXT, deleted_at TEXT, superseded_by TEXT)')
    con.execute('INSERT INTO topics VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)',
                ('note:synthetic', 'Synthetic routing note', body, '2026-10-19', '2026-10-19', 'active',
                 json.dumps(topic['frontmatter'])))
    monkeypatch.setattr(hs, 'open_snapshot', lambda: con)
    rows = hs.search_snapshot('Codex', 3, kind='topic')
    rows = hs.snapshot_row_metadata(con, rows)
    assert len(rows) == 1
    assert 'body' not in rows[0] and 'paused' not in rows[0]['snippet']
    assert scanner.scan_text('/synthetic/note.md', body)
    assert scanner.SUPERSEDED_MARK in rp.render_block(rows), 'Actual snapshot search discarded the late claim'


def measure(fn, count=30):
    fn()
    values = []
    for _ in range(count):
        start = time.perf_counter()
        fn()
        values.append((time.perf_counter() - start) * 1000)
    return {'median_ms': round(statistics.median(values), 3),
            'p95_ms': round(sorted(values)[int(count * .95)-1], 3)}


def test_latency_shapes(monkeypatch):
    cases = {
        'three_search_snippets': [hit('Synthetic capability context. ' * 9, ident=f'note:s-{i}') for i in range(3)],
        'three_64k_bodies': [hit('Synthetic capability context.\n' * 2200, ident=f'note:b-{i}', snippet='Synthetic context.') for i in range(3)],
        'three_500char_numeric_snippets': [
            dict(kind='topic', id=f'note:n-{i}', snippet='Codex is low: ' + '8' * 486)
            for i in range(3)
        ],
    }
    report = {}
    for label, rows in cases.items():
        with monkeypatch.context() as m:
            m.setattr(scanner, 'scan_text', lambda *args: [])
            baseline = measure(lambda: rp.render_block(rows))
        full = measure(lambda: rp.render_block(rows))
        report[label] = {'baseline': baseline, 'scanner_enabled': full,
                         'median_added_ms': round(full['median_ms'] - baseline['median_ms'], 3)}
    print('LATENCY ' + json.dumps(report, sort_keys=True))
    assert report['three_search_snippets']['median_added_ms'] < 5
    assert report['three_500char_numeric_snippets']['median_added_ms'] < 5, 'Real-sized search rows exceed a few ms'


def test_long_numeric_level_latency():
    report = {}
    for size in [1000, 2000, 4000]:
        text = 'Codex is low: ' + '8' * size
        report[str(size)] = measure(lambda: scanner.scan_text('/synthetic/note.md', text), count=3)
    print('NUMERIC_LATENCY ' + json.dumps(report, sort_keys=True))
    assert report['4000']['median_ms'] < 5, 'Scanner exceeds a few ms on one 4 KB line'
