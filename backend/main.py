import os
import asyncio
import hashlib
import hmac
import base64
import binascii
import secrets
import json
import struct
import time
import urllib.parse
import uuid
import logging
from datetime import datetime
from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import httpx
import uvicorn
from xmltv_parser import parse_xmltv, parse_xmltv_programs
from database import get_db_connection, init_db
from config import (
    DATA_DIR,
    MDNS_ENABLED,
    TMDB_API_KEY,
    XMLTV_FEED_URL,
    XMLTV_KEEP_FUTURE_HOURS,
    XMLTV_KEEP_PAST_HOURS,
    XMLTV_REFRESH_INTERVAL_HOURS,
)
from xmltv_sync import download_xmltv
from mdns import register_mdns_service
import tmdb

app = FastAPI(title="HomelabTV Backend")
logger = logging.getLogger(__name__)

PAIRING_LIFETIME_SECONDS = 5 * 60
PAIRING_REQUEST_LIMIT = 5
PAIRING_APPROVAL_LIMIT = 10
try:
    ADMIN_AUTH_FAILURE_LIMIT = int(os.environ.get("HOMELAB_ADMIN_AUTH_FAILURE_LIMIT", "10"))
except ValueError as exc:
    raise RuntimeError("HOMELAB_ADMIN_AUTH_FAILURE_LIMIT must be a non-negative integer") from exc
if ADMIN_AUTH_FAILURE_LIMIT < 0:
    raise RuntimeError("HOMELAB_ADMIN_AUTH_FAILURE_LIMIT must be a non-negative integer")
RATE_LIMIT_WINDOW_SECONDS = 5 * 60
ADMIN_SESSION_COOKIE = "homelab_admin_session"
ADMIN_SESSION_LIFETIME_SECONDS = 12 * 60 * 60
_xmltv_data_cache_signature = None
_xmltv_data_cache = ([], [])
_xmltv_data_cache_lock = asyncio.Lock()
_xmltv_refresh_task = None
_mdns_service = None


class AdminLoginRequest(BaseModel):
    password: str = Field(max_length=512)


def generate_pairing_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class PairingStartRequest(BaseModel):
    device_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    device_secret: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    friendly_name: str | None = Field(default=None, max_length=100)
    app_version: str | None = Field(default=None, max_length=64)
    android_version: str | None = Field(default=None, max_length=64)
    manufacturer: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=100)


class PairingStartResponse(BaseModel):
    device_id: str
    user_code: str
    expires_at: int
    expires_in_seconds: int


class DevicePollRequest(BaseModel):
    device_secret: str = Field(min_length=43, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class PairingApprovalRequest(BaseModel):
    user_code: str = Field(pattern=r"^[0-9]{6}$")


class PairingPollResponse(BaseModel):
    status: str
    device_credential: str | None = None


class DeviceChannelInventoryEntry(BaseModel):
    channel_id: int = Field(gt=0)
    input_id: str | None = Field(default=None, max_length=512)
    display_number: str = Field(max_length=64)
    display_name: str = Field(max_length=512)
    original_display_name: str | None = Field(default=None, max_length=512)
    service_type: str | None = Field(default=None, max_length=128)
    channel_type: str | None = Field(default=None, max_length=128)
    network_id: int | None = None
    transport_stream_id: int | None = None
    service_id: int | None = None
    internal_provider_id: str | None = Field(default=None, max_length=1024)


class DeviceChannelInventoryRequest(BaseModel):
    inventory_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    channels: list[DeviceChannelInventoryEntry] = Field(max_length=5000)


class DeviceChannelMappingRequest(BaseModel):
    xmltv_id: str | None = Field(default=None, max_length=256)


class ChannelListEntryRequest(BaseModel):
    logical_channel_id: str = Field(min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=512)
    custom_name: str | None = Field(default=None, max_length=512)
    hidden: bool = False


class ChannelListRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    entries: list[ChannelListEntryRequest] = Field(max_length=5000)


class DeviceChannelListAssignmentRequest(BaseModel):
    list_id: str | None = Field(default=None, max_length=128)


# Serve assets and templates relative to this module, independent of the process working directory.
BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BACKEND_DIR, "static")
TEMPLATES_DIR = os.path.join(BACKEND_DIR, "templates")
os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(TEMPLATES_DIR, exist_ok=True)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

IMAGE_CACHE_DIR = os.path.join(STATIC_DIR, "imgcache")
IMAGE_EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
}
MAX_CACHED_IMAGE_BYTES = 10 * 1024 * 1024

@app.on_event("startup")
async def startup():
    global _xmltv_refresh_task, _mdns_service
    await init_db()
    if MDNS_ENABLED:
        try:
            _mdns_service = await register_mdns_service()
        except Exception:
            logger.exception("Could not advertise the backend via mDNS")
    if XMLTV_FEED_URL:
        if not XMLTV_FEED_URL.startswith("https://"):
            raise RuntimeError("HOMELAB_XMLTV_URL must use HTTPS")
        _xmltv_refresh_task = asyncio.create_task(_xmltv_refresh_loop())


@app.on_event("shutdown")
async def shutdown():
    global _xmltv_refresh_task, _mdns_service
    if _xmltv_refresh_task is not None:
        _xmltv_refresh_task.cancel()
        try:
            await _xmltv_refresh_task
        except asyncio.CancelledError:
            pass
        _xmltv_refresh_task = None
    if _mdns_service is not None:
        await _mdns_service.async_close()
        _mdns_service = None


async def _xmltv_refresh_loop():
    destination = os.path.join(DATA_DIR, "automatic.xml")
    while True:
        try:
            await download_xmltv(
                XMLTV_FEED_URL,
                destination,
                past_hours=XMLTV_KEEP_PAST_HOURS,
                future_hours=XMLTV_KEEP_FUTURE_HOURS,
            )
            channels, _ = await _get_xmltv_data(xmltv_files())
            await _cache_xmltv_channel_logos(channels)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Automatic XMLTV refresh failed; keeping the last valid feed")
        await asyncio.sleep(XMLTV_REFRESH_INTERVAL_HOURS * 60 * 60)


async def _enforce_rate_limit(db, rate_key: str, limit: int):
    now = int(time.time())
    await db.execute("BEGIN IMMEDIATE")
    cursor = await db.execute(
        "SELECT window_started_at, request_count FROM pairing_rate_limits WHERE rate_key = ?",
        (rate_key,),
    )
    row = await cursor.fetchone()
    if row is None or now - row[0] >= RATE_LIMIT_WINDOW_SECONDS:
        window_started_at, request_count = now, 1
        await db.execute(
            """
            INSERT INTO pairing_rate_limits (rate_key, window_started_at, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT(rate_key) DO UPDATE SET
                window_started_at = excluded.window_started_at,
                request_count = 1
            """,
            (rate_key, now),
        )
    else:
        window_started_at, request_count = row[0], row[1] + 1
        await db.execute(
            "UPDATE pairing_rate_limits SET request_count = ? WHERE rate_key = ?",
            (request_count, rate_key),
        )
    await db.commit()
    if request_count > limit:
        retry_after = max(1, RATE_LIMIT_WINDOW_SECONDS - (now - window_started_at))
        await db.close()
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Try again later.",
            headers={"Retry-After": str(retry_after)},
        )


def _client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _admin_session_signature(token: str, issued_at: int) -> str:
    return hmac.new(
        token.encode("utf-8"),
        f"homelab-admin:{issued_at}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _has_valid_admin_session(request: Request, token: str) -> bool:
    value = request.cookies.get(ADMIN_SESSION_COOKIE, "")
    issued_at_text, separator, signature = value.partition(".")
    if not separator or not issued_at_text.isdigit():
        return False
    issued_at = int(issued_at_text)
    now = int(time.time())
    if issued_at > now or now - issued_at >= ADMIN_SESSION_LIFETIME_SECONDS:
        return False
    expected_signature = _admin_session_signature(token, issued_at)
    return secrets.compare_digest(signature, expected_signature)


def _has_valid_admin_authorization(request: Request, token: str) -> bool:
    authorization = request.headers.get("authorization", "")
    scheme, separator, credential = authorization.partition(" ")
    candidate = ""
    if separator and scheme.lower() == "bearer":
        candidate = credential
    elif separator and scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(credential, validate=True)
            username, colon, password = decoded.partition(b":")
            if colon and secrets.compare_digest(username, b"admin"):
                candidate = password.decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            candidate = ""
    return bool(candidate) and secrets.compare_digest(candidate.encode("utf-8"), token.encode("utf-8"))


async def _record_admin_auth_failure(request: Request):
    if ADMIN_AUTH_FAILURE_LIMIT == 0:
        return
    db = await get_db_connection()
    await _enforce_rate_limit(
        db,
        f"admin-auth:{_client_address(request)}",
        ADMIN_AUTH_FAILURE_LIMIT,
    )
    await db.close()


async def require_admin(request: Request):
    expected = os.environ.get("HOMELAB_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Administrator access is not configured")

    if not (
        _has_valid_admin_session(request, expected)
        or _has_valid_admin_authorization(request, expected)
    ):
        await _record_admin_auth_failure(request)
        raise HTTPException(
            status_code=401,
            detail="Administrator authentication required",
        )


async def _device_for_credential(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, separator, credential = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not credential:
        raise HTTPException(
            status_code=401,
            detail="Device credential required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    db = await get_db_connection()
    now = int(time.time())
    cursor = await db.execute(
        """
        SELECT c.device_id
        FROM device_credentials c
        JOIN devices d ON d.id = c.device_id
        WHERE c.credential_hash = ? AND c.revoked_at IS NULL AND d.pairing_status = 'paired'
        """,
        (hash_secret(credential),),
    )
    row = await cursor.fetchone()
    if row is None:
        await db.close()
        raise HTTPException(
            status_code=401,
            detail="Device credential is invalid or revoked",
            headers={"WWW-Authenticate": "Bearer"},
        )
    await db.execute(
        "UPDATE device_credentials SET last_used_at = ? WHERE device_id = ?",
        (now, row[0]),
    )
    await db.execute(
        "UPDATE devices SET last_seen_at = ?, updated_at = ? WHERE id = ?",
        (now, now, row[0]),
    )
    await db.commit()
    await db.close()
    return row[0]


def calculate_inventory_hash(channels: list[DeviceChannelInventoryEntry]) -> str:
    digest = hashlib.sha256()
    for channel in sorted(channels, key=lambda item: item.channel_id):
        values = (
            channel.channel_id,
            channel.input_id,
            channel.display_number,
            channel.display_name,
            channel.original_display_name,
            channel.service_type,
            channel.channel_type,
            channel.network_id,
            channel.transport_stream_id,
            channel.service_id,
            channel.internal_provider_id,
        )
        for value in values:
            if value is None:
                digest.update(struct.pack(">i", -1))
            else:
                encoded = str(value).encode("utf-8")
                digest.update(struct.pack(">I", len(encoded)))
                digest.update(encoded)
    return digest.hexdigest()


def _channel_values(channel: DeviceChannelInventoryEntry) -> tuple:
    return (
        channel.input_id,
        channel.display_number,
        channel.display_name,
        channel.original_display_name,
        channel.service_type,
        channel.channel_type,
        channel.network_id,
        channel.transport_stream_id,
        channel.service_id,
        channel.internal_provider_id,
    )


async def _upsert_device(db, payload: PairingStartRequest, now: int):
    device_id = payload.device_id
    await db.execute(
        """
        INSERT INTO devices (
            id, friendly_name, app_version, android_version, manufacturer, model,
            pairing_status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            friendly_name = CASE
                WHEN ? IS NULL THEN devices.friendly_name
                ELSE excluded.friendly_name
            END,
            app_version = COALESCE(excluded.app_version, devices.app_version),
            android_version = COALESCE(excluded.android_version, devices.android_version),
            manufacturer = COALESCE(excluded.manufacturer, devices.manufacturer),
            model = COALESCE(excluded.model, devices.model),
            updated_at = excluded.updated_at
        """,
        (
            device_id,
            payload.friendly_name or f"TV-{device_id[:8]}",
            payload.app_version,
            payload.android_version,
            payload.manufacturer,
            payload.model,
            now,
            now,
            payload.friendly_name,
        ),
    )


@app.get("/api/v1/devices/me")
async def get_current_device(request: Request):
    authenticated_device_id = await _device_for_credential(request)
    db = await get_db_connection()
    cursor = await db.execute(
        """
        SELECT id, friendly_name, app_version, android_version, manufacturer, model,
               pairing_status, last_seen_at, last_sync_at, available_channels,
               assigned_channel_list, inventory_revision
        FROM devices WHERE id = ?
        """,
        (authenticated_device_id,),
    )
    row = await cursor.fetchone()
    await db.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_record(row)


@app.put("/api/v1/devices/me/channels")
async def sync_device_channels(payload: DeviceChannelInventoryRequest, request: Request):
    device_id = await _device_for_credential(request)
    channel_ids = [channel.channel_id for channel in payload.channels]
    if len(channel_ids) != len(set(channel_ids)):
        raise HTTPException(status_code=422, detail="Channel inventory contains duplicate channel IDs")
    if calculate_inventory_hash(payload.channels) != payload.inventory_hash:
        raise HTTPException(status_code=422, detail="Inventory hash does not match the submitted channel data")

    db = await get_db_connection()
    await db.execute("BEGIN IMMEDIATE")
    now = int(time.time())
    cursor = await db.execute(
        "SELECT inventory_hash, inventory_revision FROM devices WHERE id = ?",
        (device_id,),
    )
    device = await cursor.fetchone()
    if device is None:
        await db.close()
        raise HTTPException(status_code=404, detail="Device not found")

    if device[0] == payload.inventory_hash:
        await db.execute(
            "UPDATE devices SET last_sync_at = ?, available_channels = ?, updated_at = ? WHERE id = ?",
            (now, len(payload.channels), now, device_id),
        )
        await db.commit()
        await db.close()
        return {
            "changed": False,
            "inventory_hash": payload.inventory_hash,
            "revision": device[1],
            "channel_count": len(payload.channels),
            "changes": {"added": [], "removed": [], "renamed": [], "renumbered": [], "updated": []},
        }

    previous_cursor = await db.execute(
        """
        SELECT channel_id, input_id, display_number, display_name, original_display_name,
               service_type, channel_type, network_id, transport_stream_id, service_id,
               internal_provider_id
        FROM device_channels WHERE device_id = ? AND removed_at IS NULL
        """,
        (device_id,),
    )
    previous_rows = await previous_cursor.fetchall()
    previous = {
        row[0]: (
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            row[8],
            row[9],
            row[10],
        )
        for row in previous_rows
    }
    current = {channel.channel_id: channel for channel in payload.channels}
    previous_ids = set(previous)
    current_ids = set(current)
    added = sorted(current_ids - previous_ids)
    removed = sorted(previous_ids - current_ids)
    renamed = []
    renumbered = []
    updated = []
    for channel_id in sorted(previous_ids & current_ids):
        old_values = previous[channel_id]
        new_values = _channel_values(current[channel_id])
        if old_values == new_values:
            continue
        if old_values[2] != new_values[2]:
            renamed.append(
                {
                    "channel_id": channel_id,
                    "previous_name": old_values[2],
                    "display_name": new_values[2],
                }
            )
        if old_values[1] != new_values[1]:
            renumbered.append(
                {
                    "channel_id": channel_id,
                    "previous_number": old_values[1],
                    "display_number": new_values[1],
                }
            )
        if old_values[0] != new_values[0] or old_values[3:] != new_values[3:]:
            updated.append(channel_id)

    changes = {
        "added": added,
        "removed": removed,
        "renamed": renamed,
        "renumbered": renumbered,
        "updated": updated,
    }
    revision = device[1] + 1

    await db.execute(
        "UPDATE device_channels SET removed_at = ? WHERE device_id = ? AND removed_at IS NULL",
        (now, device_id),
    )
    for channel in payload.channels:
        await db.execute(
            """
            INSERT INTO device_channels (
                device_id, channel_id, input_id, display_number, display_name,
                original_display_name, service_type, channel_type, network_id,
                transport_stream_id, service_id, internal_provider_id, xmltv_id,
                first_seen_at, last_seen_at, removed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(device_id, channel_id) DO UPDATE SET
                input_id = excluded.input_id,
                display_number = excluded.display_number,
                display_name = excluded.display_name,
                original_display_name = excluded.original_display_name,
                service_type = excluded.service_type,
                channel_type = excluded.channel_type,
                network_id = excluded.network_id,
                transport_stream_id = excluded.transport_stream_id,
                service_id = excluded.service_id,
                internal_provider_id = excluded.internal_provider_id,
                last_seen_at = excluded.last_seen_at,
                removed_at = NULL
            """,
            (
                device_id,
                channel.channel_id,
                channel.input_id,
                channel.display_number,
                channel.display_name,
                channel.original_display_name,
                channel.service_type,
                channel.channel_type,
                channel.network_id,
                channel.transport_stream_id,
                channel.service_id,
                channel.internal_provider_id,
                None,
                now,
                now,
            ),
        )

    inventory_json = json.dumps(
        [channel.model_dump(mode="json") for channel in sorted(payload.channels, key=lambda item: item.channel_id)],
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    await db.execute(
        """
        INSERT INTO device_inventory_revisions (
            device_id, revision, inventory_hash, channel_count, changes_json,
            inventory_json, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            device_id,
            revision,
            payload.inventory_hash,
            len(payload.channels),
            json.dumps(changes, ensure_ascii=False, separators=(",", ":")),
            inventory_json,
            now,
        ),
    )
    await db.execute(
        """
        UPDATE devices SET inventory_hash = ?, inventory_revision = ?, last_sync_at = ?,
            available_channels = ?, updated_at = ? WHERE id = ?
        """,
        (payload.inventory_hash, revision, now, len(payload.channels), now, device_id),
    )
    await db.commit()
    await db.close()
    return {
        "changed": True,
        "inventory_hash": payload.inventory_hash,
        "revision": revision,
        "channel_count": len(payload.channels),
        "changes": changes,
    }


@app.get("/api/v1/devices/{device_id}/channels")
async def get_device_channels(device_id: str, request: Request):
    await require_admin(request)
    db = await get_db_connection()
    device_cursor = await db.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,))
    if await device_cursor.fetchone() is None:
        await db.close()
        raise HTTPException(status_code=404, detail="Device not found")
    cursor = await db.execute(
        """
        SELECT channel_id, input_id, display_number, display_name, original_display_name,
               service_type, channel_type, network_id, transport_stream_id, service_id,
               internal_provider_id, xmltv_id, first_seen_at, last_seen_at, removed_at
        FROM device_channels
        WHERE device_id = ?
        ORDER BY removed_at IS NOT NULL, display_number, channel_id
        """,
        (device_id,),
    )
    rows = await cursor.fetchall()
    await db.close()
    return [
        {
            "channel_id": row[0],
            "input_id": row[1],
            "display_number": row[2],
            "display_name": row[3],
            "original_display_name": row[4],
            "service_type": row[5],
            "channel_type": row[6],
            "network_id": row[7],
            "transport_stream_id": row[8],
            "service_id": row[9],
            "internal_provider_id": row[10],
            "xmltv_id": row[11],
            "first_seen_at": row[12],
            "last_seen_at": row[13],
            "removed_at": row[14],
            "available": row[14] is None,
        }
        for row in rows
    ]




@app.put("/api/v1/devices/{device_id}/channels/{channel_id}/mapping")
async def map_device_channel(
    device_id: str,
    channel_id: int,
    payload: DeviceChannelMappingRequest,
    request: Request,
):
    await require_admin(request)
    db = await get_db_connection()
    cursor = await db.execute(
        "SELECT 1 FROM device_channels WHERE device_id = ? AND channel_id = ?",
        (device_id, channel_id),
    )
    if await cursor.fetchone() is None:
        await db.close()
        raise HTTPException(status_code=404, detail="Device channel not found")
    await db.execute(
        "UPDATE device_channels SET xmltv_id = ? WHERE device_id = ? AND channel_id = ?",
        (payload.xmltv_id, device_id, channel_id),
    )
    await db.commit()
    await db.close()
    return {"device_id": device_id, "channel_id": channel_id, "xmltv_id": payload.xmltv_id}


async def _channel_list_records(db, list_id: str | None = None, summary: bool = False):
    where_clause = "WHERE id = ?" if list_id is not None else ""
    parameters = (list_id,) if list_id is not None else ()
    cursor = await db.execute(
        f"""
        SELECT id, name, description, created_at, updated_at,
               (SELECT COUNT(*) FROM channel_list_entries e WHERE e.list_id = channel_lists.id)
        FROM channel_lists {where_clause}
        ORDER BY name COLLATE NOCASE, id
        """,
        parameters,
    )
    lists = await cursor.fetchall()
    if summary:
        return [
            {
                "id": row[0],
                "name": row[1],
                "description": row[2],
                "created_at": row[3],
                "updated_at": row[4],
                "entry_count": row[5],
            }
            for row in lists
        ]
    if not lists:
        return []
    list_ids = [row[0] for row in lists]
    placeholders = ", ".join("?" for _ in list_ids)
    entries_cursor = await db.execute(
        f"""
        SELECT id, list_id, logical_channel_id, display_name, custom_name, sort_index, hidden
        FROM channel_list_entries WHERE list_id IN ({placeholders})
        ORDER BY list_id, sort_index, id
        """,
        list_ids,
    )
    entries = await entries_cursor.fetchall()
    entries_by_list = {}
    for row in entries:
        entries_by_list.setdefault(row[1], []).append(
            {
                "id": row[0],
                "logical_channel_id": row[2],
                "display_name": row[3],
                "custom_name": row[4],
                "sort_index": row[5],
                "hidden": bool(row[6]),
            }
        )
    return [
        {
            "id": row[0],
            "name": row[1],
            "description": row[2],
            "created_at": row[3],
            "updated_at": row[4],
            "entry_count": row[5],
            "entries": entries_by_list.get(row[0], []),
        }
        for row in lists
    ]


async def _save_channel_list_entries(db, list_id: str, entries: list[ChannelListEntryRequest], now: int):
    logical_ids = [entry.logical_channel_id for entry in entries]
    if len(logical_ids) != len(set(logical_ids)):
        raise HTTPException(status_code=422, detail="Channel list contains duplicate logical channel IDs")
    await db.execute("DELETE FROM channel_list_entries WHERE list_id = ?", (list_id,))
    await db.executemany(
        """
        INSERT INTO channel_list_entries (
            id, list_id, logical_channel_id, display_name, custom_name,
            sort_index, hidden, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                str(uuid.uuid4()),
                list_id,
                entry.logical_channel_id,
                entry.display_name,
                entry.custom_name,
                sort_index,
                int(entry.hidden),
                now,
                now,
            )
            for sort_index, entry in enumerate(entries)
        ],
    )


@app.get("/api/v1/channel-lists")
async def get_channel_lists(request: Request, summary: bool = False):
    await require_admin(request)
    db = await get_db_connection()
    result = await _channel_list_records(db, summary=summary)
    await db.close()
    return result


@app.get("/api/v1/channel-lists/{list_id}")
async def get_channel_list(list_id: str, request: Request):
    await require_admin(request)
    db = await get_db_connection()
    records = await _channel_list_records(db, list_id=list_id)
    await db.close()
    if not records:
        raise HTTPException(status_code=404, detail="Channel list not found")
    return records[0]


@app.post("/api/v1/channel-lists")
async def create_channel_list(payload: ChannelListRequest, request: Request):
    await require_admin(request)
    list_id = str(uuid.uuid4())
    now = int(time.time())
    db = await get_db_connection()
    await db.execute("BEGIN IMMEDIATE")
    await db.execute(
        """
        INSERT INTO channel_lists (id, name, description, is_system, is_shared, created_by, created_at, updated_at)
        VALUES (?, ?, ?, 0, 1, 'admin', ?, ?)
        """,
        (list_id, payload.name.strip(), payload.description, now, now),
    )
    await _save_channel_list_entries(db, list_id, payload.entries, now)
    await db.commit()
    result = (await _channel_list_records(db, list_id=list_id))[0]
    await db.close()
    return result


@app.put("/api/v1/channel-lists/{list_id}")
async def update_channel_list(list_id: str, payload: ChannelListRequest, request: Request):
    await require_admin(request)
    now = int(time.time())
    db = await get_db_connection()
    await db.execute("BEGIN IMMEDIATE")
    cursor = await db.execute("SELECT 1 FROM channel_lists WHERE id = ?", (list_id,))
    if await cursor.fetchone() is None:
        await db.close()
        raise HTTPException(status_code=404, detail="Channel list not found")
    await db.execute(
        "UPDATE channel_lists SET name = ?, description = ?, updated_at = ? WHERE id = ?",
        (payload.name.strip(), payload.description, now, list_id),
    )
    await _save_channel_list_entries(db, list_id, payload.entries, now)
    await db.commit()
    result = (await _channel_list_records(db, list_id=list_id))[0]
    await db.close()
    return result


@app.delete("/api/v1/channel-lists/{list_id}")
async def delete_channel_list(list_id: str, request: Request):
    await require_admin(request)
    db = await get_db_connection()
    await db.execute("DELETE FROM device_list_assignments WHERE list_id = ?", (list_id,))
    await db.execute("DELETE FROM channel_list_entries WHERE list_id = ?", (list_id,))
    cursor = await db.execute("DELETE FROM channel_lists WHERE id = ?", (list_id,))
    await db.commit()
    deleted = cursor.rowcount
    await db.close()
    if not deleted:
        raise HTTPException(status_code=404, detail="Channel list not found")
    return {"deleted": True}


@app.put("/api/v1/devices/{device_id}/channel-list")
async def assign_device_channel_list(
    device_id: str,
    payload: DeviceChannelListAssignmentRequest,
    request: Request,
):
    await require_admin(request)
    db = await get_db_connection()
    device_cursor = await db.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,))
    if await device_cursor.fetchone() is None:
        await db.close()
        raise HTTPException(status_code=404, detail="Device not found")
    if payload.list_id is not None:
        list_cursor = await db.execute("SELECT 1 FROM channel_lists WHERE id = ?", (payload.list_id,))
        if await list_cursor.fetchone() is None:
            await db.close()
            raise HTTPException(status_code=404, detail="Channel list not found")
    now = int(time.time())
    await db.execute("DELETE FROM device_list_assignments WHERE device_id = ?", (device_id,))
    if payload.list_id is not None:
        await db.execute(
            """
            INSERT INTO device_list_assignments (id, device_id, list_id, is_active, assigned_at)
            VALUES (?, ?, ?, 1, ?)
            """,
            (str(uuid.uuid4()), device_id, payload.list_id, now),
        )
    await db.execute(
        "UPDATE devices SET assigned_channel_list = ?, updated_at = ? WHERE id = ?",
        (payload.list_id, now, device_id),
    )
    await db.commit()
    await db.close()
    return {"device_id": device_id, "list_id": payload.list_id}


@app.get("/api/v1/devices/me/channel-lists")
async def get_current_device_channel_lists(request: Request):
    device_id = await _device_for_credential(request)
    db = await get_db_connection()
    cursor = await db.execute(
        """
        SELECT l.id, l.name, COUNT(e.id),
               EXISTS(
                   SELECT 1
                   FROM device_list_assignments a
                   WHERE a.device_id = ? AND a.list_id = l.id AND a.is_active = 1
               )
        FROM channel_lists l
        LEFT JOIN channel_list_entries e ON e.list_id = l.id AND e.hidden = 0
        GROUP BY l.id
        ORDER BY l.name COLLATE NOCASE, l.id
        """,
        (device_id,),
    )
    rows = await cursor.fetchall()
    await db.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "entry_count": row[2],
            "assigned": bool(row[3]),
        }
        for row in rows
    ]


@app.get("/api/v1/devices/me/channel-list")
async def get_current_device_channel_list(request: Request, list_id: str | None = None):
    device_id = await _device_for_credential(request)
    db = await get_db_connection()
    if list_id is None:
        cursor = await db.execute(
            """
            SELECT l.id, l.name
            FROM device_list_assignments a
            JOIN channel_lists l ON l.id = a.list_id
            WHERE a.device_id = ? AND a.is_active = 1
            """,
            (device_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT id, name FROM channel_lists WHERE id = ?",
            (list_id,),
        )
    assigned_list = await cursor.fetchone()
    if assigned_list is None:
        await db.close()
        if list_id is not None:
            raise HTTPException(status_code=404, detail="Channel list not found")
        return {"active": False, "list_id": None, "name": None, "channels": []}
    entries_cursor = await db.execute(
        """
        SELECT e.logical_channel_id, e.display_name, e.custom_name,
               c.channel_id, c.display_number, c.display_name
        FROM channel_list_entries e
        LEFT JOIN device_channels c
            ON c.device_id = ? AND c.xmltv_id = e.logical_channel_id AND c.removed_at IS NULL
        WHERE e.list_id = ? AND e.hidden = 0
        ORDER BY e.sort_index, e.id
        """,
        (device_id, assigned_list[0]),
    )
    rows = await entries_cursor.fetchall()
    await db.close()
    return {
        "active": True,
        "list_id": assigned_list[0],
        "name": assigned_list[1],
        "channels": [
            {
                "logical_channel_id": row[0],
                "display_name": row[2] or row[1],
                "custom_name": row[2],
                "list_number": index,
                "channel_id": row[3],
                "display_number": row[4],
                "device_channel_name": row[5],
                "available": row[3] is not None,
            }
            for index, row in enumerate(rows, start=1)
        ],
    }


def _device_record(row):
    return {
        "id": row[0],
        "friendly_name": row[1],
        "app_version": row[2],
        "android_version": row[3],
        "manufacturer": row[4],
        "model": row[5],
        "pairing_status": row[6],
        "last_seen_at": row[7],
        "last_sync_at": row[8],
        "available_channels": row[9],
        "assigned_channel_list": row[10],
        "inventory_revision": row[11],
    }


@app.get("/api/v1/devices")
async def list_devices(request: Request):
    await require_admin(request)
    db = await get_db_connection()
    rows = await db.execute(
        """
        SELECT id, friendly_name, app_version, android_version, manufacturer, model,
               pairing_status, last_seen_at, last_sync_at, available_channels,
               assigned_channel_list, inventory_revision
        FROM devices ORDER BY friendly_name ASC
        """
    )
    devices = [_device_record(row) for row in await rows.fetchall()]
    await db.close()
    return devices


@app.get("/api/v1/devices/{device_id}")
async def get_device(device_id: str, request: Request):
    await require_admin(request)
    db = await get_db_connection()
    cursor = await db.execute(
        """
        SELECT id, friendly_name, app_version, android_version, manufacturer, model,
               pairing_status, last_seen_at, last_sync_at, available_channels,
               assigned_channel_list, inventory_revision
        FROM devices WHERE id = ?
        """,
        (device_id,),
    )
    row = await cursor.fetchone()
    await db.close()
    if row is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return _device_record(row)


@app.post("/api/v1/devices/pair", response_model=PairingStartResponse)
async def create_pairing_session(payload: PairingStartRequest, request: Request):
    db = await get_db_connection()
    await _enforce_rate_limit(
        db,
        f"pair-start:{_client_address(request)}",
        PAIRING_REQUEST_LIMIT,
    )
    now = int(time.time())
    await db.execute("BEGIN IMMEDIATE")

    cursor = await db.execute(
        """
        SELECT 1 FROM device_credentials
        WHERE device_id = ? AND revoked_at IS NULL
        """,
        (payload.device_id,),
    )
    if await cursor.fetchone():
        await db.close()
        raise HTTPException(status_code=409, detail="Device is already paired; revoke it before pairing again")

    await _upsert_device(db, payload, now)
    await db.execute(
        """
        UPDATE pairing_sessions SET status = 'expired'
        WHERE device_id = ? AND status IN ('pending', 'approved')
        """,
        (payload.device_id,),
    )
    await db.execute("DELETE FROM pairing_sessions WHERE expires_at < ?", (now - 86400,))

    user_code = generate_pairing_code()
    cursor = await db.execute(
        "SELECT 1 FROM pairing_sessions WHERE pairing_code = ?",
        (user_code,),
    )
    while await cursor.fetchone():
        user_code = generate_pairing_code()
        cursor = await db.execute(
            "SELECT 1 FROM pairing_sessions WHERE pairing_code = ?",
            (user_code,),
        )

    expires_at = now + PAIRING_LIFETIME_SECONDS
    await db.execute(
        """
        INSERT INTO pairing_sessions (
            id, device_id, pairing_code, secret_hash, expires_at, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """,
        (str(uuid.uuid4()), payload.device_id, user_code, hash_secret(payload.device_secret), expires_at, now),
    )
    await db.execute(
        "UPDATE devices SET pairing_status = 'pending', last_seen_at = ?, updated_at = ? WHERE id = ?",
        (now, now, payload.device_id),
    )
    await db.commit()
    await db.close()
    return PairingStartResponse(
        device_id=payload.device_id,
        user_code=user_code,
        expires_at=expires_at,
        expires_in_seconds=PAIRING_LIFETIME_SECONDS,
    )


@app.post("/api/v1/devices/{device_id}/poll", response_model=PairingPollResponse)
async def poll_pairing_session(device_id: str, payload: DevicePollRequest):
    db = await get_db_connection()
    now = int(time.time())
    cursor = await db.execute(
        """
        SELECT id, secret_hash, expires_at, status
        FROM pairing_sessions
        WHERE device_id = ?
        ORDER BY created_at DESC, rowid DESC LIMIT 1
        """,
        (device_id,),
    )
    session = await cursor.fetchone()
    if session is None or not secrets.compare_digest(session[1], hash_secret(payload.device_secret)):
        await db.close()
        raise HTTPException(status_code=404, detail="Pairing session not found")

    if session[2] <= now and session[3] in ("pending", "approved"):
        await db.execute(
            "UPDATE pairing_sessions SET status = 'expired' WHERE id = ?",
            (session[0],),
        )
        await db.execute(
            "UPDATE devices SET pairing_status = 'unpaired', updated_at = ? "
            "WHERE id = ? AND pairing_status IN ('pending', 'approved')",
            (now, device_id),
        )
        await db.commit()
        await db.close()
        return PairingPollResponse(status="expired")

    if session[3] == "pending":
        await db.close()
        return PairingPollResponse(status="pending")
    if session[3] == "rejected":
        await db.close()
        return PairingPollResponse(status="rejected")
    if session[3] == "expired":
        await db.close()
        return PairingPollResponse(status="expired")
    if session[3] == "complete":
        await db.close()
        return PairingPollResponse(status="paired")
    if session[3] != "approved":
        await db.close()
        raise HTTPException(status_code=409, detail="Pairing session is in an invalid state")

    credential = secrets.token_urlsafe(32)
    await db.execute("BEGIN IMMEDIATE")
    cursor = await db.execute(
        "SELECT status FROM pairing_sessions WHERE id = ?",
        (session[0],),
    )
    latest_status = await cursor.fetchone()
    if latest_status is None or latest_status[0] != "approved":
        await db.close()
        return PairingPollResponse(status="pending")

    await db.execute(
        """
        INSERT INTO device_credentials (device_id, credential_hash, created_at, revoked_at, last_used_at)
        VALUES (?, ?, ?, NULL, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            credential_hash = excluded.credential_hash,
            created_at = excluded.created_at,
            revoked_at = NULL,
            last_used_at = excluded.last_used_at
        """,
        (device_id, hash_secret(credential), now, now),
    )
    await db.execute(
        "UPDATE pairing_sessions SET status = 'complete', used_at = ? WHERE id = ?",
        (now, session[0]),
    )
    await db.execute(
        "UPDATE devices SET pairing_status = 'paired', last_seen_at = ?, updated_at = ? WHERE id = ?",
        (now, now, device_id),
    )
    await db.commit()
    await db.close()
    return PairingPollResponse(status="paired", device_credential=credential)


@app.post("/api/v1/pairing/approve")
async def approve_pairing(payload: PairingApprovalRequest, request: Request):
    await require_admin(request)
    db = await get_db_connection()
    await _enforce_rate_limit(
        db,
        f"pair-approve:{_client_address(request)}",
        PAIRING_APPROVAL_LIMIT,
    )
    now = int(time.time())
    await db.execute("BEGIN IMMEDIATE")
    cursor = await db.execute(
        """
        SELECT id, device_id FROM pairing_sessions
        WHERE pairing_code = ? AND status = 'pending' AND expires_at > ?
        """,
        (payload.user_code, now),
    )
    session = await cursor.fetchone()
    if session is None:
        await db.commit()
        await db.close()
        raise HTTPException(status_code=404, detail="Pairing code is invalid or expired")

    await db.execute(
        "UPDATE pairing_sessions SET status = 'approved', approved_by = 'admin' WHERE id = ?",
        (session[0],),
    )
    await db.execute(
        "UPDATE devices SET pairing_status = 'approved', updated_at = ? WHERE id = ?",
        (now, session[1]),
    )
    await db.commit()
    await db.close()
    return {"status": "approved"}


@app.post("/api/v1/pairing/reject")
async def reject_pairing(payload: PairingApprovalRequest, request: Request):
    await require_admin(request)
    db = await get_db_connection()
    await _enforce_rate_limit(
        db,
        f"pair-approve:{_client_address(request)}",
        PAIRING_APPROVAL_LIMIT,
    )
    now = int(time.time())
    await db.execute("BEGIN IMMEDIATE")
    cursor = await db.execute(
        """
        SELECT id, device_id FROM pairing_sessions
        WHERE pairing_code = ? AND status = 'pending' AND expires_at > ?
        """,
        (payload.user_code, now),
    )
    session = await cursor.fetchone()
    if session is None:
        await db.commit()
        await db.close()
        raise HTTPException(status_code=404, detail="Pairing code is invalid or expired")
    await db.execute(
        "UPDATE pairing_sessions SET status = 'rejected', approved_by = 'admin' WHERE id = ?",
        (session[0],),
    )
    await db.execute(
        "UPDATE devices SET pairing_status = 'rejected', updated_at = ? WHERE id = ?",
        (now, session[1]),
    )
    await db.commit()
    await db.close()
    return {"status": "rejected"}


@app.post("/api/v1/devices/{device_id}/revoke")
async def revoke_device_credential(device_id: str, request: Request):
    await require_admin(request)
    db = await get_db_connection()
    now = int(time.time())
    cursor = await db.execute("SELECT 1 FROM devices WHERE id = ?", (device_id,))
    if await cursor.fetchone() is None:
        await db.close()
        raise HTTPException(status_code=404, detail="Device not found")
    await db.execute(
        "UPDATE device_credentials SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
        (now, device_id),
    )
    await db.execute(
        "UPDATE pairing_sessions SET status = 'rejected' "
        "WHERE device_id = ? AND status IN ('pending', 'approved')",
        (device_id,),
    )
    await db.execute(
        "UPDATE devices SET pairing_status = 'revoked', updated_at = ? WHERE id = ?",
        (now, device_id),
    )
    await db.commit()
    await db.close()
    return {"device_id": device_id, "status": "revoked"}


def local_image_url(url, request):
    """Rewrites a remote image URL to a LAN URL served by this server, so the TV
    (which may have no internet) never fetches artwork directly. Local /static
    paths and URLs already pointing at us are just absolutized; None passes through."""
    if not url:
        return None
    base = str(request.base_url).rstrip("/")
    if url.startswith("/"):
        return base + url
    if url.startswith("http://") or url.startswith("https://"):
        if url.startswith(base):
            return url
        return base + "/img?u=" + urllib.parse.quote(url, safe="")
    return url


async def _cache_remote_image(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or _is_blocked_host(parsed.hostname):
        logger.warning("Skipping unsupported XMLTV logo URL: %s", url)
        return None

    os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    for ext in IMAGE_EXT_BY_TYPE.values():
        cached = os.path.join(IMAGE_CACHE_DIR, key + ext)
        if os.path.isfile(cached):
            return f"/static/imgcache/{key}{ext}"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                final_url = urllib.parse.urlparse(str(response.url))
                if final_url.scheme not in ("http", "https") or _is_blocked_host(final_url.hostname):
                    logger.warning("Skipping XMLTV logo redirect to unsupported host: %s", response.url)
                    return None
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                extension = IMAGE_EXT_BY_TYPE.get(content_type)
                if not extension:
                    logger.warning("XMLTV logo has unsupported content type %s: %s", content_type, url)
                    return None
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > MAX_CACHED_IMAGE_BYTES:
                    logger.warning("XMLTV logo exceeds the image cache size limit: %s", url)
                    return None

                cached = os.path.join(IMAGE_CACHE_DIR, key + extension)
                temporary = os.path.join(IMAGE_CACHE_DIR, f"{key}.{uuid.uuid4().hex}.tmp")
                total_bytes = 0
                try:
                    with open(temporary, "wb") as image_file:
                        async for chunk in response.aiter_bytes():
                            total_bytes += len(chunk)
                            if total_bytes > MAX_CACHED_IMAGE_BYTES:
                                logger.warning("XMLTV logo exceeds the image cache size limit: %s", url)
                                return None
                            image_file.write(chunk)
                    if total_bytes == 0:
                        logger.warning("XMLTV logo is empty: %s", url)
                        return None
                    os.replace(temporary, cached)
                finally:
                    if os.path.exists(temporary):
                        os.remove(temporary)
                logger.info("Cached XMLTV channel logo from %s", url)
                return f"/static/imgcache/{key}{extension}"
    except (httpx.HTTPError, OSError, ValueError):
        logger.exception("Could not cache XMLTV channel logo from %s", url)
        return None


async def _cache_xmltv_channel_logos(channels):
    semaphore = asyncio.Semaphore(8)

    async def cache_channel(channel):
        icon_url = channel.get("icon")
        if not icon_url or icon_url.startswith("/static/"):
            return dict(channel)
        async with semaphore:
            local_path = await _cache_remote_image(icon_url)
        return {**channel, "icon": local_path}

    return await asyncio.gather(*(cache_channel(channel) for channel in channels))


async def _cached_channel_logo_url(url, request):
    if not url:
        return None

    base = str(request.base_url).rstrip("/")
    if url.startswith("/static/"):
        return base + url
    if url.startswith(base + "/static/"):
        return url

    parsed = urllib.parse.urlparse(url)
    if parsed.path == "/img":
        remote_url = urllib.parse.parse_qs(parsed.query).get("u", [None])[0]
        if remote_url:
            url = remote_url

    cached_path = await _cache_remote_image(url)
    return local_image_url(cached_path, request) if cached_path else None


def _program_media_type(program):
    categories = (category.casefold() for category in program.get("categories", []))
    return "movie" if any("film" in category or "movie" in category for category in categories) else "tv"


def _is_blocked_host(host):
    """Basic SSRF guard: don't let the proxy reach loopback/link-local/private hosts."""
    if not host:
        return True
    host = host.lower()
    if host in ("localhost", "0.0.0.0", "::1", "metadata.google.internal"):
        return True
    return (
        host.startswith("127.")
        or host.startswith("10.")
        or host.startswith("192.168.")
        or host.startswith("169.254.")
        or any(host.startswith(f"172.{n}.") for n in range(16, 32))
    )

@app.get("/img")
async def image_proxy(u: str):
    """Fetch-and-cache proxy for remote artwork. The TV requests images from here
    (on the LAN); the server downloads from TMDB/etc once, caches to disk, and
    serves the bytes so images work even when the TV has no internet."""
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme not in ("http", "https") or _is_blocked_host(parsed.hostname):
        raise HTTPException(status_code=400, detail="Unsupported image URL")

    os.makedirs(IMAGE_CACHE_DIR, exist_ok=True)
    key = hashlib.sha256(u.encode("utf-8")).hexdigest()
    for ext in (".jpg", ".png", ".webp", ".gif", ".img"):
        cached = os.path.join(IMAGE_CACHE_DIR, key + ext)
        if os.path.exists(cached):
            return FileResponse(cached)

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(u)
            resp.raise_for_status()
    except Exception:
        raise HTTPException(status_code=502, detail="Could not fetch image")

    ctype = resp.headers.get("content-type", "").split(";")[0].strip()
    if not ctype.startswith("image/"):
        raise HTTPException(status_code=415, detail="Not an image")
    ext = IMAGE_EXT_BY_TYPE.get(ctype, ".img")
    with open(os.path.join(IMAGE_CACHE_DIR, key + ext), "wb") as f:
        f.write(resp.content)
    return Response(content=resp.content, media_type=ctype)

@app.get("/")
async def ui_home(request: Request):
    """Serve the Web UI for channel mapping."""
    expected = os.environ.get("HOMELAB_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Administrator access is not configured")
    if not _has_valid_admin_session(request, expected):
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"request": request},
        )
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})


@app.post("/api/v1/admin/login")
async def admin_login(payload: AdminLoginRequest, request: Request):
    expected = os.environ.get("HOMELAB_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Administrator access is not configured")
    if not secrets.compare_digest(payload.password.encode("utf-8"), expected.encode("utf-8")):
        await _record_admin_auth_failure(request)
        raise HTTPException(status_code=401, detail="Incorrect administrator password")

    db = await get_db_connection()
    await db.execute(
        "DELETE FROM pairing_rate_limits WHERE rate_key = ?",
        (f"admin-auth:{_client_address(request)}",),
    )
    await db.commit()
    await db.close()

    issued_at = int(time.time())
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        ADMIN_SESSION_COOKIE,
        f"{issued_at}.{_admin_session_signature(expected, issued_at)}",
        max_age=ADMIN_SESSION_LIFETIME_SECONDS,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/v1/admin/logout")
async def admin_logout():
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(ADMIN_SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return response


def xmltv_files():
    """Every .xml file in the data directory counts as an XMLTV source.
    Files that aren't valid XMLTV simply contribute no channels/programs."""
    try:
        return sorted(
            os.path.join(DATA_DIR, name)
            for name in os.listdir(DATA_DIR)
            if name.lower().endswith(".xml")
        )
    except OSError:
        return []

@app.get("/api/xmltv/raw")
async def get_raw_xmltv(request: Request, file: str | None = None):
    """Debug helper: list the XMLTV source files, or download one raw.
    Only files already discovered by xmltv_files() can be fetched, so this
    can't be used to read arbitrary paths."""
    await require_admin(request)
    files = xmltv_files()
    if file is None:
        return [os.path.basename(p) for p in files]
    for path in files:
        if os.path.basename(path) == file:
            return FileResponse(path, media_type="application/xml")
    raise HTTPException(status_code=404, detail=f"No XMLTV file named '{file}'")

@app.get("/api/xmltv/channels")
async def get_xmltv_channels(request: Request):
    """Returns XMLTV channels with logos cached and served from this server."""
    paths = xmltv_files()
    channels, _ = await _get_xmltv_data(paths)
    channels_with_cached_logos = await _cache_xmltv_channel_logos(channels)
    return [
        {**channel, "icon": local_image_url(channel["icon"], request)}
        if channel.get("icon")
        else channel
        for channel in channels_with_cached_logos
    ]


def _read_xmltv_data(paths):
    channels = []
    programs = []
    for path in paths:
        channels.extend(parse_xmltv(path))
        programs.extend(parse_xmltv_programs(path))
    return list({channel["id"]: channel for channel in channels}.values()), programs


async def _get_xmltv_data(paths):
    global _xmltv_data_cache_signature, _xmltv_data_cache
    signature_items = []
    for path in paths:
        stat = os.stat(path)
        signature_items.append((path, stat.st_mtime_ns, stat.st_size))
    signature = tuple(signature_items)
    async with _xmltv_data_cache_lock:
        if signature != _xmltv_data_cache_signature:
            channels, programs = await run_in_threadpool(_read_xmltv_data, paths)
            _xmltv_data_cache = (channels, programs)
            _xmltv_data_cache_signature = signature
    return _xmltv_data_cache


def _xmltv_timestamp(value):
    if not value or len(value) <= 14:
        return None
    try:
        return datetime.strptime(
            f"{value[:14]} {value[14:].strip()}",
            "%Y%m%d%H%M%S %z",
        ).timestamp()
    except ValueError:
        return None


def _programme_in_guide_window(program, window_start, window_end):
    start = _xmltv_timestamp(program.get("start"))
    stop = _xmltv_timestamp(program.get("stop"))
    if start is None or stop is None:
        return True
    return stop > window_start and start < window_end


class MappingRequest(BaseModel):
    physical_channel: str
    xmltv_id: str
    custom_name: str | None = None
    custom_image: str | None = None
    xml_source: str | None = None

@app.post("/api/mappings")
async def save_mapping(mapping: MappingRequest, request: Request):
    """Saves a mapping from physical ATSC channel to XMLTV ID with custom overrides."""
    await require_admin(request)
    db = await get_db_connection()
    await db.execute(
        """
        INSERT INTO channel_mappings
            (physical_channel, xmltv_id, custom_name, custom_image, xml_source)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(physical_channel) DO UPDATE SET
            xmltv_id = excluded.xmltv_id,
            custom_name = excluded.custom_name,
            custom_image = excluded.custom_image,
            xml_source = excluded.xml_source
        """,
        (mapping.physical_channel, mapping.xmltv_id, mapping.custom_name, mapping.custom_image, mapping.xml_source)
    )
    await db.commit()
    await db.close()
    return {"status": "success"}

@app.get("/api/mappings")
async def get_mappings(request: Request):
    """Gets all current channel mappings and custom overrides."""
    await require_admin(request)
    db = await get_db_connection()
    cursor = await db.execute("SELECT physical_channel, xmltv_id, custom_name, custom_image, xml_source, custom_logo FROM channel_mappings")
    rows = await cursor.fetchall()
    await db.close()
    return [{
        "physical_channel": row[0],
        "xmltv_id": row[1],
        "custom_name": row[2],
        "custom_image": row[3],
        "xml_source": row[4],
        "custom_logo": row[5]
    } for row in rows]

@app.delete("/api/mappings/{physical_channel}")
async def delete_mapping(physical_channel: str, request: Request):
    """Removes a physical-to-XMLTV channel mapping."""
    await require_admin(request)
    db = await get_db_connection()
    cursor = await db.execute("DELETE FROM channel_mappings WHERE physical_channel = ?", (physical_channel,))
    await db.commit()
    deleted = cursor.rowcount
    await db.close()
    if deleted == 0:
        raise HTTPException(status_code=404, detail=f"No mapping exists for physical channel '{physical_channel}'")
    return {"status": "success"}

ALLOWED_LOGO_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

@app.post("/api/logo/{physical_channel}")
async def upload_logo(physical_channel: str, request: Request, file: UploadFile = File(...)):
    """Uploads a custom channel logo image and attaches it to an existing mapping."""
    await require_admin(request)
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported image type '{ext}'. Use one of: {sorted(ALLOWED_LOGO_EXTENSIONS)}")

    db = await get_db_connection()
    cursor = await db.execute("SELECT 1 FROM channel_mappings WHERE physical_channel = ?", (physical_channel,))
    exists = await cursor.fetchone()
    if not exists:
        await db.close()
        raise HTTPException(status_code=404, detail=f"No mapping exists for physical channel '{physical_channel}'")

    logo_dir = os.path.join(STATIC_DIR, "logos")
    os.makedirs(logo_dir, exist_ok=True)
    safe_name = "".join(c if c.isalnum() else "_" for c in physical_channel)
    logo_path = f"/static/logos/{safe_name}{ext}"
    with open(os.path.join(logo_dir, f"{safe_name}{ext}"), "wb") as f:
        f.write(await file.read())

    await db.execute("UPDATE channel_mappings SET custom_logo = ? WHERE physical_channel = ?", (logo_path, physical_channel))
    await db.commit()
    await db.close()
    return {"status": "success", "logo_url": logo_path}

@app.get("/api/guide")
async def get_enriched_guide(request: Request):
    """The master endpoint for the Android TV app. Returns physical channels merged
    with XMLTV schedule data, channel logos, and TMDB artwork (served from the
    local tmdb_cache so repeat requests are fast and cheap)."""
    # 1. Prefer mappings for this paired device; keep legacy mappings for
    # clients that are not paired yet.
    authorization = request.headers.get("authorization", "")
    scheme, separator, _ = authorization.partition(" ")
    device_id = None
    if separator and scheme.lower() == "bearer":
        device_id = await _device_for_credential(request)
    db = await get_db_connection()
    if device_id is not None:
        cursor = await db.execute(
            """
            SELECT c.display_number, COALESCE(c.xmltv_id, m.xmltv_id),
                   m.custom_name, m.custom_image, m.xml_source, m.custom_logo
            FROM device_channels c
            LEFT JOIN channel_mappings m ON m.physical_channel = c.display_number
            WHERE c.device_id = ? AND c.removed_at IS NULL
              AND COALESCE(c.xmltv_id, m.xmltv_id) IS NOT NULL
            """,
            (device_id,),
        )
    else:
        cursor = await db.execute(
            """
            SELECT physical_channel, xmltv_id, custom_name, custom_image, xml_source, custom_logo
            FROM channel_mappings
            """
        )
    mappings = {row[0]: {"xmltv_id": row[1], "custom_name": row[2], "custom_image": row[3], "xml_source": row[4], "custom_logo": row[5]} for row in await cursor.fetchall()}

    # 2. Get cached XMLTV channels and programs; file changes invalidate both.
    xmltv_channel_records, all_programs = await _get_xmltv_data(xmltv_files())
    xmltv_channels = {channel["id"]: channel for channel in xmltv_channel_records}
    mapped_xmltv_ids = {item["xmltv_id"] for item in mappings.values()}
    mapped_channels = [
        xmltv_channels[xmltv_id]
        for xmltv_id in mapped_xmltv_ids
        if xmltv_id in xmltv_channels
    ]
    cached_mapped_channels = await _cache_xmltv_channel_logos(mapped_channels)
    xmltv_channels.update({channel["id"]: channel for channel in cached_mapped_channels})

    # The app displays a rolling 24-hour window; omit older and distant schedules.
    window_start = int(time.time() // (30 * 60) * (30 * 60))
    window_end = window_start + 24 * 60 * 60
    programs_by_channel = {}
    for p in all_programs:
        if _programme_in_guide_window(p, window_start, window_end):
            programs_by_channel.setdefault(p["channel_id"], []).append(p)

    # TMDB searches dominate cold-start latency. Reuse each title across mapped
    # channels and keep outbound concurrency bounded so a large guide won't flood TMDB.
    titles = {
        (program["title"], _program_media_type(program))
        for xmltv_id in mapped_xmltv_ids
        for program in programs_by_channel.get(xmltv_id, [])
    }
    tmdb_semaphore = asyncio.Semaphore(8)

    async def lookup_title(title, media_type):
        async with tmdb_semaphore:
            return (title, media_type), await tmdb.search_tmdb_cached(db, title, media_type)

    title_meta = dict(await asyncio.gather(*(lookup_title(title, media_type) for title, media_type in titles)))

    enriched_guide = []

    # 3. For each physical channel, attach its full schedule and TMDB artwork
    for physical_ch, override_data in mappings.items():
        xml_id = override_data["xmltv_id"]
        ch_programs = sorted(programs_by_channel.get(xml_id, []), key=lambda p: p["start"] or "")
        xmltv_ch = xmltv_channels.get(xml_id, {})

        enriched_programs = []
        for prog in ch_programs:
            tmdb_meta = title_meta.get((prog["title"], _program_media_type(prog)))
            prog_data = {
                "title": prog["title"],
                "start": prog["start"],
                "stop": prog["stop"],
                "description": prog["description"],
                "episode": prog.get("episode"),
                "episode_title": prog.get("episode_title"),
                "flag": prog.get("flag"),
                "poster_url": None,
                "backdrop_url": None,
            }
            xmltv_image = local_image_url(prog.get("image_url"), request)
            if tmdb_meta:
                raw_poster = (
                    override_data["custom_image"]
                    or tmdb_meta["poster_url"]
                    or prog.get("image_url")
                )
                # Route all artwork through the LAN image proxy so the TV never
                # needs internet access to load posters/backdrops.
                prog_data["poster_url"] = local_image_url(raw_poster, request)
                prog_data["backdrop_url"] = local_image_url(tmdb_meta["backdrop_url"], request)
            else:
                prog_data["poster_url"] = xmltv_image
            enriched_programs.append(prog_data)

        # Uploaded logo wins, then poster override, then the XMLTV icon. Keep
        # remote URLs out of the guide response; the app only receives server cache URLs.
        logo = await _cached_channel_logo_url(
            override_data["custom_logo"] or override_data["custom_image"] or xmltv_ch.get("icon"),
            request,
        )

        enriched_guide.append({
            "physical_channel": physical_ch,
            "xmltv_id": xml_id,
            "name": override_data["custom_name"] or xmltv_ch.get("name") or physical_ch,
            "logo_url": logo,
            "xml_source": override_data["xml_source"],
            "programs": enriched_programs
        })

    await db.commit()  # persist any new tmdb_cache rows written during enrichment
    await db.close()
    return enriched_guide

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=30060, reload=True)
