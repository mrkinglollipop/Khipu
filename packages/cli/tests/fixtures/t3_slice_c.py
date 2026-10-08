"""SQLite-backed hub cursor for hermetic T3 recall checks and benchmarks."""
import json
import sqlite3

from tests.fixtures.t3 import THREAD


def memory_fixture(episodes=1000):
    con = sqlite3.connect(':memory:', check_same_thread=False)
    con.executescript('''
        CREATE TABLE episodes(id INTEGER, raw TEXT, deleted_at TEXT, summary TEXT);
        CREATE TABLE decisions(id INTEGER, text TEXT, episode_id INTEGER, decided_at TEXT,
                               superseded_by INTEGER, retracted_at TEXT);
        CREATE TABLE commitments(id INTEGER, text TEXT, opened_episode INTEGER, opened_at TEXT,
                                 status TEXT, due_after TEXT);
    ''')
    con.executemany('INSERT INTO episodes VALUES (?, ?, NULL, ?)', (
        (i, json.dumps({'t3_thread_id': THREAD if i < 10 else 'other'}), f'recall hook fixture {i}')
        for i in range(episodes)
    ))
    con.execute("INSERT INTO decisions VALUES (1, 'Preserve the recall budget', 1, '2026-10-07', NULL, NULL)")
    con.execute("INSERT INTO commitments VALUES (2, 'Verify thread continuity', 2, '2026-10-07', 'open', NULL)")

    class Cursor:
        def execute(self, sql, params=None):
            if sql.startswith('SET '):
                return
            sql = sql.replace("e.raw->>'t3_thread_id'", "json_extract(e.raw, '$.t3_thread_id')")
            sql = sql.replace('%s', '?').replace('now()', "datetime('now')")
            self.rows = con.execute(sql, params or ()).fetchall()

        def fetchall(self):
            return self.rows

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    class Connection:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    return con, Connection
