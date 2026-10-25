import aiosqlite
import os
import time
from config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "homelabtv.db")

# How long a TMDB lookup stays fresh before we re-query (7 days)
TMDB_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60


async def get_db_connection():
    db = await aiosqlite.connect(DB_PATH)
    return db


async def _ensure_table(db, name: str, ddl: str):
    """Create a table if it does not already exist."""
    await db.execute(f"CREATE TABLE IF NOT EXISTS {name} {ddl}")


async def init_db():
    db = await get_db_connection()

    await _ensure_table(
        db,
        "channel_mappings",
        """
        (
            physical_channel TEXT PRIMARY KEY,
            xmltv_id TEXT NOT NULL,
            custom_name TEXT,
            custom_image TEXT,
            xml_source TEXT,
            custom_logo TEXT
        )
        """,
    )

    await _ensure_table(
        db,
        "tmdb_cache",
        """
        (
            title TEXT PRIMARY KEY,
            tmdb_id INTEGER,
            poster_url TEXT,
            backdrop_url TEXT,
            overview TEXT,
            fetched_at INTEGER NOT NULL
        )
        """,
    )

    await _ensure_table(
        db,
        "devices",
        """
        (
            id TEXT PRIMARY KEY,
            friendly_name TEXT NOT NULL,
            app_version TEXT,
            android_version TEXT,
            manufacturer TEXT,
            model TEXT,
            last_seen_at INTEGER,
            last_sync_at INTEGER,
            available_channels INTEGER DEFAULT 0,
            inventory_hash TEXT,
            inventory_revision INTEGER NOT NULL DEFAULT 0,
            assigned_channel_list TEXT,
            pairing_status TEXT NOT NULL DEFAULT 'unpaired',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
    )

    await _ensure_table(
        db,
        "device_channels",
        """
        (
            device_id TEXT NOT NULL,
            channel_id INTEGER NOT NULL,
            input_id TEXT,
            display_number TEXT NOT NULL,
            display_name TEXT NOT NULL,
            original_display_name TEXT,
            service_type TEXT,
            channel_type TEXT,
            network_id INTEGER,
            transport_stream_id INTEGER,
            service_id INTEGER,
            internal_provider_id TEXT,
            first_seen_at INTEGER NOT NULL,
            last_seen_at INTEGER NOT NULL,
            removed_at INTEGER,
            PRIMARY KEY(device_id, channel_id),
            FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
        )
        """,
    )

    await _ensure_table(
        db,
        "device_inventory_revisions",
        """
        (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            inventory_hash TEXT NOT NULL,
            channel_count INTEGER NOT NULL,
            changes_json TEXT NOT NULL,
            inventory_json TEXT NOT NULL,
            synced_at INTEGER NOT NULL,
            UNIQUE(device_id, revision),
            FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
        )
        """,
    )

    await _ensure_table(
        db,
        "device_credentials",
        """
        (
            device_id TEXT PRIMARY KEY,
            credential_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            revoked_at INTEGER,
            last_used_at INTEGER,
            FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
        )
        """,
    )

    await _ensure_table(
        db,
        "pairing_sessions",
        """
        (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            pairing_code TEXT NOT NULL UNIQUE,
            secret_hash TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            used_at INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            approved_by TEXT,
            FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE
        )
        """,
    )

    await _ensure_table(
        db,
        "pairing_rate_limits",
        """
        (
            rate_key TEXT PRIMARY KEY,
            window_started_at INTEGER NOT NULL,
            request_count INTEGER NOT NULL
        )
        """,
    )

    await _ensure_table(
        db,
        "channel_lists",
        """
        (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            is_system INTEGER NOT NULL DEFAULT 0,
            is_shared INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
    )

    await _ensure_table(
        db,
        "channel_list_entries",
        """
        (
            id TEXT PRIMARY KEY,
            list_id TEXT NOT NULL,
            logical_channel_id TEXT NOT NULL,
            display_name TEXT,
            custom_name TEXT,
            sort_index INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(list_id) REFERENCES channel_lists(id) ON DELETE CASCADE
        )
        """,
    )

    await _ensure_table(
        db,
        "device_list_assignments",
        """
        (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            list_id TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            assigned_at INTEGER NOT NULL,
            FOREIGN KEY(device_id) REFERENCES devices(id) ON DELETE CASCADE,
            FOREIGN KEY(list_id) REFERENCES channel_lists(id) ON DELETE CASCADE
        )
        """,
    )

    # Backward-compatibility migration for older single-device channel_mapping rows.
    cursor = await db.execute("PRAGMA table_info(channel_mappings)")
    columns = [row[1] for row in await cursor.fetchall()]
    if "custom_logo" not in columns:
        await db.execute("ALTER TABLE channel_mappings ADD COLUMN custom_logo TEXT")

    cursor = await db.execute("PRAGMA table_info(devices)")
    device_columns = {row[1] for row in await cursor.fetchall()}
    if "inventory_hash" not in device_columns:
        await db.execute("ALTER TABLE devices ADD COLUMN inventory_hash TEXT")
    if "inventory_revision" not in device_columns:
        await db.execute(
            "ALTER TABLE devices ADD COLUMN inventory_revision INTEGER NOT NULL DEFAULT 0"
        )

    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pairing_sessions_device_status "
        "ON pairing_sessions (device_id, status)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_channels_available "
        "ON device_channels (device_id, removed_at)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_inventory_revisions_device "
        "ON device_inventory_revisions (device_id, revision DESC)"
    )
    await db.commit()
    await db.close()


async def get_cached_tmdb(db, title: str):
    """Returns cached TMDB metadata for a title, or None on miss/expiry.
    A cached row with tmdb_id NULL means 'we searched and found nothing' —
    that negative result is cached too, so we don't hammer TMDB for titles
    like 'Local News at 6' on every guide request."""
    cursor = await db.execute(
        "SELECT tmdb_id, poster_url, backdrop_url, overview, fetched_at FROM tmdb_cache WHERE title = ?",
        (title,)
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    tmdb_id, poster_url, backdrop_url, overview, fetched_at = row
    if time.time() - fetched_at > TMDB_CACHE_TTL_SECONDS:
        return None
    return {
        "hit": True,
        "tmdb_id": tmdb_id,
        "poster_url": poster_url,
        "backdrop_url": backdrop_url,
        "overview": overview,
    }

async def set_cached_tmdb(db, title: str, meta):
    """Stores a TMDB result (or a negative result when meta is None)."""
    await db.execute(
        "INSERT OR REPLACE INTO tmdb_cache (title, tmdb_id, poster_url, backdrop_url, overview, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            title,
            meta.get("tmdb_id") if meta else None,
            meta.get("poster_url") if meta else None,
            meta.get("backdrop_url") if meta else None,
            meta.get("overview") if meta else None,
            int(time.time()),
        )
    )
