import os
import hashlib
import urllib.parse
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import httpx
import uvicorn
import xml.etree.ElementTree as ET
from xmltv_parser import parse_xmltv, parse_xmltv_programs
from database import get_db_connection, init_db
from config import TMDB_API_KEY, DATA_DIR
import tmdb

app = FastAPI(title="HomelabTV Backend")

# We will serve the Web UI from a 'static' and 'templates' folder
if not os.path.exists("static"): os.makedirs("static")
if not os.path.exists("templates"): os.makedirs("templates")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

IMAGE_CACHE_DIR = os.path.join("static", "imgcache")
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
async def get_raw_xmltv(file: str | None = None):
    """Debug helper: list the XMLTV source files, or download one raw.
    Only files already discovered by xmltv_files() can be fetched, so this
    can't be used to read arbitrary paths."""
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
async def save_mapping(mapping: MappingRequest):
    """Saves a mapping from physical ATSC channel to XMLTV ID with custom overrides."""
    db = await get_db_connection()
    await db.execute(
        "INSERT OR REPLACE INTO channel_mappings (physical_channel, xmltv_id, custom_name, custom_image, xml_source) VALUES (?, ?, ?, ?, ?)",
        (mapping.physical_channel, mapping.xmltv_id, mapping.custom_name, mapping.custom_image, mapping.xml_source)
    )
    await db.commit()
    await db.close()
    return {"status": "success"}

@app.get("/api/mappings")
async def get_mappings():
    """Gets all current channel mappings and custom overrides."""
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
async def delete_mapping(physical_channel: str):
    """Removes a physical-to-XMLTV channel mapping."""
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
async def upload_logo(physical_channel: str, file: UploadFile = File(...)):
    """Uploads a custom channel logo image and attaches it to an existing mapping."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_LOGO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported image type '{ext}'. Use one of: {sorted(ALLOWED_LOGO_EXTENSIONS)}")

    db = await get_db_connection()
    cursor = await db.execute("SELECT 1 FROM channel_mappings WHERE physical_channel = ?", (physical_channel,))
    exists = await cursor.fetchone()
    if not exists:
        await db.close()
        raise HTTPException(status_code=404, detail=f"No mapping exists for physical channel '{physical_channel}'")

    os.makedirs(os.path.join("static", "logos"), exist_ok=True)
    safe_name = "".join(c if c.isalnum() else "_" for c in physical_channel)
    logo_path = f"/static/logos/{safe_name}{ext}"
    with open(os.path.join("static", "logos", f"{safe_name}{ext}"), "wb") as f:
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
