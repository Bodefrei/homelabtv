import httpx
from config import TMDB_API_KEY
import urllib.parse
from database import get_cached_tmdb, set_cached_tmdb

TMDB_BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w500"
BACKDROP_BASE_URL = "https://image.tmdb.org/t/p/w1280"

async def search_tmdb(query: str, media_type: str = "tv"):
    """Match a TV show or movie title against TMDB and return metadata and artwork."""
    if not TMDB_API_KEY or TMDB_API_KEY == "":
        return None

    if media_type not in {"tv", "movie"}:
        raise ValueError(f"Unsupported TMDB media type: {media_type}")

    encoded_query = urllib.parse.quote(query)
    search_url = f"{TMDB_BASE_URL}/search/{media_type}?api_key={TMDB_API_KEY}&query={encoded_query}&page=1"
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(search_url)
            if response.status_code == 200:
                data = response.json()
                if data.get("results") and len(data["results"]) > 0:
                    best_match = data["results"][0]
                    return {
                        "tmdb_id": best_match.get("id"),
                        "title": best_match.get("name") if media_type == "tv" else best_match.get("title"),
                        "overview": best_match.get("overview"),
                        "poster_url": f"{IMAGE_BASE_URL}{best_match['poster_path']}" if best_match.get("poster_path") else None,
                        "backdrop_url": f"{BACKDROP_BASE_URL}{best_match['backdrop_path']}" if best_match.get("backdrop_path") else None
                    }
    except Exception as e:
        print(f"TMDB Search Error for '{query}': {e}")

    return None

async def search_tmdb_cached(db, title: str, media_type: str = "tv"):
    """TMDB lookup backed by the local tmdb_cache table. Negative results are
    cached too, so each unique title costs at most one API call per TTL window."""
    cache_key = f"{media_type}:{title}" if media_type != "tv" else title
    cached = await get_cached_tmdb(db, cache_key)
    if cached is not None:
        if cached["tmdb_id"] is None:
            return None
        return cached
    meta = await search_tmdb(title, media_type)
    await set_cached_tmdb(db, cache_key, meta)
    return meta
