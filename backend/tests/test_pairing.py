import base64
import asyncio
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import database
import main


ADMIN_TOKEN = "test-admin-token-" + secrets.token_urlsafe(24)


class PairingApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_db_path = database.DB_PATH
        self.previous_admin_token = os.environ.get("HOMELAB_ADMIN_TOKEN")
        database.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        os.environ["HOMELAB_ADMIN_TOKEN"] = ADMIN_TOKEN
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        database.DB_PATH = self.previous_db_path
        if self.previous_admin_token is None:
            os.environ.pop("HOMELAB_ADMIN_TOKEN", None)
        else:
            os.environ["HOMELAB_ADMIN_TOKEN"] = self.previous_admin_token
        self.temp_dir.cleanup()

    @staticmethod
    def pair_payload(device_id="test-tv-0001", device_secret=None):
        return {
            "device_id": device_id,
            "device_secret": device_secret or secrets.token_urlsafe(32),
            "friendly_name": "Living room TV",
            "app_version": "1.0.0",
            "android_version": "14",
            "manufacturer": "Example",
            "model": "TV",
        }

    @staticmethod
    def admin_headers():
        return {"Authorization": f"Bearer {ADMIN_TOKEN}"}

    def test_pairing_approval_and_one_time_credential_delivery(self):
        payload = self.pair_payload()
        start = self.client.post("/api/v1/devices/pair", json=payload)
        self.assertEqual(start.status_code, 200)
        start_data = start.json()
        self.assertRegex(start_data["user_code"], r"^[0-9]{6}$")
        self.assertEqual(start_data["expires_in_seconds"], 300)
        self.assertNotIn("device_secret", start_data)
        self.assertEqual(self.client.get("/api/v1/devices").status_code, 401)
        self.assertEqual(self.client.post(
            "/api/v1/pairing/approve",
            json={"user_code": start_data["user_code"]},
        ).status_code, 401)

        waiting = self.client.post(
            f"/api/v1/devices/{payload['device_id']}/poll",
            json={"device_secret": payload["device_secret"]},
        )
        self.assertEqual(waiting.json()["status"], "pending")

        approved = self.client.post(
            "/api/v1/pairing/approve",
            headers=self.admin_headers(),
            json={"user_code": start_data["user_code"]},
        )
        self.assertEqual(approved.status_code, 200)
        result = self.client.post(
            f"/api/v1/devices/{payload['device_id']}/poll",
            json={"device_secret": payload["device_secret"]},
        )
        self.assertEqual(result.status_code, 200)
        credential = result.json()["device_credential"]
        self.assertTrue(credential)
        self.assertEqual(result.json()["status"], "paired")

        second_poll = self.client.post(
            f"/api/v1/devices/{payload['device_id']}/poll",
            json={"device_secret": payload["device_secret"]},
        )
        self.assertEqual(second_poll.json(), {"status": "paired", "device_credential": None})
        identity = self.client.get(
            "/api/v1/devices/me",
            headers={"Authorization": f"Bearer {credential}"},
        )
        self.assertEqual(identity.status_code, 200)
        self.assertEqual(identity.json()["id"], payload["device_id"])

        db = sqlite3.connect(database.DB_PATH)
        try:
            stored_hash = db.execute(
                "SELECT credential_hash FROM device_credentials WHERE device_id = ?",
                (payload["device_id"],),
            ).fetchone()[0]
        finally:
            db.close()
        self.assertNotEqual(stored_hash, credential)
        self.assertEqual(
            self.client.get("/api/v1/devices", headers=self.admin_headers()).status_code,
            200,
        )

    def test_rejecting_pairing_does_not_issue_a_credential(self):
        payload = self.pair_payload()
        code = self.client.post("/api/v1/devices/pair", json=payload).json()["user_code"]
        response = self.client.post(
            "/api/v1/pairing/reject",
            headers=self.admin_headers(),
            json={"user_code": code},
        )
        self.assertEqual(response.status_code, 200)
        poll = self.client.post(
            f"/api/v1/devices/{payload['device_id']}/poll",
            json={"device_secret": payload["device_secret"]},
        )
        self.assertEqual(poll.json(), {"status": "rejected", "device_credential": None})

    def test_pairing_code_is_single_use(self):
        payload = self.pair_payload()
        code = self.client.post("/api/v1/devices/pair", json=payload).json()["user_code"]
        headers = self.admin_headers()
        self.assertEqual(
            self.client.post("/api/v1/pairing/approve", headers=headers, json={"user_code": code}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/api/v1/pairing/approve", headers=headers, json={"user_code": code}).status_code,
            404,
        )

    def test_pairing_secret_and_admin_authentication_are_required(self):
        payload = self.pair_payload()
        start = self.client.post("/api/v1/devices/pair", json=payload)
        self.assertEqual(start.status_code, 200)
        wrong_secret = self.client.post(
            f"/api/v1/devices/{payload['device_id']}/poll",
            json={"device_secret": secrets.token_urlsafe(32)},
        )
        self.assertEqual(wrong_secret.status_code, 404)
        bad_admin = self.client.post(
            "/api/v1/pairing/approve",
            headers={"Authorization": "Bearer wrong"},
            json={"user_code": start.json()["user_code"]},
        )
        self.assertEqual(bad_admin.status_code, 401)

    def test_admin_browser_basic_auth_is_supported_and_mapping_api_is_protected(self):
        self.assertEqual(self.client.get("/").status_code, 401)
        encoded = base64.b64encode(f"admin:{ADMIN_TOKEN}".encode()).decode()
        headers = {"Authorization": f"Basic {encoded}"}
        page = self.client.get("/", headers=headers)
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="pairing-code"', page.text)
        self.assertIn("/api/v1/pairing/approve", page.text)
        self.assertIn("/api/v1/pairing/reject", page.text)
        self.assertEqual(self.client.get("/api/mappings").status_code, 401)
        self.assertEqual(self.client.get("/api/mappings", headers=headers).status_code, 200)

    def test_revoked_credential_is_rejected(self):
        payload = self.pair_payload()
        code = self.client.post("/api/v1/devices/pair", json=payload).json()["user_code"]
        self.client.post(
            "/api/v1/pairing/approve",
            headers=self.admin_headers(),
            json={"user_code": code},
        )
        credential = self.client.post(
            f"/api/v1/devices/{payload['device_id']}/poll",
            json={"device_secret": payload["device_secret"]},
        ).json()["device_credential"]

        revoked = self.client.post(
            f"/api/v1/devices/{payload['device_id']}/revoke",
            headers=self.admin_headers(),
        )
        self.assertEqual(revoked.status_code, 200)
        identity = self.client.get(
            "/api/v1/devices/me",
            headers={"Authorization": f"Bearer {credential}"},
        )
        self.assertEqual(identity.status_code, 401)

    def paired_device(self):
        payload = self.pair_payload()
        started = self.client.post("/api/v1/devices/pair", json=payload).json()
        approved = self.client.post(
            "/api/v1/pairing/approve",
            headers=self.admin_headers(),
            json={"user_code": started["user_code"]},
        )
        self.assertEqual(approved.status_code, 200)
        credential = self.client.post(
            f"/api/v1/devices/{payload['device_id']}/poll",
            json={"device_secret": payload["device_secret"]},
        ).json()["device_credential"]
        return payload["device_id"], credential

    @staticmethod
    def inventory_channel(channel_id, display_number, display_name, **metadata):
        return {
            "channel_id": channel_id,
            "input_id": metadata.get("input_id", "tuner/input"),
            "display_number": display_number,
            "display_name": display_name,
            "original_display_name": metadata.get("original_display_name"),
            "service_type": metadata.get("service_type", "AUDIO_VIDEO"),
            "channel_type": metadata.get("channel_type", "TYPE_ATSC_T"),
            "network_id": metadata.get("network_id", 0x1234),
            "transport_stream_id": metadata.get("transport_stream_id", 0x5678),
            "service_id": metadata.get("service_id", 101),
            "internal_provider_id": metadata.get("internal_provider_id", "service-101"),
        }

    @staticmethod
    def inventory_payload(channels):
        entries = [main.DeviceChannelInventoryEntry.model_validate(channel) for channel in channels]
        return {
            "inventory_hash": main.calculate_inventory_hash(entries),
            "channels": channels,
        }

    def test_channel_inventory_is_authenticated_deduplicated_and_tracks_changes(self):
        device_id, credential = self.paired_device()
        headers = {"Authorization": f"Bearer {credential}"}
        initial_channels = [
            self.inventory_channel(101, "7.1", "Example One"),
            self.inventory_channel(202, "7.2", "Example Two"),
        ]

        unauthenticated = self.client.put(
            "/api/v1/devices/me/channels",
            json=self.inventory_payload(initial_channels),
        )
        self.assertEqual(unauthenticated.status_code, 401)

        first = self.client.put(
            "/api/v1/devices/me/channels",
            headers=headers,
            json=self.inventory_payload(initial_channels),
        )
        self.assertEqual(first.status_code, 200)
        first_data = first.json()
        self.assertTrue(first_data["changed"])
        self.assertEqual(first_data["revision"], 1)
        self.assertEqual(first_data["changes"]["added"], [101, 202])

        unchanged = self.client.put(
            "/api/v1/devices/me/channels",
            headers=headers,
            json=self.inventory_payload(initial_channels),
        )
        self.assertEqual(unchanged.status_code, 200)
        self.assertFalse(unchanged.json()["changed"])
        self.assertEqual(unchanged.json()["revision"], 1)

        rescanned_channels = [
            self.inventory_channel(101, "7.3", "Example One HD", service_id=102),
            self.inventory_channel(303, "8.1", "Example Three"),
        ]
        changed = self.client.put(
            "/api/v1/devices/me/channels",
            headers=headers,
            json=self.inventory_payload(rescanned_channels),
        )
        self.assertEqual(changed.status_code, 200)
        change_data = changed.json()
        self.assertTrue(change_data["changed"])
        self.assertEqual(change_data["revision"], 2)
        self.assertEqual(change_data["changes"]["added"], [303])
        self.assertEqual(change_data["changes"]["removed"], [202])
        self.assertEqual(change_data["changes"]["renamed"], [{
            "channel_id": 101,
            "previous_name": "Example One",
            "display_name": "Example One HD",
        }])
        self.assertEqual(change_data["changes"]["renumbered"], [{
            "channel_id": 101,
            "previous_number": "7.1",
            "display_number": "7.3",
        }])
        self.assertEqual(change_data["changes"]["updated"], [101])

        admin_channels = self.client.get(
            f"/api/v1/devices/{device_id}/channels",
            headers=self.admin_headers(),
        )
        self.assertEqual(admin_channels.status_code, 200)
        by_id = {channel["channel_id"]: channel for channel in admin_channels.json()}
        self.assertTrue(by_id[101]["available"])
        self.assertFalse(by_id[202]["available"])
        self.assertTrue(by_id[303]["available"])

        db = sqlite3.connect(database.DB_PATH)
        try:
            revisions = db.execute(
                "SELECT revision, channel_count FROM device_inventory_revisions "
                "WHERE device_id = ? ORDER BY revision",
                (device_id,),
            ).fetchall()
        finally:
            db.close()
        self.assertEqual(revisions, [(1, 2), (2, 2)])

    def test_channel_inventory_rejects_wrong_hash_and_duplicate_ids(self):
        _, credential = self.paired_device()
        headers = {"Authorization": f"Bearer {credential}"}
        channel = self.inventory_channel(101, "7.1", "Example One")

        wrong_hash = {"inventory_hash": "0" * 64, "channels": [channel]}
        response = self.client.put(
            "/api/v1/devices/me/channels",
            headers=headers,
            json=wrong_hash,
        )
        self.assertEqual(response.status_code, 422)

        duplicate = self.inventory_payload([channel, channel])
        response = self.client.put(
            "/api/v1/devices/me/channels",
            headers=headers,
            json=duplicate,
        )
        self.assertEqual(response.status_code, 422)

    def test_device_channel_inventory_is_not_exposed_to_unauthenticated_admins(self):
        device_id, _ = self.paired_device()
        response = self.client.get(f"/api/v1/devices/{device_id}/channels")
        self.assertEqual(response.status_code, 401)
        missing = self.client.get(
            "/api/v1/devices/not-a-device/channels",
            headers=self.admin_headers(),
        )
        self.assertEqual(missing.status_code, 404)

    def test_pairing_session_expires_after_five_minutes(self):
        payload = self.pair_payload()
        with patch("main.time.time", return_value=1_800_000_000):
            start = self.client.post("/api/v1/devices/pair", json=payload)
        with patch("main.time.time", return_value=1_800_000_301):
            poll = self.client.post(
                f"/api/v1/devices/{payload['device_id']}/poll",
                json={"device_secret": payload["device_secret"]},
            )
        self.assertEqual(poll.status_code, 200)
        self.assertEqual(poll.json()["status"], "expired")

    def test_pairing_start_is_rate_limited(self):
        response_codes = []
        for index in range(main.PAIRING_REQUEST_LIMIT + 1):
            response = self.client.post(
                "/api/v1/devices/pair",
                json=self.pair_payload(device_id=f"test-tv-{index:04d}"),
            )
            response_codes.append(response.status_code)
        self.assertEqual(response_codes[-1], 429)
        self.assertEqual(response_codes.count(200), main.PAIRING_REQUEST_LIMIT)

    def test_admin_authentication_failures_are_rate_limited(self):
        statuses = [
            self.client.get(
                "/api/v1/devices",
                headers={"Authorization": "Bearer incorrect-token"},
            ).status_code
            for _ in range(main.ADMIN_AUTH_FAILURE_LIMIT + 1)
        ]
        self.assertEqual(statuses[:main.ADMIN_AUTH_FAILURE_LIMIT], [401] * main.ADMIN_AUTH_FAILURE_LIMIT)
        self.assertEqual(statuses[-1], 429)

    def test_legacy_database_migration_preserves_existing_mapping(self):
        legacy_db_path = Path(self.temp_dir.name) / "legacy.db"
        legacy_db = sqlite3.connect(legacy_db_path)
        legacy_db.execute(
            """
            CREATE TABLE channel_mappings (
                physical_channel TEXT PRIMARY KEY,
                xmltv_id TEXT NOT NULL,
                custom_name TEXT,
                custom_image TEXT,
                xml_source TEXT
            )
            """
        )
        legacy_db.execute(
            "INSERT INTO channel_mappings (physical_channel, xmltv_id) VALUES ('7.1', 'abc')"
        )
        legacy_db.commit()
        legacy_db.close()

        previous_db_path = database.DB_PATH
        database.DB_PATH = str(legacy_db_path)
        try:
            asyncio.run(database.init_db())
        finally:
            database.DB_PATH = previous_db_path

        migrated_db = sqlite3.connect(legacy_db_path)
        try:
            mapping = migrated_db.execute(
                "SELECT physical_channel, xmltv_id, custom_logo FROM channel_mappings"
            ).fetchone()
            tables = {
                row[0]
                for row in migrated_db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        finally:
            migrated_db.close()
        self.assertEqual(mapping, ("7.1", "abc", None))
        self.assertIn("devices", tables)
        self.assertIn("pairing_rate_limits", tables)


if __name__ == "__main__":
    unittest.main()
