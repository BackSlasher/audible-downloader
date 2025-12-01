# Audible Downloader

Download and convert Audible audiobooks to MP3 with chapter splitting.

## Quick Start (Web UI)

```bash
docker compose up
```

Open http://localhost:8000

1. Select your Audible marketplace and login via browser
2. Paste the callback URL to complete authentication
3. Click books to select, then "Download Selected"
4. Download ZIP files when processing completes

## CLI Mode

```bash
# Docker
docker compose run --rm cli

# Local
uv sync
uv run audible-downloader
```

## Data

- Web UI: Data stored in `./data/` (database + downloads)
- CLI: Downloads in `./downloads/`, auth in `./.audible-downloader/`
