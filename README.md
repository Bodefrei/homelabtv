<p align="center">
  <img src="docs/logo.svg" alt="HomelabTV logo" width="400">
</p>

<p align="center">
  <a href="https://github.com/TheGav179/homelabtv/actions/workflows/ci.yml"><img src="https://github.com/TheGav179/homelabtv/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/TheGav179/homelabtv/releases"><img src="https://img.shields.io/github/v/release/TheGav179/homelabtv?include_prereleases&label=release" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT License"></a>
</p>

An open-source live TV app for Android TV that uses your TV's **built-in tuner** for video and your **homelab server** for guide data — and keeps working when the server is down.

Think of it as a replacement for the manufacturer's TV app (Sony's, in particular), with a Jellyfin-style UI, a real timeline guide, and none of the telemetry.

<p align="center">
  <img src="docs/guide-mock.svg" alt="Timeline guide" width="760">
</p>
<p align="center"><sub>Illustration of the guide — the TV's protected video path blocks direct screen captures, so real screenshots are welcome contributions.</sub></p>

## Why it's different

Most self-hosted live TV setups (Plex, Jellyfin, Emby, Channels DVR, Tvheadend) put the **server in the video path**: a network tuner feeds the server, the server streams to the app, and if the server is down there's no TV.

HomelabTV splits the jobs:

```
┌─────────────────────────┐          ┌──────────────────────────────┐
│       Android TV        │          │   Homelab server (Docker)    │
│                         │   HTTP   │                              │
│  Antenna → TV tuner ────┼────╳─────┤  XMLTV files (WebGrab+Plus,  │
│  (video never leaves    │   guide  │  zap2xml, ...)               │
│   the TV)               │   data   │  + TMDB artwork cache        │
│                         │   only   │  + channel mapping web UI    │
└─────────────────────────┘          └──────────────────────────────┘
```

- **Video** comes from the TV's own ATSC tuner via Android's TV Input Framework. The server is never in the video path.
- **Guide data** (schedules, posters, backdrops, logos) comes from the server and is cached on the TV. Server offline? Everything keeps working — the guide just shows how old its data is.

## Built for WAN-blocked TVs

This app assumes a mindset where the smart TV **never touches the internet**. A common way to get there: give the TV a static IP with a bogus (or empty) gateway, or block its MAC at the router — it can still reach everything on your LAN, but nothing beyond it. Telemetry, ads, and forced updates die at the router.

> **⚠️ Mind IPv6.** The bogus-gateway trick only blocks IPv4 — if your LAN has IPv6, the TV auto-configures a working route straight past it and stays fully online. Verify from the TV (`adb shell ping6 google.com`); if it answers, disable IPv6 (or router advertisements) on the LAN, or block the TV's **MAC** at the router — per-address IPv6 blocking is whack-a-mole because devices rotate privacy addresses.

HomelabTV is proof that the TV doesn't need WAN for any of this: live video comes off the antenna, and everything that *does* need the internet — XMLTV scraping, TMDB artwork — happens **server-side**. The TV only ever talks to your server over plain HTTP on your LAN, and thanks to the offline-first cache it doesn't even need *that* to keep working.

## Features

- Jellyfin-style dark UI with backdrop art from TMDB
- Full-screen timeline guide (EPG) with a live mini-player, "now" line, and time-anchored D-pad navigation (long program blocks don't fling your cursor sideways)
- Guide opens on the channel you're watching
- Channel zapper bar with progress and "next up"
- Direct channel number entry from the remote's number pad
- Detailed info banner (Info / Options tabs) with audio track and closed-caption selection; select the next program to view its details
- Airing-status badges (New / Rerun / Premiere / Live / News) from XMLTV flags, shown in the info banner and guide
- Offline-first: last guide sync is cached on the TV as JSON
- During a paired device's favorites-list sync, the server checks all visible entries across every server favorites list and downloads only their remote channel logos. Cached logos are refreshed only after ten days, with at most one upstream image request every three seconds. Failed requests are marked and retried only after ten days. The app receives only server-local URLs and copies the selected list's images into its persistent logo cache for offline use.
- Resumes on the channel you were last watching
- Channel rescan integration: launch the tuner's scan wizard from Settings; the channel list reloads automatically when you return
- Server web UI with separate **Fernseher** and **XMLTV & Sender** areas, device-specific XMLTV matching, shared ordered channel lists, channel logos, and name/poster overrides

## Requirements

- **TV**: Android TV with a built-in tuner exposed through the TV Input Framework (tested on Sony Bravia with ATSC; other brands with hardware tuners should work — reports welcome). Streaming boxes without tuners will only show the guide.
- **Server**: anything that runs Docker.
- **Guide source**: one or more XMLTV files (WebGrab+Plus, [zap2xml](https://github.com/jef/zap2xml), or any XMLTV producer). Every `.xml` file in the data directory is parsed.
- Optional: a free [TMDB API key](https://www.themoviedb.org/settings/api) for series and movie posters/backdrops. When XMLTV provides a programme image, it is used as a poster fallback. Sender logos come from the XMLTV channel icon or can be uploaded in the server's **XMLTV & Sender** area.

## Setup

### 1. Backend

Create `backend/.env` with a private administrator token (the file is ignored by Git):

```dotenv
HOMELAB_ADMIN_TOKEN=paste-a-long-random-token-here
HOMELAB_ADMIN_AUTH_FAILURE_LIMIT=10
TMDB_API_KEY=yourkey
HOMELAB_XMLTV_URL=https://www.free-epg.de/api/epg/de.xml.gz
HOMELAB_XMLTV_REFRESH_HOURS=12
HOMELAB_XMLTV_KEEP_PAST_HOURS=1
HOMELAB_XMLTV_KEEP_FUTURE_HOURS=30
HOMELAB_MDNS_ENABLED=true
```

Generate a token with Python:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Start the backend from the `backend` directory:

```bash
cd backend
# put your XMLTV files in ./data (or edit docker-compose.yml to mount your existing directory)
docker compose up -d
```

Open `http://<server>:30060`. The HomelabTV login page asks for the value of `HOMELAB_ADMIN_TOKEN`. The admin APIs also accept that token as a Bearer credential. Keep it private and use HTTPS through a trusted reverse proxy when accessing the server over an untrusted network; plain HTTP is only suitable for a trusted local network.

`HOMELAB_ADMIN_AUTH_FAILURE_LIMIT` controls failed administrator logins per client within five minutes. It defaults to `10`; set it to another non-negative integer to change the limit, or `0` to disable this lockout. Disabling it removes a protection against password guessing, so use it only on a trusted network. Restart/recreate the backend container after changing `.env`.

The backend downloads the configured gzip-compressed XMLTV feed immediately at startup and every 12 hours by default. It replaces `backend/data/automatic.xml` atomically, keeping programmes from one hour in the past through 30 hours in the future; failed downloads leave the last valid file in place. Set `HOMELAB_XMLTV_URL` to an empty value to disable automatic downloads. The Android app independently refreshes its cached guide from the server every 15 minutes.

Opening the web interface shows a HomelabTV login page for the administrator token instead of the browser's HTTP Basic authentication dialog. A successful login creates a 12-hour HttpOnly session cookie; use **Log out** to end the session.

The backend advertises `_homelabtv._tcp.local.` via mDNS on port `30060`. The Android TV can search for it under **Settings → Find HomelabTV server on network**. This Docker Compose setup uses host networking so mDNS multicast is visible on the LAN; it requires a Linux Docker host, and the backend listens directly on host port `30060`. Set `HOMELAB_MDNS_ENABLED=false` to disable advertising. Alternatively, enter a hostname or IP address in the app's server field; the app adds `http://` and port `30060` automatically.

#### Pair an Android TV

In the TV app, open **Menu → Settings**, enter the server URL, then choose **Pair this TV**. The app creates a persistent application-specific device ID and a strong device secret, displays a six-digit code, and polls for administrator approval. In the server web interface, enter the code under **Pair an Android TV** and approve or reject it. The app stores the resulting device credential encrypted with an Android Keystore-backed key.

The versioned API used by this flow is:

1. `POST /api/v1/devices/pair` starts pairing. The six-digit `user_code` expires after five minutes; the server stores only the device-secret hash.
2. An administrator approves or rejects the code at `POST /api/v1/pairing/approve` or `POST /api/v1/pairing/reject`. These endpoints require the admin token as a Bearer credential and `{"user_code":"123456"}` as the JSON body.
3. The TV polls `POST /api/v1/devices/{device_id}/poll` with its device secret. After approval, the server returns the device credential once.

`GET /api/v1/devices/me` checks a device credential. Administrators can list devices at `GET /api/v1/devices` and revoke a credential with `POST /api/v1/devices/{device_id}/revoke`. Admin and pairing attempts are rate-limited.

After pairing, the TV uploads its physical `TvContract` channel inventory to `PUT /api/v1/devices/me/channels` using its device credential. The server verifies a deterministic SHA-256 hash, records changed inventories as revisions, and marks channels missing from a later scan as unavailable instead of deleting their history. The app syncs on startup, after reloading channels, after pairing, and on request from **Settings → Sync Channel Inventory**. Administrators can inspect current and previously scanned channels at `GET /api/v1/devices/{device_id}/channels`. Use HTTPS with a trusted certificate when possible; HTTP should only be used on a trusted local network.

In the web interface, use **Fernseher** to match each TV channel to an XMLTV channel and to create shared, ordered favorites lists. Lists use XMLTV IDs as logical identifiers; each paired TV resolves those IDs to its own Android channel IDs. Each list entry can have an optional alternative display name, applied only while that server favorites list is active. Assign a list to a TV; when the app is set to use server favorites, available entries are used in that order. Unassigned lists do not change the app's existing channel lineup. Use **XMLTV & Sender** to maintain legacy global mappings and their display/logo overrides.

The TV app's **Settings → Channel list** option independently selects either the TV's scanned lineup or the server favorites assigned to this TV. It defaults to the TV lineup; the server does not force a favorites list on the app. In server mode, channel numbers shown by the zapper, details banner, and guide are the list positions (1, 2, 3, …), while tuning and EPG matching continue to use the TV's real channel IDs.

Example zap2xml companion container (6-hour refresh, 3 days of data):

```yaml
services:
  zap2xml:
    image: ghcr.io/jef/zap2xml:latest
    environment:
      - POSTAL_CODE=YOUR_ZIP
      - COUNTRY=USA
      - LINEUP_ID=USA-OTA-YOUR_ZIP
      - OUTPUT_FILE=/data/xmltv.xml
      - TIMESPAN=72
      - SLEEP_TIME=21600
    volumes:
      - ./data:/data
    restart: unless-stopped
```

> **Tip:** if a guide scraper starts getting IP-blocked, route just that container through a VPN with [gluetun](https://github.com/qmcgaw/gluetun) (`network_mode: "service:gluetun"` on the scraper). The HomelabTV backend itself has no reason to go through it — only the scrapers talk to the outside world.

### 2. App

**Option A — prebuilt APK**: download the latest `homelabtv-*.apk` from [Releases](https://github.com/TheGav179/homelabtv/releases) and sideload it:

```bash
adb connect <tv-ip>:5555
adb install -r homelabtv-*.apk
```

(Or use any sideloading method you like — "Downloader"-style apps on the TV work too.)

**Option B — build it yourself**:

```bash
cd android-app
./gradlew assembleDebug
adb connect <tv-ip>:5555
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

On first launch the app asks for the `READ_TV_LISTINGS` permission (required to see the TV's scanned channels), then set your server URL in **Menu (Back button) → Settings**.

## Remote controls

| Key | Action |
|---|---|
| D-pad Up/Down, CH +/- | Change channel |
| Last-channel key, D-pad Left | Jump back to the previously watched channel |
| Number keys | Direct channel entry (`0` `6` → `06.x`, `6` `1` → `61.x`; see Caveats) |
| OK | Toggle channel info bar |
| OK (long press) | Detailed banner: show/episode info, audio + subtitle tracks |
| OK (long press, in guide) | Set/remove a reminder on a future program |
| GUIDE | Open/close the timeline guide (opens on the current channel) |
| INFO | Info bar with clock; quick second press → detailed banner |
| BACK | Close current overlay / open the side menu |

When a reminded program starts, a banner appears: OK tunes to it, BACK dismisses.

## Caveats

- Channel-number entry uses ATSC-style `major.minor` numbering with three modes (**Settings → Number Entry**). **Auto** (default) learns from your scanned lineup: a typed prefix completes the instant no other channel number could extend it — with majors 6, 10, 13 and 20, typing `6` jumps straight to the subchannel, `1` waits for a second digit, and if a 61.1 exists in your lineup, `6` waits too. The same logic applies to subchannels (with minors 1, 2 and 12, typing `.2` commits instantly but `.1` waits). **Leading Zero** always takes two-digit majors (`06` for channel 6). **Quick** completes any first digit above a configurable threshold (Settings shows the threshold item while Quick is active). In every mode, pausing for 2 seconds commits whatever you've typed.
- Manufacturer firmware quirks vary. Example: Sony shows its own system InfoBar on the ⓘ key at firmware level, which no app can suppress — that's why everything is also reachable via OK.
- The first guide request after new XMLTV data lands is slow while new titles are enriched against TMDB (cached for 7 days afterward); the app tolerates this with generous timeouts.

## License

[MIT](LICENSE)
