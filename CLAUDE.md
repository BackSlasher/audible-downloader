# Audible Downloader

CLI and web app for downloading Audible audiobooks as DRM-free m4b, with MP3 on request.

## Project Structure

```
audible_downloader/
├── cli.py          # CLI entry point (interactive terminal UI)
├── web.py          # FastAPI web app
├── db.py           # SQLite database (the stored credential, books, jobs)
├── worker.py       # Background job processor for downloads
└── static/         # Web UI (HTML, CSS, JS)
tests/              # pytest; the ffmpeg tests stand a plain AAC file in for an aaxc
```

## Access and identity

The app holds **one** Audible account and has no users, sessions or passwords. Who may
reach it is decided outside the app, by the client certificate its reverse proxy
requires (`modules/fort/services.nix` in the athena-nixos repo). The cookie carries
only an in-flight OAuth handshake, for the minutes it takes to complete.

This matters because an Audible "login" is really a **device registration**: the stored
credential is an RSA device key plus an `adp_token` and refresh token, which cannot be
scoped down or expired, and Amazon caps how many registrations a customer may hold. So
connecting an account is a costly, account-mutating act, not a cheap sign-in — which is
why it must not be repeated merely because a browser forgot a cookie, and why
`release_previous_device` frees the registration it replaces.

## How It Works

1. **Connecting**: OAuth with Audible via browser, registers a device, stores the one credential
2. **Library**: Fetches from Audible API using `audible-cli` library models
3. **Download**: Gets AAXC (preferred) or AAX format with chapter metadata
4. **Remux**: ffmpeg decrypts and stream-copies the AAC into `book.m4b` with chapter
   marks from the API's chapter list and the cover as an attached picture. No encoder
   runs, so the audio is bit-identical to Audible's file. The encrypted download is
   deleted afterwards; the m4b is the source for anything else.
5. **MP3** (opt-in, per book): splits the m4b by chapter into a zip, at twice the
   source bitrate. `worker._mp3_target_bitrate` explains why not at the source's own.

A job walks `pending_download → downloading → pending_convert → converting → completed`,
and re-enters `pending_mp3 → encoding_mp3 → completed` when MP3s are requested. A book
has at most one job row, so the MP3 pass reuses it.

## Key Dependencies

- `audible` / `audible-cli` - Audible API wrapper and models
- `fastapi` - Web framework
- `ffmpeg` - Audio conversion (system dependency)

## Running

```bash
# Web UI
docker compose up
# http://localhost:8000

# CLI
uv run audible-downloader
```

## Data Storage

- **Web**: `./data/audible.db` (SQLite) + `./data/downloads/{email}/{book}/`
- **CLI**: `./downloads/` + `./.audible-downloader/auth.json`
