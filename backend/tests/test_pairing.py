import base64
import asyncio
import hashlib
import os
import secrets
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
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
        self.previous_mdns_enabled = main.MDNS_ENABLED
        main.MDNS_ENABLED = False
        database.DB_PATH = str(Path(self.temp_dir.name) / "test.db")
        os.environ["HOMELAB_ADMIN_TOKEN"] = ADMIN_TOKEN
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        database.DB_PATH = self.previous_db_path
        main.MDNS_ENABLED = self.previous_mdns_enabled
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

    def test_admin_browser_uses_login_page_and_mapping_api_is_protected(self):
        login_page = self.client.get("/")
        self.assertEqual(login_page.status_code, 200)
        self.assertIn('id="login-form"', login_page.text)
        self.assertNotIn("www-authenticate", login_page.headers)
        encoded = base64.b64encode(f"admin:{ADMIN_TOKEN}".encode()).decode()
        headers = {"Authorization": f"Basic {encoded}"}
        page = self.client.get("/", headers=headers)
        self.assertIn('id="login-form"', page.text)
        self.assertEqual(self.client.get("/api/mappings").status_code, 401)
        self.assertEqual(self.client.get("/api/mappings", headers=headers).status_code, 200)

    def test_admin_login_creates_session_cookie_for_web_ui(self):
        invalid = self.client.post(
            "/api/v1/admin/login",
            json={"password": "not-the-token"},
        )
        self.assertEqual(invalid.status_code, 401)

        logged_in = self.client.post(
            "/api/v1/admin/login",
            json={"password": ADMIN_TOKEN},
        )
        self.assertEqual(logged_in.status_code, 200)
        self.assertIn("httponly", logged_in.headers["set-cookie"].lower())
        self.assertIn("samesite=strict", logged_in.headers["set-cookie"].lower())
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("XMLTV &amp; Sender", page.text)
        self.assertIn("Gemeinsame Favoritenlisten", page.text)
        self.assertIn("Fernseher und Senderzuordnung", page.text)
        self.assertIn('v-for="channel in pagedInventoryChannels"', page.text)
        self.assertIn('v-for="channel in pagedXmlChannels"', page.text)
        self.assertIn('v-for="channel in filteredAvailableListChannels"', page.text)
        self.assertIn('v-for="mapData in pagedMappings"', page.text)
        self.assertIn("saveExistingMapping(mapData.physical)", page.text)
        self.assertIn("deviceLabel(device)", page.text)
        self.assertIn("async deleteDevice()", page.text)
        self.assertIn("Fernseher löschen", page.text)
        self.assertIn("Anzeigename", page.text)
        self.assertNotIn('v-for="channel in xmlChannels"', page.text)
        self.assertEqual(self.client.get("/api/mappings").status_code, 200)

    def test_saving_existing_mapping_updates_name_without_removing_logo(self):
        db = sqlite3.connect(database.DB_PATH)
        try:
            db.execute(
                """
                INSERT INTO channel_mappings
                    (physical_channel, xmltv_id, custom_name, custom_logo)
                VALUES (?, ?, ?, ?)
                """,
                ("7.1", "channel.one", "Old Name", "/static/logos/7_1.png"),
            )
            db.commit()
        finally:
            db.close()

        response = self.client.post(
            "/api/mappings",
            headers=self.admin_headers(),
            json={
                "physical_channel": "7.1",
                "xmltv_id": "channel.one",
                "custom_name": "Updated Name",
                "custom_image": None,
                "xml_source": None,
            },
        )
        self.assertEqual(response.status_code, 200)

        db = sqlite3.connect(database.DB_PATH)
        try:
            row = db.execute(
                "SELECT custom_name, custom_logo FROM channel_mappings WHERE physical_channel = ?",
                ("7.1",),
            ).fetchone()
        finally:
            db.close()
        self.assertEqual(row, ("Updated Name", "/static/logos/7_1.png"))

        logged_out = self.client.post("/api/v1/admin/logout")
        self.assertEqual(logged_out.status_code, 200)
        self.assertIn("max-age=0", logged_out.headers["set-cookie"].lower())
        self.assertIn('id="login-form"', self.client.get("/").text)

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

    def test_admin_can_delete_device_and_its_device_data(self):
        device_id, _ = self.paired_device()
        db = sqlite3.connect(database.DB_PATH)
        try:
            db.execute(
                "INSERT INTO device_channels (device_id, channel_id, display_number, display_name, first_seen_at, last_seen_at) "
                "VALUES (?, 1, '1.1', 'Channel', 1, 1)",
                (device_id,),
            )
            db.execute(
                "INSERT INTO device_inventory_revisions "
                "(device_id, revision, inventory_hash, channel_count, changes_json, inventory_json, synced_at) "
                "VALUES (?, 1, 'hash', 1, '[]', '[]', 1)",
                (device_id,),
            )
            db.execute(
                "INSERT INTO channel_lists (id, name, is_system, is_shared, created_at, updated_at) "
                "VALUES ('list-1', 'Favorites', 0, 0, 1, 1)"
            )
            db.execute(
                "INSERT INTO device_list_assignments (id, device_id, list_id, is_active, assigned_at) "
                "VALUES ('assignment-1', ?, 'list-1', 1, 1)",
                (device_id,),
            )
            db.commit()
        finally:
            db.close()

        unauthorized = self.client.delete(f"/api/v1/devices/{device_id}")
        self.assertEqual(unauthorized.status_code, 401)

        deleted = self.client.delete(
            f"/api/v1/devices/{device_id}",
            headers=self.admin_headers(),
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"device_id": device_id, "deleted": True})
        self.assertEqual(self.client.get("/api/v1/devices", headers=self.admin_headers()).json(), [])

        db = sqlite3.connect(database.DB_PATH)
        try:
            for table in (
                "device_channels",
                "device_inventory_revisions",
                "device_credentials",
                "pairing_sessions",
                "device_list_assignments",
            ):
                count = db.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE device_id = ?",
                    (device_id,),
                ).fetchone()[0]
                self.assertEqual(count, 0, table)
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM channel_lists WHERE id = 'list-1'").fetchone()[0],
                1,
            )
        finally:
            db.close()

        missing = self.client.delete(
            f"/api/v1/devices/{device_id}",
            headers=self.admin_headers(),
        )
        self.assertEqual(missing.status_code, 404)

    def paired_device(self, device_id="test-tv-0001"):
        payload = self.pair_payload(device_id=device_id)
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

    def test_shared_channel_list_resolves_in_order_to_each_devices_channel_ids(self):
        tv_one, credential_one = self.paired_device("test-tv-one")
        tv_two, credential_two = self.paired_device("test-tv-two")
        headers_one = {"Authorization": f"Bearer {credential_one}"}
        headers_two = {"Authorization": f"Bearer {credential_two}"}
        admin = self.admin_headers()

        for device_id, credential, channels in (
            (tv_one, credential_one, [(110, "7.1", "One News"), (120, "7.2", "One Sports")]),
            (tv_two, credential_two, [(210, "2.1", "Two News"), (220, "2.2", "Two Sports")]),
        ):
            entries = [self.inventory_channel(*channel) for channel in channels]
            synced = self.client.put(
                "/api/v1/devices/me/channels",
                headers={"Authorization": f"Bearer {credential}"},
                json=self.inventory_payload(entries),
            )
            self.assertEqual(synced.status_code, 200, device_id)

        for device_id, channel_id, xmltv_id in (
            (tv_one, 110, "news.xml"),
            (tv_one, 120, "sports.xml"),
            (tv_two, 210, "news.xml"),
            (tv_two, 220, "sports.xml"),
        ):
            mapped = self.client.put(
                f"/api/v1/devices/{device_id}/channels/{channel_id}/mapping",
                headers=admin,
                json={"xmltv_id": xmltv_id},
            )
            self.assertEqual(mapped.status_code, 200)

        updated = self.client.put(
            "/api/v1/devices/me/channels",
            headers=headers_one,
            json=self.inventory_payload([
                self.inventory_channel(110, "7.1", "One News HD"),
                self.inventory_channel(120, "7.2", "One Sports"),
            ]),
        )
        self.assertEqual(updated.status_code, 200)
        refreshed = self.client.get(
            f"/api/v1/devices/{tv_one}/channels",
            headers=admin,
        )
        self.assertEqual(
            {item["channel_id"]: item["xmltv_id"] for item in refreshed.json()},
            {110: "news.xml", 120: "sports.xml"},
        )

        created = self.client.post(
            "/api/v1/channel-lists",
            headers=admin,
            json={
                "name": "Abend",
                "description": "Gemeinsame Favoriten",
                "entries": [
                    {"logical_channel_id": "sports.xml", "custom_name": "Sports Plus"},
                    {"logical_channel_id": "news.xml"},
                    {"logical_channel_id": "missing.xml"},
                ],
            },
        )
        self.assertEqual(created.status_code, 200)
        list_id = created.json()["id"]
        self.assertEqual(
            [entry["logical_channel_id"] for entry in created.json()["entries"]],
            ["sports.xml", "news.xml", "missing.xml"],
        )
        summary = self.client.get("/api/v1/channel-lists?summary=true", headers=admin)
        self.assertEqual(summary.status_code, 200)
        summary_list = next(item for item in summary.json() if item["id"] == list_id)
        self.assertEqual(summary_list["entry_count"], 3)
        self.assertNotIn("entries", summary_list)
        details = self.client.get(f"/api/v1/channel-lists/{list_id}", headers=admin)
        self.assertEqual(details.status_code, 200)
        self.assertEqual(
            [entry["logical_channel_id"] for entry in details.json()["entries"]],
            ["sports.xml", "news.xml", "missing.xml"],
        )
        self.assertEqual(details.json()["entries"][0]["custom_name"], "Sports Plus")
        second_list = self.client.post(
            "/api/v1/channel-lists",
            headers=admin,
            json={
                "name": "Morgens",
                "entries": [{"logical_channel_id": "news.xml"}],
            },
        )
        self.assertEqual(second_list.status_code, 200)
        second_list_id = second_list.json()["id"]

        for device_id in (tv_one, tv_two):
            assigned = self.client.put(
                f"/api/v1/devices/{device_id}/channel-list",
                headers=admin,
                json={"list_id": list_id},
            )
            self.assertEqual(assigned.status_code, 200)

        xmltv_channels = [
            {"id": "news.xml", "icon": "https://logos.example/news.png"},
            {"id": "sports.xml", "icon": "https://logos.example/sports.png"},
        ]

        async def cache_favorite_logos(channels):
            return [
                {**channel, "icon": f"/static/imgcache/{channel['id']}.png"}
                for channel in channels
            ]

        with (
            patch("main.xmltv_files", return_value=[]),
            patch("main._get_xmltv_data", new=AsyncMock(return_value=(xmltv_channels, []))),
            patch("main._cache_xmltv_channel_logos", new=AsyncMock(side_effect=cache_favorite_logos)) as cache_logos,
        ):
            resolved_one = self.client.get("/api/v1/devices/me/channel-list", headers=headers_one)
            resolved_two = self.client.get("/api/v1/devices/me/channel-list", headers=headers_two)
            selected = self.client.get(
                f"/api/v1/devices/me/channel-list?list_id={second_list_id}",
                headers=headers_one,
            )
        self.assertEqual(
            [item["channel_id"] for item in resolved_one.json()["channels"]],
            [120, 110, None],
        )
        self.assertEqual(
            [item["logo_url"] for item in resolved_one.json()["channels"]],
            [
                "http://testserver/static/imgcache/sports.xml.png",
                "http://testserver/static/imgcache/news.xml.png",
                None,
            ],
        )
        self.assertEqual(
            [channel["id"] for channel in cache_logos.await_args.args[0]],
            ["news.xml", "sports.xml"],
        )
        self.assertEqual(
            [item["list_number"] for item in resolved_one.json()["channels"]],
            [1, 2, 3],
        )
        self.assertEqual(resolved_one.json()["channels"][0]["custom_name"], "Sports Plus")
        self.assertEqual(resolved_one.json()["channels"][0]["display_name"], "Sports Plus")
        self.assertFalse(resolved_one.json()["channels"][-1]["available"])
        self.assertEqual(
            [item["channel_id"] for item in resolved_two.json()["channels"]],
            [220, 210, None],
        )
        self.assertFalse(resolved_two.json()["channels"][-1]["available"])
        list_options = self.client.get(
            "/api/v1/devices/me/channel-lists",
            headers=headers_one,
        )
        self.assertEqual(list_options.status_code, 200)
        self.assertEqual(
            {item["name"]: item["assigned"] for item in list_options.json()},
            {"Abend": True, "Morgens": False},
        )
        self.assertEqual(selected.status_code, 200)
        self.assertEqual(selected.json()["list_id"], second_list_id)
        self.assertEqual(
            [item["channel_id"] for item in selected.json()["channels"]],
            [110],
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/devices/me/channel-lists",
            ).status_code,
            401,
        )

    def test_device_specific_mapping_overrides_legacy_guide_mapping(self):
        device_id, credential = self.paired_device()
        channel = self.inventory_channel(101, "7.1", "Device channel")
        response = self.client.put(
            "/api/v1/devices/me/channels",
            headers={"Authorization": f"Bearer {credential}"},
            json=self.inventory_payload([channel]),
        )
        self.assertEqual(response.status_code, 200)
        mapped = self.client.put(
            f"/api/v1/devices/{device_id}/channels/101/mapping",
            headers=self.admin_headers(),
            json={"xmltv_id": "device-news.xml"},
        )
        self.assertEqual(mapped.status_code, 200)
        db = sqlite3.connect(database.DB_PATH)
        try:
            db.execute(
                "INSERT INTO channel_mappings (physical_channel, xmltv_id) VALUES (?, ?)",
                ("7.1", "legacy-news.xml"),
            )
            db.commit()
        finally:
            db.close()

        with (
            patch("main.xmltv_files", return_value=[]),
            patch("main.parse_xmltv", return_value=[]),
            patch("main.parse_xmltv_programs", return_value=[]),
        ):
            response = self.client.get(
                "/api/guide",
                headers={"Authorization": f"Bearer {credential}"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["xmltv_id"], "device-news.xml")

    def test_guide_uses_movie_artwork_and_xmltv_image_fallback(self):
        db = sqlite3.connect(database.DB_PATH)
        try:
            db.execute(
                "INSERT INTO channel_mappings (physical_channel, xmltv_id) VALUES (?, ?)",
                ("7.1", "movie-channel"),
            )
            db.commit()
        finally:
            db.close()

        channels = [{
            "id": "movie-channel",
            "name": "Movie Channel",
            "icon": "https://images.example/channel.png",
        }]
        programs = [{
            "channel_id": "movie-channel",
            "start": "invalid",
            "stop": "invalid",
            "title": "Example Film",
            "description": "",
            "categories": ["Film"],
            "image_url": "https://images.example/poster.png",
        }]
        with (
            patch("main.xmltv_files", return_value=[]),
            patch("main._get_xmltv_data", new=AsyncMock(return_value=(channels, programs))),
            patch(
                "main._cache_xmltv_channel_logos",
                new=AsyncMock(return_value=[{**channels[0], "icon": "/static/imgcache/channel.png"}]),
            ),
            patch("main.tmdb.search_tmdb_cached", new=AsyncMock(return_value=None)) as lookup,
        ):
            response = self.client.get("/api/guide")

        self.assertEqual(response.status_code, 200)
        lookup.assert_awaited_once()
        self.assertEqual(lookup.await_args.args[1:], ("Example Film", "movie"))
        guide_channel = response.json()[0]
        self.assertIsNone(guide_channel["logo_url"])
        self.assertIn("/img?u=", guide_channel["programs"][0]["poster_url"])

    def test_xmltv_channel_listing_does_not_download_unfavorited_logos(self):
        channels = [{
            "id": "news.channel",
            "name": "News",
            "icon": "https://logos.example/news.png",
            "source_file": "guide.xml",
        }]
        with (
            patch("main.xmltv_files", return_value=[]),
            patch("main._get_xmltv_data", new=AsyncMock(return_value=(channels, []))),
            patch("main._cache_xmltv_channel_logos", new=AsyncMock()) as cache_logos,
        ):
            response = self.client.get("/api/xmltv/channels")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["icon"], "https://logos.example/news.png")
        cache_logos.assert_not_awaited()

    def test_failed_xmltv_logo_cache_does_not_return_remote_url(self):
        async def run():
            with patch("main._cache_remote_image", new=AsyncMock(return_value=None)):
                return await main._cache_xmltv_channel_logos([{
                    "id": "news.channel",
                    "icon": "https://logos.example/news.png",
                }])

        channels = asyncio.run(run())

        self.assertIsNone(channels[0]["icon"])

    def test_guide_omits_logo_when_server_cannot_cache_it(self):
        db = sqlite3.connect(database.DB_PATH)
        try:
            db.execute(
                "INSERT INTO channel_mappings (physical_channel, xmltv_id) VALUES (?, ?)",
                ("7.1", "news-channel"),
            )
            db.commit()
        finally:
            db.close()

        channels = [{
            "id": "news-channel",
            "name": "News",
            "icon": "https://logos.example/news.png",
        }]
        with (
            patch("main.xmltv_files", return_value=[]),
            patch("main._get_xmltv_data", new=AsyncMock(return_value=(channels, []))),
            patch("main._cache_xmltv_channel_logos", new=AsyncMock(return_value=channels)),
            patch("main._cache_remote_image", new=AsyncMock(return_value=None)),
        ):
            response = self.client.get("/api/guide")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()[0]["logo_url"])
        self.assertNotIn("/img?u=", response.text)

    def test_server_downloads_and_reuses_cached_xmltv_logo(self):
        cache_dir = Path(self.temp_dir.name) / "images"

        async def run():
            requests = []
            user_agents = []

            async def handler(request):
                requests.append(request.url)
                user_agents.append(request.headers.get("user-agent"))
                return httpx.Response(
                    200,
                    headers={"content-type": "image/png"},
                    content=b"\x89PNG\r\n\x1a\nlogo",
                )

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with (
                patch.object(main, "IMAGE_CACHE_DIR", str(cache_dir)),
                patch.object(main, "IMAGE_RETRY_DIR", str(Path(self.temp_dir.name) / "retry")),
                patch.object(main, "IMAGE_FETCH_INTERVAL_SECONDS", 0),
                patch("main.httpx.AsyncClient", return_value=client),
            ):
                url = "https://logos.example/channel.png"
                first = await main._cache_remote_image(url)
                second = await main._cache_remote_image(url)
            await client.aclose()
            return first, second, requests, user_agents

        first, second, requests, user_agents = asyncio.run(run())

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("/static/imgcache/"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(
            user_agents,
            ["HomelabTV/1.0 (https://github.com/Bodefrei/homelabtv)"],
        )
        cached_image = cache_dir / Path(first).name
        self.assertEqual(cached_image.read_bytes(), b"\x89PNG\r\n\x1a\nlogo")

    def test_server_caches_svg_xmltv_logos(self):
        cache_dir = Path(self.temp_dir.name) / "svg-images"

        async def run():
            async def handler(request):
                return httpx.Response(
                    200,
                    headers={"content-type": "image/svg+xml"},
                    content=b"<svg xmlns='http://www.w3.org/2000/svg'/>",
                )

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with (
                patch.object(main, "IMAGE_CACHE_DIR", str(cache_dir)),
                patch.object(main, "IMAGE_RETRY_DIR", str(Path(self.temp_dir.name) / "retry")),
                patch.object(main, "IMAGE_FETCH_INTERVAL_SECONDS", 0),
                patch("main.httpx.AsyncClient", return_value=client),
            ):
                result = await main._cache_remote_image("https://logos.example/channel.svg")
            await client.aclose()
            return result

        cached_path = asyncio.run(run())

        self.assertTrue(cached_path.endswith(".svg"))
        self.assertTrue((cache_dir / Path(cached_path).name).is_file())

    def test_server_refreshes_logo_only_after_ten_day_cache_age(self):
        cache_dir = Path(self.temp_dir.name) / "stale-images"
        url = "https://logos.example/stale.png"
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        cache_dir.mkdir()
        cached_image = cache_dir / f"{key}.png"
        cached_image.write_bytes(b"old image")
        old_timestamp = time.time() - main.IMAGE_CACHE_MAX_AGE_SECONDS - 1
        os.utime(cached_image, (old_timestamp, old_timestamp))

        async def run():
            requests = []

            async def handler(request):
                requests.append(request.url)
                return httpx.Response(
                    200,
                    headers={"content-type": "image/png"},
                    content=b"new image",
                )

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with (
                patch.object(main, "IMAGE_CACHE_DIR", str(cache_dir)),
                patch.object(main, "IMAGE_RETRY_DIR", str(Path(self.temp_dir.name) / "retry")),
                patch.object(main, "IMAGE_FETCH_INTERVAL_SECONDS", 0),
                patch("main.httpx.AsyncClient", return_value=client),
            ):
                result = await main._cache_remote_image(url)
            await client.aclose()
            return result, requests

        result, requests = asyncio.run(run())

        self.assertEqual(requests, [url])
        self.assertTrue(result.endswith(f"/{key}.png"))
        self.assertEqual(cached_image.read_bytes(), b"new image")
        self.assertGreater(cached_image.stat().st_mtime, old_timestamp)

    def test_forbidden_remote_logo_is_not_cached_or_returned(self):
        cache_dir = Path(self.temp_dir.name) / "forbidden-images"

        async def run():
            requests = []

            async def handler(request):
                requests.append(request.url)
                return httpx.Response(403)

            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            with (
                patch.object(main, "IMAGE_CACHE_DIR", str(cache_dir)),
                patch.object(main, "IMAGE_RETRY_DIR", str(Path(self.temp_dir.name) / "retry")),
                patch.object(main, "IMAGE_FETCH_INTERVAL_SECONDS", 0),
                patch("main.httpx.AsyncClient", return_value=client),
            ):
                url = "https://logos.example/blocked.png"
                first = await main._cache_remote_image(url)
                second = await main._cache_remote_image(url)
            await client.aclose()
            return first, second, requests

        first, second, requests = asyncio.run(run())
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(requests, ["https://logos.example/blocked.png"])
        self.assertEqual(list(cache_dir.glob("*.png")), [])
        self.assertEqual(len(list((Path(self.temp_dir.name) / "retry").glob("*.retry"))), 1)

    def test_xmltv_channel_endpoint_caches_parse_until_source_changes(self):
        source = Path(self.temp_dir.name) / "guide.xml"
        source.write_text("<tv/>", encoding="utf-8")
        channels = [{"id": "one.xml", "name": "Channel One", "source_file": "guide.xml"}]
        main._xmltv_data_cache_signature = None
        main._xmltv_data_cache = ([], [])

        with (
            patch("main.xmltv_files", return_value=[str(source)]),
            patch("main.parse_xmltv", return_value=channels) as parse_xmltv,
            patch("main.parse_xmltv_programs", return_value=[]) as parse_xmltv_programs,
        ):
            first = self.client.get("/api/xmltv/channels")
            second = self.client.get("/api/xmltv/channels")

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), channels)
        self.assertEqual(second.json(), channels)
        parse_xmltv.assert_called_once_with(str(source))
        parse_xmltv_programs.assert_called_once_with(str(source))

    def test_guide_window_keeps_overlapping_programs_and_drops_distant_ones(self):
        def xmltv_time(timestamp):
            return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%d%H%M%S %z")

        window_start, window_end = 1000, 2000
        self.assertTrue(main._programme_in_guide_window(
            {"start": xmltv_time(900), "stop": xmltv_time(1100)},
            window_start,
            window_end,
        ))
        self.assertFalse(main._programme_in_guide_window(
            {"start": xmltv_time(2000), "stop": xmltv_time(2100)},
            window_start,
            window_end,
        ))
        self.assertTrue(main._programme_in_guide_window(
            {"start": "invalid", "stop": "invalid"},
            window_start,
            window_end,
        ))

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

    def test_admin_authentication_failure_limit_is_configurable(self):
        with patch.object(main, "ADMIN_AUTH_FAILURE_LIMIT", 2):
            statuses = [
                self.client.get(
                    "/api/v1/devices",
                    headers={"Authorization": "******"},
                ).status_code
                for _ in range(3)
            ]
        self.assertEqual(statuses, [401, 401, 429])

    def test_admin_authentication_lockout_can_be_disabled(self):
        with patch.object(main, "ADMIN_AUTH_FAILURE_LIMIT", 0):
            statuses = [
                self.client.get(
                    "/api/v1/devices",
                    headers={"Authorization": "******"},
                ).status_code
                for _ in range(12)
            ]
        self.assertEqual(statuses, [401] * 12)

        db = sqlite3.connect(database.DB_PATH)
        try:
            attempts = db.execute(
                "SELECT COUNT(*) FROM pairing_rate_limits WHERE rate_key LIKE 'admin-auth:%'"
            ).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(attempts, 0)

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
