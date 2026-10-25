import os
import hashlib
import base64
import binascii
import secrets
import json
import struct
import time
import urllib.parse
import uuid
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
import httpx
import uvicorn
import xml.etree.ElementTree as ET
from xmltv_parser import parse_xmltv, parse_xmltv_programs
from database import get_db_connection, init_db
from config import TMDB_API_KEY, DATA_DIR
import tmdb

app = FastAPI(title="HomelabTV Backend")

PAIRING_LIFETIME_SECONDS = 5 * 60
PAIRING_REQUEST_LIMIT = 5
PAIRING_APPROVAL_LIMIT = 10
ADMIN_AUTH_FAILURE_LIMIT = 10
RATE_LIMIT_WINDOW_SECONDS = 5 * 60


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
}

@app.on_event("startup")
async def startup():
    await init_db()


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


async def require_admin(request: Request):
    expected = os.environ.get("HOMELAB_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Administrator access is not configured")

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

    if not candidate or not secrets.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8")):
        db = await get_db_connection()
        await _enforce_rate_limit(
            db,
            f"admin-auth:{_client_address(request)}",
            ADMIN_AUTH_FAILURE_LIMIT,
        )
        await db.close()
        raise HTTPException(
            status_code=401,
            detail="Administrator authentication required",
            headers={"WWW-Authenticate": 'Basic realm="HomelabTV admin", Bearer'},
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
               pairing_status, last_seen_at, last_sync_at, available_channels, assigned_channel_list
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
                transport_stream_id, service_id, internal_provider_id,
                first_seen_at, last_seen_at, removed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
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
               internal_provider_id, first_seen_at, last_seen_at, removed_at
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
            "first_seen_at": row[11],
            "last_seen_at": row[12],
            "removed_at": row[13],
            "available": row[13] is None,
        }
        for row in rows
    ]


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
    }


@app.get("/api/v1/devices")
async def list_devices(request: Request):
    await require_admin(request)
    db = await get_db_connection()
    rows = await db.execute(
        """
        SELECT id, friendly_name, app_version, android_version, manufacturer, model,
               pairing_status, last_seen_at, last_sync_at, available_channels, assigned_channel_list
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
               pairing_status, last_seen_at, last_sync_at, available_channels, assigned_channel_list
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
    await require_admin(request)
    return templates.TemplateResponse(request=request, name="index.html", context={"request": request})

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
async def get_xmltv_channels():
    """Reads every XMLTV file in the data directory and returns the available channels."""
    channels = []
    for path in xmltv_files():
        channels.extend(parse_xmltv(path))

    # Deduplicate
    unique_channels = {c["id"]: c for c in channels}
    return list(unique_channels.values())

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
        "INSERT OR REPLACE INTO channel_mappings (physical_channel, xmltv_id, custom_name, custom_image, xml_source) VALUES (?, ?, ?, ?, ?)",
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
    # 1. Get mappings with overrides
    db = await get_db_connection()
    cursor = await db.execute("SELECT physical_channel, xmltv_id, custom_name, custom_image, xml_source, custom_logo FROM channel_mappings")
    mappings = {row[0]: {"xmltv_id": row[1], "custom_name": row[2], "custom_image": row[3], "xml_source": row[4], "custom_logo": row[5]} for row in await cursor.fetchall()}

    # 2. Get all XMLTV channels (for names/logos) and programs from every XML source
    xmltv_channels = {}
    all_programs = []
    for path in xmltv_files():
        for ch in parse_xmltv(path):
            xmltv_channels[ch["id"]] = ch
        all_programs.extend(parse_xmltv_programs(path))

    # Group programs by channel once instead of scanning the full list per channel
    programs_by_channel = {}
    for p in all_programs:
        programs_by_channel.setdefault(p["channel_id"], []).append(p)

    enriched_guide = []

    # 3. For each physical channel, attach its full schedule and TMDB artwork
    for physical_ch, override_data in mappings.items():
        xml_id = override_data["xmltv_id"]
        ch_programs = sorted(programs_by_channel.get(xml_id, []), key=lambda p: p["start"] or "")
        xmltv_ch = xmltv_channels.get(xml_id, {})

        # Enrich once per unique title — the cache makes this cheap after the first run
        title_meta = {}
        for prog in ch_programs:
            title = prog["title"]
            if title not in title_meta:
                title_meta[title] = await tmdb.search_tmdb_cached(db, title)

        enriched_programs = []
        for prog in ch_programs:
            tmdb_meta = title_meta.get(prog["title"])
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
            if tmdb_meta:
                raw_poster = override_data["custom_image"] if override_data["custom_image"] else tmdb_meta["poster_url"]
                # Route all artwork through the LAN image proxy so the TV never
                # needs internet access to load posters/backdrops.
                prog_data["poster_url"] = local_image_url(raw_poster, request)
                prog_data["backdrop_url"] = local_image_url(tmdb_meta["backdrop_url"], request)
            enriched_programs.append(prog_data)

        # Uploaded logo wins, then poster override, then the XMLTV icon.
        logo = local_image_url(
            override_data["custom_logo"] or override_data["custom_image"] or xmltv_ch.get("icon") or None,
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
