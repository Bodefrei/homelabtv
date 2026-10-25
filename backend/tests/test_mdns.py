import unittest
from unittest.mock import AsyncMock, patch

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mdns


class MdnsAdvertisementTests(unittest.IsolatedAsyncioTestCase):
    async def test_registers_homelabtv_service_on_backend_port(self):
        class FakeAsyncZeroconf:
            def __init__(self, ip_version):
                self.ip_version = ip_version
                self.async_register_service = AsyncMock()
                self.async_close = AsyncMock()

        with (
            patch("mdns.AsyncZeroconf", FakeAsyncZeroconf),
            patch("mdns._lan_ipv4_address", return_value=b"\x0a\x00\x00\x02"),
        ):
            zeroconf = await mdns.register_mdns_service()

        service = zeroconf.async_register_service.await_args.args[0]
        self.assertEqual(zeroconf.ip_version, mdns.IPVersion.V4Only)
        self.assertEqual(service.type, "_homelabtv._tcp.local.")
        self.assertEqual(service.port, 30060)
        self.assertEqual(service.addresses, [b"\x0a\x00\x00\x02"])
        self.assertEqual(service.server, "homelabtv.local.")

    async def test_closes_zeroconf_if_registration_fails(self):
        class FakeAsyncZeroconf:
            def __init__(self, ip_version):
                self.ip_version = ip_version
                self.async_register_service = AsyncMock(side_effect=RuntimeError("registration failed"))
                self.async_close = AsyncMock()

        with (
            patch("mdns.AsyncZeroconf", FakeAsyncZeroconf),
            patch("mdns._lan_ipv4_address", return_value=b"\x0a\x00\x00\x02"),
        ):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                await mdns.register_mdns_service()
