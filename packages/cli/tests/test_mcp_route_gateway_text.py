"""MCP gateway route copy text: each test fails when the tool text is stale."""
from __future__ import annotations

import unittest

from khipu import mcp_server


class GatewayRouteTextTest(unittest.TestCase):
    def test_module_docstring_matches_the_gateway_route_copy(self):
        self.assertNotIn(
            "the gateway answers\n    ``available: false``). Khipu reports",
            mcp_server.__doc__,
        )

    def test_gateway_refusal_reason_does_not_claim_no_copy_while_route_serves_one(self):
        self.assertNotIn("has no copy yet", mcp_server.ROUTING_GATEWAY_REASON)
