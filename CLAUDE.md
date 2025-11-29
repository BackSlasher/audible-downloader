# Audible Downloader

CLI and web app for downloading Audible audiobooks and converting them to MP3.

## Project Structure

```
audible_downloader/
├── cli.py          # CLI entry point (interactive terminal UI)
├── web.py          # FastAPI web app
├── db.py           # SQLite database (users, books, jobs)
├── worker.py       # Background job processor for downloads
└── static/         # Web UI (HTML, CSS, JS)
```

## How It Works

1. **Authentication**: OAuth with Audible via browser, stores auth data per user
2. **Library**: Fetches from Audible API using `audible-cli` library models
3. **Download**: Gets AAXC (preferred) or AAX format with chapter metadata
4. **Convert**: ffmpeg decrypts and converts to chaptered MP3s
5. **Zip**: Creates downloadable zip of MP3 files

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
