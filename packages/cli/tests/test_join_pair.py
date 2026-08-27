"""Unit tests for nearby join connect helpers (no live Bonjour)."""

from __future__ import annotations

import errno
import os
import time
import unittest
from unittest import mock

from khipu import join_pair as jp


class ConnectCandidatesTest(unittest.TestCase):
    def test_prefers_txt_ipv4_then_getaddrinfo(self) -> None:
        with mock.patch.object(
            jp.socket,
            "getaddrinfo",
            return_value=[
                (jp.socket.AF_INET, 0, 0, "", ("10.0.0.9", 8788)),
                (jp.socket.AF_INET, 0, 0, "", ("10.0.0.8", 8788)),
            ],
        ):
            got = jp.connect_candidates("mbp.local", 8788, preferred_ipv4="192.168.1.50")
        self.assertEqual(
            got,
            [
                ("192.168.1.50", 8788),
                ("10.0.0.9", 8788),
                ("10.0.0.8", 8788),
                ("mbp.local", 8788),
            ],
        )

    def test_dedupes_preferred_already_in_getaddrinfo(self) -> None:
        with mock.patch.object(
            jp.socket,
            "getaddrinfo",
            return_value=[(jp.socket.AF_INET, 0, 0, "", ("192.168.1.50", 8788))],
        ):
            got = jp.connect_candidates("mbp.local", 8788, preferred_ipv4="192.168.1.50")
        self.assertEqual(got, [("192.168.1.50", 8788), ("mbp.local", 8788)])

    def test_skips_tailscale_preferred_and_link_local(self) -> None:
        with mock.patch.object(
            jp.socket,
            "getaddrinfo",
            return_value=[
                (jp.socket.AF_INET, 0, 0, "", ("100.64.1.2", 8788)),
                (jp.socket.AF_INET, 0, 0, "", ("192.168.1.50", 8788)),
            ],
        ):
            got = jp.connect_candidates("mbp.local", 8788, preferred_ipv4="100.64.1.2")
        self.assertEqual(got, [("192.168.1.50", 8788), ("mbp.local", 8788)])


class FriendlyConnectErrorTest(unittest.TestCase):
    def test_errno_65_mentions_airdrop_and_wifi(self) -> None:
        exc = OSError(errno.EHOSTUNREACH, "No route to host")
        msg = jp.friendly_connect_error(exc, [("mbp.local", 8788)])
        self.assertIn("Bonjour", msg)
        self.assertIn("AirDrop", msg)
        self.assertIn("Wi", msg)
        self.assertIn("mbp.local:8788", msg)

    def test_string_no_route_without_errno(self) -> None:
        exc = OSError("[Errno 65] No route to host")
        msg = jp.friendly_connect_error(exc, [("10.0.0.2", 8788)])
        self.assertIn("AirDrop", msg)
        self.assertIn("10.0.0.2:8788", msg)


class ResolveTxtIpv4Test(unittest.TestCase):
    def test_ipv4_txt_regex(self) -> None:
        line = "txtvers=1 ipv4=192.168.4.20 path=/"
        match = jp._IPV4_TXT_RE.search(line)
        assert match is not None
        self.assertEqual(match.group(1), "192.168.4.20")


class UsableLanIpv4Test(unittest.TestCase):
    def test_accepts_rfc1918(self) -> None:
        self.assertEqual(jp.usable_lan_ipv4("192.168.1.50"), "192.168.1.50")
        self.assertEqual(jp.usable_lan_ipv4("10.0.0.2"), "10.0.0.2")

    def test_rejects_loopback_link_local_cgnat(self) -> None:
        self.assertIsNone(jp.usable_lan_ipv4("127.0.0.1"))
        self.assertIsNone(jp.usable_lan_ipv4("169.254.1.1"))
        self.assertIsNone(jp.usable_lan_ipv4("100.64.0.1"))
        self.assertIsNone(jp.usable_lan_ipv4("100.127.255.254"))


class ResolveServiceGraceTest(unittest.TestCase):
    def test_returns_without_ipv4_instead_of_hanging(self) -> None:
        r_fd, w_fd = os.pipe()
        rf = os.fdopen(r_fd, "r")
        wf = os.fdopen(w_fd, "w")
        wf.write("Khipu Join (mbp)._khipu-join._tcp.local. can be reached at mbp.local.:8788\n")
        wf.flush()

        class _Proc:
            stdout = rf

            def terminate(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def kill(self) -> None:
                return None

        try:
            with mock.patch.object(jp.subprocess, "Popen", return_value=_Proc()):
                t0 = time.time()
                host, port, ipv4 = jp._resolve_service("Khipu Join (mbp)", timeout_s=5.0)
                elapsed = time.time() - t0
        finally:
            try:
                wf.close()
            except OSError:
                pass
            try:
                rf.close()
            except OSError:
                pass
        self.assertEqual(host, "mbp.local")
        self.assertEqual(port, 8788)
        self.assertIsNone(ipv4)
        self.assertLess(elapsed, 2.0)


if __name__ == "__main__":
    unittest.main()
