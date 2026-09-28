import json
import socket
from pathlib import Path
from unittest import mock

import pytest

BASE = Path('/private/tmp/khipu-parity-20260927')
selectors = json.loads((BASE / 'test-selectors.json').read_text())
blocked = 'Live database/network access is prohibited in this offline parity review'
with mock.patch('khipu.db.connect', side_effect=AssertionError(blocked)), \
     mock.patch('khipu.db.resolve_dsn', side_effect=AssertionError(blocked)), \
     mock.patch.object(socket.socket, 'connect', side_effect=AssertionError(blocked)):
    raise SystemExit(pytest.main(['-q', '-p', 'no:cacheprovider', *selectors]))
