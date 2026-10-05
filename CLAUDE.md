# Audible Downloader

CLI and web app for downloading Audible audiobooks as DRM-free m4b, with MP3 on request.

## Project Structure

```
audible_downloader/
├── cli.py          # CLI entry point (interactive terminal UI)
├── web.py          # FastAPI web app
├── db.py           # SQLite database (users, books, jobs)
├── worker.py       # Background job processor for downloads
└── static/         # Web UI (HTML, CSS, JS)
tests/              # pytest; the ffmpeg tests stand a plain AAC file in for an aaxc
```

## How It Works

1. **Authentication**: OAuth with Audible via browser, stores auth data per user
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
