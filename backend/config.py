import os

# Get a free API key at https://www.themoviedb.org/settings/api and pass it
# via the TMDB_API_KEY environment variable. Without it, the guide still works
# but has no posters/backdrops.
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# Directory containing your XMLTV .xml files (every .xml in it is parsed)
DATA_DIR = os.environ.get("HOMELAB_DATA_DIR", "data")

if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

XMLTV_FEED_URL = os.environ.get("HOMELAB_XMLTV_URL", "").strip()
MDNS_ENABLED = os.environ.get("HOMELAB_MDNS_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

try:
    XMLTV_REFRESH_INTERVAL_HOURS = int(os.environ.get("HOMELAB_XMLTV_REFRESH_HOURS", "12"))
    XMLTV_KEEP_PAST_HOURS = int(os.environ.get("HOMELAB_XMLTV_KEEP_PAST_HOURS", "1"))
    XMLTV_KEEP_FUTURE_HOURS = int(os.environ.get("HOMELAB_XMLTV_KEEP_FUTURE_HOURS", "30"))
except ValueError as exc:
    raise RuntimeError("XMLTV refresh and retention settings must be integers") from exc

if XMLTV_REFRESH_INTERVAL_HOURS < 1:
    raise RuntimeError("HOMELAB_XMLTV_REFRESH_HOURS must be at least 1")
if XMLTV_KEEP_PAST_HOURS < 0 or XMLTV_KEEP_FUTURE_HOURS < 24:
    raise RuntimeError(
        "XMLTV retention must keep a non-negative past window and at least 24 hours of future data"
    )
