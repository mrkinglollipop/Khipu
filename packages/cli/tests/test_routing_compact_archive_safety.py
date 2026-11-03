"""Crash, corruption, retention and reconstruction regressions; no live state."""
import gzip
import json
import os
from datetime import timedelta

import pytest

from khipu import routing, routing_compact as rc
from tests import routing_compact_synth as synth
from tests.test_routing_compact import END, _log, world


def _chosen():
    return {"before": 3, "positions": [1], "left_out": [b'B'], "kept": [b'A', b'C']}


def test_uncommitted_archive_cannot_duplicate_live_lines(world):
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'A\nB\nC\n')
    folder = rc.archive_dir()
    folder.mkdir()
    (folder / 'events-20261102T120000Z-aaaaaa.jsonl.gz').write_bytes(rc._archive_bytes(_chosen(), END))
    assert rc.full_log() == [b'A', b'B', b'C']


def test_committed_generation_reconstructs_with_appended_tail(world):
    path = routing.events_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    rc._replace(path, b'A\nC\nD\n', 'events-20261102T120000Z-aaaaaa.jsonl.gz', rc._archive_bytes(_chosen(), END))
    assert rc.full_log() == [b'A', b'B', b'C', b'D']

