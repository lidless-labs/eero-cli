from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from eero_cli import cli


class FormattingTests(unittest.TestCase):
    def test_device_id_from_url(self) -> None:
        self.assertEqual(cli._device_id_from_url("/2.2/devices/device-1/"), "device-1")

    def test_extract_networks_from_nested_response(self) -> None:
        response = {"data": {"networks": [{"id": "net-1", "name": "Home"}]}}

        self.assertEqual(cli._extract_networks(response), [{"id": "net-1", "name": "Home"}])

    def test_network_id_from_url_fallback(self) -> None:
        self.assertEqual(
            cli._network_id_of({"url": "https://api.eero.com/2.2/networks/net-2"}),
            "net-2",
        )

    def test_last_active_handles_iso_timestamp(self) -> None:
        value = cli._fmt_last_active(datetime.now(timezone.utc).isoformat())

        self.assertTrue(value.endswith("ago"))


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.devices = [
            {
                "nickname": "Kitchen Speaker",
                "hostname": "speaker-1",
                "mac": "BC:24:11:AA:BB:CC",
                "connected": False,
            },
            {
                "nickname": "Office Laptop",
                "hostname": "laptop",
                "mac": "AA-BB-CC-00-11-22",
                "connected": True,
            },
            {
                "nickname": "",
                "hostname": "garage-sensor",
                "mac": "DE:AD:BE:EF:00:01",
                "connected": False,
            },
        ]

    def test_filter_devices_by_name_regex(self) -> None:
        result = cli._filter_devices(self.devices, name_regex="speaker")

        self.assertEqual([d["nickname"] for d in result], ["Kitchen Speaker"])

    def test_filter_devices_by_mac_prefix(self) -> None:
        result = cli._filter_devices(self.devices, mac_prefix="aabb")

        self.assertEqual([d["nickname"] for d in result], ["Office Laptop"])

    def test_filter_devices_by_online_state(self) -> None:
        result = cli._filter_devices(self.devices, only_online=True)

        self.assertEqual([d["nickname"] for d in result], ["Office Laptop"])

    def test_filter_devices_by_offline_state(self) -> None:
        result = cli._filter_devices(self.devices, only_offline=True)

        self.assertEqual(len(result), 2)

    def test_print_device_table_reports_empty_match(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            cli._print_device_table([])

        self.assertIn("(no devices match)", output.getvalue())


class NetworkSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_network_id_requires_explicit_network_for_destructive_multi_network(self) -> None:
        client = AsyncMock()
        client.get_networks.return_value = {
            "data": {
                "networks": [
                    {"id": "net-1", "name": "Home"},
                    {"id": "net-2", "name": "Lab"},
                ],
            },
        }

        with self.assertRaises(SystemExit) as ctx:
            await cli._resolve_network_id(client, destructive=True)

        self.assertIn("pass --network-id", str(ctx.exception))

    async def test_resolve_network_id_accepts_requested_visible_network(self) -> None:
        client = AsyncMock()
        client.get_networks.return_value = {"data": {"networks": [{"id": "net-1", "name": "Home"}]}}

        self.assertEqual(await cli._resolve_network_id(client, "net-1", destructive=True), "net-1")


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_creates_private_session_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            session_path = Path(temp_dir) / "nested" / "session.json"
            args = argparse.Namespace(session_path=session_path, identifier="user@example.com", code=None)
            client = AsyncMock()
            client.__aenter__.return_value = client
            client.login.return_value = True
            client._api.auth._credentials.session_expiry = None
            client._api.auth._save_credentials = AsyncMock()

            with patch.object(cli, "EeroClient", return_value=client):
                code = await cli._auth(args)

            self.assertEqual(code, 0)
            self.assertTrue(session_path.parent.exists())
            self.assertEqual(session_path.parent.stat().st_mode & 0o777, 0o700)
            self.assertIsNotNone(client._api.auth._credentials.session_expiry)


if __name__ == "__main__":
    unittest.main()
